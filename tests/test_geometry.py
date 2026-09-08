#!/usr/bin/env python3
"""geometry.measure_regions: voids, self-intersection, unions, missing scale.

Sections in this module (printed in this order):
  - geometry

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from geometry import measure_regions
from tests import ck

print("geometry")
K = 0.1
a, _ = measure_regions([[(0,0),(2000,0),(2000,1300),(0,1300)]], K,
                       holes={0: [[(200,200),(600,200),(600,500),(200,500)], [(1400,800),(1700,800),(1700,1100),(1400,1100)]]})
ck("voids 23,900", a == 23900)
a, f = measure_regions([[(0,0),(1000,1000),(1000,0),(0,1000)]], K); ck("self-intersect repaired+flagged", a == 5000 and f)
a, f = measure_regions([[(0,0),(1000,0),(1000,1000),(0,1000)], [(500,500),(1500,500),(1500,1500),(500,1500)]], K)
ck("overlap unioned 17,500", a == 17500 and f)
a, _ = measure_regions([[(0,0),(1,1)]], K); ck("degenerate <3 -> 0", a == 0.0)
try: measure_regions([[(0,0),(1,0),(1,1)]], None); ck("missing scale raises", False)
except ValueError: ck("missing scale raises", True)

