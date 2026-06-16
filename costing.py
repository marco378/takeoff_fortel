#!/usr/bin/env python3
"""
Stage-2 costing engine (deterministic) — reverse-engineered from the real
'Costing Winvic California Drive.xlsx' (REV02) and validated to the penny.

Two checks:
  1) rate_buildup() reproduces the yard supply&lay rate £44.89/m² from first principles.
  2) The full BOQ sums to the sheet's stated TOTAL NETT = £1,823,687.32.
"""

MESH_KG = {"A142": 2.22, "A193": 3.02, "A252": 3.95, "A393": 6.16, "B785": 8.14}


def rate_buildup(depth_mm, conc_rate, conc_wastage, mesh, layers, steel_rate_t,
                 steel_wastage, lap_acc, dpm, curing, labour, trim, margin):
    """Per-m² supply & lay rate = (concrete + steel + dpm + curing + labour + trim) x (1+margin)."""
    conc = depth_mm / 1000 * conc_rate
    conc_tot = conc * (1 + conc_wastage)
    steel = MESH_KG[mesh] * layers * steel_rate_t / 1000
    steel_tot = steel * (1 + steel_wastage + lap_acc)
    nett = conc_tot + steel_tot + dpm + curing + labour + trim
    return round(nett * (1 + margin), 2), {
        "concrete": round(conc_tot, 2), "steel": round(steel_tot, 2),
        "dpm": dpm, "curing": curing, "labour": labour, "trim": trim,
        "nett": round(nett, 2), "margin%": int(margin * 100)}


# --- Check 1: reproduce the yard rate from first principles ---
yard_rate, parts = rate_buildup(190, 128, 0.03, "A252", 1, 850, 0.10, 0.18,
                                0.46, 0.23, 10.00, 0.40, 0.11)
print("Yard rate build-up:", parts)
print(f"  -> £{yard_rate}/m²   (sheet: £44.89)   {'OK' if yard_rate == 44.89 else 'MISMATCH'}")

# --- Check 2: full BOQ -> TOTAL NETT ---
# (section, description, qty, unit, rate)  — rates as used in the BOQ
BOQ = [
    ("Yard", "190mm slab C32/40", 26080, "m2", 44.89),
    ("Yard", "Final trim +/-50mm", 26080, "m2", 1.40),
    ("Yard", "Joints (4.9x6.5 bays)", 26080, "m2", 4.85),
    ("Yard", "E/O manhole details", 26, "Nr", 75.00),
    ("Yard", "Pouring top of channels", 731, "Lm", 17.50),
    ("Yard", "E/O transition details", 13, "Lm", 110.00),
    ("Yard", "24m boom pump", 1, "Item", 45900.00),
    ("Yard", "E/O 205mm w/ A393", 2930, "m2", 4.86),
    ("Dock", "250mm slab C32/40", 930, "m2", 63.37),
    ("Dock", "Final trim", 930, "m2", 1.50),
    ("Dock", "Perimeter edge formwork 250dp", 500, "Lm", 25.10),
    ("Dock", "24m boom pump", 1, "Item", 2700.00),
    ("Dock", "Foundation bases C40 (34no 2x2x1)", 136, "m3", 167.80),
    ("Dock", "A393 x2 in base bottoms", 272, "m2", 45.95),
    ("Dock", "E/O concrete wastage (dig by others)", 1, "Item", 2282.08),
    ("GF ancillary", "150mm slab C32/40", 520, "m2", 36.84),
    ("GF ancillary", "Final trim", 520, "m2", 1.50),
    ("GF ancillary", "Saw cuts & movement joints", 520, "m2", 3.80),
    ("GF ancillary", "Perimeter edge formwork 150dp", 190, "Lm", 15.00),
    ("GF ancillary", "E/O column details", 22, "Nr", 55.00),
    ("GF ancillary", "24m boom pump", 1, "Item", 4050.00),
    ("Upper floors", "130mm C30 on MD60 deck", 3692, "m2", 32.92),
    ("Upper floors", "24m mobile boom pump", 1, "Item", 5400.00),
    ("Prelims", "Accommodation/subsistence", 15, "weeks", 4250.00),
    ("Prelims", "Cubes (1 set/3 per 150m3)", 1, "Item", 17425.00),
    ("Prelims", "Skips for washing out", 15, "weeks", 1450.00),
    ("Prelims", "COSHH container", 15, "weeks", 225.00),
    ("Prelims", "Carbon reduction measure", 1, "Item", 19200.00),
    ("Prelims", "14m telehandler", 8, "weeks", 1750.00),
    ("Prelims", "O&M", 1, "Item", 750.00),
    ("Prelims", "Protection (Hessian & DPM)", 1, "Item", 3250.00),
]

total = 0.0
print(f"\n{'section':14}{'description':38}{'qty':>8} {'rate':>11} {'value':>14}")
for sec, desc, qty, unit, rate in BOQ:
    val = round(qty * rate, 2)
    total += val
    print(f"{sec:14}{desc:38}{qty:>8} {rate:>11,.2f} {val:>14,.2f}")

total = round(total, 2)
print("\n" + "-" * 86)
print(f"{'TOTAL NETT':52}{total:>32,.2f}")
print(f"{'SHEET TOTAL NETT':52}{1823687.32:>32,.2f}")
print("\nVALIDATION:", "EXACT MATCH ✅" if total == 1823687.32 else f"OFF BY £{round(total-1823687.32,2)} ❌")
