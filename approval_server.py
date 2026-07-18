#!/usr/bin/env python3
"""
Fortel AI Takeoff — Approval Server

Flask app that handles all manual-review interactions:

  GET  /                     → redirect to portal
  GET  /portal               → serve assessor_portal.html
  GET  /jobs                 → list all jobs (JSON)
  GET  /job/<id>             → single job detail (JSON)
  GET  /snapshot/<id>        → drawing PNG with AI polygon overlaid
  POST /approve/<id>         → mark approved; trigger costing with original area
  POST /reject/<id>          → mark rejected; log reason
  POST /adjust/<id>          → accept assessor's corrected polygon/scale; re-cost

All state lives in approval_jobs.json (created by approval_email.py).
Training data (decisions + corrections) is appended to training_log.jsonl.

Run:
  pip install flask pillow pymupdf shapely --break-system-packages
  python3 approval_server.py          # default port 5001

Environment:
  APPROVAL_PORT    default 5001
  APPROVAL_HOST    default 127.0.0.1 (single-team deployment; set 0.0.0.0 only with
                    APPROVAL_TOKEN also set, e.g. on the shared office Mac)
  PORTAL_TOKEN     shared secret gating every mutating/data-bearing route (APPROVAL_TOKEN is
                    accepted as an older alias). If unset, the server runs with NO auth (fine
                    for 127.0.0.1-only local use) but refuses to bind 0.0.0.0 without one (see
                    main guard below).
  JOBS_FILE        override path to the jobs datastore (default approval_jobs.json next to
                    this file) — lets QA/test instances point at a scratch file instead of
                    colliding with the live jobs file (CLAUDE.md: "QA jobs out of
                    approval_jobs.json").
  CLIENT_RATES_FILE optional override path; otherwise client_rates.json is stored beside
                    JOBS_FILE (including on the same Railway volume).
"""
import os, json, io, datetime, traceback, uuid, re, threading, zipfile, email, shutil, secrets, hashlib, math
from email import policy
from pathlib import Path
from flask import Flask, request, jsonify, send_file, redirect, Response

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

# Railway's container filesystem is EPHEMERAL — every deploy wiped approval_jobs.json
# (observed live: job_count 2 -> 0 across the 16 Jul deploy). When a Railway volume is
# attached, Railway sets RAILWAY_VOLUME_MOUNT_PATH; store the job state there so pending
# client jobs survive deploys. Explicit JOBS_FILE env still wins; local dev unchanged.
_VOLUME_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
JOBS_FILE    = Path(os.getenv("JOBS_FILE") or
                    ((Path(_VOLUME_DIR) / "approval_jobs.json") if _VOLUME_DIR
                     else (Path(__file__).parent / "approval_jobs.json")))
from client_rates import rates_path_for_jobs
CLIENT_RATES_FILE = Path(os.getenv("CLIENT_RATES_FILE") or rates_path_for_jobs(JOBS_FILE))
# JOBS_ARCHIVE_FILE / BACKUP_DIR used to derive from JOBS_FILE.parent only — so a QA instance
# started with JOBS_FILE=approval_jobs.qa.json still shared approval_jobs_archive.json and
# backups/ with the live instance (both live in the same directory). Archive/backups now
# derive from the JOBS_FILE STEM instead, so approval_jobs.qa.json gets its own
# approval_jobs.qa_archive.json and backups_approval_jobs.qa/ — never colliding with the live
# instance's approval_jobs_archive.json / backups/. Dedicated env overrides win if set
# (e.g. a QA setup that wants archive/backups somewhere else entirely).
JOBS_ARCHIVE_FILE = Path(os.getenv("JOBS_ARCHIVE_FILE") or
                         (JOBS_FILE.parent / f"{JOBS_FILE.stem}_archive.json"))
TRAINING_LOG = Path(__file__).parent / "training_log.jsonl"
PORTAL_HTML  = Path(__file__).parent / "assessor_portal.html"
BACKUP_DIR   = Path(os.getenv("BACKUP_DIR") or
                    (JOBS_FILE.parent / ("backups" if JOBS_FILE.stem == "approval_jobs"
                                         else f"backups_{JOBS_FILE.stem}")))
BACKUP_KEEP  = 14   # keep the newest N daily backups

_jobs_lock = threading.Lock()

# ── Auth (pragmatic shared-secret — right-sized for a single small team) ─────
# PORTAL_TOKEN (or its older alias APPROVAL_TOKEN — both accepted, PORTAL_TOKEN wins if both
# are set), if set, gates every route except /status (health-check) and the static portal
# shell itself (the portal's own fetch() calls still need the token/cookie to get any data
# back, so an unauthenticated visitor sees an empty, non-functional page — not a 404, since a
# 404 here would be more confusing than useful).
APPROVAL_TOKEN = os.getenv("PORTAL_TOKEN") or os.getenv("APPROVAL_TOKEN", "")
_TOKEN_COOKIE  = "approval_token"


def _detect_build_info() -> dict:
    """Resolve deploy SHA/date once at startup; health checks must never depend on git."""
    sha = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    date = (os.getenv("RAILWAY_GIT_COMMIT_DATE") or "").strip()
    repo_dir = Path(__file__).parent
    try:
        import subprocess
        if not sha:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
                text=True, timeout=2, check=True,
            ).stdout.strip()
        if sha and not date:
            date = subprocess.run(
                ["git", "show", "-s", "--format=%cI", sha], cwd=repo_dir,
                capture_output=True, text=True, timeout=2, check=True,
            ).stdout.strip()
    except Exception:
        # Railway images may intentionally omit .git. The env SHA still remains useful.
        pass
    return {"sha": sha or "unknown", "date": date or "unknown"}


BUILD_INFO = _detect_build_info()


def _build_label() -> str:
    sha = BUILD_INFO.get("sha") or "unknown"
    short_sha = sha[:7] if sha != "unknown" else sha
    return f"Build {short_sha} · {BUILD_INFO.get('date') or 'unknown'}"


def _token_ok(supplied: str) -> bool:
    if not APPROVAL_TOKEN or not supplied:
        return False
    # constant-time compare — this is a shared secret, not a public value
    return secrets.compare_digest(supplied, APPROVAL_TOKEN)


def _portal_login_page(error: bool = False) -> str:
    """Render a small shared-code login form without exposing the configured secret."""
    import html as _html
    error_html = ('<p style="color:#c0392b;font-size:13px;margin:10px 0 0 0">Incorrect code</p>'
                  if error else "")
    build_label = _html.escape(_build_label())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — Sign in</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:360px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <h2 style="color:#13294b;margin:0 0 8px 0">Fortel Approval Portal</h2>
      <p style="color:#666;font-size:14px">Enter the access code to continue.</p>
      <form method="post" action="/portal">
        <input type="password" name="code" placeholder="Access code" autofocus required
               style="width:100%;box-sizing:border-box;padding:10px;font-size:14px;
                      border:1px solid #ccc;border-radius:6px;margin-top:14px">
        <button type="submit" style="display:block;width:100%;box-sizing:border-box;
               padding:12px;margin-top:10px;background:#13294b;color:#fff;border:none;
               border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;">Enter</button>
      </form>
      {error_html}
    </div>
    <div id="buildFooter" style="position:fixed;right:12px;bottom:8px;color:#777;
         font-size:11px">{build_label}</div>
    </body></html>"""


@app.before_request
def _require_token():
    if not APPROVAL_TOKEN:
        return None  # no token configured -> auth disabled (local/dev use)
    if request.method == "OPTIONS":
        return None
    if request.path in ("/status", "/"):
        return None
    # One-time bootstrap: /portal?token=XXX sets the cookie, then redirects to the clean URL
    # so the token never lingers in browser history/bookmarks past the first visit.
    if request.path == "/portal":
        qtoken = request.args.get("token", "")
        if _token_ok(qtoken):
            resp = redirect("/portal")
            resp.set_cookie(_TOKEN_COOKIE, APPROVAL_TOKEN, httponly=True, samesite="Lax",
                             max_age=60 * 60 * 24 * 30)
            return resp
        if _token_ok(request.cookies.get(_TOKEN_COOKIE, "")):
            return None
        if request.method == "POST":
            if _token_ok(request.form.get("code", "")):
                resp = redirect("/portal")
                resp.set_cookie(_TOKEN_COOKIE, APPROVAL_TOKEN, httponly=True, samesite="Lax",
                                 max_age=60 * 60 * 24 * 30)
                return resp
            return Response(_portal_login_page(error=True), 200,
                             {"Content-Type": "text/html; charset=utf-8"})
        return Response(_portal_login_page(), 200,
                        {"Content-Type": "text/html; charset=utf-8"})
    # Every other route: accept Bearer header, cookie, or (for emailed action links) ?token=
    supplied = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        supplied = auth_header[len("Bearer "):]
    if not supplied:
        supplied = request.cookies.get(_TOKEN_COOKIE, "")
    if not supplied:
        supplied = request.args.get("token", "")
    if not _token_ok(supplied):
        return jsonify({"error": "unauthorized — missing or invalid token"}), 401
    return None


# ── helpers ──────────────────────────────────────────────────────────────────

def load_jobs() -> dict:
    """Read approval_jobs.json.

    save_jobs() below writes atomically (tmp file + os.replace), so a well-formed writer
    can never leave a torn/partial file on disk. But this file predates that fix, and any
    external writer (a script, a stray editor) could still leave a transiently-partial file
    mid-write; guard the parse so a concurrent /jobs poll never 500s on a race, it just sees
    a momentarily-empty job list.
    """
    if JOBS_FILE.exists():
        try:
            text = JOBS_FILE.read_text()
            return json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            # A non-empty file that fails to parse is a real corruption event, not the benign
            # torn-read race the try/except above was originally written for (this file
            # predates atomic writes for some external writers). Preserve the evidence instead
            # of silently returning {} and then having the next save_jobs() overwrite it —
            # rename the bad file aside so it can be inspected/recovered, and log loudly so a
            # blank job list in the portal is never a silent mystery.
            try:
                if JOBS_FILE.exists() and JOBS_FILE.stat().st_size > 0:
                    corrupt_path = JOBS_FILE.with_suffix(
                        f".json.corrupt-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}")
                    shutil.copy2(JOBS_FILE, corrupt_path)
                    print(f"[load_jobs] CRITICAL: {JOBS_FILE} failed to parse ({e}); "
                          f"preserved a copy at {corrupt_path}. Returning empty job list.")
            except OSError:
                pass
            return {}
    return {}

def _rotate_backup():
    """Once per calendar day, copy the current jobs file into backups/ before the first save
    of the day, then prune to the newest BACKUP_KEEP. Cheap insurance against a corrupting
    write/edit destroying all decision history — no database needed at this scale.

    Backup filenames are keyed off JOBS_FILE.stem (not hardcoded "approval_jobs") so that if
    BACKUP_DIR is ever shared between two differently-named jobs files (e.g. an explicit
    BACKUP_DIR override), their dated backups don't collide or get pruned against each other.
    """
    try:
        stem = JOBS_FILE.stem
        if JOBS_FILE.exists():
            BACKUP_DIR.mkdir(exist_ok=True)
            today = datetime.date.today().isoformat()
            dated = BACKUP_DIR / f"{stem}.{today}.json"
            if not dated.exists():
                shutil.copy2(JOBS_FILE, dated)
        # Prune unconditionally (not just when a new backup was just made) — otherwise a
        # backlog of old backups (e.g. BACKUP_KEEP lowered, or files added by another process)
        # never gets cleaned up once today's backup already exists.
        if BACKUP_DIR.exists():
            backups = sorted(BACKUP_DIR.glob(f"{stem}.*.json"))
            for stale in backups[:-BACKUP_KEEP]:
                stale.unlink(missing_ok=True)
    except OSError as e:
        print(f"[_rotate_backup] WARNING: could not rotate jobs backup: {e}")

def save_jobs(jobs: dict):
    """Write approval_jobs.json atomically.

    Plain write_text() is NOT atomic — it truncates the file then streams bytes in, so any
    concurrent reader (the portal polls GET /jobs every 15s; multiple upload/approve/reject/
    adjust/watchdog writers can all fire close together) can observe a half-written file and
    hit a JSONDecodeError. That surfaced in the field as "the server is unstable". Write to a
    temp file in the same directory and os.replace() it into place — POSIX guarantees rename
    is atomic, so readers always see either the old or the new complete file, never a partial
    one.
    """
    _rotate_backup()
    tmp = JOBS_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, JOBS_FILE)

def log_training(entry: dict):
    """Append a decision to the training log for model improvement."""
    TRAINING_LOG.parent.mkdir(exist_ok=True)
    with TRAINING_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()

def get_job(job_id: str) -> dict | None:
    """Look up a job in the hot store, falling back to the archive.

    /archive moves a job's record into approval_jobs_archive.json and deletes it from the hot
    JOBS_FILE (see archive_job) — but /snapshot/<id>, /job/<id> and /quotation/<id>.<fmt> all
    route through get_job()/require_job(), so an archived job used to 404 on every one of
    them. That's a real regression for an assessor who archives a job then later wants to look
    at (or re-download the quotation for) what they archived — soft-delete is supposed to mean
    "hidden from the default list", not "unreachable". Fall back to the archive so archived
    jobs keep working everywhere except the default /jobs listing.
    """
    job = load_jobs().get(job_id)
    if job is not None:
        return job
    return _load_archive().get(job_id)

def require_job(job_id: str):
    j = get_job(job_id)
    if not j:
        return None, jsonify({"error": f"job {job_id!r} not found"}), 404
    return j, None, None


# ── CORS ──────────────────────────────────────────────────────────────────────
# The portal is served same-origin from /portal and needs no CORS at all. A wildcard
# Access-Control-Allow-Origin combined with (formerly) no auth meant ANY webpage open in
# ANY browser on the LAN could drive /approve, /reject etc. cross-origin — closed per the
# prod audit. If a legitimate cross-origin caller is ever needed (e.g. an n8n instance on a
# different host calling /webhook/n8n from browser JS — most n8n setups call server-side and
# don't need this at all), set APPROVAL_CORS_ORIGIN to that single origin. Never '*'.
_CORS_ORIGIN = os.getenv("APPROVAL_CORS_ORIGIN", "")


@app.after_request
def add_cors(resp):
    if _CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"]  = _CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:p>", methods=["OPTIONS"])
def options(p=""):
    return Response(status=204)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/portal")

@app.route("/portal", methods=["GET", "POST"])
def portal():
    if PORTAL_HTML.exists():
        return PORTAL_HTML.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "Portal HTML not found — make sure assessor_portal.html is in the same folder.", 404

@app.route("/jobs")
def list_jobs():
    return jsonify(load_jobs())

@app.route("/job/<job_id>")
def single_job(job_id):
    j, err, code = require_job(job_id)
    if err: return err, code
    return jsonify(j)


def _client_rate_defaults() -> dict:
    """Read current defaults without duplicating or modifying any value."""
    from defaults import DEFAULT_SPEC
    from takeoff_pipeline import MANHOLE_EO_RATE
    from client_rates import RATE_FIELDS
    return {
        key: (MANHOLE_EO_RATE if key == "manhole_eo_rate" else DEFAULT_SPEC[key])
        for key in RATE_FIELDS
    }


def _apply_current_client_rates(spec: dict, *, manhole_in_scope: bool = False):
    """Apply the persisted layer to a resolved pricing spec; calculation stays elsewhere."""
    from client_rates import apply_client_rates
    from takeoff_pipeline import MANHOLE_EO_RATE
    return apply_client_rates(
        spec, MANHOLE_EO_RATE, path=CLIENT_RATES_FILE,
        manhole_in_scope=manhole_in_scope)


@app.route("/rates", methods=["GET", "POST"])
def client_rates_endpoint():
    """Show/save client rate overrides. The global token gate protects both methods."""
    from client_rates import (ClientRatesError, effective_rate_payload,
                              save_client_rates)
    defaults = _client_rate_defaults()
    try:
        if request.method == "GET":
            return jsonify(effective_rate_payload(defaults, path=CLIENT_RATES_FILE))
        data = request.get_json(silent=True) or {}
        who = "assessor-token-authenticated" if APPROVAL_TOKEN else "assessor-local"
        saved, changes = save_client_rates(
            data.get("rates"), defaults, path=CLIENT_RATES_FILE, who=who)
        if not changes:
            return jsonify({"error": "no rate values changed; no version was saved"}), 409
        payload = effective_rate_payload(defaults, path=CLIENT_RATES_FILE)
        payload.update({"status": "saved", "changes": changes,
                        "version": saved["version"]})
        return jsonify(payload)
    except ClientRatesError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/snapshot/<job_id>")
def snapshot(job_id):
    """Render the drawing page with the AI polygon overlaid, return PNG."""
    j, err, code = require_job(job_id)
    if err: return err, code
    try:
        from approval_email import render_snapshot, snapshot_scale
        res  = j.get("result", {})
        # "pdf_path" is used by new upload-form jobs; legacy jobs used "pdf"
        pdf  = res.get("pdf_path") or j.get("pdf_path") or j.get("pdf", "")
        # Resolve relative paths (legacy records) against the server directory
        if pdf and not Path(pdf).is_absolute():
            pdf = str(Path(__file__).parent / pdf)
        poly = res.get("polygon_pts")
        # Multi-page tender packs: takeoff_pipeline.takeoff() ranks every page and measures
        # the best one (result["page"]), NOT necessarily page 0 — see router.rank_pages. The
        # AI's polygon_pts are in that measured page's PDF-point coordinate space. Rendering
        # page 0 unconditionally (the old behaviour) showed the WRONG page for any multi-page
        # pack whose best page wasn't 0, so the "AI polygon" either looked misplaced/garbled
        # or simply didn't correspond to anything visible on screen — this was the field
        # report "need to show the actual highlighted area in AI polygon". Always render the
        # SAME page the measurement came from.
        page = res.get("page") or 0
        if not pdf or not Path(pdf).exists():
            return jsonify({"error": "PDF not on disk — snapshot unavailable"}), 404
        # Guard against a stale/out-of-range page index (e.g. a page count mismatch after
        # the source file was replaced) — fall back to page 0 rather than 500ing.
        try:
            import fitz as _fitz
            with _fitz.open(pdf) as _doc:
                if not (0 <= page < _doc.page_count):
                    page = 0
        except Exception:
            page = 0
        png = render_snapshot(pdf, page=page, polygon_pts=poly)
        resp = send_file(io.BytesIO(png), mimetype="image/png")
        # Expose the ACTUAL render scale (snapshot px per PDF point) so the portal can
        # convert scale_k (m/pt) -> metres per canvas pixel:  mpp = scale_k / snap_scale.
        # Without this the portal assumed 0.5 and mis-scaled area on wide (A1/A0) sheets.
        resp.headers["X-Snapshot-Scale"] = f"{snapshot_scale(pdf, page=page):.6f}"
        resp.headers["Access-Control-Expose-Headers"] = "X-Snapshot-Scale"
        return resp
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Decision endpoints ────────────────────────────────────────────────────────

def _zone_block_reason(job: dict) -> str | None:
    """Return the assessor action needed before zone-aware approval/quotation is safe."""
    result = job.get("result") or {}
    zones = job.get("zones")
    if not isinstance(zones, list):
        zones = result.get("zones") if isinstance(result.get("zones"), list) else []
    required = bool(
        job.get("zone_classification_required")
        or result.get("zone_classification_required")
        or job.get("zone_allocation_stale")
        or result.get("zone_allocation_stale")
        or job.get("zone_reference_mismatch")
        or result.get("zone_reference_mismatch")
        or any((zone.get("category") or "").strip().lower() == "unclassified"
               for zone in zones if isinstance(zone, dict))
    )
    if not required:
        return None
    if job.get("zone_allocation_stale") or result.get("zone_allocation_stale"):
        return ("zone allocation is stale after an aggregate adjustment; assessor must "
                "reclassify/remeasure the drawing zones")
    if job.get("zone_reference_mismatch") or result.get("zone_reference_mismatch"):
        return ("measured zone quantities do not match the client reference beyond tolerance; "
                "assessor must review the mismatch before approval")
    return ("one or more measured markup zones are unclassified; assessor must classify "
            "every zone before approval")


def _approve_block_reason(job: dict) -> str | None:
    """
    Server-side hard-block mirroring the escalation-guard mechanism (fb5b92b, >£200k
    assumed-spec jobs): MEASURED_UNVERIFIED and UNMEASURED jobs cannot be approved until an
    assessor has confirmed scale+extent (via /adjust, which sets scale_confirmed=True).
    Returns a reason string to block, or None to allow.
    """
    result = job.get("result") or {}
    zone_reason = _zone_block_reason(job)
    if zone_reason:
        return zone_reason
    if job.get("spec_pricing_warning"):
        return "slab specification is saved but requires human pricing review before approval"
    state = job.get("measurement_state") or result.get("measurement_state")
    if job.get("scale_confirmed"):
        return None
    if state == "REJECTED":
        return "job is REJECTED — cannot be approved"
    if state == "UNMEASURED":
        return ("UNMEASURED — no reliable area was measured; assessor must supply area+scale "
                "via Adjust before this job can be approved")
    if state == "MEASURED_UNVERIFIED":
        return ("MEASURED_UNVERIFIED — scale unverified, low confidence, or implausible area; "
                "assessor must confirm scale+extent via Adjust before this job can be approved")
    return None


@app.route("/approve/<job_id>", methods=["GET", "POST"])
def approve(job_id):
    """
    Approve: accept the AI's measurement as-is, proceed to costing.

    GET performs NO mutation — it only renders a confirm page with a POST button. Mutating
    routes accepting GET with SameSite=Lax cookies is a top-level-navigation CSRF hole: any
    page (or a pre-fetching email client / link scanner) that merely links to or navigates to
    this URL would have approved/rejected a job just by being opened, no user click required.
    The emailed action buttons and the portal's own JS both POST already (see
    approval_email.build_html_email and assessor_portal.html's submitDecision) — GET now only
    exists so an emailed link lands on a safe, human-readable "confirm this?" page.
    """
    if request.method == "GET":
        j = get_job(job_id)
        if not j:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return _html_confirm_page("approve", job_id)

    data = request.get_json(silent=True) or {} if request.is_json else {}

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        block_reason = _approve_block_reason(job)
        if block_reason:
            return jsonify({"error": f"approve blocked: {block_reason}"}), 409
        jobs[job_id].update({
            "status":     "approved",
            "decision":   "approved",
            "decided_at": now_iso(),
            "note":       data.get("note", ""),
        })
        save_jobs(jobs)

    # Trigger costing with the AI's area (use snapshot captured inside lock).
    # If the assessor already used spec-override, preserve their costing rather than
    # recomputing with defaults — recomputing would silently undo the correction.
    res = job.get("result", {})
    if job.get("costing") and job.get("spec_override"):
        costing_result = job["costing"]
    else:
        costing_result = _run_costing(res.get("area_m2"), res)
    # Auto-generate and save quotation
    quotation_paths = _save_quotation(job_id, res, costing_result)
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id]["costing"] = costing_result
        jobs[job_id]["quotation_paths"] = quotation_paths
        save_jobs(jobs)

    log_training({
        "event":      "approve",
        "job_id":     job_id,
        "file":       res.get("file"),
        "area_m2":    res.get("area_m2"),
        "flags":      res.get("flags", []),
        "timestamp":  now_iso(),
    })

    # The confirm page's <form> does a plain (non-JSON) POST and wants the human-readable
    # result page back; the portal's own JS POSTs JSON and wants JSON back (unchanged).
    if not request.is_json:
        return _html_confirmation("approved", job_id, costing_result)
    return jsonify({"status": "approved", "job_id": job_id, "costing": costing_result})


@app.route("/reject/<job_id>", methods=["GET", "POST"])
def reject(job_id):
    """Reject: mark the measurement as wrong; do not proceed to costing.

    GET performs NO mutation (see approve() docstring for the CSRF rationale) — it renders a
    confirm page whose button issues the actual POST.
    """
    if request.method == "GET":
        j = get_job(job_id)
        if not j:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return _html_confirm_page("reject", job_id)

    data = request.get_json(silent=True) or {} if request.is_json else {}

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        jobs[job_id].update({
            "status":     "rejected",
            "decision":   "rejected",
            "decided_at": now_iso(),
            "note":       data.get("note", "rejected via portal"),
        })
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":     "reject",
        "job_id":    job_id,
        "file":      res.get("file"),
        "flags":     res.get("flags", []),
        "timestamp": now_iso(),
    })

    if not request.is_json:
        return _html_confirmation("rejected", job_id, None)
    return jsonify({"status": "rejected", "job_id": job_id})


@app.route("/adjust/<job_id>", methods=["GET", "POST"])
def adjust(job_id):
    """
    Adjust: assessor provides corrected polygon region(s) and/or scale (or a bare assessed area for
    UNMEASURED jobs where there's no AI polygon to correct — e.g. raster/scanned drawings).
    Re-runs geometry measurement with the assessor's inputs when a polygon+scale is given,
    then costs. Any assessor-supplied area (polygon-derived OR a direct assessed_area_m2)
    sets scale_confirmed=True, which is what unblocks /approve for MEASURED_UNVERIFIED and
    UNMEASURED jobs (see _approve_block_reason).

    GET already performed NO mutation before this CSRF pass (it only redirects into the
    portal for manual polygon adjustment there, which itself POSTs) — adjust doesn't need
    the confirm-page treatment approve/reject got since there's nothing to confirm without
    the assessor's actual polygon/scale input.
    """
    if request.method == "GET":
        # Quick check before redirect (no lock needed — read-only, no mutation)
        if not load_jobs().get(job_id):
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return redirect(f"/portal?job={job_id}")

    data = request.get_json(silent=True) or {}
    vertices   = data.get("vertices", [])    # legacy single [[x,y], ...]
    regions_in = data.get("regions")         # new multi-region [[[x,y], ...], ...]
    candidate_ids = data.get("candidate_ids") or []
    scale_k    = data.get("scale_k")         # m/px
    area_m2    = data.get("assessed_area_m2")
    note       = data.get("note", "")

    def _valid_region(region):
        return (
            isinstance(region, list)
            and 3 <= len(region) <= 500
            and all(isinstance(point, (list, tuple)) and len(point) == 2
                    and all(isinstance(value, (int, float)) and math.isfinite(value)
                            and abs(value) <= 10_000_000 for value in point)
                    for point in region)
        )

    if regions_in is not None:
        if (not isinstance(regions_in, list) or not 1 <= len(regions_in) <= 50
                or not all(_valid_region(region) for region in regions_in)):
            return jsonify({"error": "regions must contain 1-50 valid polygons"}), 400
        regions = regions_in
    elif vertices:
        if not _valid_region(vertices):
            return jsonify({"error": "vertices must contain a valid polygon"}), 400
        regions = [vertices]
    else:
        regions = []

    candidate_ids_valid = (
        isinstance(candidate_ids, list)
        and len(candidate_ids) <= len(regions)
        and all(isinstance(candidate_id, str) and candidate_id for candidate_id in candidate_ids)
    )
    if (not candidate_ids_valid
            or len(candidate_ids) != len(set(candidate_ids))):
        return jsonify({"error": "candidate_ids must be unique known candidate identifiers"}), 400

    # If assessor traced one or more polygons + scale, re-measure (heavy I/O — outside lock).
    # Legacy `vertices` remains exactly one region; Office GA candidates can now be combined.
    if regions and scale_k:
        try:
            from geometry import measure_regions, polygon_perimeter_lm
            area_m2, gflags = measure_regions(regions, scale_k)
            perimeters = [polygon_perimeter_lm(region, scale_k) for region in regions]
            perimeter_lm = round(sum(value for value in perimeters if value is not None), 2)
        except Exception as e:
            area_m2, perimeter_lm, gflags = None, None, [f"geometry error: {e}"]
    else:
        perimeter_lm = None
        gflags = []

    # A valid assessor-supplied area (however it arrived) is a human confirmation of
    # scale+extent — this is what unblocks approve for MEASURED_UNVERIFIED/UNMEASURED jobs.
    # Still run the plausibility guard (sanity.plausible): an assessor can fat-finger a trace
    # too, so an implausible area does NOT silently confirm — it stays blocked for a second look.
    from sanity import plausible as _plausible
    plaus_flags = _plausible(area_m2) if area_m2 else []
    gflags = gflags + plaus_flags
    confirmed = bool(area_m2 and area_m2 > 0 and not plaus_flags)

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        stored_candidates = {
            candidate.get("candidate_id")
            for candidate in (job.get("candidate_polygons")
                              or (job.get("result") or {}).get("candidate_polygons") or [])
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        }
        if any(candidate_id not in stored_candidates for candidate_id in candidate_ids):
            return jsonify({"error": "one or more candidate_ids are stale or unknown"}), 409
        stored_result = dict(job.get("result") or {})
        had_zone_allocation = bool(area_m2 and area_m2 > 0) and bool(
            (isinstance(job.get("zones"), list) and job.get("zones"))
            or (isinstance(stored_result.get("zones"), list) and stored_result.get("zones"))
        )
        zone_stale_flag = None
        if had_zone_allocation:
            # /adjust supplies one replacement aggregate trace/area, not a per-zone edit.
            # Keeping the old marked-PDF split would make the four quotation sections add up
            # to the superseded measurement. Preserve raw annotation evidence, but clear the
            # derived zones and hard-block approval/quotation until an assessor reclassifies
            # or remeasures them.
            zone_stale_flag = (
                "ZONE ALLOCATION STALE: aggregate adjustment replaced the measured area; "
                "assessor must reclassify/remeasure zones"
            )
            gflags = list(gflags) + [zone_stale_flag]
            result_flags = list(stored_result.get("flags") or [])
            if zone_stale_flag not in result_flags:
                result_flags.append(zone_stale_flag)
            stored_result.update({
                "zones": [],
                "zone_classification_required": True,
                "zone_allocation_stale": True,
                "flags": result_flags,
                "needs_assessor": True,
            })
        costing_result = _run_costing(area_m2, stored_result) if area_m2 else None
        jobs[job_id].update({
            "status":            "adjusted",
            "decision":          "adjusted",
            "decided_at":        now_iso(),
            "scale_confirmed":   confirmed or job.get("scale_confirmed", False),
            "measurement_state": ("MEASURED_VERIFIED" if confirmed
                                  else "MEASURED_UNVERIFIED" if (area_m2 and plaus_flags)
                                  else job.get("measurement_state")),
            "adjusted": {
                "vertices": regions[0] if len(regions) == 1 else [],
                "regions":  regions,
                "candidate_ids": candidate_ids,
                "scale_k":  scale_k,
                "area_m2":  area_m2,
                "perimeter_lm": perimeter_lm,
                "flags":    gflags,
                "note":     note,
            },
            "costing": costing_result,
        })
        if had_zone_allocation:
            jobs[job_id].update({
                "zones": [],
                "zone_classification_required": True,
                "zone_allocation_stale": True,
                "needs_assessor": True,
                "result": stored_result,
            })
            top_flags = list(jobs[job_id].get("flags") or [])
            if zone_stale_flag not in top_flags:
                top_flags.append(zone_stale_flag)
            jobs[job_id]["flags"] = top_flags
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":          "adjust",
        "job_id":         job_id,
        "file":           res.get("file"),
        "ai_area_m2":     res.get("area_m2"),
        "assessed_area":  area_m2,
        "ai_polygon":     res.get("polygon_pts"),
        "assessed_polygon": regions[0] if len(regions) == 1 else None,
        "assessed_regions": regions,
        "candidate_ids":    candidate_ids,
        "scale_k":        scale_k,
        "flags":          res.get("flags", []),
        "timestamp":      now_iso(),
    })

    return jsonify({
        "status":   "adjusted",
        "job_id":   job_id,
        "area_m2":  area_m2,
        "perimeter_lm": perimeter_lm,
        "region_count": len(regions),
        "costing":  costing_result,
        "flags":    gflags,
    })


@app.route("/zones/<job_id>", methods=["POST"])
def classify_zones(job_id):
    """Persist explicit assessor classifications for previously unknown markup subjects."""
    data = request.get_json(silent=True) or {}
    classifications = data.get("classifications") or []
    acknowledge_mismatch = data.get("acknowledge_reference_mismatch") is True
    if not isinstance(classifications, list) or (not classifications and not acknowledge_mismatch):
        return jsonify({"error": "classifications or mismatch acknowledgement required"}), 400
    requested = {
        str(item.get("zone_key") or ""): str(item.get("category") or "").strip().lower()
        for item in classifications if isinstance(item, dict)
    }
    allowed = {"external_yard", "dock", "ground_floor", "upper_floor",
               "channel", "transition", "other"}
    if any(not key or category not in allowed for key, category in requested.items()):
        return jsonify({"error": "invalid zone_key/category"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        result = dict(job.get("result") or {})
        source_zones = job.get("zones") if isinstance(job.get("zones"), list) else result.get("zones")
        zones = [dict(zone) for zone in (source_zones or []) if isinstance(zone, dict)]
        seen = set()
        for zone in zones:
            zone_key = str(zone.get("zone_key") or "")
            if zone_key not in requested:
                continue
            category = requested[zone_key]
            is_area = isinstance(zone.get("area_m2"), (int, float))
            is_length = isinstance(zone.get("length_lm"), (int, float))
            if is_area and category not in {"external_yard", "dock", "ground_floor",
                                            "upper_floor", "other"}:
                return jsonify({"error": f"area zone {zone_key!r} cannot be {category}"}), 400
            if is_length and category not in {"channel", "transition", "other"}:
                return jsonify({"error": f"length zone {zone_key!r} cannot be {category}"}), 400
            if not is_area and not is_length and category != "other":
                return jsonify({"error": f"unparsed zone {zone_key!r} can only be other"}), 400
            zone.update({
                "category": category,
                "classification_source": "assessor",
                "needs_assessor": False,
            })
            seen.add(zone_key)
        missing = set(requested) - seen
        if missing:
            return jsonify({"error": f"unknown zone_key(s): {', '.join(sorted(missing))}"}), 400

        still_unclassified = any(zone.get("category") == "unclassified" for zone in zones)
        result_flags = [flag for flag in (result.get("flags") or [])
                        if not str(flag).startswith("assessor: classify zone")]
        top_flags = [flag for flag in (job.get("flags") or [])
                     if not str(flag).startswith("assessor: classify zone")]
        if acknowledge_mismatch:
            acknowledgement = "assessor acknowledged zone-vs-BOQ mismatch after review"
            if acknowledgement not in result_flags:
                result_flags.append(acknowledgement)
            if acknowledgement not in top_flags:
                top_flags.append(acknowledgement)
            result["zone_reference_mismatch"] = False
            result["zone_reference_reviewed_at"] = now_iso()
        if still_unclassified:
            for zone in zones:
                if zone.get("category") == "unclassified":
                    subject = ", ".join(zone.get("subjects") or []) or zone.get("zone_key")
                    flag = f"assessor: classify zone '{subject}'"
                    result_flags.append(flag)
                    top_flags.append(flag)

        brief_specs = dict(job.get("brief_specs") or result.get("brief_specs") or {})
        from slab_spec import empty_brief_spec
        for zone in zones:
            category = zone.get("category")
            if category in {"external_yard", "dock", "ground_floor", "upper_floor"}:
                brief_specs.setdefault(category, empty_brief_spec(category))

        result.update({
            "zones": zones,
            "brief_specs": brief_specs,
            "zone_classification_required": still_unclassified,
            "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                result.get("zone_reference_mismatch", False)),
            "flags": result_flags,
        })
        job.update({
            "zones": zones,
            "brief_specs": brief_specs,
            "zone_classification_required": still_unclassified,
            "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                job.get("zone_reference_mismatch", result.get("zone_reference_mismatch", False))),
            "flags": top_flags,
            "result": result,
        })
        jobs[job_id] = job
        save_jobs(jobs)
    return jsonify({"status": "zones_updated", "zones": zones,
                    "zone_classification_required": still_unclassified,
                    "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                        result.get("zone_reference_mismatch", False))})


@app.route("/spec-override/<job_id>", methods=["POST"])
def spec_override(job_id):
    """Capture Fortel's slab checklist and re-price only its supplied pricing fields.

    The extra Brief_Spec fields are presentation/provenance only.  The existing rate
    calculation remains unchanged; partial common specs continue to use the existing
    costing fallbacks but remain visibly provisional instead of being marked confirmed.
    """
    data = request.get_json(silent=True) or {}
    nested_fields = data.get("fields")
    if nested_fields is not None and not isinstance(nested_fields, dict):
        return jsonify({"error": "fields must be an object"}), 400
    from slab_spec import (COMMON_FIELDS, FIELD_LABELS, build_brief_spec,
                           confirmed_values, normalise_slab_type, schema_definition)
    supplied_slab_type = data.get("slab_type")
    zone_category = data.get("zone_category")
    if supplied_slab_type and (
            not isinstance(supplied_slab_type, str)
            or supplied_slab_type not in schema_definition()):
        return jsonify({"error": "unknown slab_type"}), 400
    if zone_category and (
            not isinstance(zone_category, str)
            or zone_category not in schema_definition()):
        return jsonify({"error": "unknown zone_category"}), 400
    supplied_fields = dict(nested_fields) if nested_fields is not None else {
        key: data[key] for key in FIELD_LABELS if key in data
    }

    with _jobs_lock:
        jobs = load_jobs()
        job  = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409

        res     = dict(job.get("result") or {})
        adj     = job.get("adjusted") or {}
        area_m2 = adj.get("area_m2") or res.get("area_m2")
        if zone_category:
            # A mixed marked sheet has no safe job-level rate to inherit (the real BOQ uses
            # different Yard/Dock build-ups). Capture the client checklist per zone, but leave
            # its rate for explicit assessor entry in the editable quotation.
            zones = job.get("zones") if isinstance(job.get("zones"), list) else res.get("zones") or []
            if not any(zone.get("category") == zone_category for zone in zones
                       if isinstance(zone, dict)):
                return jsonify({"error": "zone_category is not present on this job"}), 400
            brief_specs = dict(job.get("brief_specs") or res.get("brief_specs") or {})
            try:
                brief_spec = build_brief_spec(
                    zone_category,
                    confirmed=supplied_fields,
                    source="assessor",
                    existing=brief_specs.get(zone_category) or {},
                    replace=nested_fields is not None,
                )
            except (TypeError, ValueError) as e:
                return jsonify({"error": f"invalid spec field: {e}"}), 400
            brief_specs[zone_category] = brief_spec
            res["brief_specs"] = brief_specs
            job["brief_specs"] = brief_specs
            job["result"] = res
            jobs[job_id] = job
            save_jobs(jobs)
            return jsonify({
                "status": "ok", "job_id": job_id, "costing": job.get("costing"),
                "brief_spec": brief_spec, "brief_specs": brief_specs,
                "spec_schema": schema_definition(), "repriced": False,
                "pricing_warning": "",
            })
        try:
            existing_brief = job.get("brief_spec") or res.get("brief_spec") or {}
            slab_type = normalise_slab_type(
                data.get("slab_type") or existing_brief.get("slab_type"),
                text=" ".join(str(res.get(key) or "") for key in
                              ("quotation_section", "file", "project_name", "type")),
            )
            costing = dict(job.get("costing") or res.get("costing") or {})
            effective_spec = costing.get("spec") or {}
            brief_spec = build_brief_spec(
                slab_type,
                effective_spec=effective_spec,
                confirmed=supplied_fields,
                source="assessor",
                existing=existing_brief,
                replace=nested_fields is not None,
            )
            confirmed = confirmed_values(brief_spec)
            pricing_override = {key: confirmed[key] for key in COMMON_FIELDS if key in confirmed}

            # Optional checklist metadata never touches a rate.  When a common pricing field
            # is submitted, preserve the legacy override behaviour and use the same existing
            # defaults + rate_buildup calculation, while keeping missing fields provisional.
            existing_confirmed = confirmed_values(existing_brief)
            pricing_fields_submitted = any(
                key in supplied_fields and supplied_fields.get(key) not in (None, "")
                for key in COMMON_FIELDS
            ) or any(
                key in existing_confirmed and key in supplied_fields
                and supplied_fields.get(key) in (None, "")
                for key in COMMON_FIELDS
            )
            repriced = False
            rates_provenance = {}
            pricing_warning = job.get("spec_pricing_warning") or ""
            if pricing_fields_submitted and not area_m2:
                from costing import MESH_KG
                if pricing_override.get("mesh") and pricing_override["mesh"] not in MESH_KG:
                    pricing_warning = (
                        "Specification saved, but the current rate build-up does not support "
                        "one or more supplied pricing fields; human pricing review required."
                    )
                else:
                    pricing_warning = ""
            if pricing_fields_submitted and area_m2:
                from defaults import spec_with_defaults, assumption_note, flag_assumed
                from costing  import rate_buildup
                try:
                    spec, _ = spec_with_defaults(pricing_override)
                    spec, _manhole_rate, rates_provenance = _apply_current_client_rates(spec)
                    assumed = not all(key in pricing_override for key in COMMON_FIELDS)
                    rate, parts = rate_buildup(**{key: spec[key] for key in [
                        "depth_mm", "conc_rate", "conc_wastage", "mesh", "layers",
                        "steel_rate_t", "steel_wastage", "lap_acc", "dpm", "curing",
                        "labour", "trim", "margin"]})
                    costing = {
                        "area_m2": area_m2,
                        "rate": rate,
                        "total_gbp": round(area_m2 * rate, 2),
                        "spec": spec,
                        "assumed": assumed,
                        "note": assumption_note(spec) if assumed else "Spec overridden by assessor",
                        "flags": flag_assumed(spec, assumed),
                        "breakdown": parts,
                    }
                    costing.update(rates_provenance)
                    # Rebuild so fallback values used in the unchanged calculation are visible,
                    # field-by-field, as assumed rather than as blank confirmed client data.
                    brief_spec = build_brief_spec(
                        slab_type,
                        effective_spec=spec,
                        confirmed=pricing_override | {
                            key: value for key, value in confirmed.items() if key not in COMMON_FIELDS
                        },
                        source="assessor",
                        replace=True,
                    )
                    repriced = True
                    pricing_warning = ""
                except Exception:
                    # The client checklist deliberately accepts open text. Preserve an
                    # unsupported-but-valid client specification without inventing a rate or
                    # losing their entry; approval stays blocked for human pricing review.
                    pricing_warning = (
                        "Specification saved, but the current rate build-up does not support "
                        "one or more supplied pricing fields; human pricing review required."
                    )
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid spec field: {e}"}), 400
        except Exception as e:
            return jsonify({"error": f"costing failed: {e}"}), 500

        res["brief_spec"] = brief_spec
        if pricing_override:
            res["engineer_spec"] = dict(pricing_override)
        else:
            res.pop("engineer_spec", None)
        jobs[job_id]["result"] = res
        jobs[job_id]["brief_spec"] = brief_spec
        if costing:
            jobs[job_id]["costing"] = costing
        jobs[job_id]["spec_override"] = confirmed_values(brief_spec)
        if pricing_warning:
            jobs[job_id]["spec_pricing_warning"] = pricing_warning
        else:
            jobs[job_id].pop("spec_pricing_warning", None)
        save_jobs(jobs)

    return jsonify({
        "status": "ok", "job_id": job_id, "costing": costing,
        "brief_spec": brief_spec, "spec_schema": schema_definition(),
        "repriced": repriced, "pricing_warning": pricing_warning,
    })


# ── Soft delete (archive) ─────────────────────────────────────────────────────
#
# Aryan asked for a portal "delete estimation" button. A hard delete would destroy client
# decision history and training-log context, and an unauthenticated/mis-clicked hard delete
# of an approved six-figure job is unrecoverable — approval_jobs.json is the only system of
# record. So: SOFT delete only. /archive sets status='deleted' + an archived_at timestamp and
# moves a COPY of the record into approval_jobs_archive.json (never destroys it); the job is
# then removed from the hot jobs file (kept small) and hidden from the default /jobs list.
# /unarchive restores it. There is deliberately no hard-delete endpoint — see CLAUDE.md
# ("never commit client data") and the prod-audit finding this implements.

def _load_archive() -> dict:
    if JOBS_ARCHIVE_FILE.exists():
        try:
            return json.loads(JOBS_ARCHIVE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_archive(archive: dict):
    tmp = JOBS_ARCHIVE_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(archive, indent=2))
    os.replace(tmp, JOBS_ARCHIVE_FILE)


@app.route("/archive/<job_id>", methods=["POST"])
def archive_job(job_id):
    """Soft-delete: move the job to approval_jobs_archive.json, mark status='deleted' there,
    and drop it from the hot jobs file. Blocked for already-approved jobs — those represent a
    committed client quotation and need Jas (a human, out-of-band) to unwind, not a button."""
    data = request.get_json(silent=True) or {}
    note = data.get("note", "")

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job.get("status") == "processing":
            return jsonify({"error": "job is still processing — wait for it to finish"}), 409
        if job.get("decision") == "approved":
            return jsonify({"error": "job is already approved — this represents a committed "
                                      "client quotation; ask Jas to archive/unwind it manually, "
                                      "it cannot be deleted from the portal"}), 409

        job = dict(job)
        job["archived"]    = True
        job["archived_at"] = now_iso()
        job["archive_note"] = note
        job["status"]      = "deleted"

        archive = _load_archive()
        archive[job_id] = job
        _save_archive(archive)

        del jobs[job_id]
        save_jobs(jobs)

    log_training({"event": "archive", "job_id": job_id, "note": note, "timestamp": now_iso()})
    return jsonify({"status": "archived", "job_id": job_id})


@app.route("/unarchive/<job_id>", methods=["POST"])
def unarchive_job(job_id):
    """Reverse an /archive — mistakes must be recoverable. Restores the job into the hot
    jobs file and removes it from the archive."""
    with _jobs_lock:
        archive = _load_archive()
        job = archive.get(job_id)
        if not job:
            return jsonify({"error": f"archived job {job_id!r} not found"}), 404

        job = dict(job)
        job["archived"] = False
        job.pop("archived_at", None)
        job.pop("archive_note", None)
        # Restore a sane status — 'pending' unless the job carries its own decision already.
        job["status"] = job.get("decision") or "pending"

        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)

        del archive[job_id]
        _save_archive(archive)

    log_training({"event": "unarchive", "job_id": job_id, "timestamp": now_iso()})
    return jsonify({"status": "unarchived", "job_id": job_id})


@app.route("/jobs/archived")
def list_archived_jobs():
    """Archived jobs, kept out of the default /jobs listing (which the portal polls every
    15s) so the hot list stays focused on live work."""
    return jsonify(_load_archive())


# ── Costing / quotation helpers ───────────────────────────────────────────────

class QuotationPricingBlocked(RuntimeError):
    """A saved specification or zone-review state makes quotation output unsafe."""


def _quotation_result_for_job(job: dict, result_override=None, costing_override=None) -> dict:
    """Effective approved result, preferring assessor-adjusted measurements when present."""
    result = dict(result_override if result_override is not None else (job.get("result") or {}))
    costing = costing_override if costing_override is not None else job.get("costing")
    if costing:
        result["costing"] = dict(costing)
    if job.get("brief_spec"):
        result["brief_spec"] = dict(job["brief_spec"])
    if isinstance(job.get("brief_specs"), dict):
        result["brief_specs"] = dict(job["brief_specs"])
    if isinstance(job.get("zones"), list):
        result["zones"] = list(job["zones"])
    if "zone_classification_required" in job:
        result["zone_classification_required"] = bool(job["zone_classification_required"])
    if job.get("zone_reference_mismatch"):
        result["zone_reference_mismatch"] = True
    if job.get("zone_allocation_stale"):
        result["zone_allocation_stale"] = True
    adjusted = job.get("adjusted") or {}
    if adjusted.get("area_m2"):
        result["area_m2"] = adjusted["area_m2"]
        if result.get("costing"):
            result["costing"] = dict(result["costing"])
            result["costing"]["area_m2"] = adjusted["area_m2"]
            # Preserve the route's existing adjusted-area total behaviour unchanged.
            result["costing"]["total_gbp"] = round(
                adjusted["area_m2"] * (result["costing"].get("rate") or 0), 2)
    if adjusted and "perimeter_lm" in adjusted:
        if adjusted.get("perimeter_lm") is not None:
            result["perimeter_lm"] = adjusted["perimeter_lm"]
        elif adjusted.get("area_m2"):
            # A direct area-only adjustment has no matching final geometry. Do not present
            # the superseded AI outline's perimeter as if it described the adjusted area.
            result.pop("perimeter_lm", None)
            result.pop("polygon_pts", None)
    return result


def _quotation_for_job(job_id: str, result_override=None, costing_override=None):
    """Build one project quotation from every approved/adjusted sibling job."""
    from quotation import generate_quotation

    hot_jobs = load_jobs()
    all_jobs = dict(_load_archive())
    all_jobs.update(hot_jobs)
    anchor = all_jobs.get(job_id, {})
    project_ref = anchor.get("project_ref")
    unmeasured = []
    if project_ref:
        # ONE quotation per case (Aryan, 17 Jul: "it needs to be one that contains information
        # of all the documents in one case"). Every sibling document participates:
        #   - approved/adjusted -> firm quantities (as before)
        #   - pending BUT measured -> included, marked PROVISIONAL (pending assessor approval)
        #   - unmeasured/refused (e.g. line/hatch office GA plans awaiting a manual trace)
        #     -> listed explicitly in the quotation as awaiting assessor measurement, so a
        #        document can never silently vanish from the case output.
        siblings, seen = [], set()
        for sibling_id, sibling in all_jobs.items():
            if sibling.get("project_ref") != project_ref or sibling_id in seen:
                continue
            seen.add(sibling_id)
            res = sibling.get("result") or {}
            area = (sibling.get("adjusted") or {}).get("area_m2") or res.get("area_m2")
            if sibling.get("decision") in ("approved", "adjusted") or area:
                siblings.append((sibling_id, sibling))
            else:
                fname = res.get("file") or Path(str(sibling.get("pdf") or "")).name or sibling_id
                state = res.get("measurement_state") or sibling.get("status") or "UNMEASURED"
                unmeasured.append({"file": fname, "state": state})
        siblings.sort(key=lambda pair: (pair[1].get("created_at") or "", pair[0]))
        # A case where NOTHING is measured yet (e.g. only office GA plans awaiting trace)
        # must still produce the case workbook — every document listed as awaiting
        # measurement, totals empty. Only fall back to the anchor when the case is
        # genuinely empty (no unmeasured docs either).
        if not siblings and not unmeasured:
            siblings = [(job_id, anchor)]
    else:
        siblings = [(job_id, anchor)]

    zone_blocks = []
    for sibling_id, sibling in siblings:
        # Zone gates only apply to documents CONTRIBUTING quantities — an unmeasured
        # line/hatch sheet awaiting a trace must not block the whole case quotation.
        res = sibling.get("result") or {}
        if not ((sibling.get("adjusted") or {}).get("area_m2") or res.get("area_m2")):
            continue
        reason = _zone_block_reason(sibling)
        if reason:
            zone_blocks.append((sibling_id, reason))
    # A FIRM quotation (anchor approved/adjusted) keeps the hard zone gate. A DRAFT case
    # (nothing approved yet — Aryan's fresh-upload flow) degrades the gate to a loud caveat
    # inside the workbook instead of a 409, so the team can see the whole case early.
    draft = anchor.get("decision") not in ("approved", "adjusted")
    caveats = []
    if zone_blocks:
        if not draft:
            sibling_id, reason = zone_blocks[0]
            raise QuotationPricingBlocked(
                f"quotation blocked: project drawing {sibling_id} {reason}"
            )
        for sibling_id, reason in zone_blocks:
            fname = ((all_jobs.get(sibling_id) or {}).get("result") or {}).get("file") or sibling_id
            caveats.append(f"CLASSIFY BEFORE APPROVAL — {fname}: {reason}. Quantities from "
                           "this drawing are provisional until the assessor classifies its zones.")

    if any(sibling.get("spec_pricing_warning") for _, sibling in siblings):
        raise QuotationPricingBlocked(
            "quotation blocked: a project drawing has a saved specification that requires "
            "human pricing review"
        )

    results = []
    for sibling_id, sibling in siblings:
        unit = _quotation_result_for_job(
            sibling,
            result_override if sibling_id == job_id else None,
            costing_override if sibling_id == job_id else None,
        )
        if sibling.get("decision") not in ("approved", "adjusted"):
            unit = dict(unit)
            unit["pending_approval"] = True
        results.append(unit)
    project = anchor.get("project_name") or (results[0].get("file", "") if results else "")
    client = anchor.get("client_name") or ""
    return generate_quotation(
        results, project=project, client=client, ref=project_ref or None,
        commercial=anchor.get("commercial") or None,
        unmeasured=unmeasured or None, caveats=caveats or None,
    )

def _save_quotation(job_id: str, result: dict, costing: dict | None) -> dict:
    """Generate and save quotation files for this job. Returns paths dict."""
    try:
        from quotation import save_quotation
        q = _quotation_for_job(job_id, result_override=result, costing_override=costing)
        out_dir = Path(__file__).parent / "quotations"
        return save_quotation(q, out_dir=str(out_dir))
    except Exception as e:
        return {"error": str(e)}


def _run_costing(area_m2, result: dict) -> dict | None:
    """Run costing with defaults (or any spec stored in result)."""
    if not area_m2 or area_m2 <= 0:
        return None
    try:
        from defaults import spec_with_defaults, assumption_note, flag_assumed
        from costing  import rate_buildup

        engineer_spec = result.get("engineer_spec")  # None if architect-only
        spec, _ = spec_with_defaults(engineer_spec)
        manhole_in_scope = bool(result.get("manhole_count") or
                                 result.get("manhole_count_estimate"))
        spec, manhole_rate, rates_provenance = _apply_current_client_rates(
            spec, manhole_in_scope=manhole_in_scope)
        from slab_spec import COMMON_FIELDS
        supplied = engineer_spec or {}
        assumed = not all(supplied.get(key) is not None for key in COMMON_FIELDS)
        rate, parts   = rate_buildup(**{k: spec[k] for k in [
            "depth_mm","conc_rate","conc_wastage","mesh","layers",
            "steel_rate_t","steel_wastage","lap_acc","dpm","curing",
            "labour","trim","margin"]})
        total = round(area_m2 * rate, 2)
        costing = {
            "area_m2":   area_m2,
            "rate":      rate,
            "total_gbp": total,
            "spec":      spec,
            "assumed":   assumed,
            "note":      assumption_note(spec) if assumed else "",
            "flags":     flag_assumed(spec, assumed),
            "breakdown": parts,
        }
        if manhole_in_scope:
            from takeoff_pipeline import manhole_eo_line
            line, is_estimate = manhole_eo_line(
                result.get("manhole_count"), result.get("manhole_count_estimate"),
                rate=manhole_rate)
            if line:
                description, qty, unit, extra_rate = line
                extra_value = round(qty * extra_rate, 2)
                costing["extras"] = [{
                    "description": description, "qty": qty, "unit": unit,
                    "rate": extra_rate, "value": extra_value, "estimate": is_estimate,
                }]
                costing["grand_total_gbp"] = round(total + extra_value, 2)
        costing.update(rates_provenance)
        return costing
    except Exception as e:
        return {"error": str(e)}


# ── HTML email-click confirm page (GET, no mutation) ─────────────────────────

def _html_confirm_page(action: str, job_id: str) -> str:
    """Rendered for GET /approve|reject/<job_id> — the link an assessor clicks straight out
    of the email. Performs NO mutation itself: it's a plain HTML page whose <form> issues the
    real POST when (and only when) the human clicks the button. This is what keeps a mutating
    action from firing on mere top-level navigation (an email client link-preview/scanner
    prefetching the URL, or an attacker page that just links here, would otherwise have
    silently approved/rejected a job under the SameSite=Lax cookie — see approve()'s docstring).
    """
    labels  = {"approve": ("✅ Approve", "#27ae60"), "reject": ("✗ Reject", "#c0392b")}
    label, col = labels.get(action, (action.title(), "#13294b"))
    # Preserve ?token=... (or a cookie already covers it) so the POST from this page's form
    # is itself authorised when the token gate is on.
    token = request.args.get("token", "")
    action_url = f"/{action}/{job_id}" + (f"?token={token}" if token else "")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — Confirm {action.title()}</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:460px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <h2 style="color:#13294b;margin:0 0 8px 0">Confirm action</h2>
      <p style="color:#666;font-size:14px">
        Job <b>{job_id}</b> — clicking below will <b>{action}</b> this job.
      </p>
      <form method="POST" action="{action_url}">
        <button type="submit" style="
           display:inline-block;padding:12px 28px;margin:14px 4px 4px 4px;
           background:{col};color:#fff;border:none;border-radius:8px;
           font-size:15px;font-weight:700;cursor:pointer;">{label}</button>
      </form>
      <a href="/portal?job={job_id}"
         style="display:inline-block;margin-top:10px;color:#888;font-size:12px;
                text-decoration:none;">Open in portal instead →</a>
    </div></body></html>"""


def _html_confirmation(action: str, job_id: str, costing) -> str:
    colours = {"approved": "#27ae60", "rejected": "#c0392b", "adjusted": "#2980b9"}
    icons   = {"approved": "✅", "rejected": "✗", "adjusted": "✏️"}
    col = colours.get(action, "#13294b")
    icon = icons.get(action, "")

    cost_block = ""
    if costing and "total_gbp" in costing:
        cost_block = f"""
        <div style="margin:20px 0;padding:16px;background:#f7f8fa;border-radius:8px;text-align:center">
          <div style="font-size:13px;color:#666">Estimated value</div>
          <div style="font-size:32px;font-weight:800;color:#13294b">
            £{costing['total_gbp']:,.2f}
          </div>
          <div style="font-size:13px;color:#888">
            {costing.get('area_m2',0):,.0f} m² @ £{costing.get('rate',0):.2f}/m²
          </div>
          {f'<div style="font-size:12px;color:#e67e22;margin-top:6px">{costing.get("note","")}</div>'
           if costing.get("note") else ""}
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — {action.title()}</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:460px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <div style="font-size:48px">{icon}</div>
      <h2 style="color:{col};margin:12px 0 4px 0">{action.title()}</h2>
      <p style="color:#666;font-size:14px">Job <b>{job_id}</b> has been {action}.</p>
      {cost_block}
      <a href="/portal?job={job_id}"
         style="display:inline-block;padding:10px 24px;background:#13294b;color:#fff;
                text-decoration:none;border-radius:6px;font-size:14px;margin-top:8px">
        Open in Portal →
      </a>
    </div></body></html>"""


# ── Quotation endpoints ───────────────────────────────────────────────────────

@app.route("/quotation/<job_id>.<fmt>")
def quotation_download(job_id, fmt):
    """Serve the ONE case quotation (all sibling documents) anchored at this job.

    Previously gated on this job being approved/adjusted, which made a fresh case
    undownloadable and (combined with approved-only sibling aggregation) produced a
    separate workbook per document — Aryan's 17 Jul report. Now: a DRAFT case quotation
    is available as soon as ANY document in the case has a measured area; every
    not-yet-approved quantity is marked provisional inside the quotation itself, and
    unmeasured documents are listed as awaiting assessor trace. Nothing firm is implied
    before approval — the provisional markings carry that state."""
    j, err, code = require_job(job_id)
    if err: return err, code
    # No "nothing measured yet" gate: an office-only case (all documents awaiting assessor
    # trace) still gets its case workbook — every document listed, totals empty. A refused
    # download here is exactly what read as "skipping the office drawings" in the field.

    try:
        from quotation import quotation_text, quotation_html, quotation_json, quotation_xlsx
        q = _quotation_for_job(job_id)
        if fmt == "txt":
            return Response(quotation_text(q),  mimetype="text/plain; charset=utf-8")
        elif fmt == "html":
            return Response(quotation_html(q),  mimetype="text/html; charset=utf-8")
        elif fmt == "json":
            return Response(quotation_json(q),  mimetype="application/json")
        elif fmt == "xlsx":
            filename = f"{_sanitise_filename(str(j.get('project_ref') or job_id))}.xlsx"
            return send_file(
                io.BytesIO(quotation_xlsx(q)),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=filename,
            )
        else:
            return jsonify({"error": f"unknown format {fmt!r}"}), 400
    except QuotationPricingBlocked as e:
        return jsonify({"error": str(e)}), 409
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── n8n webhook endpoint ──────────────────────────────────────────────────────

@app.route("/webhook/n8n", methods=["POST"])
def n8n_webhook():
    """
    Inbound webhook from n8n — receives a completed takeoff result and creates
    an approval job, then returns the email HTML payload for n8n to send.

    Expected body:
      {"pdf_path": "...", "result": {...}, "polygon_pts": [[x,y],...], "to": "..."}

    n8n flow:
      HTTP Request node (POST /webhook/n8n) → Email node (body = response.html)
    """
    data = request.get_json(silent=True) or {}
    pdf_path    = data.get("pdf_path", "")
    result      = data.get("result", {})
    polygon_pts = data.get("polygon_pts")
    to          = data.get("to", os.getenv("APPROVAL_TO", "inderjit@fortel.co.uk"))

    if not result:
        return jsonify({"error": "result required"}), 400

    # Containment guard: pdf_path comes straight from the request body. Without this, any
    # caller could point it at an arbitrary file readable by the server user and have it
    # rendered/exfiltrated back as a base64 PNG. Only allow paths that resolve inside this
    # server's own drawings/ directory (same pattern already used at /upload).
    if pdf_path:
        drawings_dir = (Path(__file__).parent / "drawings").resolve()
        try:
            resolved = Path(pdf_path).resolve()
            resolved.relative_to(drawings_dir)
        except ValueError:
            return jsonify({"error": "pdf_path must resolve inside the server's drawings/ "
                                      "directory"}), 400

    try:
        from approval_email import create_job, render_snapshot, png_to_b64, build_html_email
        if polygon_pts is not None and not result.get("polygon_pts"):
            result = dict(result)
            result["polygon_pts"] = polygon_pts
        job_id = create_job(pdf_path, result)
        # Snapshot (best-effort — PDF may not be on this server's disk)
        b64 = ""
        if pdf_path and Path(pdf_path).exists():
            png = render_snapshot(pdf_path, polygon_pts=polygon_pts)
            b64 = png_to_b64(png)
        html = build_html_email(job_id, result, b64)
        return jsonify({
            "job_id":   job_id,
            "to":       to,
            "subject":  f"Fortel AI Takeoff — Review: {result.get('file', job_id)}",
            "html":     html,
            "portal":   f"{os.getenv('APPROVAL_BASE_URL','http://localhost:5001')}/review/{job_id}",
            "area_m2":  result.get("area_m2"),
            "total_gbp": result.get("costing", {}).get("total_gbp"),
            "flags":    result.get("flags", []),
        })
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Health-check ─────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    """Health-check for deploy tests."""
    jobs = load_jobs()
    return jsonify({"status": "ok", "job_count": len(jobs), "build": BUILD_INFO})


# ── Upload endpoint ───────────────────────────────────────────────────────────

TAKEOFF_TIMEOUT_S = int(os.getenv("TAKEOFF_TIMEOUT_S", "120"))


def _mark_job_unmeasured(job_id: str, flag: str, extra: dict = None, watchdog_fired: bool = False):
    """Flip a job to UNMEASURED with a flag — used by both the error handler and the
    watchdog so a job NEVER gets stranded on 'processing' forever.

    status stays "error" (the legacy field the portal already renders specially: it still
    fetches the snapshot and lets the assessor trace manually — see assessor_portal.html's
    job.status === 'error' branch) while measurement_state carries the new four-state value
    so the state machine is explicit and machine-checkable.

    watchdog_fired=True marks the job with a "_watchdog_fired" sentinel so that IF the real
    takeoff thread later completes successfully, _run_takeoff can detect the conflict, strip
    the now-stale "PIPELINE TIMEOUT" flag instead of baking it permanently into a job that
    actually succeeded, and log the race instead of silently overwriting.
    """
    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return
        flags = list(job.get("flags") or [])
        flags.append(flag)
        job.update({
            "status":            "error",
            "measurement_state": "UNMEASURED",
            "needs_assessor":    True,
            "area_m2":           job.get("area_m2"),
            "flags":             flags,
        })
        if watchdog_fired:
            job["_watchdog_fired"] = True
        if extra:
            job.update(extra)
        # keep 'result' consistent with the top-level fields so the portal (which reads
        # job.result.flags in some views) sees the same picture
        res = dict(job.get("result") or {})
        res["flags"] = flags
        res.setdefault("measurement_state", "UNMEASURED")
        res.setdefault("area_m2", job.get("area_m2"))
        job["result"] = res
        jobs[job_id] = job
        save_jobs(jobs)


def _run_takeoff(job_id: str, pdf_path: str, project_name: str, project_ref: str):
    """
    Background thread: run takeoff pipeline and update job record when done.

    Hardened per the "never break, never strand" invariant:
      - ANY exception during takeoff -> job becomes UNMEASURED with a
        "PIPELINE ERROR: ... ; route to assessor" flag, never a bare crash/"error" dead-end.
      - A watchdog timer flips the job to UNMEASURED with a timeout flag if the pipeline
        hasn't finished within TAKEOFF_TIMEOUT_S; the worker thread may keep running (daemon
        thread, no forced kill) but the job record is never left stuck on "processing".
      - watchdog-vs-completion race: threading.Timer.cancel() is a no-op once the timer has
        already fired, so if takeoff() finishes just after the 120s mark the watchdog may have
        already flipped the job to UNMEASURED before this thread gets the lock back. That's
        fine — the completed result below always overwrites it (a late success should win over
        a timeout placeholder) — but the watchdog's "PIPELINE TIMEOUT" flag must not survive
        into the completed job's flags (it would be a permanently-confusing lie on a job that
        in fact succeeded). Detect the "_watchdog_fired" sentinel, strip that one flag, and log
        the race so it's visible in the server log rather than silently swallowed.
    """
    watchdog = threading.Timer(
        TAKEOFF_TIMEOUT_S, _mark_job_unmeasured, args=(
            job_id,
            f"PIPELINE TIMEOUT: takeoff did not finish within {TAKEOFF_TIMEOUT_S}s; "
            "route to assessor — the worker thread may still be running in the background.",
        ),
        kwargs={"watchdog_fired": True},
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        import takeoff_pipeline
        takeoff_kwargs = {"project_name": project_name, "project_ref": project_ref}
        # A few integrations/tests provide a narrow takeoff-compatible callable. Preserve
        # that interface while the real pipeline receives the explicit isolated rates path.
        import inspect
        if "client_rates_path" in inspect.signature(takeoff_pipeline.takeoff).parameters:
            takeoff_kwargs["client_rates_path"] = CLIENT_RATES_FILE
        result = takeoff_pipeline.takeoff(pdf_path, **takeoff_kwargs)
        watchdog.cancel()
        with _jobs_lock:
            jobs = load_jobs()
            job = jobs.get(job_id)
            if job is None:
                return  # job vanished (shouldn't happen) — nothing to update
            # Preserve any pre-takeoff flags already on the job (e.g. zip/eml disambiguation
            # notes recorded at upload time) rather than letting the pipeline result overwrite them.
            pre_flags = list(job.get("flags") or [])
            if job.get("_watchdog_fired"):
                # The watchdog already fired and flipped this job to UNMEASURED before we got
                # here. We're overwriting that with a real completed result (the right call —
                # late success beats a timeout placeholder) but strip the now-stale
                # "PIPELINE TIMEOUT" flag it appended so it doesn't linger on a job that in
                # fact succeeded, and log the race so it shows up in the server log.
                pre_flags = [f for f in pre_flags if not f.startswith("PIPELINE TIMEOUT")]
                print(f"[watchdog-race] job {job_id}: pipeline finished AFTER the "
                      f"{TAKEOFF_TIMEOUT_S}s watchdog already marked it UNMEASURED; "
                      f"overwriting with the completed result (state={result.get('measurement_state')}).")
            job.pop("_watchdog_fired", None)
            job.update({
                "project_name":     result.get("project_name", project_name),
                "project_ref":      result.get("project_ref",  project_ref),
                "type":             result.get("type"),
                "method":           result.get("method"),
                "confidence":       result.get("confidence"),
                "source_discipline": result.get("source_discipline"),
                "area_m2":          result.get("area_m2"),
                "measurement_state": result.get("measurement_state"),
                "needs_assessor":   result.get("needs_assessor", True),
                "scale_verified":   result.get("scale_verified"),
                "scale_confirmed":  False,
                "scale_src":        result.get("scale_src"),
                "scale_sources":    result.get("scale_sources"),
                "costing":          result.get("costing"),
                "flags":            pre_flags + result.get("flags", []),
                "polygon_pts":      result.get("polygon_pts"),
                "candidate_polygons": result.get("candidate_polygons", []),
                "perimeter_lm":     result.get("perimeter_lm"),
                # Mirror zone-aware marked-PDF evidence at job level for the portal while
                # retaining the canonical nested pipeline result for backward compatibility.
                "zones":            result.get("zones", []),
                "markup_annotations": result.get("markup_annotations", []),
                "brief_specs":      result.get("brief_specs", {}),
                "zone_classification_required": bool(
                    result.get("zone_classification_required", False)),
                "zone_reference_mismatch": bool(
                    result.get("zone_reference_mismatch", False)),
                "zone_allocation_stale": bool(result.get("zone_allocation_stale", False)),
                "result":           result,
                "status":           "pending",
            })
            jobs[job_id] = job
            save_jobs(jobs)
    except Exception as e:
        watchdog.cancel()
        _mark_job_unmeasured(
            job_id,
            f"PIPELINE ERROR: {e}; route to assessor",
            extra={"error": traceback.format_exc()},
        )


def _sanitise_filename(name: str) -> str:
    """Strip dangerous characters; keep alphanumeric, dash, underscore, dot."""
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w.\-]", "", name)   # \w = [a-zA-Z0-9_]
    return name[:80]


MAX_EXTRACT_BYTES  = 200 * 1024 * 1024   # safety cap on total bytes extracted from a zip
MAX_EXTRACT_FILES  = 200                 # safety cap on member count
CAD_EXTENSIONS     = (".dwg", ".dxf")


def _rejected_job_record(project_name, project_ref, client_name, filename, reason) -> tuple[str, dict]:
    job_id = str(uuid.uuid4())
    job = {
        "id":               job_id,
        "pdf_path":         None,
        "project_name":     project_name,
        "project_ref":      project_ref,
        "client_name":      client_name,
        "type":             None,
        "method":           None,
        "confidence":       None,
        "source_discipline": None,
        "area_m2":          None,
        "measurement_state": "REJECTED",
        "needs_assessor":   False,
        "scale_verified":   None,
        "scale_src":        None,
        "scale_sources":    None,
        "costing":          None,
        "flags":            [f"REJECTED: {reason}"],
        "polygon_pts":      None,
        "status":           "rejected",
        "decision":         "rejected",
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "decided_at":       now_iso(),
        "note":             reason,
        "adjusted":         None,
        "result":           {"file": filename, "measurement_state": "REJECTED",
                             "flags": [f"REJECTED: {reason}"]},
    }
    return job_id, job


def _create_rejected_job(project_name, project_ref, client_name, filename, reason) -> str:
    """Create a REJECTED job record — visible in the portal job list with a human-readable
    reason (never a bare HTTP 400 that vanishes)."""
    job_id, job = _rejected_job_record(
        project_name, project_ref, client_name, filename, reason)
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)
    return job_id


def _processing_job_record(project_name, project_ref, client_name, pdf_path, flags) -> tuple[str, dict]:
    job_id = str(uuid.uuid4())
    return job_id, {
        "id":               job_id,
        "pdf_path":         str(pdf_path),
        "project_name":     project_name,
        "project_ref":      project_ref,
        "client_name":      client_name,
        "type":             None,
        "method":           None,
        "confidence":       None,
        "source_discipline": None,
        "area_m2":          None,
        "measurement_state": None,
        "needs_assessor":   None,
        "scale_verified":   None,
        "scale_confirmed":  False,
        "scale_src":        None,
        "scale_sources":    None,
        "costing":          None,
        "flags":            list(flags),
        "polygon_pts":      None,
        "status":           "processing",
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "decision":         None,
        "adjusted":         None,
        "result":           {"file": Path(pdf_path).name},
    }


def _unique_prefixed_path(dest_dir: Path, prefix: str, filename: str) -> Path:
    safe_name = _sanitise_filename(filename) or f"upload_{uuid.uuid4().hex[:8]}"
    safe_prefix = _sanitise_filename(prefix) or "project"
    target = dest_dir / f"{safe_prefix}_{safe_name}"
    if target.exists():
        target = dest_dir / f"{safe_prefix}_{Path(safe_name).stem}_{uuid.uuid4().hex[:8]}{Path(safe_name).suffix}"
    return target


def _safe_extract_zip(zip_path: Path, dest_dir: Path, prefix: str = "") -> tuple[list, list]:
    """Extract PDFs from a zip archive, guarding against zip-slip and oversize archives.
    Returns (list of extracted PDF Paths, flags).

    `prefix` (typically the sanitised project_ref) is prepended to every extracted filename so
    two uploads whose archives happen to contain a same-named member (e.g. both ship a
    "Yard_Area_Proposed_Site_Plan.pdf") never collide/overwrite each other on disk — mirrors the
    `{project_ref}_{filename}` convention already used for direct .pdf uploads below."""
    flags = []
    pdfs = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_EXTRACT_FILES:
                flags.append(f"zip has {len(infos)} entries — only first {MAX_EXTRACT_FILES} considered")
                infos = infos[:MAX_EXTRACT_FILES]
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if not info.filename.lower().endswith(".pdf"):
                    continue
                total += info.file_size
                if total > MAX_EXTRACT_BYTES:
                    flags.append("zip extraction stopped — size cap exceeded")
                    break
                # zip-slip guard: resolved member path must stay inside dest_dir
                member_name = _sanitise_filename(Path(info.filename).name)
                if not member_name:
                    continue
                target = _unique_prefixed_path(dest_dir, prefix, member_name).resolve()
                if not str(target).startswith(str(dest_dir.resolve())):
                    flags.append(f"skipped unsafe zip entry: {info.filename!r}")
                    continue
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                pdfs.append(target)
    except zipfile.BadZipFile as e:
        flags.append(f"corrupt zip archive: {e}")
    return pdfs, flags


def _extract_eml_pdfs(eml_path: Path, dest_dir: Path, prefix: str = "") -> tuple[list, list]:
    """Parse a .eml with the stdlib email lib, save any PDF attachments. Returns (paths, flags).

    `prefix` is prepended to every extracted attachment filename for the same collision-avoidance
    reason as _safe_extract_zip above (two enquiry emails can easily carry an attachment named
    "Proposed_Site_Plan.pdf")."""
    flags = []
    pdfs = []
    pfx = f"{prefix}_" if prefix else ""
    try:
        with open(eml_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        for part in msg.iter_attachments():
            fname = part.get_filename() or ""
            if fname.lower().endswith(".pdf"):
                safe = _sanitise_filename(fname) or f"attachment_{uuid.uuid4().hex[:8]}.pdf"
                target = dest_dir / f"{pfx}{safe}"
                payload = part.get_payload(decode=True)
                if payload:
                    target.write_bytes(payload)
                    pdfs.append(target)
        if not pdfs:
            flags.append("no PDF attachments found in .eml")
    except Exception as e:
        flags.append(f"failed to parse .eml: {e}")
    return pdfs, flags


def _rank_pdfs_by_priority(pdf_paths: list) -> list:
    """Rank candidate PDFs by router.drawing_priority on filename (best first)."""
    from router import drawing_priority
    return sorted(pdf_paths, key=lambda p: drawing_priority(Path(p).name), reverse=True)


def _open_pdf_safely(path: Path):
    """Try to open a PDF and confirm it's readable. Returns (fitz.Document|None, reason|None)."""
    try:
        import fitz
        if path.stat().st_size == 0:
            return None, "zero-byte file"
        doc = fitz.open(str(path))
        if doc.needs_pass:
            return None, "encrypted/password-protected PDF"
        if doc.page_count < 1:
            return None, "PDF has no pages"
        _ = doc[0].get_text()   # force a real read — catches some corrupt-stream cases
        return doc, None
    except Exception as e:
        return None, f"corrupt or unreadable PDF ({e})"


@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept a new drawing/enquiry from the assessor portal without CLI intervention.

    multipart/form-data fields:
      pdf          – one or more uploaded files (required; repeat the multipart field). Accepts:
                       .pdf            -> takeoff runs directly
                       .zip            -> every contained PDF gets its own project job, ordered
                                          by router.drawing_priority for deterministic display
                       .eml            -> PDF attachments extracted, same ranking
                       .png/.jpg/.jpeg -> wrapped into a single-page PDF, routed as raster/UNMEASURED
                       .dwg/.dxf/other -> REJECTED job, "CAD/unsupported format — please export PDF"
                     Encrypted/corrupt/zero-byte PDFs (at any stage above) -> REJECTED job with
                     the specific reason instead of a bare HTTP 400.
      project_name – human-readable project name (required)
      project_ref  – Fortel reference / sequential number (required)

    Returns 202 {"job_id": "...", "status": "processing"} for a takeoff-bound job, or
    201 {"job_id": "...", "status": "rejected"} for a REJECTED job — always 2xx with a job
    record the portal can show, never a bare 400 that vanishes. Multi-job responses also include
    job_ids and jobs while retaining the legacy job_id/status fields.
    """
    # ── Validate required form fields
    project_name = (request.form.get("project_name") or "").strip()
    project_ref  = (request.form.get("project_ref")  or "").strip()
    client_name  = (request.form.get("client_name")  or "").strip()
    # Optional enquiry identification from the n8n workflow (Aryan, 16 Jul: "the request now
    # include the subject and body information for better identification") — stored on every
    # job in the batch so a failure/review is attributable to the right enquiry email.
    email_subject = (request.form.get("email_subject") or request.form.get("subject") or "").strip()[:300]
    email_body    = (request.form.get("email_body") or request.form.get("body") or "").strip()[:2000]
    up_files     = [f for f in request.files.getlist("pdf") if f and f.filename]

    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    if not project_ref:
        return jsonify({"error": "project_ref is required"}), 400
    if not up_files:
        return jsonify({"error": "file is required"}), 400

    drawings_dir = Path(__file__).parent / "drawings"
    drawings_dir.mkdir(exist_ok=True)
    upload_items = []

    for up_file in up_files:
        original_filename = up_file.filename or "upload"
        ext = Path(original_filename).suffix.lower()
        stage_path = _unique_prefixed_path(drawings_dir, project_ref, original_filename)
        try:
            stage_path.resolve().relative_to(drawings_dir.resolve())
        except ValueError:
            return jsonify({"error": "invalid filename (path traversal detected)"}), 400
        up_file.save(str(stage_path))

        if ext == ".pdf":
            upload_items.append({"path": stage_path, "filename": original_filename, "flags": []})
        elif ext == ".zip":
            pdfs, flags = _safe_extract_zip(stage_path, drawings_dir, prefix=project_ref)
            stage_path.unlink(missing_ok=True)
            ranked = _rank_pdfs_by_priority(pdfs)
            if not ranked:
                upload_items.append({"filename": original_filename,
                                     "reason": "zip archive contained no extractable PDFs"})
            else:
                zip_flags = list(flags)
                zip_flags.append(f"zip contained {len(ranked)} PDFs; every PDF queued as a "
                                 "separate drawing under this project")
                for pdf_path in ranked:
                    upload_items.append({"path": pdf_path, "filename": pdf_path.name,
                                         "flags": zip_flags})
        elif ext == ".eml":
            pdfs, flags = _extract_eml_pdfs(stage_path, drawings_dir, prefix=project_ref)
            stage_path.unlink(missing_ok=True)
            ranked = _rank_pdfs_by_priority(pdfs)
            if not ranked:
                upload_items.append({"filename": original_filename,
                                     "reason": "no PDF attachments found in .eml"})
            else:
                pdf_path = ranked[0]
                eml_flags = list(flags)
                if len(ranked) > 1:
                    others = ", ".join(p.name for p in ranked[1:6])
                    eml_flags.append(f".eml contained {len(ranked)} PDF attachments; measured "
                                     f"'{pdf_path.name}' (highest drawing_priority); others: {others}")
                upload_items.append({"path": pdf_path, "filename": pdf_path.name,
                                     "flags": eml_flags})
        elif ext in (".png", ".jpg", ".jpeg"):
            try:
                import fitz
                img_doc = fitz.open(str(stage_path))
                pdf_doc = fitz.open()
                rect = img_doc[0].rect
                page = pdf_doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(rect, filename=str(stage_path))
                pdf_path = _unique_prefixed_path(
                    drawings_dir, project_ref, f"{Path(original_filename).stem}.pdf")
                pdf_doc.save(str(pdf_path))
                pdf_doc.close()
                img_doc.close()
                stage_path.unlink(missing_ok=True)
                upload_items.append({
                    "path": pdf_path,
                    "filename": original_filename,
                    "flags": [f"image ({ext}) wrapped into a single-page PDF for takeoff — "
                              "raster source, routes to UNMEASURED/mandatory assessor trace"],
                })
            except Exception as e:
                stage_path.unlink(missing_ok=True)
                upload_items.append({"filename": original_filename,
                                     "reason": f"could not wrap image into PDF: {e}"})
        elif ext in CAD_EXTENSIONS:
            stage_path.unlink(missing_ok=True)
            upload_items.append({"filename": original_filename,
                                 "reason": "CAD/unsupported format — please export PDF"})
        else:
            stage_path.unlink(missing_ok=True)
            upload_items.append({
                "filename": original_filename,
                "reason": f"unsupported file type '{ext or '(none)'}' — please upload PDF, "
                          "ZIP, EML, PNG or JPG",
            })

    records = []
    workers = []
    response_jobs = []
    for item in upload_items:
        pdf_path = item.get("path")
        reason = item.get("reason")
        if pdf_path and not reason:
            doc, reason = _open_pdf_safely(pdf_path)
            if doc is not None:
                doc.close()
        if reason:
            job_id, job = _rejected_job_record(
                project_name, project_ref, client_name, item["filename"], reason)
            status = "rejected"
        else:
            job_id, job = _processing_job_record(
                project_name, project_ref, client_name, pdf_path, item.get("flags", []))
            status = "processing"
            workers.append((job_id, str(pdf_path)))
        if email_subject:
            job["email_subject"] = email_subject
        if email_body:
            job["email_body"] = email_body
        records.append((job_id, job))
        response_jobs.append({"job_id": job_id, "status": status, "filename": item["filename"]})

    with _jobs_lock:
        jobs = load_jobs()
        for job_id, job in records:
            jobs[job_id] = job
        save_jobs(jobs)

    for job_id, pdf_path in workers:
        threading.Thread(
            target=_run_takeoff,
            args=(job_id, pdf_path, project_name, project_ref),
            daemon=True,
        ).start()

    primary = next((j for j in response_jobs if j["status"] == "processing"), response_jobs[0])
    payload = {"job_id": primary["job_id"], "status": primary["status"]}
    if len(response_jobs) > 1:
        payload.update({
            "project_ref": project_ref,
            "job_ids": [j["job_id"] for j in response_jobs],
            "jobs": response_jobs,
        })
    return jsonify(payload), (202 if workers else 201)


# ── Startup sweep ─────────────────────────────────────────────────────────────

def _sweep_stranded_processing_jobs():
    """Any job left on status='processing' at process start was orphaned by a restart/crash —
    the watchdog Timer and worker thread that would have resolved it die with the old process,
    and nothing else ever revisits it. No takeoff can legitimately still be "running" at boot
    (threads are daemon, they don't survive), so unconditionally flip every such job to
    UNMEASURED with a clear flag rather than leaving it spinning in the portal forever."""
    with _jobs_lock:
        jobs = load_jobs()
        changed = False
        for job_id, job in jobs.items():
            if job.get("status") == "processing":
                flags = list(job.get("flags") or [])
                flags.append("PIPELINE INTERRUPTED: server restarted while takeoff was "
                              "running; route to assessor")
                job.update({
                    "status":            "error",
                    "measurement_state": "UNMEASURED",
                    "needs_assessor":    True,
                    "flags":             flags,
                })
                res = dict(job.get("result") or {})
                res["flags"] = flags
                res.setdefault("measurement_state", "UNMEASURED")
                job["result"] = res
                changed = True
                print(f"[startup-sweep] job {job_id} was stranded on 'processing' at a prior "
                      "restart — marked UNMEASURED, routed to assessor.")
        if changed:
            save_jobs(jobs)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Railway injects PORT and requires binding to all interfaces. Bare-metal/local runs keep
    # the safer loopback default and still refuse a wide bind without portal authentication.
    is_railway = bool(os.getenv("RAILWAY_PROJECT_ID"))
    _raw_port = (os.getenv("PORT") or "").strip()
    if _raw_port:
        port = int(_raw_port)
        host = "0.0.0.0"
    else:
        port = int((os.getenv("APPROVAL_PORT") or "5001").strip())
        host = os.getenv("APPROVAL_HOST", "127.0.0.1")
        if not is_railway and host not in ("127.0.0.1", "localhost") and not APPROVAL_TOKEN:
            print(f"REFUSING to bind {host} without APPROVAL_TOKEN set — anyone on the network "
                  "could approve/reject/adjust jobs. Set APPROVAL_TOKEN, or leave APPROVAL_HOST "
                  "at the 127.0.0.1 default for local-only use. Falling back to 127.0.0.1.")
            host = "127.0.0.1"

    _sweep_stranded_processing_jobs()

    # Startup banner goes to stdout, which run.sh redirects straight into logs/portal.log
    # (and launchd's own copy). Printing the raw token there means it sits in a plaintext
    # log file indefinitely — mask everything but the first 4 chars so the log is still
    # useful for confirming *which* token is loaded (e.g. after a rotation) without being a
    # second place the live secret is stored. Get the real value from `.env`/env, not the log.
    def _mask_token(t: str) -> str:
        if not t:
            return ""
        return f"{t[:4]}…" if len(t) > 4 else "…"

    print("Fortel Approval Server — config:")
    print(f"  host:port     = {host}:{os.getenv('APPROVAL_PORT', 5001)}")
    print(f"  jobs file     = {JOBS_FILE}")
    print(f"  client rates  = {CLIENT_RATES_FILE}")
    print(f"  jobs archive  = {JOBS_ARCHIVE_FILE}")
    print(f"  backups dir   = {BACKUP_DIR}")
    print(f"  base url      = {os.getenv('APPROVAL_BASE_URL', 'http://localhost:5001')}")
    print(f"  token set     = {'yes (' + _mask_token(APPROVAL_TOKEN) + ')' if APPROVAL_TOKEN else 'no'}")
    print(f"  cors origin   = {_CORS_ORIGIN or '(none — same-origin only)'}")
    print(f"  portal        = http://{host}:{port}/portal"
          + (f"?token={_mask_token(APPROVAL_TOKEN)} (masked — see .env for the real value)"
             if APPROVAL_TOKEN else ""))

    app.run(host=host, port=port, debug=False)
