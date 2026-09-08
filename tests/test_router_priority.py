#!/usr/bin/env python3
"""router.drawing_priority: which sheet in a pack to measure.

Sections in this module (printed in this order):
  - drawing selection (from the call)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("drawing selection (from the call)")
from router import drawing_priority
ck("construction/kerbing drawing beats site plan",
   drawing_priority("RIBVE-XX-DR-CE-0750 construction kerbing") > drawing_priority("Proposed Site Plan"))
ck("engineer external-works beats architect hard-landscaping",
   drawing_priority("External Construction Thickness Layout", source="engineer")
   > drawing_priority("Unit 1 Hard Landscaping", source="architect"))

