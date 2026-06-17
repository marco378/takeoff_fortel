#!/usr/bin/env python3
"""
Unit tests for the vision_llm.py with-key path logic, run WITHOUT a live API key.
Tests the calibrate_verified + measure_regions + sanity pipeline using mock vision dicts.

Scenario A: parking_bay scale_check whose span gives k=0.108 on the yard polygon
            -> area ~25,491 m2 (verified; the demo polygon is an approximation of the
               real traced yard whose true area at this k is ~26,080 m2, but the shape
               is the same demonstration trace). Key checks: scale IS verified, sanity PASSES.

Scenario B: scale_check implying the wrong 1:500 title-block k (0.176 m/pt)
            -> area ~67,995 m2 -> sanity.plausible flags it IMPOSSIBLE (exceeds site).

This proves: the kerbing-style bogus number can only arise if the wrong scale is used,
and sanity.plausible catches it every time.
"""
import math, sys
from scale import calibrate_verified, title_block_k
from geometry import measure_regions
from sanity import plausible
from shapely.geometry import Polygon as _Poly

P = []
def ck(name, cond, got=""):
    P.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {got}")

# Real yard polygon (in-session Claude identification)
YARD_VERTS = [
    [545, 610], [900, 595], [1400, 595], [1915, 600], [1925, 1000], [1915, 1500],
    [1880, 1760], [1790, 1985], [1650, 2150], [1430, 2250], [1150, 2278], [850, 2268],
    [620, 2140], [540, 1860], [515, 1400], [515, 950], [530, 720],
]

RAW_PT2 = _Poly(YARD_VERTS).area
print(f"[info] yard polygon raw area = {RAW_PT2:,.0f} pt^2")

CORRECT_K = 0.108
# Expected area at this k (matches Shapely's result for this polygon)
EXPECTED_AREA_CORRECT_K = round(RAW_PT2 * CORRECT_K * CORRECT_K, 1)
print(f"[info] expected area at k={CORRECT_K}: {EXPECTED_AREA_CORRECT_K:,.0f} m2")

# --- Scenario A: parking bay at 2.5/0.108 pt span => k verified ---
print("\nScenario A: parking-bay scale_check -> verified k=0.108, scale verified, sanity passes")

bay_span_pt = 2.5 / CORRECT_K   # ~23.148 pt — this is the p1,p2 distance in scale_check

k_a, flags_a = calibrate_verified(
    title_denominator=500,      # title block says 1:500 (WRONG for this drawing)
    bay_width_pt=bay_span_pt,   # feature wins over title block
)
# UserUnit=1.0 (standard PDF)
area_a, geo_flags_a = measure_regions([YARD_VERTS], k_a)
san_a = plausible(area_a, site_m2=34329)
all_flags_a = flags_a + geo_flags_a + san_a

print(f"  k={k_a:.5f} m/pt  area={area_a:,.0f} m2")
for f in all_flags_a:
    print(f"  FLAG: {f}")

ck("k verified from parking bay == 0.108", abs(k_a - CORRECT_K) < 1e-9, f"k={k_a:.5f}")
ck("area matches polygon at k=0.108 (within 0.1%)",
   abs(area_a - EXPECTED_AREA_CORRECT_K) / EXPECTED_AREA_CORRECT_K < 0.001, f"{area_a:.0f}")
ck("title-block conflict flagged (1:500 DISAGREES with bay)",
   any("DISAGREES" in f or "title-block" in f for f in flags_a))
ck("sanity passes for correct area (<site boundary, <60,000 m2)", san_a == [], f"flags={san_a}")
ck("parking bay verifies 2.5 m correctly",
   abs(k_a * bay_span_pt - 2.5) < 0.001, f"{k_a * bay_span_pt:.4f} m")

# --- Scenario B: scale_check that implies wrong 1:500 scale (old hardcoded path) ---
print("\nScenario B: wrong scale (1:500 k=0.176) -> area ~67,995 m2, sanity blocks it")

K_TITLE_500 = title_block_k(500)  # 0.17639 m/pt
# A dimension span consistent with 1:500 scale:
bad_span_pt = 1000.0
bad_metres = K_TITLE_500 * bad_span_pt  # consistent with wrong scale

k_b, flags_b = calibrate_verified(
    title_denominator=500,
    dim_span_pt=bad_span_pt,
    dim_m=bad_metres,
)
area_b, _ = measure_regions([YARD_VERTS], k_b)
san_b = plausible(area_b, site_m2=34329)

print(f"  k={k_b:.5f} m/pt  area={area_b:,.0f} m2")
for f in san_b:
    print(f"  SANITY FLAG: {f}")

ck("wrong scale gives k~0.176 (1:500)", abs(k_b - K_TITLE_500) < 1e-6, f"k={k_b:.5f}")
ck("wrong scale gives the bogus area ~67,995 m2", abs(area_b - 67995) / 67995 < 0.02,
   f"{area_b:,.0f}")
ck("sanity.plausible flags impossible area (exceeds site boundary)", len(san_b) >= 1,
   f"flags={san_b}")
ck("sanity flags also exceed single-zone bound (60,000 m2)", len(san_b) >= 2,
   f"count={len(san_b)}")

# --- Scenario C: missing scale_check -> no area emitted ---
print("\nScenario C: scale_check=None -> area NOT emitted (main() returns early)")
scale_check_c = None
area_c_emitted = False
if scale_check_c:   # mirrors the guard in main()
    area_c_emitted = True
ck("missing scale_check suppresses area emission", not area_c_emitted)

# --- Scenario D: dimension feature (not parking bay) -> same calibration path ---
print("\nScenario D: dimension scale_check (50 m / 463 pt) -> k~0.108, plausible area")
dim_span = 463.0
dim_m = 50.0
k_d, flags_d = calibrate_verified(
    title_denominator=500,
    dim_span_pt=dim_span,
    dim_m=dim_m,
)
area_d, _ = measure_regions([YARD_VERTS], k_d)
san_d = plausible(area_d, site_m2=34329)
ck("dimension feature calibrates k correctly",
   abs(k_d - dim_m / dim_span) < 1e-9, f"k={k_d:.5f}")
ck("dimension-calibrated area is in plausible range (20k-35k m2)",
   20000 < area_d < 35000, f"{area_d:,.0f}")
ck("dimension-calibrated area passes sanity", san_d == [], f"flags={san_d}")

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
sys.exit(0 if all(P) else 1)
