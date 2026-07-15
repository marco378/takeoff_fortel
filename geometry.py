#!/usr/bin/env python3
"""
Robust takeoff geometry — hardened against adversarial failures found in testing:
  - self-intersecting LLM trace      -> make_valid (deterministic) + flag for IoU re-check
  - slab with voids (SOP: omit voids)-> subtract void rings (Polygon holes)
  - overlapping multi-region slabs   -> union, not sum (no double-count)
  - missing scale                    -> raise (never hardcode / never silently guess)
  - hole outside outer ring          -> difference() per-hole (Polygon constructor + make_valid
                                        silently adds area when hole lies outside the ring)
"""
import math
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
from shapely.ops import unary_union
from shapely.validation import make_valid


def polygon_perimeter_lm(vertices, metres_per_unit):
    """Closed polygon perimeter in metres for coordinates measured in one linear unit.

    Pipeline ``polygon_pts`` are PDF points and use ``scale_k`` metres/PDF-point;
    assessor-adjusted vertices are canvas pixels and use metres/canvas-pixel.  The same
    first-power conversion is therefore correct for both coordinate spaces.
    """
    try:
        scale = float(metres_per_unit)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(scale) or scale <= 0:
        return None
    if not vertices or len(vertices) < 3:
        return None
    try:
        points = [(float(x), float(y)) for x, y in vertices]
    except (TypeError, ValueError):
        return None
    if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in points):
        return None
    length = sum(math.dist(points[i], points[(i + 1) % len(points)])
                 for i in range(len(points)))
    return round(length * scale, 1)


def _build_region(outer_verts, hole_verts_list, idx, flags):
    """Build a single region polygon with holes correctly subtracted.

    IMPORTANT: we do NOT pass holes to the Polygon() constructor, because when a hole
    lies outside the outer ring Shapely marks the polygon as invalid, and make_valid()
    then turns BOTH the outer ring and the escaped hole into separate filled polygons —
    adding area instead of subtracting it.  Instead we build the outer ring first,
    validate it, then subtract each hole individually via .difference(), which is safe
    regardless of whether the hole is inside, partially outside, or entirely outside.
    """
    if len(outer_verts) < 3:
        flags.append(f"region {idx}: <3 vertices — skipped (degenerate trace)")
        return None
    if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in outer_verts):
        flags.append(f"region {idx}: non-finite coord (NaN/Inf) — skipped (bad trace)")
        return None
    p = Polygon(outer_verts)
    if not p.is_valid:
        p = make_valid(p)
        flags.append(f"region {idx}: invalid trace (self-intersection) repaired — verify by IoU")
    if p.is_empty:
        flags.append(f"region {idx}: invalid outer trace — skipped")
        return None
    if p.area < 1:
        flags.append(f"region {idx}: near-zero area — likely a sliver/bad trace; flag for re-trace")
    # Subtract each void individually via difference(); this is safe whether the hole is
    # inside, partially overlapping, or entirely outside the outer ring.
    for h in hole_verts_list:
        if len(h) < 3:
            continue
        hp = Polygon(h)
        if not hp.is_valid:
            hp = make_valid(hp)
        if not hp.is_empty:
            p = p.difference(hp)
    if not p.is_valid:
        p = make_valid(p)
    return p


def measure_regions(regions, k, holes=None):
    """regions: list of outer vertex-lists. holes: {region_index: [void_vertex_list, ...]}.
    Returns (area_m2, flags). k = per-viewport scale (m/pt); REQUIRED."""
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension)")
    holes = holes or {}
    flags = []
    polys = []
    for i, v in enumerate(regions):
        p = _build_region(v, holes.get(i, []), i, flags)
        if p is not None:
            polys.append(p)
    if not polys:
        return 0.0, flags + ["no valid regions"]
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
    # E: hole outside outer ring (bug fix check)
    outer_e = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    outside_hole = [(2000, 2000), (3000, 2000), (3000, 3000), (2000, 3000)]
    a, f = measure_regions([outer_e], K, holes={0: [outside_hole]})
    print(f"E hole-outside:   {a:,.0f} m2 (true 10,000 — hole is outside ring)  flags={f}")
