#!/usr/bin/env python3
"""Self-contained CI tests (NO client drawings — those are gitignored). Exit non-zero on failure."""
import sys
from reportlab.pdfgen import canvas
from geometry import measure_regions
from scale import detect_scale_bar
from pricing import slab_rate, price_project

P = []
def ck(n, c, g=""):
    P.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n} {g}")

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

print("scale")
c = canvas.Canvas("/tmp/_sb.pdf", pagesize=(1400,2200)); c.rect(200,1000,1000,800); c.line(100,150,600,150); c.drawString(250,160,"0          50 m"); c.save()
k, info = detect_scale_bar("/tmp/_sb.pdf"); ck("scale-bar k=0.1", k == 0.1, info)

print("pricing")
r, _ = slab_rate({"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11})
ck("yard rate 44.89", r == 44.89)
tot, rows = price_project([{"name":"Yard","area_m2":26080,"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11}])
ck("yard slab line GBP1,170,731.20", rows[0][5] == 1170731.20)
ck("unknown mesh handled", slab_rate({"depth_mm":150,"conc_rate":128,"mesh":"A999","layers":1,"steel_rate_t":850,"margin":0.11})[0] is None)

print("guards (95,463 m² incident)")
from scale import scale_consensus
from sanity import plausible
ck("mixed-scale dimensions flagged (no auto-pick)", scale_consensus([(257.2,710),(166,420),(50,75),(35,80)])[0] is None)
ck("consistent dimensions accepted", abs(scale_consensus([(100,1000),(50,500)])[0] - 0.1) < 1e-6)
ck("impossible area blocked", len(plausible(95463, site_m2=34329)) >= 1)
ck("correct area passes", plausible(26080, site_m2=34329) == [])

print("Fortel scale verification (from the call)")
from scale import calibrate_verified, verify_against_feature, title_block_k
geom = 2235703  # real yard polygon area in pt²
k_v, _ = calibrate_verified(title_denominator=500, bay_width_pt=2.5/0.108)  # verify vs 2.5 m bay
ck("parking-bay verify flips wrong title scale to truth", abs(geom*k_v*k_v - 26080) < 50)
ck("title-only scale flagged as a lie", len(verify_against_feature(title_block_k(500), 2.5/0.108, 2.5)) >= 1)

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
sys.exit(0 if all(P) else 1)
