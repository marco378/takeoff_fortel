#!/usr/bin/env python3
"""
Fortel AI Takeoff — Quotation Generator

Turns a pipeline result dict into a formatted quotation output:
  - Plain-text quotation body (for email / Word paste)
  - JSON quotation record (for the tracker / n8n)
  - HTML quotation (for email or browser view)

The quotation matches how Fortel actually issues quotes:
  - Lists the drawing used and its discipline (engineer / architect)
  - States the area measured
  - Gives the rate build-up (depth, mesh, mix)
  - Declares ASSUMPTIONS when build-up is not from an engineer drawing
  - States "subject to confirmation" wherever assumptions were made

Usage:
  from quotation import generate_quotation, quotation_text, quotation_html

  result = takeoff_pipeline.takeoff("drawings/D77.pdf")
  q = generate_quotation(result, project="Hemington D77 Hard Landscaping",
                         client="Fortel", ref="FTL-2026-D77")
  print(quotation_text(q))

Run standalone:
  python3 quotation.py          # self-test with synthetic data
"""
import datetime, json, uuid, io, contextlib
from pathlib import Path

with contextlib.redirect_stdout(io.StringIO()):
    from costing import rate_buildup, MESH_KG
from defaults import spec_with_defaults, assumption_note

# Fortel company details for the quotation header
FORTEL_NAME    = "Fortel Group Limited"
FORTEL_ADDRESS = "Fortel House, Birmingham"
FORTEL_EMAIL   = "estimating@fortel.co.uk"
FORTEL_TEL     = "+44 (0)121 000 0000"

STANDARD_TERMS = (
    "This quotation is based on the drawings referenced below. "
    "Areas and build-ups are subject to confirmation upon receipt of the full construction specification. "
    "Rates are inclusive of supply, labour, plant and DPM. "
    "Excludes earthworks, drainage, kerbing and any items not listed above. "
    "Validity: 30 days from date of issue."
)


# ── Core generator ────────────────────────────────────────────────────────────

def generate_quotation(result: dict, project: str = "", client: str = "",
                       ref: str = None, extras: list = None) -> dict:
    """
    Build a structured quotation dict from a takeoff result.

    result  : dict from takeoff_pipeline.takeoff()
    project : free-text project/job name
    client  : client company name
    ref     : quote reference (auto-generated if None)
    extras  : [(desc, qty, unit, rate_gbp)] extra BOQ lines (manholes, slot drains, etc.)

    Returns a rich quotation dict with all sections pre-formatted.
    """
    ref     = ref or f"FTL-{datetime.date.today().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
    today   = datetime.date.today().strftime("%-d %B %Y")
    costing = result.get("costing", {})
    area    = costing.get("area_m2") or result.get("area_m2") or 0
    spec    = costing.get("spec", {})
    assumed = costing.get("assumed", True)
    rate    = costing.get("rate")
    total   = costing.get("total_gbp")
    bdwn    = costing.get("breakdown", {})
    flags   = result.get("flags", [])
    discipline = result.get("source_discipline", "unknown")

    # ── Line items ───────────────────────────────────────────────────────────
    depth_mm = spec.get("depth_mm", 190)
    mesh     = spec.get("mesh", "A252")
    mix      = spec.get("conc_mix", "C32/40")
    layers   = spec.get("layers", 1)

    line_items = []
    if area and rate:
        slab_desc = f"{depth_mm}mm {mix} slab, {layers}× {mesh} mesh (supply & lay)"
        line_items.append({
            "section": "Concrete slab",
            "description": slab_desc,
            "qty": area,
            "unit": "m²",
            "rate": rate,
            "value": round(area * rate, 2),
        })
        # Standard adders (from Winvic template — always present)
        adders = [
            ("Finishing", "Final trim ±50mm",       area, "m²",  1.40),
            ("Finishing", "Saw cuts & bay joints",   area, "m²",  4.85),
        ]
        for sec, desc, qty, unit, r in adders:
            line_items.append({"section": sec, "description": desc,
                                "qty": qty, "unit": unit, "rate": r,
                                "value": round(qty * r, 2)})

    # Extra-over items (manholes, slot drains, transitions, etc.)
    for desc, qty, unit, r in (extras or []):
        line_items.append({"section": "Extra-over", "description": desc,
                            "qty": qty, "unit": unit, "rate": r,
                            "value": round(qty * r, 2)})

    subtotal = round(sum(li["value"] for li in line_items), 2)

    # ── Assumption declarations ───────────────────────────────────────────────
    declarations = []
    if assumed:
        declarations.append(assumption_note(spec))
    if discipline == "architect":
        declarations.append(
            "Area measured from architect's hard-landscaping drawing — ±5% tolerance applies. "
            "No engineer construction-detail drawing found in the pack."
        )
    # Surface any pipeline flags as notes
    assumption_flags = [f for f in flags if "ASSUMED" in f or "architect" in f.lower()
                        or "tolerance" in f.lower()]
    for f in assumption_flags:
        if f not in declarations:
            declarations.append(f)

    return {
        "ref":           ref,
        "date":          today,
        "project":       project,
        "client":        client,
        "drawing":       result.get("file", ""),
        "drawing_type":  result.get("type", ""),
        "discipline":    discipline,
        "area_m2":       area,
        "spec": {
            "depth_mm": depth_mm, "mesh": mesh, "conc_mix": mix, "layers": layers,
        },
        "rate":          rate,
        "breakdown":     bdwn,
        "line_items":    line_items,
        "subtotal_gbp":  subtotal,
        "total_gbp":     subtotal,   # no VAT on quotation (net only, Fortel standard)
        "assumed":       assumed,
        "declarations":  declarations,
        "pipeline_flags": flags,
        "terms":         STANDARD_TERMS,
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def quotation_text(q: dict) -> str:
    """Plain-text quotation — suitable for email body or Word paste."""
    lines = [
        f"{FORTEL_NAME}",
        f"Quotation Ref: {q['ref']}",
        f"Date: {q['date']}",
        "",
        f"Project : {q['project'] or '—'}",
        f"Client  : {q['client'] or '—'}",
        f"Drawing : {q['drawing']} ({q.get('drawing_type','')}, {q.get('discipline','')}'s drawing)",
        "",
        "=" * 70,
        f"{'DESCRIPTION':<42}{'QTY':>8}{'UNIT':>6}{'RATE £':>9}{'VALUE £':>13}",
        "-" * 70,
    ]
    last_section = None
    for li in q["line_items"]:
        if li["section"] != last_section:
            lines.append(f"\n  {li['section'].upper()}")
            last_section = li["section"]
        lines.append(
            f"  {li['description']:<40}{li['qty']:>8,.0f}{li['unit']:>6}"
            f"{li['rate']:>9.2f}{li['value']:>13,.2f}"
        )
    lines += [
        "-" * 70,
        f"{'TOTAL NETT (excl. VAT)':<55}{q['total_gbp']:>15,.2f}",
        "=" * 70,
        "",
    ]
    if q["declarations"]:
        lines.append("NOTES / ASSUMPTIONS:")
        for d in q["declarations"]:
            lines.append(f"  • {d}")
        lines.append("")
    lines += [
        "STANDARD TERMS:",
        f"  {q['terms']}",
        "",
        f"Issued by: {FORTEL_NAME} · {FORTEL_EMAIL} · {FORTEL_TEL}",
    ]
    return "\n".join(lines)


def quotation_html(q: dict) -> str:
    """Self-contained HTML quotation — suitable for email or browser view."""
    def _row(li):
        return (f"<tr><td>{li['section']}</td><td>{li['description']}</td>"
                f"<td style='text-align:right'>{li['qty']:,.0f} {li['unit']}</td>"
                f"<td style='text-align:right'>£{li['rate']:.2f}</td>"
                f"<td style='text-align:right'>£{li['value']:,.2f}</td></tr>")

    assumed_banner = ""
    if q["assumed"]:
        assumed_banner = (
            f"<div style='background:#fef9ec;border-left:4px solid #e67e22;"
            f"padding:10px 16px;margin:16px 0;font-size:13px;color:#7a5200'>"
            f"⚠ <b>Build-up assumed</b> — {q['declarations'][0] if q['declarations'] else ''}"
            f"</div>"
        )

    decl_html = ""
    if q["declarations"]:
        items = "".join(f"<li>{d}</li>" for d in q["declarations"])
        decl_html = f"<h3>Notes / Assumptions</h3><ul style='font-size:13px'>{items}</ul>"

    rows = "\n".join(_row(li) for li in q["line_items"])

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Quotation {q['ref']}</title>
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#111;max-width:760px;margin:32px auto;padding:0 16px}}
header{{background:#13294b;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0}}
header h1{{margin:0;font-size:22px}}
header p{{margin:4px 0 0 0;opacity:.75;font-size:13px}}
.meta{{display:flex;gap:40px;padding:14px 0;border-bottom:1px solid #ddd;font-size:13px;color:#555}}
.meta b{{color:#111}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th{{background:#f7f8fa;padding:7px 10px;text-align:left;font-size:12px;
    text-transform:uppercase;letter-spacing:.5px;color:#666;border-bottom:2px solid #ddd}}
td{{padding:6px 10px;border-bottom:1px solid #eee;font-size:13px}}
.total-row td{{font-weight:700;font-size:15px;border-top:2px solid #111;background:#f7f8fa}}
.terms{{font-size:12px;color:#888;margin-top:20px;line-height:1.6}}
footer{{font-size:11px;color:#bbb;margin-top:20px;padding-top:12px;border-top:1px solid #eee}}
</style></head><body>
<header>
  <h1>{FORTEL_NAME}</h1>
  <p>Quotation {q['ref']} &nbsp;·&nbsp; {q['date']}</p>
</header>
<div class="meta">
  <div><b>Project</b><br>{q.get('project') or '—'}</div>
  <div><b>Client</b><br>{q.get('client') or '—'}</div>
  <div><b>Drawing</b><br>{q['drawing']}</div>
  <div><b>Area</b><br><b style='font-size:18px;color:#13294b'>{q['area_m2']:,.0f} m²</b></div>
</div>
{assumed_banner}
<table>
  <thead><tr>
    <th>Section</th><th>Description</th><th style='text-align:right'>Qty</th>
    <th style='text-align:right'>Rate</th><th style='text-align:right'>Value</th>
  </tr></thead>
  <tbody>{rows}</tbody>
  <tfoot>
    <tr class='total-row'>
      <td colspan='4'>TOTAL NETT (excl. VAT)</td>
      <td style='text-align:right'>£{q['total_gbp']:,.2f}</td>
    </tr>
  </tfoot>
</table>
{decl_html}
<p class='terms'>{q['terms']}</p>
<footer>{FORTEL_NAME} &nbsp;·&nbsp; {FORTEL_EMAIL} &nbsp;·&nbsp; {FORTEL_TEL}</footer>
</body></html>"""


def quotation_json(q: dict) -> str:
    """JSON record — for the tracker / n8n."""
    return json.dumps(q, indent=2, default=str)


def save_quotation(q: dict, out_dir: str = ".") -> dict:
    """Save text + HTML + JSON versions to disk. Returns paths dict."""
    base = Path(out_dir) / q["ref"]
    paths = {}
    base.parent.mkdir(parents=True, exist_ok=True)
    (p := Path(f"{base}.txt")).write_text(quotation_text(q));  paths["txt"]  = str(p)
    (p := Path(f"{base}.html")).write_text(quotation_html(q)); paths["html"] = str(p)
    (p := Path(f"{base}.json")).write_text(quotation_json(q)); paths["json"] = str(p)
    return paths


# ── Standalone demo ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, io, contextlib

    # Simulate a pipeline result (as returned by takeoff_pipeline.takeoff)
    demo_result = {
        "file":             "SGP-D77-Hard-Landscaping.pdf",
        "type":             "UNMARKED vector",
        "confidence":       "medium",
        "source_discipline":"architect",
        "area_m2":          3172,
        "costing": {
            "area_m2":   3172,
            "rate":      44.89,
            "total_gbp": 142_391.08,
            "assumed":   True,
            "spec": {
                "depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40",
                "layers": 1, "conc_rate": 128,
            },
            "breakdown": {
                "concrete": 25.05, "steel": 4.30, "dpm": 0.46,
                "curing": 0.23, "labour": 10.00, "trim": 0.40,
                "nett": 40.44, "margin%": 11,
            },
        },
        "flags": [
            "BUILD-UP ASSUMED: 190mm / A252 / C32/40 — no engineer construction-detail found",
            "ARCHITECT drawing — build-up ASSUMED; no construction-detail sheet found.",
            "assessor: confirm extent + scale",
        ],
    }

    extras = [
        ("Manholes (inside concrete boundary)", 3, "Nr",  75.00),
        ("Slot drain channels (linear metres)", 48, "Lm", 17.50),
    ]

    q = generate_quotation(demo_result, project="Hemington D77 Hard Landscaping",
                           client="Winvic Construction", ref="FTL-2026-D77A",
                           extras=extras)

    print("── Plain text quotation ─────────────────────────────────────────────")
    print(quotation_text(q))
    print()

    paths = save_quotation(q, out_dir="quotations")
    print(f"Saved: {paths}")

    # Quick validation
    assert q["total_gbp"] > 0, "total must be positive"
    assert q["assumed"] is True, "should flag assumed build-up"
    assert any("ASSUMED" in d for d in q["declarations"]), "must declare assumption"
    assert len(q["line_items"]) >= 3, "at least slab + trim + joints"
    print("\nAll assertions passed ✅")
