#!/usr/bin/env python3
"""scale_consensus + sanity.plausible guards, and verified calibration.

Sections in this module (printed in this order):
  - guards (95,463 m² incident)
  - Fortel scale verification (from the call)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("guards (95,463 m² incident)")
from scale import scale_consensus
from sanity import plausible
ck("mixed-scale dimensions flagged (no auto-pick)", scale_consensus([(257.2,710),(166,420),(50,75),(35,80)])[0] is None)
ck("consistent dimensions accepted", abs(scale_consensus([(100,1000),(50,500)])[0] - 0.1) < 1e-6)
ck("impossible area blocked", len(plausible(95463, site_m2=34329)) >= 1)
ck("correct area passes", plausible(26080, site_m2=34329) == [])

print("Fortel scale verification (from the call)")
from scale import calibrate_verified, verify_against_feature, title_block_k, ratio_from_k
geom = 2235703  # real yard polygon area in pt²
k_v, _ = calibrate_verified(title_denominator=500, bay_width_pt=2.5/0.108)  # verify vs 2.5 m bay
ck("parking-bay verify flips wrong title scale to truth", abs(geom*k_v*k_v - 26080) < 50)
ck("title-only scale flagged as a lie", len(verify_against_feature(title_block_k(500), 2.5/0.108, 2.5)) >= 1)
ck("the same internal scale k is presented to assessors as the conventional 1:250 ratio",
   ratio_from_k(title_block_k(250)) == 250)

