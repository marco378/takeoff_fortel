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


def measure_regions_with_cutouts(regions, cutouts, k):
    """Measure regions with cutouts using proper geometric subtraction.
    
    The canonical calculation:
        measured_geometry = union(all measured regions)
        cutout_geometry = union(all cutout polygons)
        removed_geometry = measured_geometry.intersection(cutout_geometry)
        net_geometry = measured_geometry.difference(cutout_geometry)
        
        gross_area = measured_geometry.area
        cutout_area = removed_geometry.area
        net_area = net_geometry.area
    
    Args:
        regions: list of outer vertex-lists (each is [[x,y], ...])
        cutouts: list of cutout vertex-lists (each is [[x,y], ...])
        k: per-viewport scale (m/pt); REQUIRED
        
    Returns:
        (gross_area_m2, removed_area_m2, net_area_m2, flags)
    """
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension)")
    
    flags = []
    
    # Build region polygons
    region_polys = []
    for i, v in enumerate(regions):
        if len(v) < 3:
            flags.append(f"region {i}: <3 vertices — skipped (degenerate trace)")
            continue
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in v):
            flags.append(f"region {i}: non-finite coord (NaN/Inf) — skipped (bad trace)")
            continue
        p = Polygon(v)
        if not p.is_valid:
            p = make_valid(p)
            flags.append(f"region {i}: invalid trace (self-intersection) repaired — verify by IoU")
        if p.is_empty:
            flags.append(f"region {i}: invalid outer trace — skipped")
            continue
        if p.area < 1:
            flags.append(f"region {i}: near-zero area — likely a sliver/bad trace; flag for re-trace")
        region_polys.append(p)
    
    # Build cutout polygons
    cutout_polys = []
    for i, v in enumerate(cutouts):
        if len(v) < 3:
            continue
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in v):
            continue
        p = Polygon(v)
        if not p.is_valid:
            p = make_valid(p)
        if not p.is_empty:
            cutout_polys.append(p)
    
    # Handle empty cases
    if not region_polys:
        return 0.0, 0.0, 0.0, flags + ["no valid regions"]
    
    if not cutout_polys:
        # No cutouts — just measure regions
        measured = unary_union(region_polys)
        gross_m2 = round(measured.area * k * k, 1)
        return gross_m2, 0.0, gross_m2, flags
    
    # Union all regions and all cutouts
    measured_geometry = unary_union(region_polys)
    cutout_geometry = unary_union(cutout_polys)
    
    # Compute intersection (what actually gets removed)
    removed_geometry = measured_geometry.intersection(cutout_geometry)
    
    # Compute net (what remains)
    net_geometry = measured_geometry.difference(cutout_geometry)
    
    # Convert to area in m2
    gross_m2 = round(measured_geometry.area * k * k, 1)
    removed_m2 = round(removed_geometry.area * k * k, 1)
    net_m2 = round(net_geometry.area * k * k, 1)
    
    # Add informative flags
    naive_cutout_m2 = round(sum(p.area for p in cutout_polys) * k * k, 1)
    if removed_m2 < naive_cutout_m2 * 0.999:
        flags.append(
            f"cutouts partially outside measured region — removed {removed_m2:,.1f} m2, "
            f"not full {naive_cutout_m2:,.1f} m2 polygon area"
        )
    
    if len(cutout_polys) > 1:
        # Check for overlapping cutouts
        cutout_union_m2 = round(cutout_geometry.area * k * k, 1)
        if cutout_union_m2 < naive_cutout_m2 * 0.999:
            flags.append(
                f"overlapping cutouts — union area {cutout_union_m2:,.1f} m2, "
                f"not sum {naive_cutout_m2:,.1f} m2"
            )
    
    return gross_m2, removed_m2, net_m2, flags


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
    
    # Tests for measure_regions_with_cutouts
    print("\n--- Tests for measure_regions_with_cutouts ---")
    
    # Test 1: Normal internal cutout
    # Region = 200 m2, Cutout = 10 m2, Expected net = 190 m2
    # At k=0.1 m/pt, 1 sq pt = 0.01 m2
    # So 200 m2 = 20,000 sq pt
    region_200 = [(0, 0), (1000, 0), (1000, 20), (0, 20)]  # 1000x20 = 20,000 sq pt = 200 m2
    cutout_10 = [(100, 5), (200, 5), (200, 10), (100, 10)]  # 100x5 = 500 sq pt = 5 m2
    # Actually let me recalculate: 100x5 = 500 sq pt * 0.01 = 5 m2
    # Let me use: 200x5 = 1000 sq pt = 10 m2
    cutout_10 = [(100, 5), (300, 5), (300, 10), (100, 10)]  # 200x5 = 1000 sq pt = 10 m2
    gross, removed, net, flags = measure_regions_with_cutouts([region_200], [cutout_10], K)
    print(f"Test 1 (internal):    gross={gross}, removed={removed}, net={net} (expected: 200, 10, 190)")
    assert abs(gross - 200.0) < 0.1, f"Test 1 failed: gross={gross}"
    assert abs(removed - 10.0) < 0.1, f"Test 1 failed: removed={removed}"
    assert abs(net - 190.0) < 0.1, f"Test 1 failed: net={net}"
    
    # Test 2: Cutout partially outside
    # Region = 200 m2, Cutout polygon = 28 m2, Intersection = 7 m2, Expected net = 193 m2
    cutout_partial = [(-50, 5), (200, 5), (200, 12), (-50, 12)]  # 250x7 = 1750 sq pt = 17.5 m2
    # Intersection with region: 200x7 = 1400 sq pt = 14 m2
    # Actually let me recalculate to get 7 m2 intersection
    # 7 m2 = 700 sq pt = 200x3.5
    cutout_partial = [(-50, 5), (200, 5), (200, 8.5), (-50, 8.5)]  # 250x3.5 = 875 sq pt = 8.75 m2
    # Intersection: 200x3.5 = 700 sq pt = 7 m2
    gross, removed, net, flags = measure_regions_with_cutouts([region_200], [cutout_partial], K)
    print(f"Test 2 (partial):     gross={gross}, removed={removed}, net={net} (expected: 200, 7, 193)")
    assert abs(gross - 200.0) < 0.1, f"Test 2 failed: gross={gross}"
    assert abs(removed - 7.0) < 0.5, f"Test 2 failed: removed={removed}"
    assert abs(net - 193.0) < 0.5, f"Test 2 failed: net={net}"
    
    # Test 3: Cutout completely outside
    # Region = 200 m2, Cutout = 20 m2, Intersection = 0, Expected net = 200 m2
    cutout_outside = [(3000, 3000), (4000, 3000), (4000, 4000), (3000, 4000)]
    gross, removed, net, flags = measure_regions_with_cutouts([region_200], [cutout_outside], K)
    print(f"Test 3 (outside):     gross={gross}, removed={removed}, net={net} (expected: 200, 0, 200)")
    assert abs(gross - 200.0) < 0.1, f"Test 3 failed: gross={gross}"
    assert abs(removed - 0.0) < 0.1, f"Test 3 failed: removed={removed}"
    assert abs(net - 200.0) < 0.1, f"Test 3 failed: net={net}"
    
    # Test 4: Two overlapping cutouts
    # A = 10 m2, B = 10 m2, Overlap = 2.5 m2, Expected removed = 17.5 m2
    cutout_a = [(100, 5), (300, 5), (300, 10), (100, 10)]  # 200x5 = 1000 sq pt = 10 m2
    cutout_b = [(200, 7.5), (400, 7.5), (400, 12.5), (200, 12.5)]  # 200x5 = 1000 sq pt = 10 m2
    # Overlap: 100x2.5 = 250 sq pt = 2.5 m2
    gross, removed, net, flags = measure_regions_with_cutouts([region_200], [cutout_a, cutout_b], K)
    print(f"Test 4 (overlapping): gross={gross}, removed={removed}, net={net} (expected: 200, 17.5, 182.5)")
    print(f"  Flags: {flags}")
    assert abs(gross - 200.0) < 0.1, f"Test 4 failed: gross={gross}"
    assert abs(removed - 17.5) < 0.5, f"Test 4 failed: removed={removed}"
    assert abs(net - 182.5) < 0.5, f"Test 4 failed: net={net}"
    
    # Test 5: Multiple measured regions
    # Region A = 100 m2, Region B = 100 m2, Cutout intersects A by 10 m2
    region_a = [(0, 0), (500, 0), (500, 20), (0, 20)]  # 500x20 = 10,000 sq pt = 100 m2
    region_b = [(750, 0), (1250, 0), (1250, 20), (750, 20)]  # 500x20 = 10,000 sq pt = 100 m2
    cutout_in_a = [(50, 5), (150, 5), (150, 10), (50, 10)]  # 100x5 = 500 sq pt = 5 m2
    # Actually I want 10 m2 = 1000 sq pt = 200x5
    cutout_in_a = [(50, 5), (250, 5), (250, 10), (50, 10)]  # 200x5 = 1000 sq pt = 10 m2
    gross, removed, net, flags = measure_regions_with_cutouts([region_a, region_b], [cutout_in_a], K)
    print(f"Test 5 (multi-region): gross={gross}, removed={removed}, net={net} (expected: 200, 10, 190)")
    assert abs(gross - 200.0) < 0.1, f"Test 5 failed: gross={gross}"
    assert abs(removed - 10.0) < 0.1, f"Test 5 failed: removed={removed}"
    assert abs(net - 190.0) < 0.1, f"Test 5 failed: net={net}"
    
    # Test 6: Cutout intersects multiple regions (should not double-count)
    region_c = [(0, 0), (500, 0), (500, 20), (0, 20)]  # 100 m2
    region_d = [(400, 0), (900, 0), (900, 20), (400, 20)]  # 100 m2
    # Regions overlap by 100x20 = 2000 sq pt = 20 m2, so gross = 180 m2
    # Cutout spans both regions: 300x10 = 3000 sq pt = 30 m2
    cutout_spanning = [(350, 5), (650, 5), (650, 15), (350, 15)]
    gross, removed, net, flags = measure_regions_with_cutouts([region_c, region_d], [cutout_spanning], K)
    print(f"Test 6 (spanning):    gross={gross}, removed={removed}, net={net} (expected: 180, 30, 150)")
    assert abs(gross - 180.0) < 0.1, f"Test 6 failed: gross={gross}"
    assert abs(removed - 30.0) < 0.5, f"Test 6 failed: removed={removed}"
    assert abs(net - (gross - removed)) < 0.1, f"Test 6 failed: net != gross - removed"
    
    # Test 7: Multiple overlapping measured regions
    region_e = [(0, 0), (600, 0), (600, 20), (0, 20)]  # 600x20 = 12,000 sq pt = 120 m2
    region_f = [(400, 0), (1000, 0), (1000, 20), (400, 20)]  # 600x20 = 12,000 sq pt = 120 m2
    # Overlap: 200x20 = 4000 sq pt = 40 m2, so gross = 200 m2
    cutout_in_overlap = [(450, 5), (550, 5), (550, 15), (450, 15)]  # 100x10 = 1000 sq pt = 10 m2
    gross, removed, net, flags = measure_regions_with_cutouts([region_e, region_f], [cutout_in_overlap], K)
    print(f"Test 7 (overlap+cutout): gross={gross}, removed={removed}, net={net} (expected: 200, 10, 190)")
    assert abs(gross - 200.0) < 0.1, f"Test 7 failed: gross={gross}"
    assert abs(removed - 10.0) < 0.1, f"Test 7 failed: removed={removed}"
    assert abs(net - 190.0) < 0.1, f"Test 7 failed: net={net}"
    
    print("\nAll tests passed!")
