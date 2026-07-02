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
import fitz, re
from shapely.geometry import Polygon
from markup import parse_area_m2


def read_marked(pdf):
    """MARKED path: sum the Bluebeam area markups (exact, multi-region, NO scale needed).
    Uses robust label parsing (m²/Area=/imperial sq ft)."""
    p = fitz.open(pdf)[0]
    areas = []
    for a in (p.annots() or []):
        if a.type[1] == "Polygon":
            v = parse_area_m2(a.info.get("content", "") or "")
            if v is not None:
                areas.append(v)
    return round(sum(areas), 1), len(areas)


def count_manholes_marked(pdf, page=0):
    """MARKED path: count manhole markers Fortel placed on the drawing.

    Convention: a manhole is annotated as a Bluebeam Circle annot (small circle/count
    marker dropped at each manhole location) — the same convention already assumed for
    gold.json's manhole_count/marker_count entries. We deliberately do NOT require any
    particular label text on the annot (real Fortel markup sometimes just drops a bare
    circle stamp per manhole, sometimes labels it "MH"/a number), so this counts every
    Circle-type annot on the page. If a future convention needs filtering (e.g. Circle
    annots used for something else too), narrow this by content/colour then.

    Returns int count (0 if none / no annots / page has no Circle annots)."""
    try:
        p = fitz.open(pdf)[page]
    except Exception:
        return 0
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
