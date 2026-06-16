#!/usr/bin/env python3
"""
Robust takeoff geometry — hardened against adversarial failures found in testing:
  - self-intersecting LLM trace      -> make_valid (deterministic) + flag for IoU re-check
  - slab with voids (SOP: omit voids)-> subtract void rings (Polygon holes)
  - overlapping multi-region slabs   -> union, not sum (no double-count)
  - missing scale                    -> raise (never hardcode / never silently guess)
"""
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
from shapely.ops import unary_union
from shapely.validation import make_valid


def _valid(poly, idx, flags):
    if not poly.is_valid:
        poly = make_valid(poly)
        flags.append(f"region {idx}: invalid trace (self-intersection) repaired — verify by IoU")
    return poly


def measure_regions(regions, k, holes=None):
    """regions: list of outer vertex-lists. holes: {region_index: [void_vertex_list, ...]}.
    Returns (area_m2, flags). k = per-viewport scale (m/pt); REQUIRED."""
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension)")
    holes = holes or {}
    flags = []
    polys = [_valid(Polygon(v, holes.get(i, [])), i, flags) for i, v in enumerate(regions)]
    u = unary_union(polys)
    naive = sum(p.area for p in polys)
    if u.area < naive * 0.999:
        flags.append(f"regions overlap — used union {u.area*k*k:,.0f} m2, not sum {naive*k*k:,.0f} m2")
    return round(u.area * k * k, 1), flags


if __name__ == "__main__":
    K = 0.1
    # A: voids
    outer = [(0, 0), (2000, 0), (2000, 1300), (0, 1300)]
    v1 = [(200, 200), (600, 200), (600, 500), (200, 500)]
    v2 = [(1400, 800), (1700, 800), (1700, 1100), (1400, 1100)]
    a, f = measure_regions([outer], K, holes={0: [v1, v2]})
    print(f"A voids:          {a:,.0f} m2 (true 23,900)  flags={f}")
    # B self-intersection
    a, f = measure_regions([[(0, 0), (1000, 1000), (1000, 0), (0, 1000)]], K)
    print(f"B self-intersect: {a:,.0f} m2  flags={f}")
    # C overlap (two rects overlapping by 500x500 pt)
    r1 = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    r2 = [(500, 500), (1500, 500), (1500, 1500), (500, 1500)]
    a, f = measure_regions([r1, r2], K)
    print(f"C overlap:        {a:,.0f} m2 (true 17,500 not 20,000)  flags={f}")
    # D clean multi-region (no overlap) — dock-style 4 slabs
    quads = [[(x, 0), (x + 400, 0), (x + 400, 400), (x, 400)] for x in (0, 600, 1200, 1800)]
    a, f = measure_regions(quads, K)
    print(f"D 4 clean slabs:  {a:,.0f} m2 (true 6,400)  flags={f}")
