#!/usr/bin/env python3
"""
Vector takeoff engine — measure slab quantities from a CAD-exported PDF.

Design principle: the LLM/vision layer SELECTS regions (which polygon is the yard,
which is a void to exclude); this deterministic engine MEASURES them. The model
never guesses a number. Here the selection is a rule-based stub — the hook where
Claude-vision plugs in for real, handbook-driven zone classification.
"""
import sys, re, json
import fitz  # PyMuPDF
from shapely.geometry import Polygon


def paper_pt_to_real_m(pt, scale):        # paper points -> real metres
    return pt * (0.0254 / 72) * scale


def paper_area_to_real_m2(area_pt2, scale):
    return area_pt2 * ((0.0254 / 72) * scale) ** 2


def detect_scale(page):
    """Calibrate from a '1:NNN' label. Real version also reads a scale bar / grid dim."""
    t = page.get_text()
    m = re.search(r'1\s*:\s*(\d{2,4})', t)
    if m:
        return float(m.group(1)), f'text "1:{m.group(1)}"'
    return None, 'undetected'


def _poly(items):
    pts = []
    for it in items:
        op = it[0]
        if op == 'l':
            pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
        elif op == 're':
            r = it[1]; pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif op == 'qu':
            q = it[1]; pts += [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
        elif op == 'c':
            pts += [(it[1].x, it[1].y), (it[4].x, it[4].y)]
    if len(pts) < 3:
        return None
    try:
        p = Polygon(pts)
        if not p.is_valid:
            p = p.buffer(0)
        return p if p.area > 0 else None
    except Exception:
        return None


def takeoff(pdf_path, scale_override=None, marker_max_m2=5.0, zone="slab"):
    doc = fitz.open(pdf_path)
    page = doc[0]
    det, src = detect_scale(page)
    scale = scale_override or det
    flags = []
    if scale is None:
        scale, src = 100.0, 'DEFAULT'
        flags.append("scale undetected — defaulted 1:100, NEEDS HUMAN CHECK")
    if scale_override and det and abs(scale_override - det) / det > 0.01:
        flags.append(f"scale override 1:{int(scale_override)} != detected 1:{int(det)}")

    # Reconstruct polygons from vector paths, dedup by bounding box (fill+stroke pairs).
    raw = [p for d in page.get_drawings() if (p := _poly(d['items']))]
    seen, polys = set(), []
    for p in raw:
        k = tuple(round(v, 1) for v in p.bounds)
        if k in seen:
            continue
        seen.add(k); polys.append(p)

    if not polys:
        return {"pdf": pdf_path.split('/')[-1], "error": "no vector geometry",
                "flags": ["RASTER/scanned — route to CV + human review"]}

    polys.sort(key=lambda p: -p.area)
    main = polys[0]
    gross = paper_area_to_real_m2(main.area, scale)

    voids, markers = [], 0
    for p in polys[1:]:
        a = paper_area_to_real_m2(p.area, scale)
        if a < marker_max_m2:
            if main.contains(p.centroid):
                markers += 1
        elif a < 0.4 * gross and main.contains(p.centroid):
            voids.append(a)

    void = sum(voids)
    return {
        "pdf": pdf_path.split('/')[-1], "zone": zone,
        "scale": f"1:{int(scale)}", "scale_src": src,
        "gross_m2": round(gross, 1), "void_m2": round(void, 1),
        "net_m2": round(gross - void, 1),
        "perimeter_lm": round(paper_pt_to_real_m(main.exterior.length, scale), 1),
        "marker_count": markers,
        "confidence": 0.9 if not flags else 0.5,
        "flags": flags,
    }


if __name__ == "__main__":
    a = sys.argv[1:]
    sc = float(a[1]) if len(a) > 1 else None
    print(json.dumps(takeoff(a[0], scale_override=sc), indent=2))
