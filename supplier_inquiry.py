#!/usr/bin/env python3
"""
Fortel AI Takeoff — Concrete Supplier Inquiry Generator.

Once the pipeline has measured the area and extracted (or assumed) the slab spec,
this module generates the ready-to-send concrete supplier inquiry email.

Supplier inquiry fields (from Amarvir screen-share, standup 24 Jun 2026):
  - Concrete strength class (mix)
  - Cement type
  - Air-entrained  (always Yes for external slabs)
  - Aggregate size (always 20mm unless spec differs)
  - Water/cement ratio (always 0.45 unless spec differs)
  - Minimum cement content (if stated in spec)
  - Target slump class (S3 standard; S4 for WinVIC)
  - Quantity in m³  (area × depth / 1000)

Process (Amarvir verbatim):
  "We get two or three rates from suppliers, pick the cheapest, use that for costing."
  "Without the concrete rates we won't be able to get to any of this."

Usage:
  from supplier_inquiry import generate_inquiry, format_cubes

  result = takeoff("drawings/mysite.pdf", project_name="Agraco Twinwoods", project_ref="2132")
  email  = generate_inquiry(result)
  print(email["subject"])
  print(email["text"])
  # email["html"] → ready to feed into n8n / approval_server
"""
import datetime
import textwrap

# ── Slump class → target slump lookup (EN 206 / BS 8500) ────────────────────
SLUMP_TABLE = {
    "S1": "10–40 mm",
    "S2": "50–90 mm",
    "S3": "100–150 mm",   # standard for externals
    "S4": "160–210 mm",   # WinVIC spec
    "S5": "≥ 220 mm",
}

FORTEL_NAME  = "Fortel Group Limited"
FORTEL_EMAIL = "estimating@fortel.co.uk"


# ── Helpers ──────────────────────────────────────────────────────────────────

def format_cubes(area_m2: float, depth_mm: int, wastage: float = 0.03) -> float:
    """Return order quantity in m³ including concrete wastage."""
    net_m3 = area_m2 * depth_mm / 1000
    return round(net_m3 * (1 + wastage), 1)


def _spec_from_result(result: dict) -> dict:
    """
    Pull concrete spec fields from the pipeline result.
    Falls back to DEFAULT_SPEC values for any missing supplier fields.
    """
    from defaults import DEFAULT_SPEC
    costing = result.get("costing", {}) or {}
    raw     = costing.get("spec", {}) or {}

    return {
        "depth_mm":     raw.get("depth_mm",    DEFAULT_SPEC["depth_mm"]),
        "conc_mix":     raw.get("conc_mix",     DEFAULT_SPEC["conc_mix"]),
        "cement_type":  raw.get("cement_type",  DEFAULT_SPEC["cement_type"]),
        "air_entrained":raw.get("air_entrained",DEFAULT_SPEC["air_entrained"]),
        "aggregate_mm": raw.get("aggregate_mm", DEFAULT_SPEC["aggregate_mm"]),
        "wc_ratio":     raw.get("wc_ratio",     DEFAULT_SPEC["wc_ratio"]),
        "slump_class":  raw.get("slump_class",  DEFAULT_SPEC["slump_class"]),
        "conc_wastage": raw.get("conc_wastage", DEFAULT_SPEC["conc_wastage"]),
    }


# ── Plain-text email ─────────────────────────────────────────────────────────

def _text_body(project_name: str, project_ref: str, area_m2: float,
               spec: dict, cubes: float, date_str: str) -> str:
    proj_line = project_name or "—"
    if project_ref:
        proj_line = f"[#{project_ref}] {proj_line}"

    slump_range = SLUMP_TABLE.get(spec["slump_class"], spec["slump_class"])
    ae          = "Yes" if spec["air_entrained"] else "No"

    return textwrap.dedent(f"""\
    Dear Sir/Madam,

    Please could you provide us with a rate for the following concrete specification:

    Project:        {proj_line}
    Tender date:    {date_str}

    CONCRETE SPECIFICATION
    ──────────────────────
    Strength class:         {spec["conc_mix"]}
    Cement type:            {spec["cement_type"]}
    Air-entrained:          {ae}
    Nominal aggregate size: {spec["aggregate_mm"]} mm (crushed)
    Max water/cement ratio: {spec["wc_ratio"]}
    Target slump class:     {spec["slump_class"]}  ({slump_range})

    QUANTITY
    ────────
    Slab thickness:   {spec["depth_mm"]} mm
    Slab area:        {area_m2:,.0f} m²
    Order quantity:   {cubes:,.1f} m³  (incl. {int(spec['conc_wastage']*100)}% wastage)

    Please confirm:
      • Rate per m³ (delivered, pump-ready)
      • Any minimum order charges
      • Availability / lead time

    Many thanks,
    {FORTEL_NAME}
    {FORTEL_EMAIL}
    """)


# ── HTML email ───────────────────────────────────────────────────────────────

def _html_body(project_name: str, project_ref: str, area_m2: float,
               spec: dict, cubes: float, date_str: str) -> str:
    proj_line = project_name or "—"
    if project_ref:
        proj_line = f"<b>#{project_ref}</b> &nbsp;{proj_line}"

    slump_range = SLUMP_TABLE.get(spec["slump_class"], spec["slump_class"])
    ae          = "Yes" if spec["air_entrained"] else "No"

    def _row(label, value, stripe=False):
        bg = "#f7f8fa" if stripe else "#ffffff"
        return (f"<tr style='background:{bg}'>"
                f"<td style='padding:7px 14px;color:#555;font-size:13px;width:42%'>{label}</td>"
                f"<td style='padding:7px 14px;color:#111;font-weight:600;font-size:13px'>{value}</td>"
                f"</tr>")

    spec_rows = (
        _row("Strength class",         spec["conc_mix"],                      False) +
        _row("Cement type",            spec["cement_type"],                   True)  +
        _row("Air-entrained",          ae,                                    False) +
        _row("Nominal aggregate size", f"{spec['aggregate_mm']} mm (crushed)",True)  +
        _row("Max W/C ratio",          str(spec["wc_ratio"]),                 False) +
        _row("Target slump class",     f"{spec['slump_class']}  ({slump_range})", True)
    )

    qty_rows = (
        _row("Slab thickness",  f"{spec['depth_mm']} mm",                    False) +
        _row("Slab area",       f"{area_m2:,.0f} m²",                        True)  +
        _row("Order quantity",
             f"<b style='font-size:15px'>{cubes:,.1f} m³</b> "
             f"<span style='color:#888;font-size:11px'>"
             f"(incl. {int(spec['conc_wastage']*100)}% wastage)</span>",       False)
    )

    section = lambda title, rows: f"""
      <tr><td style="padding:12px 28px 4px 28px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;
                    color:#13294b;font-weight:700;margin-bottom:6px">{title}</div>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #dde1e7;border-radius:8px;overflow:hidden">
          {rows}
        </table>
      </td></tr>"""

    return textwrap.dedent(f"""\
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8">
    <title>Concrete Rate Request — {project_name or 'Fortel Inquiry'}</title></head>
    <body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
    <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12);">

      <tr><td style="background:#13294b;padding:18px 28px;">
        <span style="color:#fff;font-size:18px;font-weight:700;">{FORTEL_NAME}</span>
        <span style="color:#aec3e0;font-size:12px;margin-left:12px;">Concrete Rate Request</span>
      </td></tr>

      <tr><td style="padding:14px 28px 4px 28px;">
        <div style="color:#555;font-size:13px">Project: {proj_line}</div>
        <div style="color:#888;font-size:12px;margin-top:2px">Tender date: {date_str}</div>
      </td></tr>

      {section("Concrete Specification", spec_rows)}
      {section("Quantity", qty_rows)}

      <tr><td style="padding:16px 28px;">
        <p style="color:#333;font-size:13px;margin:0 0 8px 0">
          Please confirm:
        </p>
        <ul style="color:#333;font-size:13px;margin:0;padding-left:18px;line-height:1.8">
          <li>Rate per m³ (delivered, pump-ready)</li>
          <li>Any minimum order charges</li>
          <li>Availability / lead time</li>
        </ul>
      </td></tr>

      <tr><td style="background:#f7f8fa;padding:12px 28px;border-top:1px solid #eee;">
        <span style="color:#aaa;font-size:11px;">
          {FORTEL_NAME} · {FORTEL_EMAIL} · Generated {date_str}
        </span>
      </td></tr>

    </table>
    </td></tr>
    </table>
    </body></html>
    """)


# ── Main public API ──────────────────────────────────────────────────────────

def generate_inquiry(result: dict,
                     project_name: str = None,
                     project_ref:  str = None) -> dict:
    """
    Generate a concrete supplier inquiry from a pipeline result dict.

    Args:
        result:       dict returned by takeoff() — must contain area_m2 and costing
        project_name: override if not already in result (e.g. "Agraco Twinwoods")
        project_ref:  override if not already in result (e.g. "2132")

    Returns:
        {
          "subject":  str,
          "text":     str,   # plain-text email body
          "html":     str,   # HTML email body
          "cubes_m3": float, # order quantity
          "spec":     dict,  # concrete spec used
        }
    """
    area_m2  = result.get("area_m2")
    if not area_m2:
        raise ValueError("result must contain area_m2 — run takeoff() first")

    proj_name = project_name or result.get("project_name", "")
    proj_ref  = project_ref  or result.get("project_ref",  "")

    spec  = _spec_from_result(result)
    cubes = format_cubes(area_m2, spec["depth_mm"], spec["conc_wastage"])
    date_str = datetime.date.today().strftime("%d %b %Y")

    ref_part  = f"[#{proj_ref}] " if proj_ref  else ""
    name_part = f"{proj_name} — " if proj_name else ""
    subject   = f"Concrete Rate Request — {ref_part}{name_part}{spec['conc_mix']} ({cubes:,.0f} m³)"

    text = _text_body(proj_name, proj_ref, area_m2, spec, cubes, date_str)
    html = _html_body(proj_name, proj_ref, area_m2, spec, cubes, date_str)

    return {
        "subject":  subject,
        "text":     text,
        "html":     html,
        "cubes_m3": cubes,
        "spec":     spec,
    }


# ── Wire into takeoff_pipeline (optional convenience) ────────────────────────

def inquiry_from_takeoff(pdf: str, project_name: str = None,
                         project_ref: str = None, **takeoff_kwargs) -> dict:
    """
    Run the full takeoff and immediately return the supplier inquiry.
    Shortcut for: result = takeoff(pdf, ...); return generate_inquiry(result)
    """
    import contextlib, io as _io
    with contextlib.redirect_stdout(_io.StringIO()):
        from takeoff_pipeline import takeoff
    result = takeoff(pdf, project_name=project_name,
                     project_ref=project_ref, **takeoff_kwargs)
    return generate_inquiry(result, project_name=project_name, project_ref=project_ref)


# ── CLI demo ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    # Demo with a synthetic result (no PDF needed)
    demo_result = {
        "file":         "UNMARKED_Yard.pdf",
        "area_m2":      26080,
        "project_name": "Agraco Twinwoods Business Park",
        "project_ref":  "2132",
        "costing": {
            "spec": {
                "depth_mm":     190,
                "conc_mix":     "C32/40",
                "cement_type":  "CEM I",
                "air_entrained": True,
                "aggregate_mm": 20,
                "wc_ratio":     0.45,
                "slump_class":  "S3",
                "conc_wastage": 0.03,
            }
        }
    }

    email = generate_inquiry(demo_result)
    print("SUBJECT:", email["subject"])
    print(f"CUBES:   {email['cubes_m3']:,.1f} m³")
    print()
    print(email["text"])
    print(f"[HTML: {len(email['html'])} chars]")
