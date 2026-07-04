#!/usr/bin/env python3
"""
Fortel AI Takeoff — Manual Approval Email

When the pipeline raises flags that need a human decision (scale unverified,
architect drawing with assumed build-up, assessor confirmation required), this
module:

  1. Renders the drawing page as a PNG with the AI's proposed polygon overlaid
  2. Builds a self-contained HTML email with:
       - the annotated drawing snapshot
       - result table (area, scale, confidence, flags)
       - three action buttons:  ✅ APPROVE  |  ✗ REJECT  |  ✏️ ADJUST
       - link to the full portal for detailed polygon editing
  3. Sends via SMTP (env vars) OR outputs a file/webhook payload for n8n

Configuration (environment variables):
  SMTP_HOST     e.g. smtp.gmail.com
  SMTP_PORT     e.g. 587
  SMTP_USER     the sender address
  SMTP_PASS     app password / API key
  APPROVAL_BASE_URL   base URL of approval_server.py  e.g. http://localhost:5001
  APPROVAL_TO   recipient email (default: inderjit@fortel.co.uk)

Run standalone to test:
  python3 approval_email.py drawings/UNMARKED_Yard.pdf --demo
"""
import os, io, json, uuid, base64, smtplib, textwrap, hashlib, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import fitz
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APPROVAL_TO       = os.getenv("APPROVAL_TO",   "inderjit@fortel.co.uk")
APPROVAL_BASE_URL = os.getenv("APPROVAL_BASE_URL", "http://localhost:5001")
SMTP_HOST         = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER         = os.getenv("SMTP_USER",     "estimatingai@fortel.co.uk")
SMTP_PASS         = os.getenv("SMTP_PASS",     "")
# Same token env as approval_server.py (PORTAL_TOKEN, with APPROVAL_TOKEN as an older alias,
# PORTAL_TOKEN wins if both set). When set, approval_server's @app.before_request token gate
# 401s every route including /approve, /reject, /adjust — so emailed action links MUST carry
# ?token=<token> or clicking them from the email just hits a 401 page. See build_html_email.
APPROVAL_TOKEN    = os.getenv("PORTAL_TOKEN") or os.getenv("APPROVAL_TOKEN", "")

# Stored pending-review jobs (simple JSON file; replace with DB in production).
# Same JOBS_FILE env override as approval_server.py so a caller (a test, a QA instance,
# n8n pointed at a scratch file) can redirect writes away from the live jobs file instead
# of hard-writing it. _load_jobs()/_save_jobs() below read this module attribute at call
# time (not a cached copy), so tests that monkeypatch approval_email.JOBS_FILE after
# import (the established pattern already used for approval_server.JOBS_FILE in
# ci_tests.py) take effect immediately.
JOBS_FILE = Path(os.getenv("JOBS_FILE") or (Path(__file__).parent / "approval_jobs.json"))


# ---------------------------------------------------------------------------
# Snapshot rendering
# ---------------------------------------------------------------------------
def snapshot_scale(pdf_path: str, page: int = 0,
                   scale: float = 0.5, max_width: int = 700) -> float:
    """The ACTUAL render scale (snapshot px per PDF point) render_snapshot() produces.

    render_snapshot caps the requested `scale` so the PNG is never wider than
    `max_width`; for wide sheets (A1/A0 ≈ 1684–3370 pt) the effective scale is therefore
    BELOW 0.5.  The portal needs this exact value to convert the pipeline's scale_k
    (metres per PDF point) into metres per CANVAS pixel (mpp = scale_k / snapshot_scale)
    AND to place the AI polygon (stored in PDF points) on the canvas (canvas_px = pt ×
    snapshot_scale).  Both depend on this being EXACTLY the PNG's px/pt.

    PyMuPDF rounds the rendered pixmap width UP to whole pixels, so the realised px/pt is
    pixmap.width / page_width — marginally above the requested matrix scale.  We return
    that realised value (not the requested matrix scale) so the portal's polygon overlay
    lines up to the pixel and the area is exact, with no sub-percent drift on narrow sheets.
    """
    p = fitz.open(pdf_path)[page]
    s = min(scale, max_width / p.rect.width)
    pix = p.get_pixmap(matrix=fitz.Matrix(s, s))
    return pix.width / p.rect.width


def render_snapshot(pdf_path: str, page: int = 0,
                    polygon_pts: list = None, scale: float = 0.5,
                    max_width: int = 700) -> bytes:
    """
    Render the drawing page as PNG.  If polygon_pts is given (list of [x,y] in PDF pts),
    overlay the AI's proposed region in translucent pink + dashed outline.
    Returns PNG bytes.
    """
    doc = fitz.open(pdf_path)
    p   = doc[page]
    # Scale to fit max_width (single source of truth: snapshot_scale)
    s   = min(scale, max_width / p.rect.width)
    pix = p.get_pixmap(matrix=fitz.Matrix(s, s))
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    if polygon_pts and len(polygon_pts) >= 3:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        pts = [(x * s, y * s) for x, y in polygon_pts]
        # Fill
        d.polygon(pts, fill=(224, 0, 122, 60))
        # Border (thick)
        for i in range(len(pts)):
            d.line([pts[i], pts[(i + 1) % len(pts)]], fill=(224, 0, 122, 220), width=3)
        # Vertex dots
        for x, y in pts:
            d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(224, 0, 122, 255))
        # "AI proposal" label
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        cx = sum(x for x, y in pts) / len(pts)
        cy = sum(y for x, y in pts) / len(pts)
        d.text((cx - 30, cy - 8), "AI region", fill=(224, 0, 122, 230), font=font)
        img = img.convert("RGBA")
        img.alpha_composite(overlay)
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def png_to_b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode()


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------
def _load_jobs() -> dict:
    if JOBS_FILE.exists():
        try:
            return json.loads(JOBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            # Mirror approval_server.load_jobs()'s fail-safe: a torn/corrupt read must never
            # crash a caller (e.g. create_job below) — treat it as an empty job store rather
            # than raising mid-request.
            return {}
    return {}


def _save_jobs(jobs: dict):
    """Write atomically: temp file + os.replace, same pattern as approval_server.save_jobs.

    Plain write_text() truncates then streams bytes in, so a concurrent reader (the portal
    polling GET /jobs, or another writer) can observe a half-written file. Write to a temp
    file in the same directory and os.replace() it into place — POSIX guarantees the rename
    is atomic, so readers always see either the old or the new complete file.
    """
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, JOBS_FILE)


def create_job(pdf_path: str, result: dict,
               project_name: str = None, project_ref: str = None) -> str:
    """Store a pending approval job and return the job_id."""
    jobs = _load_jobs()
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id":           job_id,
        "pdf":          pdf_path,
        "result":       result,
        "project_name": project_name or result.get("project_name", ""),
        "project_ref":  project_ref  or result.get("project_ref",  ""),
        "status":       "pending",
        "created":      datetime.datetime.utcnow().isoformat(),
        "decision":     None,
        "adjusted":     None,
    }
    _save_jobs(jobs)
    return job_id


def update_job(job_id: str, **kwargs):
    jobs = _load_jobs()
    if job_id in jobs:
        jobs[job_id].update(kwargs)
        _save_jobs(jobs)


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------
_BUTTON = """
<a href="{url}" style="
   display:inline-block; padding:12px 24px; margin:6px 4px;
   background:{bg}; color:#fff; text-decoration:none;
   border-radius:8px; font-size:15px; font-weight:700;
   font-family:Arial,sans-serif; letter-spacing:0.3px;">{label}</a>"""

_PORTAL_BTN = """
<a href="{url}" style="
   display:inline-block; padding:10px 20px; margin:10px 0 0 0;
   background:#13294b; color:#fff; text-decoration:none;
   border-radius:6px; font-size:13px; font-family:Arial,sans-serif;">
   🔍 Open full review portal →</a>"""


def _flag_rows(flags: list) -> str:
    if not flags:
        return "<tr><td colspan=2 style='color:#2a7a2a;padding:3px 8px'>✅ No flags</td></tr>"
    rows = ""
    for f in flags:
        colour = "#c0392b" if any(w in f.upper() for w in ("IMPOSSIBLE","UNVERIFIED","ASSUMED","MIXED")) \
                 else "#e67e22"
        rows += f"<tr><td colspan=2 style='color:{colour};padding:3px 8px;font-size:13px'>⚠ {f}</td></tr>"
    return rows


def _spec_details_block(result: dict) -> str:
    """
    Prominent 4-field spec block that Inderjit requires in every email:
      slab thickness / concrete mix / mesh type / mesh layers
    Uses 'NOT FOUND' for any field the AI couldn't extract.
    """
    c    = result.get("costing", {})
    spec = c.get("spec", {}) if c else {}

    def _val(key, fallback="NOT FOUND"):
        v = spec.get(key)
        return str(v) if v not in (None, "", 0) else fallback

    thickness = _val("depth_mm")
    if thickness != "NOT FOUND":
        thickness = f"{thickness} mm"
    mix    = _val("conc_mix")
    mesh   = _val("mesh")
    layers = _val("layers")
    if layers != "NOT FOUND":
        layers = f"{layers} layer{'s' if str(layers) != '1' else ''}"

    assumed = c.get("assumed", False) if c else False
    src_tag = (" <span style='color:#e67e22;font-size:10px;font-weight:700;"
               "background:#fef0d8;padding:1px 5px;border-radius:4px;'>ASSUMED</span>"
               if assumed else "")

    def _row(label, value, stripe=False):
        bg    = "#f7f8fa" if stripe else "#fff"
        vcolour = "#c0392b" if value == "NOT FOUND" else "#111"
        vweight = "400" if value == "NOT FOUND" else "700"
        return (f"<tr style='background:{bg}'>"
                f"<td style='padding:7px 14px;color:#555;font-size:13px;width:42%'>{label}</td>"
                f"<td style='padding:7px 14px;color:{vcolour};font-weight:{vweight};"
                f"font-size:13px'>{value}</td></tr>")

    return f"""
      <tr><td style="padding:8px 28px 4px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #c8d4e8;border-radius:8px;
                      overflow:hidden;font-family:Arial,sans-serif;">
          <tr style="background:#1a3560;">
            <td colspan="2" style="padding:8px 14px;color:#aec3e0;font-size:11px;
                                   text-transform:uppercase;letter-spacing:.6px;">
              Specification Details{src_tag}
            </td>
          </tr>
          {_row("Slab thickness",  thickness,  False)}
          {_row("Concrete mix",    mix,        True)}
          {_row("Mesh type",       mesh,       False)}
          {_row("Mesh layers",     layers,     True)}
        </table>
      </td></tr>"""


def _costing_block(result: dict) -> str:
    """HTML block showing GBP total + rate build-up. Empty string if no costing."""
    c = result.get("costing", {})
    if not c or "total_gbp" not in c:
        return ""
    total  = c["total_gbp"]
    rate   = c.get("rate", 0)
    area   = c.get("area_m2", 0)
    spec   = c.get("spec", {})
    bdwn   = c.get("breakdown", {})
    note   = c.get("note", "")
    assumed = c.get("assumed", False)

    depth  = spec.get("depth_mm", "—")
    mesh   = spec.get("mesh", "—")
    mix    = spec.get("conc_mix", "C32/40")
    layers = spec.get("layers", 1)

    assumed_banner = ""
    if assumed and note:
        assumed_banner = (
            f"<tr><td colspan='2' style='padding:6px 10px;background:#fef9ec;"
            f"color:#9a6700;font-size:12px;border-top:1px solid #e8d090'>⚠ {note}</td></tr>"
        )

    bdwn_rows = ""
    for k, label in [("concrete","Concrete"),("steel","Steel/mesh"),
                     ("dpm","DPM"),("curing","Curing"),("labour","Labour"),("trim","Trim")]:
        v = bdwn.get(k)
        if v is not None:
            bdwn_rows += (f"<tr><td style='padding:2px 10px 2px 20px;color:#888;"
                          f"font-size:12px'>{label}</td>"
                          f"<td style='padding:2px 10px;color:#888;font-size:12px;"
                          f"text-align:right'>£{v:.2f}/m²</td></tr>")

    return f"""
      <tr><td style="padding:4px 28px 8px 28px;" colspan="1">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #dde1e7;border-radius:8px;overflow:hidden;">
          <tr style="background:#13294b;">
            <td style="padding:10px 14px;color:#aec3e0;font-size:11px;text-transform:uppercase;letter-spacing:.5px">
              Estimated Value
            </td>
            <td style="padding:10px 14px;text-align:right;">
              <span style="color:#fff;font-size:24px;font-weight:800;">£{total:,.2f}</span>
            </td>
          </tr>
          <tr style="background:#1a3560;">
            <td style="padding:5px 14px;color:#8fa8cc;font-size:12px">
              {area:,.0f} m² @ £{rate:.2f}/m² &nbsp;·&nbsp;
              {depth}mm {mix} / {layers}× {mesh}
            </td>
            <td style="padding:5px 14px;color:#8fa8cc;font-size:11px;text-align:right">
              nett before VAT
            </td>
          </tr>
          {bdwn_rows}
          {assumed_banner}
        </table>
      </td></tr>"""


def _with_token(url: str) -> str:
    """Append ?token=<APPROVAL_TOKEN> when the token gate is enabled, so a link clicked
    straight out of the email doesn't just hit approval_server's 401 page. No-op when no
    token is configured (local/dev, auth disabled)."""
    if not APPROVAL_TOKEN:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={APPROVAL_TOKEN}"


def build_html_email(job_id: str, result: dict, png_b64: str,
                     project_name: str = None, project_ref: str = None) -> str:
    base = APPROVAL_BASE_URL.rstrip("/")
    # GET on these routes now only renders a no-mutation confirm page with a POST button
    # (CSRF hardening — see approval_server.py's approve/reject/adjust handlers); the token
    # is still appended so the confirm page itself is reachable when the token gate is on.
    approve_url = _with_token(f"{base}/approve/{job_id}")
    reject_url  = _with_token(f"{base}/reject/{job_id}")
    adjust_url  = _with_token(f"{base}/adjust/{job_id}")
    portal_url  = _with_token(f"{base}/review/{job_id}")

    area    = result.get("area_m2")
    area_s  = f"{area:,.0f} m²" if area else "— (not measured)"
    conf    = result.get("confidence", "—")
    method  = result.get("method", "—")
    drtype  = result.get("type", "—")
    scale_s = str(result.get("scale_k", result.get("scale_src", "—")))
    flags   = [f for f in result.get("flags", [])
               if not any(skip in f for skip in ("BUILD-UP ASSUMED","ARCHITECT drawing"))]
    fname   = result.get("file", "drawing")
    discipline  = result.get("source_discipline", "")
    proj_name   = project_name or result.get("project_name", "")
    proj_ref    = project_ref  or result.get("project_ref",  "")

    flag_rows    = _flag_rows(flags)
    costing_html = _costing_block(result)
    spec_html    = _spec_details_block(result)
    approve_btn  = _BUTTON.format(url=approve_url, bg="#27ae60", label="✅ APPROVE")
    reject_btn   = _BUTTON.format(url=reject_url,  bg="#c0392b", label="✗ REJECT")
    adjust_btn   = _BUTTON.format(url=adjust_url,  bg="#2980b9", label="✏️ ADJUST")
    portal_btn   = _PORTAL_BTN.format(url=portal_url)

    disc_badge = (f"<span style='background:#e67e22;color:#fff;font-size:10px;font-weight:700;"
                  f"padding:2px 7px;border-radius:10px;text-transform:uppercase;"
                  f"margin-left:6px'>{discipline}</span>") if discipline else ""

    # Project header line — shows ref + name so Inderjit can identify in inbox
    proj_ref_s  = f"Ref&nbsp;<b>#{proj_ref}</b>&nbsp;·&nbsp;" if proj_ref else ""
    proj_name_s = f"<b style='color:#13294b;font-size:15px'>{proj_name}</b>" if proj_name else ""
    proj_line   = (f"<tr><td style='padding:10px 28px 2px 28px;font-family:Arial,sans-serif;'>"
                   f"{proj_ref_s}{proj_name_s}</td></tr>") if (proj_ref or proj_name) else ""

    return textwrap.dedent(f"""\
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8">
    <title>Fortel AI Takeoff — Review Required</title></head>
    <body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">

    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
    <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12);">

      <!-- Header -->
      <tr><td style="background:#13294b;padding:20px 28px;">
        <span style="color:#fff;font-size:20px;font-weight:700;">Fortel AI Takeoff</span>
        <span style="color:#aec3e0;font-size:13px;margin-left:12px;">Manual Review Required</span>
      </td></tr>

      <!-- Project name + ref (most prominent) -->
      {proj_line}

      <!-- Job ref + filename -->
      <tr><td style="padding:6px 28px 4px 28px;color:#555;font-size:13px;">
        Job <b style="color:#111">{job_id}</b> &nbsp;·&nbsp;
        <b>{fname}</b>{disc_badge}
      </td></tr>

      <!-- Drawing snapshot -->
      <tr><td style="padding:8px 28px;">
        <img src="data:image/png;base64,{png_b64}"
             style="width:100%;max-width:564px;border-radius:6px;border:1px solid #ddd;"
             alt="Drawing snapshot with AI region">
      </td></tr>

      <!-- Costing block (GBP total + rate breakdown) -->
      {costing_html}

      <!-- Spec details: 4 required fields -->
      {spec_html}

      <!-- Results table -->
      <tr><td style="padding:4px 28px 4px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;font-size:14px;">
          <tr style="background:#f7f8fa;">
            <td style="padding:6px 10px;width:38%;color:#666;font-size:12px;text-transform:uppercase;letter-spacing:.5px;">Field</td>
            <td style="padding:6px 10px;color:#666;font-size:12px;text-transform:uppercase;letter-spacing:.5px;">Value</td>
          </tr>
          <tr><td style="padding:5px 10px;color:#555;">Area</td>
              <td style="padding:5px 10px;font-weight:700;color:#111;font-size:15px;">{area_s}</td></tr>
          <tr style="background:#f7f8fa;">
            <td style="padding:5px 10px;color:#555;">Drawing type</td>
            <td style="padding:5px 10px;color:#111;">{drtype}</td></tr>
          <tr><td style="padding:5px 10px;color:#555;">Scale</td>
              <td style="padding:5px 10px;color:#111;">{scale_s}</td></tr>
          <tr style="background:#f7f8fa;">
            <td style="padding:5px 10px;color:#555;">Confidence</td>
            <td style="padding:5px 10px;color:#111;">{conf}</td></tr>
          <tr><td style="padding:5px 10px;color:#555;">Method</td>
              <td style="padding:5px 10px;color:#111;font-size:12px;">{method}</td></tr>
          {flag_rows}
        </table>
      </td></tr>

      <!-- Action buttons -->
      <tr><td style="padding:20px 28px;text-align:center;border-top:1px solid #eee;">
        <p style="color:#333;font-size:14px;margin:0 0 14px 0;">
          Does the AI region and area look correct?
        </p>
        {approve_btn}{reject_btn}{adjust_btn}
        <br>
        {portal_btn}
        <p style="color:#999;font-size:11px;margin:14px 0 0 0;">
          Approving accepts the area and proceeds to the quotation.<br>
          Adjusting opens the portal where you can nudge the polygon and correct the scale.
        </p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#f7f8fa;padding:12px 28px;border-top:1px solid #eee;">
        <span style="color:#aaa;font-size:11px;">
          Fortel AI Takeoff · {datetime.datetime.utcnow().strftime("%d %b %Y %H:%M")} UTC ·
          Job {job_id}
        </span>
      </td></tr>

    </table>
    </td></tr>
    </table>
    </body></html>
    """)


# ---------------------------------------------------------------------------
# Send / output
# ---------------------------------------------------------------------------
def send_email(to: str, subject: str, html: str) -> bool:
    """Send via SMTP.  Returns True on success."""
    if not SMTP_PASS:
        print(f"[approval_email] SMTP_PASS not set — saving email to disk instead")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[approval_email] SMTP error: {e}")
        return False


def save_email_file(job_id: str, html: str) -> str:
    """Fallback: save email as HTML file for manual sending or n8n pick-up."""
    out = Path(__file__).parent / "approval_emails" / f"{job_id}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    return str(out)


def n8n_webhook_payload(job_id: str, result: dict, html: str,
                        project_name: str = None, project_ref: str = None) -> dict:
    """JSON payload for n8n HTTP node — use when n8n handles the actual email send."""
    proj_name = project_name or result.get("project_name", "")
    proj_ref  = project_ref  or result.get("project_ref",  "")
    ref_part  = f"[#{proj_ref}] " if proj_ref else ""
    name_part = f"{proj_name} — " if proj_name else ""
    subject   = f"Fortel AI — {ref_part}{name_part}Review Required"
    return {
        "job_id":        job_id,
        "to":            APPROVAL_TO,
        "subject":       subject,
        "html":          html,
        "area_m2":       result.get("area_m2"),
        "flags":         result.get("flags", []),
        "portal":        f"{APPROVAL_BASE_URL}/review/{job_id}",
        "project_name":  proj_name,
        "project_ref":   proj_ref,
    }


# ---------------------------------------------------------------------------
# Main entry point (called from takeoff_pipeline.py)
# ---------------------------------------------------------------------------
def request_approval(pdf_path: str, result: dict,
                     polygon_pts: list = None, page: int = 0,
                     to: str = None,
                     project_name: str = None, project_ref: str = None) -> str:
    """
    Full flow:
      1. Create job record
      2. Render snapshot
      3. Build + send email  (subject = "Fortel AI — [#REF] Project Name — Review Required")
    Returns job_id.
    """
    to = to or APPROVAL_TO
    proj_name = project_name or result.get("project_name", "")
    proj_ref  = project_ref  or result.get("project_ref",  "")

    job_id = create_job(pdf_path, result, project_name=proj_name, project_ref=proj_ref)
    png    = render_snapshot(pdf_path, page=page, polygon_pts=polygon_pts)
    b64    = png_to_b64(png)
    html   = build_html_email(job_id, result, b64,
                               project_name=proj_name, project_ref=proj_ref)

    # Build informative subject line (Inderjit's #1 request)
    ref_part  = f"[#{proj_ref}] " if proj_ref else ""
    name_part = f"{proj_name} — " if proj_name else f"{result.get('file', job_id)} — "
    subject   = f"Fortel AI — {ref_part}{name_part}Review Required"

    sent = send_email(to, subject, html)
    if not sent:
        path = save_email_file(job_id, html)
        print(f"[approval_email] Email saved: {path}")
        print(f"[approval_email] Portal:      {APPROVAL_BASE_URL}/review/{job_id}")
    else:
        print(f"[approval_email] Email sent to {to}. Job: {job_id}")

    return job_id


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else "drawings/UNMARKED_Yard.pdf"
    demo_result = {
        "file":       "UNMARKED_Yard.pdf",
        "type":       "UNMARKED vector",
        "method":     "per-viewport scale + vision-proposed boundary + assessor confirm",
        "confidence": "medium",
        "area_m2":    24725,
        "scale_k":    0.108,
        "scale_src":  "auto scale-bar (UNVERIFIED)",
        "flags": [
            "BUILD-UP ASSUMED: 190mm / A252 / C32/40 — no engineer construction-detail found",
            "scale UNVERIFIED — assessor must confirm against parking bay (2.5 m) or scale bar",
            "assessor: confirm extent + scale",
        ],
    }
    demo_poly = [
        [100, 100], [900, 100], [900, 700], [700, 750], [400, 780], [100, 700]
    ]
    jid = request_approval(pdf, demo_result, polygon_pts=demo_poly)
    print(f"Demo job created: {jid}")
