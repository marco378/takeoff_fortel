#!/usr/bin/env python3
"""
Fortel AI Takeoff — Quotation Generator

Turns a pipeline result dict into a formatted quotation output:
  - Plain-text quotation body (for email / Word paste)
  - JSON quotation record (for the tracker / n8n)
  - HTML quotation (for email or browser view)
  - Editable Excel quotation (numeric inputs + live formulas)

The quotation matches how Fortel actually issues quotes:
  - Lists the drawing used and its discipline (engineer / architect)
  - States the area measured
  - Gives the rate build-up (depth, mesh, mix)
  - Declares ASSUMPTIONS when build-up is not from an engineer drawing
  - States "subject to confirmation" wherever assumptions were made

Usage:
  from quotation import generate_quotation, quotation_text, quotation_html, quotation_xlsx

  result = takeoff_pipeline.takeoff("drawings/D77.pdf")
  q = generate_quotation(result, project="Hemington D77 Hard Landscaping",
                         client="Fortel", ref="FTL-2026-D77")
  print(quotation_text(q))

Run standalone:
  python3 quotation.py          # self-test with synthetic data
"""
import datetime, json, uuid, io, contextlib
from collections import OrderedDict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

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

SECTION_ORDER = (
    "External yard slabs",
    "Dock slabs",
    "Ground floor slabs",
    "Upper floor slabs",
    "Prelims",
)
_SECTION_RANK = {section: index for index, section in enumerate(SECTION_ORDER)}
PROVISIONAL_LABEL = "PROVISIONAL — NO DETAILS PROVIDED"


def quotation_section(result: dict) -> str:
    """Return the client's canonical BOQ section for a drawing/result."""
    explicit = str(result.get("quotation_section") or "").strip()
    if explicit:
        for section in SECTION_ORDER:
            if explicit.casefold() == section.casefold():
                return section

    label = " ".join(str(result.get(key) or "") for key in (
        "file", "pdf", "pdf_path", "project_name", "name", "type"
    )).casefold().replace("_", "-")
    if "prelim" in label:
        return "Prelims"
    if "dock" in label:
        return "Dock slabs"
    if any(term in label for term in (
            "upper floor", "first floor", "mezzanine", "level 1", "level-1")):
        return "Upper floor slabs"
    if any(term in label for term in (
            "ground floor", "ground-floor", "office", "transport", "internal slab")):
        return "Ground floor slabs"
    return "External yard slabs"


def _normalise_section(section, fallback="External yard slabs"):
    probe = str(section or "").strip().casefold()
    aliases = {
        "yard": "External yard slabs", "external": "External yard slabs",
        "external yard": "External yard slabs", "external yard slabs": "External yard slabs",
        "dock": "Dock slabs", "dock slabs": "Dock slabs",
        "ground": "Ground floor slabs", "ground floor": "Ground floor slabs",
        "ground floor slabs": "Ground floor slabs", "gf ancillary": "Ground floor slabs",
        "upper": "Upper floor slabs", "upper floors": "Upper floor slabs",
        "upper floor slabs": "Upper floor slabs",
        "prelims": "Prelims", "preliminaries": "Prelims",
    }
    return aliases.get(probe, fallback)


def _spec_key(costing):
    # Compare the complete existing spec record so units are never merged when any supplied
    # specification provenance differs. This only groups data; it does not infer new fields.
    return json.dumps(costing.get("spec") or {}, sort_keys=True, default=str, separators=(",", ":"))


def _provisional_text(li):
    return f"{li['description']} [{PROVISIONAL_LABEL}]" if li.get("provisional") else li["description"]


def _unique_notes(notes):
    return list(dict.fromkeys(note for note in notes if note))


# ── Core generator ────────────────────────────────────────────────────────────

def generate_quotation(result: dict | list, project: str = "", client: str = "",
                       ref: str = None, extras: list = None) -> dict:
    """Build one structured quotation from one result or a project's result list.

    Units with the same canonical section, specification, existing rate and assumption
    provenance are aggregated into one quantity.  Differing specifications remain separate
    rows on the same quotation; no rate is recalculated here.
    """
    results = [r for r in (result if isinstance(result, (list, tuple)) else [result]) if r]
    if not results:
        results = [{}]
    ref = ref or f"FTL-{datetime.date.today().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
    today = datetime.date.today().strftime("%-d %B %Y")

    groups = OrderedDict()
    extra_rows = []
    declarations = []
    pipeline_flags = []
    measurements_by_key = OrderedDict()
    drawings = []

    for unit in results:
        costing = unit.get("costing") or {}
        area = costing.get("area_m2") or unit.get("area_m2") or 0
        spec = costing.get("spec") or {}
        assumed = bool(costing.get("assumed", True))
        rate = costing.get("rate")
        section = quotation_section(unit)
        drawing = unit.get("file") or Path(str(unit.get("pdf_path") or "")).name
        if drawing and drawing not in drawings:
            drawings.append(drawing)
        flags = list(unit.get("flags") or [])
        pipeline_flags.extend(flags)

        if area and rate is not None:
            # Existing rate is part of the key so stale/different priced results can never be
            # silently collapsed under one arbitrary rate. Matching specs/rates aggregate.
            key = (section, _spec_key(costing), rate, assumed)
            group = groups.setdefault(key, {
                "section": section, "spec": spec, "rate": rate, "assumed": assumed,
                "area": 0.0, "drawings": [], "breakdown": costing.get("breakdown") or {},
            })
            group["area"] += float(area)
            if drawing and drawing not in group["drawings"]:
                group["drawings"].append(drawing)

        if assumed:
            declarations.append(f"{PROVISIONAL_LABEL}: {assumption_note(spec)}")
        if unit.get("source_discipline") == "architect":
            declarations.append(
                "Area measured from architect's hard-landscaping drawing — ±5% tolerance applies. "
                "No engineer construction-detail drawing found in the pack."
            )
        declarations.extend(f for f in flags if (
            "ASSUMED" in f or "architect" in f.lower() or "tolerance" in f.lower()))

        perimeter = unit.get("perimeter_lm")
        if perimeter is None and unit.get("polygon_pts") and unit.get("scale_k"):
            from geometry import polygon_perimeter_lm
            perimeter = polygon_perimeter_lm(unit["polygon_pts"], unit["scale_k"])
        if perimeter is not None:
            mkey = (section, "Slab perimeter", "Lm", False)
            measurement = measurements_by_key.setdefault(mkey, {
                "section": section, "description": "Slab perimeter", "qty": 0.0,
                "unit": "Lm", "provisional": False, "drawings": [],
            })
            measurement["qty"] += float(perimeter)
            if drawing and drawing not in measurement["drawings"]:
                measurement["drawings"].append(drawing)

        assumed_manhole_count = unit.get("manhole_count_assumed")
        if assumed_manhole_count is not None:
            mkey = (section, "Manholes (assumed fallback)", "Nr", True)
            measurement = measurements_by_key.setdefault(mkey, {
                "section": section, "description": "Manholes (assumed fallback)", "qty": 0,
                "unit": "Nr", "provisional": True, "drawings": [],
                "provisional_reason": PROVISIONAL_LABEL,
            })
            measurement["qty"] += int(assumed_manhole_count)
            if drawing and drawing not in measurement["drawings"]:
                measurement["drawings"].append(drawing)
            provenance = next((f for f in flags if "manhole_count_assumed=" in f), "")
            declarations.append(f"{PROVISIONAL_LABEL}: {provenance or 'assumed manhole quantity'}")

        if extras is None:
            for ex in costing.get("extras", []):
                provisional = bool(ex.get("estimate", False))
                extra_rows.append({
                    "section": _normalise_section(ex.get("section"), section),
                    "description": ex["description"], "qty": ex["qty"], "unit": ex["unit"],
                    "rate": ex["rate"], "value": ex["value"], "provisional": provisional,
                    "provisional_reason": PROVISIONAL_LABEL if provisional else "",
                    "drawings": [drawing] if drawing else [],
                })
                if provisional:
                    declarations.append(
                        f"{PROVISIONAL_LABEL}: {ex['description']} is an estimate from existing "
                        "measurement provenance and must be confirmed before issue."
                    )

    line_items = []
    for group in sorted(groups.values(), key=lambda g: _SECTION_RANK[g["section"]]):
        spec = group["spec"]
        area = round(group["area"], 3)
        depth_mm = spec.get("depth_mm", 190)
        mesh = spec.get("mesh", "A252")
        mix = spec.get("conc_mix", "C32/40")
        layers = spec.get("layers", 1)
        common = {
            "section": group["section"], "qty": area, "unit": "m²",
            "drawings": group["drawings"],
        }
        slab_desc = f"{depth_mm}mm {mix} slab, {layers}× {mesh} mesh (supply & lay)"
        line_items.append({
            **common, "description": slab_desc, "rate": group["rate"],
            "value": round(area * group["rate"], 2), "provisional": group["assumed"],
            "provisional_reason": PROVISIONAL_LABEL if group["assumed"] else "",
        })
        # Existing quotation adders are preserved unchanged; only their section/aggregated
        # quantity changes so all units share the client's requested one-tab structure.
        adders = [
            ("Final trim ±50mm", area, "m²", 1.40),
            ("Saw cuts & bay joints", area, "m²", 4.85),
        ]
        for desc, qty, unit_name, item_rate in adders:
            line_items.append({
                **common, "description": desc, "qty": qty, "unit": unit_name,
                "rate": item_rate, "value": round(qty * item_rate, 2),
                "provisional": False,
            })

    if extras is not None:
        fallback_section = quotation_section(results[0])
        for ex in extras:
            if isinstance(ex, dict):
                desc, qty, unit_name, item_rate = (ex["description"], ex["qty"], ex["unit"], ex["rate"])
                section = _normalise_section(ex.get("section"), fallback_section)
                provisional = bool(ex.get("estimate", False))
            else:
                desc, qty, unit_name, item_rate = ex
                section = fallback_section
                # Use the result's existing provenance; do not infer from wording alone.
                provisional = bool(results[0].get("manhole_count_estimate") and "MH" in desc)
            extra_rows.append({
                "section": section, "description": desc, "qty": qty, "unit": unit_name,
                "rate": item_rate, "value": round(qty * item_rate, 2),
                "provisional": provisional,
                "provisional_reason": PROVISIONAL_LABEL if provisional else "",
                "drawings": [],
            })
            if provisional:
                declarations.append(f"{PROVISIONAL_LABEL}: {desc} must be confirmed before issue.")

    line_items.extend(extra_rows)
    line_items.sort(key=lambda li: _SECTION_RANK.get(li["section"], len(SECTION_ORDER)))
    measurements = sorted(measurements_by_key.values(),
                          key=lambda item: _SECTION_RANK[item["section"]])
    subtotal = round(sum(li["value"] for li in line_items), 2)
    assumed = any(bool((unit.get("costing") or {}).get("assumed", True)) for unit in results)

    if extras is None:
        declarations.append(
            "NOTE — Extra-over items (slot drains, transitions, etc. other than manholes) "
            "not included: quantities cannot be measured from the drawing automatically "
            "and require manual assessment by the assessor before issue."
        )

    first_costing = results[0].get("costing") or {}
    first_spec = first_costing.get("spec") or {}
    disciplines = _unique_notes([unit.get("source_discipline", "unknown") for unit in results])
    perimeter_measurements = [m for m in measurements if m["description"] == "Slab perimeter"]
    total_perimeter = (round(sum(float(m["qty"]) for m in perimeter_measurements), 1)
                       if perimeter_measurements else None)
    return {
        "ref": ref, "date": today, "project": project, "client": client,
        "drawing": ", ".join(drawings), "drawings": drawings,
        "drawing_type": results[0].get("type", ""),
        "discipline": ", ".join(disciplines),
        "area_m2": round(sum(float((unit.get("costing") or {}).get("area_m2")
                                   or unit.get("area_m2") or 0) for unit in results), 3),
        "perimeter_lm": total_perimeter,
        "measurements": measurements,
        "spec": {
            "depth_mm": first_spec.get("depth_mm", 190),
            "mesh": first_spec.get("mesh", "A252"),
            "conc_mix": first_spec.get("conc_mix", "C32/40"),
            "layers": first_spec.get("layers", 1),
        },
        "rate": first_costing.get("rate"), "breakdown": first_costing.get("breakdown") or {},
        "line_items": line_items, "section_order": list(SECTION_ORDER),
        "subtotal_gbp": subtotal, "total_gbp": subtotal,
        "assumed": assumed, "declarations": _unique_notes(declarations),
        "pipeline_flags": _unique_notes(pipeline_flags), "terms": STANDARD_TERMS,
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
        description = _provisional_text(li)
        lines.append(
            f"  {description:<40}{li['qty']:>8,.0f}{li['unit']:>6}"
            f"{li['rate']:>9.2f}{li['value']:>13,.2f}"
        )
    lines += [
        "-" * 70,
        f"{'TOTAL NETT (excl. VAT)':<55}{q['total_gbp']:>15,.2f}",
        "=" * 70,
        "",
    ]
    if q.get("measurements"):
        lines.append("INFORMATIONAL MEASUREMENTS (NOT PRICED):")
        for measurement in q["measurements"]:
            description = _provisional_text(measurement)
            lines.append(
                f"  {measurement['section']} — {description}: "
                f"{measurement['qty']:,.1f} {measurement['unit']}"
            )
        lines.append("")
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
        provisional = (f" <strong style='color:#9a6500'>{PROVISIONAL_LABEL}</strong>"
                       if li.get("provisional") else "")
        return (f"<tr><td>{li['section']}</td><td>{li['description']}{provisional}</td>"
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
    measurement_html = ""
    if q.get("measurements"):
        measurement_rows = ""
        for measurement in q["measurements"]:
            marker = (f" <strong style='color:#9a6500'>{PROVISIONAL_LABEL}</strong>"
                      if measurement.get("provisional") else "")
            measurement_rows += (
                f"<tr><td>{measurement['section']}</td>"
                f"<td>{measurement['description']}{marker}</td>"
                f"<td style='text-align:right'>{measurement['qty']:,.1f} "
                f"{measurement['unit']}</td></tr>"
            )
        measurement_html = (
            "<h3>Informational measurements <small>(not priced)</small></h3>"
            "<table><thead><tr><th>Section</th><th>Measurement</th>"
            "<th style='text-align:right'>Quantity</th></tr></thead>"
            f"<tbody>{measurement_rows}</tbody></table>"
        )

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
{measurement_html}
{decl_html}
<p class='terms'>{q['terms']}</p>
<footer>{FORTEL_NAME} &nbsp;·&nbsp; {FORTEL_EMAIL} &nbsp;·&nbsp; {FORTEL_TEL}</footer>
</body></html>"""


def quotation_json(q: dict) -> str:
    """JSON record — for the tracker / n8n."""
    return json.dumps(q, indent=2, default=str, ensure_ascii=False)


def _excel_text(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _excel_date(value):
    """Return a real Excel date when the quotation date is in a known format."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    for fmt in ("%d %B %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(value), fmt).date()
        except (TypeError, ValueError):
            pass
    return _excel_text(value)


def _excel_unit(value):
    """Match the unit spellings used in Fortel's editable BOQ template."""
    text = str(value or "")
    return {
        "m²": "m2", "m2": "m2",
        "m³": "m3", "m3": "m3",
        "lm": "Lm", "nr": "Nr", "item": "Item", "t": "T",
    }.get(text.casefold(), text)


def _excel_section_title(section, items):
    labels = {
        "External yard slabs": "External Yard Slabs",
        "Dock slabs": "Dock Slabs",
        "Ground floor slabs": "Ground Floor Slabs (Ancillary Areas)",
        "Upper floor slabs": "Upper Floors",
        "Prelims": "Prelims",
    }
    title = labels.get(section, str(section))
    if section != "Prelims" and any(item.get("provisional") for item in items):
        title += "- Provisional Cost (No Details)"
    return title


def quotation_xlsx(q: dict) -> bytes:
    """Editable one-sheet Excel quotation in Fortel's client-facing BOQ layout."""
    wb = Workbook()
    ws = wb.active
    ws.title = "REV_01"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A9"
    ws.sheet_properties.tabColor = "13294B"

    navy = "13294B"
    pale_blue = "EAF0F8"
    pale_gold = "FFF2CC"
    pale_grey = "F4F6F8"
    section_blue = "0070C0"
    black = "000000"
    white = "FFFFFF"
    muted = "667085"
    thin_grey = Side(style="thin", color="D9DEE7")

    ws.merge_cells("A1:E1")
    ws["A1"] = FORTEL_NAME
    ws["A1"].font = Font(name="Aptos Display", size=18, bold=True, color=white)
    ws["A1"].fill = PatternFill("solid", fgColor=navy)
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30

    ws["A2"], ws["B2"] = "Project:", _excel_text(q.get("project") or "—")
    ws["A3"], ws["B3"] = "Client:", _excel_text(q.get("client") or "—")
    ws["A4"], ws["B4"] = "Date:", _excel_date(q.get("date"))
    ws["C4"], ws["D4"] = "Quotation Ref:", _excel_text(q.get("ref") or "—")
    ws.merge_cells("B2:E2")
    ws.merge_cells("B3:E3")
    ws.merge_cells("D4:E4")
    for cell in (ws["A2"], ws["A3"], ws["A4"], ws["C4"]):
        cell.font = Font(bold=True, color=navy)
    ws["B4"].number_format = "dd/mm/yyyy"

    drawings = q.get("drawings") or []
    ws["A5"] = "Drawing ref available at tender:"
    ws["A5"].font = Font(bold=True, color=navy)
    ws["A5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws["B5"] = _excel_text("\n".join(drawings) or "—")
    ws.merge_cells("B5:E5")
    ws["B5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[5].height = max(30, 15 * max(2, len(drawings)))
    for row_index in range(2, 6):
        for col_index in range(1, 6):
            ws.cell(row_index, col_index).fill = PatternFill("solid", fgColor=pale_blue)

    ws["A6"], ws["B6"] = "Measured area (m2)", float(q.get("area_m2") or 0)
    ws["C6"] = "Slab perimeter (Lm)"
    ws["D6"] = (float(q["perimeter_lm"]) if q.get("perimeter_lm") is not None else None)
    for cell in (ws["A6"], ws["C6"]):
        cell.font = Font(bold=True, color=muted)
    ws["B6"].number_format = '#,##0.##'
    ws["D6"].number_format = '#,##0.##'

    sections = OrderedDict()
    for item in q.get("line_items", []):
        sections.setdefault(item.get("section") or "External yard slabs", []).append(item)
    ordered_sections = [section for section in q.get("section_order", SECTION_ORDER) if section in sections]
    ordered_sections += [section for section in sections if section not in ordered_sections]

    row = 8
    subtotal_rows = []
    header_fill = PatternFill("solid", fgColor=pale_grey)
    section_fill = PatternFill("solid", fgColor=section_blue)
    provisional_fill = PatternFill("solid", fgColor=pale_gold)
    headings = ("DESCRIPTION", "QTY", "UNIT", "RATE", "VALUE")

    for col, heading in enumerate(headings, 1):
        cell = ws.cell(row, col, heading)
        cell.font = Font(bold=True, color=white, size=10)
        cell.fill = PatternFill("solid", fgColor=black)
        cell.alignment = Alignment(horizontal="right" if col in (2, 4, 5) else "left")
    ws.row_dimensions[row].height = 20
    row += 1

    for section in ordered_sections:
        section_items = sections[section]
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        ws.cell(row, 1, _excel_section_title(section, section_items))
        ws.cell(row, 1).font = Font(bold=True, color=white, size=12)
        ws.cell(row, 1).fill = section_fill
        ws.cell(row, 1).alignment = Alignment(vertical="center")
        ws.row_dimensions[row].height = 22
        row += 1

        first_item_row = row
        for item in section_items:
            description = item["description"]
            if item.get("provisional"):
                description += f"\n{PROVISIONAL_LABEL}"
            ws.cell(row, 1, _excel_text(description))
            ws.cell(row, 2, float(item["qty"]))
            ws.cell(row, 3, _excel_text(_excel_unit(item["unit"])))
            ws.cell(row, 4, float(item["rate"]))
            ws.cell(row, 5, f"=ROUND(B{row}*D{row},2)")
            ws.cell(row, 2).number_format = '#,##0.##'
            ws.cell(row, 4).number_format = '£#,##0.00'
            ws.cell(row, 5).number_format = '£#,##0.00'
            for col in range(1, 6):
                ws.cell(row, col).border = Border(bottom=thin_grey)
                ws.cell(row, col).alignment = Alignment(
                    horizontal="right" if col in (2, 4, 5) else "left",
                    vertical="top", wrap_text=col == 1)
                if item.get("provisional"):
                    ws.cell(row, col).fill = provisional_fill
            if item.get("provisional"):
                ws.row_dimensions[row].height = 30
            row += 1

        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ws.cell(row, 1, f"Subtotal — {_excel_section_title(section, [])}")
        ws.cell(row, 1).font = Font(bold=True, color=navy)
        ws.cell(row, 5, f"=SUM(E{first_item_row}:E{row - 1})")
        ws.cell(row, 5).font = Font(bold=True, color=navy)
        ws.cell(row, 5).number_format = '£#,##0.00'
        for col in range(1, 6):
            ws.cell(row, col).fill = header_fill
            ws.cell(row, col).border = Border(top=thin_grey, bottom=thin_grey)
        subtotal_rows.append(row)
        row += 2

    total_row = row
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=4)
    ws.cell(total_row, 1, "TOTAL NETT")
    subtotal_refs = ",".join(f"E{r}" for r in subtotal_rows)
    ws.cell(total_row, 5, f"=SUM({subtotal_refs})" if subtotal_refs else "=0")
    ws.cell(total_row, 5).number_format = '£#,##0.00'
    for col in range(1, 6):
        ws.cell(total_row, col).fill = PatternFill("solid", fgColor=navy)
        ws.cell(total_row, col).font = Font(bold=True, color=white, size=12)

    row = total_row + 3
    measurements = q.get("measurements") or []
    if measurements:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        ws.cell(row, 1, "INFORMATIONAL MEASUREMENTS — NOT PRICED")
        ws.cell(row, 1).font = Font(bold=True, color=white)
        ws.cell(row, 1).fill = section_fill
        row += 1
        for measurement in measurements:
            description = f"{_excel_section_title(measurement['section'], [])} — {measurement['description']}"
            if measurement.get("provisional"):
                description += f"\n{PROVISIONAL_LABEL}"
            ws.cell(row, 1, description)
            ws.cell(row, 2, float(measurement["qty"]))
            ws.cell(row, 3, _excel_unit(measurement["unit"]))
            ws.cell(row, 2).number_format = '#,##0.##'
            if measurement.get("provisional"):
                for col in range(1, 6):
                    ws.cell(row, col).fill = provisional_fill
                ws.row_dimensions[row].height = 30
            row += 1
        row += 1

    if q.get("declarations"):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        ws.cell(row, 1, "NOTES / ASSUMPTIONS")
        ws.cell(row, 1).font = Font(bold=True, color=white)
        ws.cell(row, 1).fill = section_fill
        row += 1
        for note in q["declarations"]:
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
            ws.cell(row, 1, f"• {note}")
            ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[row].height = 30
            row += 1

    ws.merge_cells(start_row=row + 1, start_column=1, end_row=row + 1, end_column=5)
    ws.cell(row + 1, 1, f"STANDARD TERMS: {q['terms']}")
    ws.cell(row + 1, 1).font = Font(size=9, color=muted)
    ws.cell(row + 1, 1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row + 1].height = 45

    widths = {"A": 68, "B": 14, "C": 10, "D": 15, "E": 18}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.auto_filter.ref = f"A8:E{max(8, total_row - 2)}"
    ws.print_title_rows = "1:8"
    ws.print_area = f"A1:E{row + 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddFooter.center.text = f"{FORTEL_NAME} · {FORTEL_EMAIL} · {FORTEL_TEL}"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True
    wb.calculation.calcMode = "auto"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def save_quotation(q: dict, out_dir: str = ".") -> dict:
    """Save text, HTML, JSON and editable Excel versions to disk. Returns paths dict."""
    base = Path(out_dir) / q["ref"]
    paths = {}
    base.parent.mkdir(parents=True, exist_ok=True)
    (p := Path(f"{base}.txt")).write_text(quotation_text(q));  paths["txt"]  = str(p)
    (p := Path(f"{base}.html")).write_text(quotation_html(q)); paths["html"] = str(p)
    (p := Path(f"{base}.json")).write_text(quotation_json(q)); paths["json"] = str(p)
    (p := Path(f"{base}.xlsx")).write_bytes(quotation_xlsx(q)); paths["xlsx"] = str(p)
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
