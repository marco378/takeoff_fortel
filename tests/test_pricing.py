#!/usr/bin/env python3
"""pricing.slab_rate / price_project.

Sections in this module (printed in this order):
  - pricing

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pricing import price_project, slab_rate
from tests import ck

print("pricing")
r, _ = slab_rate({"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11})
ck("yard rate 44.89", r == 44.89)
tot, rows = price_project([{"name":"Yard","area_m2":26080,"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11}])
ck("yard slab line GBP1,170,731.20", rows[0][5] == 1170731.20)
ck("unknown mesh handled", slab_rate({"depth_mm":150,"conc_rate":128,"mesh":"A999","layers":1,"steel_rate_t":850,"margin":0.11})[0] is None)

