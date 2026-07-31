#!/usr/bin/env python3
"""
Robust parsing of Bluebeam/CAD area-markup labels (round 4).
The first reader only matched 'A = N sq m'. Real labels vary — handle:
  'A = 26,080.2 sq m', 'Area = 930 m²', 'A=520sqm', and imperial 'sq ft' (converted to m²).
"""
import re

# Explicit Bluebeam measurement labels can occur inline.  Prefix-free values are accepted
# only when the value+unit occupy their own label line.  That preserves Inderjit's real labels
# (``Unit-1&2\r235.37 sq m``) without treating incidental prose such as
# ``Allow 12.50 sq m at this rate`` as benchmark truth.
_AREA_VALUE = r"([\d,]+(?:\.\d+)?)\s*(sq\s*m|m²|m2|sq\s*ft|ft²|ft2)"
_PREFIXED_LINE_RX = re.compile(rf"^\s*(?:A|Area)\s*=?\s*{_AREA_VALUE}\s*$", re.I)
_BARE_LINE_RX = re.compile(rf"^\s*{_AREA_VALUE}\s*$", re.I)


def parse_area_m2(content):
    """Return area in m² from a markup label, or None. Converts ft² -> m²."""
    if not content:
        return None
    content = str(content)
    # PyMuPDF normalises Bluebeam CR/LF label breaks inconsistently, so split on either.
    lines = re.split(r"[\r\n]+", content)
    m = next((_PREFIXED_LINE_RX.fullmatch(line) for line in lines
              if _PREFIXED_LINE_RX.fullmatch(line)), None)
    if not m:
        m = next((_BARE_LINE_RX.fullmatch(line) for line in lines
                  if _BARE_LINE_RX.fullmatch(line)), None)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower().replace(" ", "")
    if unit in ("sqft", "ft²", "ft2"):
        val *= 0.09290304            # square feet -> square metres
    return val
