#!/usr/bin/env python3
"""
Robust takeoff (post-adversarial fixes).

Breaks found by adversarial testing and how they're handled:
  1. HARDCODED SCALE (k=0.108 only right for the yard; 63-900% errors elsewhere)
       -> scale is NEVER hardcoded. Marked path needs no scale (reads stored areas).
          Vision path REQUIRES a per-viewport calibrated scale and FLAGS if unverified.
  2. MULTI-REGION slabs (dock/office/transport are 4 polygons each)
       -> sum all slab polygons, don't assume one.
  3. UNRELIABLE SCALE SOURCES (title-block 1:500 gives 69,560 for the 26,080 yard; the
     sheet even says "do not scale"; perimeter-calibration gave 780% error; dim-lines are
     on a different viewport) -> title-block scale is returned only as UNVERIFIED + flagged.
  4. FLATTENED drawings (office/transport have 0 CAD layers) -> no layer anchor; vision-only.
"""
import fitz, math, re
from collections import OrderedDict
from shapely.geometry import Polygon
from markup import parse_area_m2


_LENGTH_RX = re.compile(
    r"(?<![\w.])([\d,]+(?:\.\d+)?)\s*(km|m|cm|mm|ft|feet|in|inch(?:es)?)\b",
    re.I,
)

# Subject is the reliable discriminator in the client-supplied Castle Donington markups.
# Colour is retained as evidence in each annotation record, but it is deliberately NOT used
# to classify: every office slab subject has the same yellow fill. Unknown subjects remain
# unclassified so a future markup convention can never silently turn into yard quantity.
_SUBJECT_CATEGORIES = {
    "yard": "external_yard",
    "dock": "dock",
    "gf core": "ground_floor",
    "gf cores": "ground_floor",
    "channel": "channel",
    "transition": "transition",
}
_UPPER_FLOOR_SUBJECT_RX = re.compile(r"^\d+(?:st|nd|rd|th)\s+floor$", re.I)


def _normalise_subject(value):
    return " ".join(str(value or "").strip().casefold().split())


def _subject_category(subject):
    probe = _normalise_subject(subject)
    if probe in _SUBJECT_CATEGORIES:
        return _SUBJECT_CATEGORIES[probe]
    if _UPPER_FLOOR_SUBJECT_RX.fullmatch(probe):
        return "upper_floor"
    return "unclassified"


def _parse_length_lm(content):
    """Read a Bluebeam length label and return metres, without using drawing scale.

    Area labels may contain other numeric text, so only an explicit linear unit is accepted.
    Square/cubic units cannot match because the unit must immediately follow the number.
    """
    if not content:
        return None
    match = _LENGTH_RX.search(str(content))
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    unit = match.group(2).casefold()
    if unit == "km":
        return value * 1000.0
    if unit == "cm":
        return value / 100.0
    if unit == "mm":
        return value / 1000.0
    if unit in ("ft", "feet"):
        return value * 0.3048
    if unit.startswith("in"):
        return value * 0.0254
    return value


def _pdf_numbers(value):
    """Numbers from a PDF array returned by xref_get_key()."""
    return [float(number) for number in re.findall(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", value or ""
    )]


def _ring_perimeter(points):
    if len(points) < 3:
        return 0.0
    return sum(
        math.dist(points[index], points[(index + 1) % len(points)])
        for index in range(len(points))
    )


def _measure_length_factor_m(doc, annot_xref):
    """Return metres per PDF coordinate unit from Bluebeam's /Measure /X format.

    In the supplied files /X's /C is the displayed linear unit per PDF point (for example
    .03527778 m/pt for a 1 cm = 1 m viewport). Reading the embedded calibration is more
    reliable than attempting to infer scale from the page or from the rounded label.
    """
    kind, measure_value = doc.xref_get_key(annot_xref, "Measure")
    if kind != "xref":
        return None
    match = re.match(r"\s*(\d+)\s+0\s+R", measure_value or "")
    if not match:
        return None
    measure_xref = int(match.group(1))
    x_kind, x_value = doc.xref_get_key(measure_xref, "X")
    if x_kind != "array":
        return None
    conversion = re.search(
        r"/U\s*(?:\(([^)]*)\)|/([^\s/<>{}\[\]()]+)).*?/C\s*"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
        x_value or "", re.S,
    )
    if not conversion:
        return None
    unit = (conversion.group(1) or conversion.group(2) or "").strip().casefold()
    factor = float(conversion.group(3))
    unit_to_m = {
        "m": 1.0, "mm": 0.001, "cm": 0.01, "km": 1000.0,
        "ft": 0.3048, "feet": 0.3048, "in": 0.0254,
    }.get(unit)
    return factor * unit_to_m if unit_to_m is not None else None


def _polygon_perimeter_lm(doc, annot_xref):
    """Bluebeam polygon boundary length, including every /Cutouts inner ring.

    The label's area is already NET of /Cutouts; for formwork the relevant boundary is the
    outer ring plus each void edge. PyMuPDF exposes only the outer /Vertices reliably here,
    so the raw nested /Cutouts array is parsed from the annotation xref.
    """
    factor = _measure_length_factor_m(doc, annot_xref)
    if factor is None:
        return None, 0

    vertices_kind, vertices_value = doc.xref_get_key(annot_xref, "Vertices")
    if vertices_kind != "array":
        return None, 0
    values = _pdf_numbers(vertices_value)
    outer = list(zip(values[::2], values[1::2]))
    perimeter = _ring_perimeter(outer)

    cutout_count = 0
    cutouts_kind, cutouts_value = doc.xref_get_key(annot_xref, "Cutouts")
    if cutouts_kind == "array":
        # /Cutouts is an array of flat coordinate arrays: [[x y ...] [x y ...]].
        for ring_value in re.findall(r"\[([^\[\]]+)\]", cutouts_value or ""):
            ring_numbers = _pdf_numbers(ring_value)
            ring = list(zip(ring_numbers[::2], ring_numbers[1::2]))
            if len(ring) >= 3:
                perimeter += _ring_perimeter(ring)
                cutout_count += 1
    return perimeter * factor, cutout_count


def _json_color(value):
    return [round(float(component), 6) for component in (value or [])] or None


def _measurement_kind(area_m2, length_lm, perimeter_lm):
    if area_m2 is not None:
        return "area"
    if length_lm is not None:
        return "length"
    if perimeter_lm is not None:
        return "perimeter"
    return "unparsed"


def read_marked_zones(pdf):
    """Read exact Bluebeam quantities plus their evidence-backed slab-zone identity.

    Returns a JSON-safe dict with the legacy aggregate and detailed provenance::

      {"area_m2", "regions", "markup_annotations", "zones", "flags"}

    ``markup_annotations`` retains one record per annotation. ``zones`` groups known
    subjects by canonical category; distinct unknown subjects get distinct unclassified
    groups. Legend stamps remain in the evidence list with ``ignored=True`` but never become
    a zone. No title-block or hardcoded drawing scale is used.
    """
    doc = fitz.open(pdf)
    page = doc[0]
    records = []
    grouped = OrderedDict()
    flags = []
    aggregate_areas = []

    for index, annot in enumerate(page.annots() or []):
        info = annot.info or {}
        annot_type = annot.type[1]
        subject = str(info.get("subject") or "").strip()
        subject_key = _normalise_subject(subject)
        content = str(info.get("content") or "")
        author = str(info.get("title") or "")
        ignored = subject_key == "legend"

        # Preserve read_marked's exact contract: only a Polygon with an explicit area label
        # contributes to aggregate area / region count.
        area_m2 = parse_area_m2(content) if annot_type == "Polygon" else None
        if area_m2 is not None:
            aggregate_areas.append(area_m2)

        length_lm = (_parse_length_lm(content)
                     if annot_type in ("Line", "PolyLine", "Polygon") else None)
        perimeter_lm = None
        cutout_count = 0
        if annot_type == "Polygon" and area_m2 is not None:
            # A malformed/non-Bluebeam /Measure dictionary must not discard a valid stored
            # area label. Perimeter is additive evidence, whereas area is the legacy contract.
            try:
                perimeter_lm, cutout_count = _polygon_perimeter_lm(doc, annot.xref)
            except (RuntimeError, TypeError, ValueError, IndexError):
                perimeter_lm, cutout_count = None, 0

        category = "other" if ignored else _subject_category(subject)
        measurable_shape = annot_type in ("Polygon", "PolyLine", "Line")
        if ignored:
            zone_key = None
        elif category == "unclassified":
            zone_key = f"unclassified:{subject_key or annot_type.casefold()}"
        else:
            zone_key = category

        colors = annot.colors or {}
        record = {
            "page": 0,
            "index": index,
            "xref": annot.xref,
            "type": annot_type,
            "subject": subject,
            "title": author,
            "author": author,
            "content": content,
            "stroke_color": _json_color(colors.get("stroke")),
            "fill_color": _json_color(colors.get("fill")),
            "category": category,
            "zone_key": zone_key,
            "classification_source": "subject" if not ignored else "ignored legend metadata",
            "measurement_kind": _measurement_kind(area_m2, length_lm, perimeter_lm),
            "area_m2": round(area_m2, 6) if area_m2 is not None else None,
            "length_lm": round(length_lm, 6) if length_lm is not None else None,
            "perimeter_lm": round(perimeter_lm, 6) if perimeter_lm is not None else None,
            "cutout_count": cutout_count,
            "ignored": ignored,
        }
        records.append(record)

        if ignored or not measurable_shape:
            continue

        if category == "unclassified":
            label = subject or f"unlabelled {annot_type}"
            flag = f"assessor: classify zone '{label}'"
            if flag not in flags:
                flags.append(flag)

        zone = grouped.setdefault(zone_key, {
            "zone_key": zone_key,
            "category": category,
            "subjects": [],
            "measurement_kind": None,
            "area_m2": None,
            "length_lm": None,
            "perimeter_lm": None,
            "annotation_count": 0,
            "cutout_count": 0,
            "classification_source": "subject",
            "needs_assessor": category == "unclassified",
        })
        if subject and subject not in zone["subjects"]:
            zone["subjects"].append(subject)
        zone["annotation_count"] += 1
        zone["cutout_count"] += cutout_count
        if area_m2 is not None:
            zone["area_m2"] = (zone["area_m2"] or 0.0) + area_m2
        if length_lm is not None:
            zone["length_lm"] = (zone["length_lm"] or 0.0) + length_lm
        if perimeter_lm is not None:
            zone["perimeter_lm"] = (zone["perimeter_lm"] or 0.0) + perimeter_lm

    zones = []
    for zone in grouped.values():
        area = zone["area_m2"]
        length = zone["length_lm"]
        perimeter = zone["perimeter_lm"]
        zone["area_m2"] = round(area, 2) if area is not None else None
        zone["length_lm"] = round(length, 2) if length is not None else None
        zone["perimeter_lm"] = round(perimeter, 2) if perimeter is not None else None
        zone["measurement_kind"] = _measurement_kind(
            zone["area_m2"], zone["length_lm"], zone["perimeter_lm"])
        zones.append(zone)

    result = {
        "area_m2": round(sum(aggregate_areas), 1),
        "regions": len(aggregate_areas),
        "markup_annotations": records,
        "zones": zones,
        "flags": flags,
    }
    doc.close()
    return result


def read_marked(pdf):
    """MARKED path: sum the Bluebeam area markups (exact, multi-region, NO scale needed).
    Uses robust label parsing (m²/Area=/imperial sq ft)."""
    marked = read_marked_zones(pdf)
    return marked["area_m2"], marked["regions"]


def count_manholes_marked(pdf, page=0):
    """MARKED path: count manhole markers Fortel placed on the drawing.

    Convention: a manhole is annotated as a Bluebeam Circle annot (small circle/count
    marker dropped at each manhole location) — the same convention already assumed for
    gold.json's manhole_count/marker_count entries. We deliberately do NOT require any
    particular label text on the annot (real Fortel markup sometimes just drops a bare
    circle stamp per manhole, sometimes labels it "MH"/a number), so this counts every
    Circle-type annot on the page. If a future convention needs filtering (e.g. Circle
    annots used for something else too), narrow this by content/colour then.

    Returns int count (0 = CONFIRMED zero Circle annots), or None when the file/page could
    not be opened — "couldn't check" must never masquerade as a confirmed zero (four-state:
    no silent numbers, and a silent 0 is still a number)."""
    try:
        p = fitz.open(pdf)[page]
    except Exception:
        return None
    return sum(1 for a in (p.annots() or []) if a.type[1] == "Circle")


def calibrate_scale(pdf):
    """Return (k_m_per_pt, source, verified?). Title-block scale is UNVERIFIED — flag it."""
    p = fitz.open(pdf)[0]
    m = re.search(r"1\s*:\s*(\d{2,4})", p.get_text())
    if m:
        s = int(m.group(1))
        return 0.0254 / 72 * s, f"title-block 1:{s}", False   # unverified — placement may differ
    return None, "none", False


def measure_vision(vertices, k):
    """VISION path: area of an LLM-identified polygon. k MUST be supplied (no hardcode)."""
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension)")
    return round(Polygon(vertices).area * k * k, 1)


if __name__ == "__main__":
    cases = {"Yard": ("Yard Area Proposed_Site_Plan.pdf", 26080),
             "Dock": ("Dock Slab Area Proposed_Site_Plan.pdf", 930),
             "Office": ("Area Office Floors Proposed_GA_Office_Plan_ref_S2_P01.pdf", 3479),
             "Transport": ("Area Hub Office Proposed_Transport_Office_ref_S2_P01.pdf", 729)}
    print("MARKED path (sum stored areas — multi-region, exact, no scale):")
    print(f"  {'drawing':10}{'regions':>8}{'measured':>10}{'gold':>8}{'err':>7}")
    ok = 0
    for name, (fn, gold) in cases.items():
        a, n = read_marked("drawings/" + fn)
        err = abs(a - gold) / gold * 100
        ok += err < 1
        print(f"  {name:10}{n:>8}{a:>10,.0f}{gold:>8,}{err:>6.1f}%")
    print(f"  => {ok}/4 exact (multi-region break fixed; scale not needed on this path)")
    print("\nVISION path scale calibration (the hard part):")
    for name, (fn, _) in cases.items():
        k, src, ver = calibrate_scale("drawings/" + fn)
        print(f"  {name:10} k={k:.4f} from {src:18} verified={ver}  -> FLAG: assessor confirms scale")
