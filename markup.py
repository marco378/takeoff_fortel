#!/usr/bin/env python3
"""
Robust parsing of Bluebeam/CAD area-markup labels (round 4).
The first reader only matched 'A = N sq m'. Real labels vary — handle:
  'A = 26,080.2 sq m', 'Area = 930 m²', 'A=520sqm', and imperial 'sq ft' (converted to m²).
"""
import re

# The 'A =' / 'Area =' prefix is OPTIONAL. Real Fortel markups from Inderjit label areas by
# what the region IS, not with an "A=" prefix — e.g. 'Unit-1&2\r235.37 sq m', 'Unit-3\r133.79
# sq m' (Tanro Voltage Park, 31 Jul). Requiring the prefix made read_marked return 0 m² / 0
# regions on his real markups, which would have scored his own ground truth as ZERO in the
# accuracy harness — we'd have looked catastrophically wrong when we simply could not read his
# labels. The number must still be immediately followed by an area unit, so bare dimensions
# ('150 mm', '1:200') and linear labels ('12.30 m') are not mistaken for areas.
_RX = re.compile(r"(?:(?:A|Area)\s*=?\s*)?([\d,]+(?:\.\d+)?)\s*(sq\s*m|m²|m2|sq\s*ft|ft²|ft2)\b", re.I)


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
