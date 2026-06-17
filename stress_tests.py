#!/usr/bin/env python3
"""
Adversarial / edge-case stress tests for the concrete takeoff system.
Prints PASS/FAIL per case, exits non-zero on any failure.
"""
import sys, io, contextlib, math

# suppress costing.py self-validation receipt on import
with contextlib.redirect_stdout(io.StringIO()):
    from costing import rate_buildup, MESH_KG
from geometry import measure_regions
from scale import (scale_consensus, title_block_k, scale_from_bay,
                   verify_against_feature, calibrate_verified)
from sanity import plausible
from markup import parse_area_m2
from router import drawing_priority
from pricing import slab_rate, price_boq, price_project

passes = 0
fails = 0

def check(name, cond, detail=""):
    global passes, fails
    if cond:
        passes += 1
        print(f"  [PASS] {name}")
    else:
        fails += 1
        print(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")

# ══════════════════════════════════════════════════════════════════════════════
print("\n── scale: title_block_k ──")

check("title_block_k(0) is None", title_block_k(0) is None)
check("title_block_k(None) is None", title_block_k(None) is None)
k500 = title_block_k(500)
expected_k500 = 500 * (0.0254 / 72)
check("title_block_k(500) value correct",
      k500 is not None and abs(k500 - expected_k500) < 1e-9, f"got {k500}")

print("\n── scale: scale_from_bay ──")
check("scale_from_bay(0) is None", scale_from_bay(0) is None)
check("scale_from_bay(None) is None", scale_from_bay(None) is None)
k_bay = scale_from_bay(100)
check("scale_from_bay(100) == 0.025", k_bay is not None and abs(k_bay - 0.025) < 1e-9, f"got {k_bay}")

print("\n── scale: verify_against_feature ──")
flags = verify_against_feature(None, 100, 2.5)
check("verify_against_feature(k=None) -> cannot-verify flag",
      len(flags) > 0 and "cannot verify" in flags[0].lower())
flags = verify_against_feature(0.025, 0, 2.5)
check("verify_against_feature(span=0) -> cannot-verify flag",
      len(flags) > 0 and "cannot verify" in flags[0].lower())
flags = verify_against_feature(0.025, 100, None)
check("verify_against_feature(real_m=None) -> cannot-verify flag",
      len(flags) > 0 and "cannot verify" in flags[0].lower())
flags = verify_against_feature(0.025, 100, 2.5)
check("verify_against_feature correct scale -> no flags", flags == [])
flags = verify_against_feature(0.033, 100, 2.5)
check("verify_against_feature wrong scale (3.3m vs 2.5m) -> flag", len(flags) > 0)
# Exactly at tol boundary (5%): got = 2.5*0.95 = 2.375, delta/2.5=0.05 -> NOT over tol
k_at_tol = 2.5 * 0.95 / 100
flags = verify_against_feature(k_at_tol, 100, 2.5, tol=0.05)
check("verify_against_feature at exact 5% tol -> no flag", flags == [], f"flags={flags}")
# Just over tol
k_over = 2.5 * 0.9499 / 100
flags = verify_against_feature(k_over, 100, 2.5, tol=0.05)
check("verify_against_feature just over 5% tol -> flag", len(flags) > 0, f"flags={flags}")

print("\n── scale: scale_consensus ──")
k, flags = scale_consensus([])
check("scale_consensus([]) -> None + flag", k is None and len(flags) > 0)
k, flags = scale_consensus([(50.0, 500)])
check("scale_consensus single ref -> k=0.1", k is not None and abs(k - 0.1) < 1e-9, f"got k={k}")
k, flags = scale_consensus([(50.0, 500), (100.0, 1000), (25.0, 250)])
check("scale_consensus identical refs -> k=0.1", k is not None and abs(k - 0.1) < 1e-9, f"got k={k}")
k, flags = scale_consensus([(1.0, 100), (100.0, 100)])  # 0.01 vs 1.0 -> 100x spread
check("scale_consensus 100x spread -> None (MIXED-SCALE)",
      k is None and any("disagree" in f.lower() or "mixed" in f.lower() for f in flags),
      f"k={k}, flags={flags}")
# Zero span in one ref should not crash and should skip that ref
try:
    k, flags = scale_consensus([(50.0, 0), (50.0, 500)])
    check("scale_consensus with zero-span ref -> k=0.1 (zero span skipped)",
          k is not None and abs(k - 0.1) < 1e-9, f"k={k}, flags={flags}")
except Exception as e:
    check("scale_consensus with zero-span ref -> no crash", False, str(e))
# None entries should not crash
try:
    k, flags = scale_consensus([(50.0, 500), (None, None)])
    check("scale_consensus with (None,None) ref -> no crash", True)
except Exception as e:
    check("scale_consensus with (None,None) ref -> no crash", False, str(e))
# Negative span should not produce negative k silently (or crash)
try:
    k, flags = scale_consensus([(50.0, -100)])
    check("scale_consensus with negative span -> no crash", True)
except Exception as e:
    check("scale_consensus with negative span -> no crash", False, str(e))

print("\n── scale: calibrate_verified ──")
# title 1:500 -> k_title ≈ 0.1764; bay at 100pt -> k_feat=0.025 -> huge disagreement
k, flags = calibrate_verified(title_denominator=500, bay_width_pt=100)
check("calibrate_verified: title/bay disagree -> k=bay(0.025)",
      k is not None and abs(k - 0.025) < 1e-9, f"k={k}")
check("calibrate_verified: title/bay disagree -> disagree flag",
      any("disagree" in f.lower() for f in flags), f"flags={flags}")
# No title, bay only
k, flags = calibrate_verified(bay_width_pt=100)
check("calibrate_verified: feature only -> k=0.025", k is not None and abs(k - 0.025) < 1e-9, f"k={k}")
# No inputs -> None + flag
k, flags = calibrate_verified()
check("calibrate_verified: no inputs -> None + flag", k is None and len(flags) > 0, f"k={k}")
# dim_span + dim_m feature
k, flags = calibrate_verified(dim_span_pt=400, dim_m=20.0)
check("calibrate_verified: dim feature -> k=0.05",
      k is not None and abs(k - 0.05) < 1e-9, f"k={k}")

print("\n── geometry: measure_regions ──")
# k=None must raise ValueError
try:
    measure_regions([[(0,0),(100,0),(100,100),(0,100)]], None)
    check("measure_regions(k=None) raises ValueError", False, "no exception raised")
except ValueError:
    check("measure_regions(k=None) raises ValueError", True)
except Exception as e:
    check("measure_regions(k=None) raises ValueError", False, f"wrong exception: {e}")

# k=0 -> area should be 0
try:
    a, f = measure_regions([[(0,0),(100,0),(100,100),(0,100)]], 0)
    check("measure_regions(k=0) -> area=0, no crash", a == 0.0, f"got {a}")
except Exception as e:
    check("measure_regions(k=0) -> no crash", False, str(e))

# empty regions list
try:
    a, f = measure_regions([], 0.1)
    check("measure_regions([]) -> area=0, no crash", a == 0.0, f"got {a}")
except Exception as e:
    check("measure_regions([]) -> no crash", False, str(e))

# Degenerate: 2-point region
try:
    a, f = measure_regions([[(0,0),(100,0)]], 0.1)
    check("measure_regions 2-point region -> area=0", a == 0.0, f"got {a}")
except Exception as e:
    check("measure_regions 2-point region -> no crash", False, str(e))

# Degenerate: all duplicate points
try:
    a, f = measure_regions([[(50,50),(50,50),(50,50),(50,50)]], 0.1)
    check("measure_regions all-duplicate points -> area=0", a == 0.0, f"got {a}")
except Exception as e:
    check("measure_regions all-duplicate points -> no crash", False, str(e))

# Hole larger than outer ring -> area >= 0
outer = [(0,0),(1000,0),(1000,1000),(0,1000)]
big_hole = [(-100,-100),(1100,-100),(1100,1100),(-100,1100)]
try:
    a, f = measure_regions([outer], 0.1, holes={0: [big_hole]})
    check("measure_regions hole > outer -> area >= 0", a >= 0, f"got {a}")
except Exception as e:
    check("measure_regions hole > outer -> no crash", False, str(e))

# Hole outside the outer ring -> full outer area retained (~10000 m2)
outer2 = [(0,0),(1000,0),(1000,1000),(0,1000)]
outside_hole = [(2000,2000),(3000,2000),(3000,3000),(2000,3000)]
try:
    a, f = measure_regions([outer2], 0.1, holes={0: [outside_hole]})
    check("measure_regions hole outside -> full outer area (~10000 m2)",
          abs(a - 10000.0) < 1.0, f"got {a}")
except Exception as e:
    check("measure_regions hole outside -> no crash", False, str(e))

# NaN coordinate
try:
    a, f = measure_regions([[(0,0),(float('nan'),0),(100,100)]], 0.1)
    check("measure_regions NaN coord -> no crash", True)
except Exception as e:
    check("measure_regions NaN coord -> no crash (expected exception OK)", True)

# Inf coordinate
try:
    a, f = measure_regions([[(0,0),(float('inf'),0),(100,100)]], 0.1)
    check("measure_regions Inf coord -> no crash", True)
except Exception as e:
    check("measure_regions Inf coord -> no crash (expected exception OK)", True)

# Hugely concave (star) polygon
import math as _math
star = []
for i in range(10):
    angle = 2 * _math.pi * i / 10
    r = 500 if i % 2 == 0 else 200
    star.append((r * _math.cos(angle), r * _math.sin(angle)))
try:
    a, f = measure_regions([star], 0.1)
    check("measure_regions star/concave -> no crash, area > 0", a > 0, f"got {a}")
except Exception as e:
    check("measure_regions star/concave -> no crash", False, str(e))

# Performance: 1000-point circle
import time as _time
big_circle = [(_math.cos(2*_math.pi*i/1000)*500, _math.sin(2*_math.pi*i/1000)*500)
              for i in range(1000)]
try:
    t0 = _time.time()
    a, f = measure_regions([big_circle], 0.1)
    elapsed = _time.time() - t0
    check(f"measure_regions 1000-point circle <5s ({elapsed:.2f}s)", elapsed < 5.0 and a > 0,
          f"area={a}, elapsed={elapsed:.2f}s")
except Exception as e:
    check("measure_regions 1000-point circle -> no crash", False, str(e))

# ══════════════════════════════════════════════════════════════════════════════
print("\n── costing / pricing: slab_rate ──")
base = dict(depth_mm=190, conc_rate=128, mesh="A252", layers=1, steel_rate_t=850, margin=0.11)

r, flags = slab_rate({**base, "mesh": "Z999"})
check("slab_rate unknown mesh -> None + flag", r is None and len(flags) > 0, f"r={r}")

for mesh in MESH_KG:
    r, flags = slab_rate({**base, "mesh": mesh})
    check(f"slab_rate mesh={mesh} -> positive rate", r is not None and r > 0, f"r={r}")

r, flags = slab_rate({**base, "depth_mm": 0})
check("slab_rate depth_mm=0 -> None + flag", r is None and len(flags) > 0)

r, flags = slab_rate({**base, "depth_mm": -100})
check("slab_rate depth_mm=-100 -> None + flag", r is None and len(flags) > 0)

r, flags = slab_rate({**base, "conc_rate": 0})
check("slab_rate conc_rate=0 -> None + flag", r is None and len(flags) > 0)

r, flags = slab_rate({**base, "margin": 0})
check("slab_rate margin=0 -> positive rate", r is not None and r > 0, f"r={r}")

zones_huge = [{**base, "name": "HugeZone", "area_m2": 999999}]
total, rows = price_project(zones_huge)
check("price_project huge area -> positive total", total > 0, f"total={total}")

total, rows = price_project([])
check("price_project empty zones -> total=0", total == 0.0, f"total={total}")

zones_zero = [{**base, "name": "Z", "area_m2": 0}]
total, rows = price_project(zones_zero)
check("price_project area_m2=0 -> total=0", total == 0.0, f"total={total}")

zones_neg = [{**base, "name": "Z", "area_m2": -100}]
total, rows = price_project(zones_neg)
check("price_project negative area_m2 -> total=0", total == 0.0, f"total={total}")

# Mixed valid + invalid zones
zones_mixed = [
    {**base, "name": "Good", "area_m2": 100},
    {**base, "mesh": "Z999", "name": "Bad", "area_m2": 100},
    {**base, "name": "Zero", "area_m2": 0},
]
total, rows = price_project(zones_mixed)
r_good, _ = slab_rate(base)
expected_mixed = round(100 * r_good, 2)
check("price_project mixed zones -> only good zone priced",
      abs(total - expected_mixed) < 0.01, f"total={total}, expected={expected_mixed}")

total, rows = price_boq([])
check("price_boq([]) -> total=0", total == 0.0)
total, rows = price_boq([("Sec", "Desc", 100, "m2", 10.5)])
check("price_boq single row -> 1050.00", abs(total - 1050.0) < 0.01, f"total={total}")
total, rows = price_boq([("Sec", "Desc", 0, "m2", 10.5)])
check("price_boq zero qty -> 0.0", total == 0.0, f"total={total}")
total, rows = price_boq([("Sec", "Desc", 100, "m2", 0.0)])
check("price_boq zero rate -> 0.0", total == 0.0, f"total={total}")

# ══════════════════════════════════════════════════════════════════════════════
print("\n── sanity: plausible ──")
flags = plausible(None)
check("plausible(None) -> empty flags", flags == [], f"flags={flags}")

flags = plausible(-1)
check("plausible(-1) -> non-positive flag",
      len(flags) > 0 and any("non-positive" in f for f in flags), f"flags={flags}")

flags = plausible(0)
check("plausible(0) -> non-positive flag", len(flags) > 0, f"flags={flags}")

flags = plausible(1000, site_m2=1000)
check("plausible(area==site) -> no IMPOSSIBLE flag",
      not any("impossible" in f.lower() for f in flags), f"flags={flags}")

# At exactly 1.02 * site -> boundary (1.02 * 1000 = 1020, NOT > 1020) -> no flag
flags = plausible(1020, site_m2=1000)
check("plausible(1.02x site) -> no IMPOSSIBLE flag",
      not any("impossible" in f.lower() for f in flags), f"flags={flags}")

flags = plausible(1021, site_m2=1000)
check("plausible(>1.02x site) -> IMPOSSIBLE flag",
      any("impossible" in f.lower() for f in flags), f"flags={flags}")

flags = plausible(59999)
check("plausible(59999) -> no implausible flag",
      not any("implausible" in f.lower() for f in flags), f"flags={flags}")

flags = plausible(60000)
check("plausible(60000==max) -> no implausible flag",
      not any("implausible" in f.lower() for f in flags), f"flags={flags}")

flags = plausible(60001)
check("plausible(60001) -> implausible flag",
      any("implausible" in f.lower() for f in flags), f"flags={flags}")

flags = plausible(50000, site_m2=None)
check("plausible(50000, site_m2=None) -> no crash", True)

flags = plausible(26080)
check("plausible(26080) -> no flags", flags == [], f"flags={flags}")

# ══════════════════════════════════════════════════════════════════════════════
print("\n── markup: parse_area_m2 ──")
check("parse_area_m2('') -> None", parse_area_m2("") is None)
check("parse_area_m2(None) -> None", parse_area_m2(None) is None)

v = parse_area_m2("A = 26080 sq m")
check("'A = 26080 sq m'", v is not None and abs(v - 26080) < 0.01, f"got {v}")

v = parse_area_m2("A = 1,234.5 sq m")
check("'A = 1,234.5 sq m'", v is not None and abs(v - 1234.5) < 0.01, f"got {v}")

v = parse_area_m2("Area = 930 m²")
check("'Area = 930 m²'", v is not None and abs(v - 930) < 0.01, f"got {v}")

v = parse_area_m2("A=520sqm")
check("'A=520sqm'", v is not None and abs(v - 520) < 0.01, f"got {v}")

v = parse_area_m2("Area=520 m2")
check("'Area=520 m2'", v is not None and abs(v - 520) < 0.01, f"got {v}")

v = parse_area_m2("A = 1,000 sq ft")
check("'1,000 sq ft' -> 92.90 m²",
      v is not None and abs(v - 92.90304) < 0.001, f"got {v}")

v = parse_area_m2("Area = 100 ft²")
check("'100 ft²' -> 9.29 m²",
      v is not None and abs(v - 9.290304) < 0.001, f"got {v}")

v = parse_area_m2("A = 200 ft2")
check("'200 ft2' -> 18.58 m²",
      v is not None and abs(v - 200 * 0.09290304) < 0.001, f"got {v}")

check("'no match here' -> None", parse_area_m2("no match here") is None)
check("'12345' (no label) -> None", parse_area_m2("12345") is None)

v = parse_area_m2("A = 100 sq m here and A = 200 sq m there")
check("multiple matches -> first (100)",
      v is not None and abs(v - 100) < 0.01, f"got {v}")

# Tab between sq and m (regex uses \s* so may or may not match)
try:
    v = parse_area_m2("A = 750 sq\tm")
    check("'sq\\tm' (tab) -> 750 or None (no crash)", v is None or abs(v - 750) < 0.01)
except Exception as e:
    check("'sq\\tm' -> no crash", False, str(e))

# ══════════════════════════════════════════════════════════════════════════════
print("\n── router: drawing_priority ──")
s = drawing_priority("DR-C-001 Construction Layout.pdf")
check("'DR-C-' construction -> score >= 2", s >= 2, f"score={s}")

s = drawing_priority("DR-CE-002 External Surfacing.pdf")
check("'DR-CE-' external surfacing -> score >= 4", s >= 4, f"score={s}")

s = drawing_priority("Proposed Site Plan.pdf")
check("'Proposed Site Plan' -> score <= 0", s <= 0, f"score={s}")

s = drawing_priority("General Arrangement.pdf")
check("'General Arrangement' -> score 0 (neutral)", s == 0, f"score={s}")

s = drawing_priority("Construction Thickness Plan.pdf")
check("'Construction Thickness Plan' -> score >= 4", s >= 4, f"score={s}")

s = drawing_priority("Site Location Plan.pdf")
check("'Site Location Plan' -> score < 0", s < 0, f"score={s}")

s = drawing_priority("External Works Kerbing and Hardstanding Details.pdf")
check("external works+kerb+hardstanding -> score >= 6", s >= 6, f"score={s}")

s = drawing_priority("Pavement Construction Details.pdf")
check("'Pavement Construction Details' -> score >= 4", s >= 4, f"score={s}")

s_constr = drawing_priority("Construction Layout.pdf")
s_site   = drawing_priority("Proposed Site Plan.pdf")
check("construction > site plan", s_constr > s_site,
      f"construction={s_constr}, site={s_site}")

try:
    s = drawing_priority("", "")
    check("drawing_priority('','') -> no crash, score=0", s == 0, f"score={s}")
except Exception as e:
    check("drawing_priority('','') -> no crash", False, str(e))

try:
    s = drawing_priority("some_drawing.pdf", None)
    check("drawing_priority(name, text=None) -> no crash", True)
except Exception as e:
    check("drawing_priority(name, text=None) -> no crash", False, str(e))

# ══════════════════════════════════════════════════════════════════════════════
print("\n── takeoff_pipeline: price_zone ──")
with contextlib.redirect_stdout(io.StringIO()):
    from takeoff_pipeline import price_zone, takeoff

total, rate, flags = price_zone(100, 190, 128, "Z999", 1, 850, 0.11)
check("price_zone unknown mesh -> total=None + flag", total is None and len(flags) > 0)

total, rate, flags = price_zone(0, 190, 128, "A252", 1, 850, 0.11)
check("price_zone area=0 -> None + flag", total is None and len(flags) > 0)

total, rate, flags = price_zone(None, 190, 128, "A252", 1, 850, 0.11)
check("price_zone area=None -> None + flag", total is None and len(flags) > 0)

total, rate, flags = price_zone(-100, 190, 128, "A252", 1, 850, 0.11)
check("price_zone negative area -> None + flag", total is None and len(flags) > 0)

total, rate, flags = price_zone(100, -190, 128, "A252", 1, 850, 0.11)
check("price_zone negative depth -> None + flag", total is None and len(flags) > 0)

total, rate, flags = price_zone(26080, 190, 128, "A252", 1, 850, 0.11)
check("price_zone yard -> £1,170,731.20",
      total is not None and abs(total - 1170731.20) < 0.02, f"total={total}")

print("\n── takeoff_pipeline: takeoff() ──")
import os as _os
_drawings_dir = "/sessions/friendly-charming-carson/mnt/outputs/takeoff/drawings"

# Use an UNMARKED PDF explicitly — a MARKED PDF ignores vision entirely (correct by design).
_unmarked_pdfs = sorted([f for f in _os.listdir(_drawings_dir)
                          if f.startswith("UNMARKED") and f.endswith(".pdf")]) \
                  if _os.path.isdir(_drawings_dir) else []
_all_pdfs = sorted([f for f in _os.listdir(_drawings_dir) if f.endswith(".pdf")]) \
             if _os.path.isdir(_drawings_dir) else []

if _all_pdfs:
    # Test 1: any PDF with no vision -> no crash
    _any_pdf = _os.path.join(_drawings_dir, _all_pdfs[0])
    try:
        result = takeoff(_any_pdf, vision=None)
        check("takeoff(pdf, vision=None) -> no crash", "type" in result)
    except Exception as e:
        check("takeoff(pdf, vision=None) -> no crash", False, str(e))

if _unmarked_pdfs:
    _updf = _os.path.join(_drawings_dir, _unmarked_pdfs[0])

    # Test 2: unmarked PDF + vision but no scale_ref -> scale-missing flag (or auto-bar attempt)
    try:
        mock_vision = {"regions": [[(0,0),(100,0),(100,100),(0,100)]], "voids": {}}
        result = takeoff(_updf, vision=mock_vision)
        check("takeoff(unmarked, vision no scale_ref) -> no crash", "type" in result)
    except Exception as e:
        check("takeoff(unmarked, vision no scale_ref) -> no crash", False, str(e))

    # Test 3: unmarked + vision + scale_ref that yields impossible area (250,000 m² on 10,000 m² site)
    # k = 5.0 m/pt; 100x100 pt region -> 250,000 m², which must be caught by sanity
    huge_scale_ref = [[0, 0], [1, 0], 5.0]
    mock_vision_huge = {
        "regions": [[(0,0),(100,0),(100,100),(0,100)]],
        "scale_ref": huge_scale_ref,
        "site_m2": 10000,
    }
    try:
        result = takeoff(_updf, vision=mock_vision_huge)
        has_flag = any("impossible" in f.lower() or "implausible" in f.lower()
                       for f in result.get("flags", []))
        check("takeoff: impossible area -> sanity flag", has_flag,
              f"flags={result.get('flags')}, area={result.get('area_m2')}")
    except Exception as e:
        check("takeoff: impossible area -> no crash", False, str(e))
else:
    print("  [SKIP] no UNMARKED PDFs in drawings/ — vision pipeline tests skipped")

# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
n = passes + fails
print(f"{passes}/{n} PASS")
if fails:
    print(f"{fails} FAILED")
    sys.exit(1)
else:
    print("All stress tests passed.")
    sys.exit(0)
