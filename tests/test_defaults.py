#!/usr/bin/env python3
"""defaults.spec_with_defaults / assumption_note / flag_assumed.

Sections in this module (printed in this order):
  - defaults (Fortel build-up assumptions)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("defaults (Fortel build-up assumptions)")
from defaults import spec_with_defaults, assumption_note, flag_assumed
_s, _assumed = spec_with_defaults()
ck("default spec depth 190mm", _s["depth_mm"] == 190)
ck("default spec mesh A252",   _s["mesh"] == "A252")
ck("default assumed=True",     _assumed is True)
ck("assumption note contains 190mm", "190 mm" in assumption_note(_s))
ck("flags empty when fully specified",
   flag_assumed({"depth_mm":200,"mesh":"A393","layers":1,"conc_mix":"C32/40"}, False) == [])
ck("flags non-empty when assumed", len(flag_assumed(_s, True)) >= 1)
_s2, _a2 = spec_with_defaults({"depth_mm": 175, "mesh": "A193", "layers": 1, "conc_mix": "C32/40"})
ck("full engineer spec -> assumed=False", _a2 is False)
ck("engineer depth 175 overrides default 190", _s2["depth_mm"] == 175)

