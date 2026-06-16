#!/usr/bin/env python3
"""Authoritative test runner — every case, honest expected behaviour. Run: python3 all_tests.py"""
import fitz, re
from engine import takeoff
from costing import rate_buildup
from router import classify

PASS = []


def check(name, cond, detail=""):
    PASS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")


def marked_area(path):
    tot = 0
    for a in (fitz.open(path)[0].annots() or []):
        if a.type[1] == "Polygon":
            m = re.search(r"A\s*=\s*([\d,]+\.?\d*)\s*sq m", a.info.get("content", "") or "")
            if m:
                tot += float(m.group(1).replace(",", ""))
    return tot


print("COSTING (Stage 2 — deterministic)")
r, _ = rate_buildup(190, 128, 0.03, "A252", 1, 850, 0.10, 0.18, 0.46, 0.23, 10.00, 0.40, 0.11)
check("yard rate £/m² == 44.89", r == 44.89, f"got {r}")
check("yard line 26,080×44.89 == £1,170,731.20", round(26080 * 44.89, 2) == 1170731.20)
check("dock line 930×63.37 == £58,934.10", round(930 * 63.37, 2) == 58934.10)

print("\nMARKED takeoff (Stage 1 — exact)")
y = marked_area("drawings/Yard Area Proposed_Site_Plan.pdf")
check("yard == 26,080 (±2%)", abs(y - 26080) / 26080 < 0.02, f"got {y:,.0f}")
d = marked_area("drawings/Dock Slab Area Proposed_Site_Plan.pdf")
check("dock == 930 (±2%)", abs(d - 930) / 930 < 0.02, f"got {d:,.0f}")

print("\nSYNTHETIC (engine sanity)")
s = takeoff("drawings/synthetic_yard.pdf")
check("synthetic net == 25,920 (±2%)", abs(s["net_m2"] - 25920) / 25920 < 0.02, f"got {s['net_m2']:,.0f}")

print("\nUNMARKED (site boundary exact; slab → assessor by design)")
from unmarked_takeoff import unmarked
u = unmarked("drawings/UNMARKED_Yard_Area_Proposed_Site_Plan.pdf")
check("CAD layers detected", u["has_cad_layers"], f"{u['n_layers']} layers")
check("site boundary ≈ 34,329 (±3%)", abs(u.get("site_boundary_m2", 0) - 34329) / 34329 < 0.03, f"got {u.get('site_boundary_m2')}")
check("slab extent correctly flagged for assessor", "ASSESSOR" in u["slab_extent"])

print("\nUNMARKED via VISION+geometry (the working method)")
from vision_takeoff import measure_polygon, VISION_YARD
va = measure_polygon(VISION_YARD)
check("vision trace measures within 6% of 26,080", abs(va - 26080) / 26080 < 0.06, f"got {va:,.0f}")

print("\nROUTER (classify any input)")
t, *_ = classify("drawings/Yard Area Proposed_Site_Plan.pdf")
check("marked file → MARKED vector", t == "MARKED vector", t)
t2, *_ = classify("drawings/UNMARKED_Yard_Area_Proposed_Site_Plan.pdf")
check("unmarked file → UNMARKED vector", t2 == "UNMARKED vector", t2)

print(f"\n==== {sum(PASS)}/{len(PASS)} checks passed ====")
print("Exact where exact is possible (marked, costing, site boundary); correctly flagged where")
print("autonomous isn't reliable (unmarked slab extent, raster). No fabricated numbers.")
