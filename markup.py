#!/usr/bin/env python3
"""
Robust parsing of Bluebeam/CAD area-markup labels (round 4).
The first reader only matched 'A = N sq m'. Real labels vary — handle:
  'A = 26,080.2 sq m', 'Area = 930 m²', 'A=520sqm', and imperial 'sq ft' (converted to m²).
"""
import re

_RX = re.compile(r"(?:A|Area)\s*=?\s*([\d,]+(?:\.\d+)?)\s*(sq\s*m|m²|m2|sq\s*ft|ft²|ft2)", re.I)


def parse_area_m2(content):
    """Return area in m² from a markup label, or None. Converts ft² -> m²."""
    if not content:
        return None
    m = _RX.search(content)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower().replace(" ", "")
    if unit in ("sqft", "ft²", "ft2"):
        val *= 0.09290304            # square feet -> square metres
    return val
