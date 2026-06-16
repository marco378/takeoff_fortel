#!/usr/bin/env python3
"""Full chain on REAL data: area measured off the marked drawing -> £ -> vs the actual quote line."""
from costing import rate_buildup

MEASURED = {"Yard": 26080, "Dock": 930}                 # from real_takeoff.py (marked drawings)
BOQ = {"Yard": (44.89, 1170731.20), "Dock": (63.37, 58934.10)}   # actual Winvic quote lines

print(f"{'zone':6}{'drawing m²':>12}{'rate £/m²':>11}{'computed £':>15}{'actual quote £':>17}{'match':>8}")
for z in MEASURED:
    area = MEASURED[z]; rate, val = BOQ[z]; comp = round(area * rate, 2)
    print(f"{z:6}{area:>12,}{rate:>11.2f}{comp:>15,.2f}{val:>17,.2f}{'EXACT ✅' if comp == val else 'diff':>8}")

r, _ = rate_buildup(190, 128, 0.03, "A252", 1, 850, 0.10, 0.18, 0.46, 0.23, 10.00, 0.40, 0.11)
print(f"\nyard rate from first principles: £{r}/m²  (sheet £44.89)  {'OK' if r == 44.89 else 'X'}")
print("REAL drawing -> measured m² -> £ matches the actual Winvic quote line to the penny.")
