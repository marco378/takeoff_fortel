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

print("drawing selection (from the call)")
from router import drawing_priority
ck("construction/kerbing drawing beats site plan",
   drawing_priority("RIBVE-XX-DR-CE-0750 construction kerbing") > drawing_priority("Proposed Site Plan"))
ck("engineer external-works beats architect hard-landscaping",
   drawing_priority("External Construction Thickness Layout", source="engineer")
   > drawing_priority("Unit 1 Hard Landscaping", source="architect"))

print("unmarked pipeline (legend-anchored colour segmentation)")
import numpy as _np
from takeoff_unmarked import segment_hatch
_im = _np.full((200, 300, 3), 255, _np.uint8); _im[50:150, 60:210] = (216, 216, 216)  # 100x150 grey
_comp = segment_hatch(_im, (216, 216, 216))
ck("segment grey hatch ~15,000 px", _comp is not None and abs(int(_comp.sum()) - 15000) < 900)
ck("segment ignores white background", int(_comp.sum()) < 200 * 300 * 0.4)
_px = int(_comp.sum()); _area = _px * (1 / 2.0) ** 2 * 0.1 * 0.1   # S=2 (1px=0.5pt), k=0.1 m/pt
ck("unmarked area math (px->m2)", abs(_area - _px * 0.0025) < 1e-6)
ck("white-segmentation blowup blocked by plausibility", len(plausible(279905)) >= 1)
_w = segment_hatch(_im, (255, 0, 0))   # colour not present
ck("absent hatch colour -> no region", _w is None or int(_w.sum()) == 0)

print("team feedback fixes (DEMO4)")
from takeoff_unmarked import drawing_style
# (a) drawing-style guard: solid fill = colour-coded; thin lines = line/hatch (don't guess on engineer sheets)
_solid = _np.full((300, 300, 3), 255, _np.uint8); _solid[40:260, 40:260] = (120, 170, 90)
ck("colour-coded sheet detected", drawing_style(_solid)[0] == "colour-coded")
_lines = _np.full((300, 300, 3), 255, _np.uint8)
for _i in range(0, 300, 12):
    _lines[:, _i] = (80, 80, 80)
ck("line/hatch sheet detected", drawing_style(_lines)[0] == "line/hatch")
# (b) dock-bay/void fix: a large interior void is kept as a DEDUCTION, not filled (team: D77 dock bays)
_v = _np.full((400, 400, 3), 255, _np.uint8); _v[40:360, 40:360] = (214, 214, 214); _v[150:250, 150:250] = 255
_kept = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=1.0)   # void=6.25 m² > 1 -> kept out
_fill = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=999)   # huge thresh -> filled
ck("large interior void kept as deduction", int(_kept.sum()) < int(_fill.sum()))
ck("void filled only when below threshold", int(_fill.sum()) - int(_kept.sum()) > 8000)

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
sys.exit(0 if all(P) else 1)
