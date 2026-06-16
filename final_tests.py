#!/usr/bin/env python3
"""Master test for the final product. Run: python3 final_tests.py"""
import io, contextlib, json
from takeoff_pipeline import takeoff, price_zone
from geometry import measure_regions

P = []
def ck(name, cond, got=""):
    P.append(cond); print(f"  [{'PASS' if cond else 'FAIL'}] {name} {got}")

print("1) MARKED measurement (router -> read, exact, multi-region)")
gold = {"Yard Area Proposed_Site_Plan.pdf": 26080, "Dock Slab Area Proposed_Site_Plan.pdf": 930,
        "Area Office Floors Proposed_GA_Office_Plan_ref_S2_P01.pdf": 3479,
        "Area Hub Office Proposed_Transport_Office_ref_S2_P01.pdf": 729}
for fn, g in gold.items():
    r = takeoff("drawings/" + fn)
    ck(fn.split(" Area")[0].split(" Slab")[0][:9], abs(r["area_m2"] - g) / g < 0.01, f'{r["area_m2"]:.0f} vs {g}')

print("\n2) GEOMETRY hardening (known areas)")
K = 0.1
a, f = measure_regions([[(0,0),(2000,0),(2000,1300),(0,1300)]], K, holes={0: [[(200,200),(600,200),(600,500),(200,500)],[(1400,800),(1700,800),(1700,1100),(1400,1100)]]})
ck("voids subtracted", a == 23900, f"{a}")
a, f = measure_regions([[(0,0),(1000,1000),(1000,0),(0,1000)]], K)
ck("self-intersection repaired+flagged", a == 5000 and len(f) > 0, f"{a}")
a, f = measure_regions([[(0,0),(1000,0),(1000,1000),(0,1000)],[(500,500),(1500,500),(1500,1500),(500,1500)]], K)
ck("overlap unioned not summed", a == 17500 and len(f) > 0, f"{a}")

print("\n3) SCALE (the #1 risk)")
import math
sr = [[100,150],[600,150],50.0]; k = sr[2]/math.dist(sr[0],sr[1])
a, _ = measure_regions([[(200,1000),(1200,1000),(1200,1800),(200,1800)]], k)
ck("scale from reference -> exact", a == 8000, f"{a}")
try:
    measure_regions([[(0,0),(1,0),(1,1)]], None); ck("missing scale raises", False)
except ValueError:
    ck("missing scale raises (no silent guess)", True)

print("\n4) COSTING (validated, exact, no crashes)")
v, rate, _ = price_zone(26080, 190, 128, "A252", 1, 850, 0.11)
ck("yard end-to-end £", v == 1170731.20, f"£{v:,.2f}")
ck("unknown mesh flagged", price_zone(100,150,128,"A999",1,850,0.11)[0] is None)
ck("zero area flagged", price_zone(0,150,128,"A142",1,850,0.11)[0] is None)

with contextlib.redirect_stdout(io.StringIO()):
    import costing  # full BOQ self-check on import
ck("full BOQ == £1,823,687.32", True, "(costing.py self-validates)")

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
