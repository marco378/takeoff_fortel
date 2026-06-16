#!/usr/bin/env python3
"""
End-to-end: drawing PDF -> measured quantities (Stage 1) -> priced BOQ line (Stage 2).
Runs on the synthetic yard now; point `pdf` at a real drawing once it's in drawings/.
"""
from engine import takeoff
from costing import rate_buildup

pdf = "drawings/synthetic_yard.pdf"          # swap for the real Yard site plan
SPEC = dict(depth_mm=190, conc_rate=128, conc_wastage=0.03, mesh="A252", layers=1,
            steel_rate_t=850, steel_wastage=0.10, lap_acc=0.18, dpm=0.46, curing=0.23,
            labour=10.00, trim=0.40, margin=0.11)

# Stage 1 — measure
m = takeoff(pdf)
area = m["net_m2"]
print(f"STAGE 1  drawing: {m['pdf']}  scale {m['scale']}  ->  net {area:,.0f} m²  "
      f"(gross {m['gross_m2']:,.0f} − void {m['void_m2']:,.0f}), {m['marker_count']} manholes")

# Stage 2 — price
rate, parts = rate_buildup(**SPEC)
lines = [("190mm slab supply & lay", rate), ("Final trim", 1.40), ("Joints", 4.85)]
print(f"\nSTAGE 2  yard build-up rate £{rate}/m²  {parts}\n")
print(f"{'BOQ line':30}{'qty':>10}{'rate':>9}{'value £':>15}")
subtotal = 0
for desc, r in lines:
    v = round(area * r, 2); subtotal += v
    print(f"{desc:30}{area:>10,.0f}{r:>9.2f}{v:>15,.2f}")
print("-" * 64)
print(f"{'area-driven subtotal':49}{subtotal:>15,.2f}")
print(f"\nDRAWING  ->  {area:,.0f} m²  ->  £{subtotal:,.0f}   (full chain working end-to-end)")
