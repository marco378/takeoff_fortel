#!/usr/bin/env python3
"""scale.detect_scale_bar: plain bar and fused intermediate ticks.

Sections in this module (printed in this order):
  - scale

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from reportlab.pdfgen import canvas
from scale import detect_scale_bar
from tests import ck

print("scale")
c = canvas.Canvas("/tmp/_sb.pdf", pagesize=(1400,2200)); c.rect(200,1000,1000,800); c.line(100,150,600,150); c.drawString(250,160,"0          50 m"); c.save()
k, info = detect_scale_bar("/tmp/_sb.pdf"); ck("scale-bar k=0.1", k == 0.1, info)

# Rotated Office GA bars expose intermediate ticks as fused 1m/5m/10m tokens. The terminal
# value must win over the tick nearest the raw bottom-right corner.
_ticks = canvas.Canvas("/tmp/_sb_fused_ticks.pdf", pagesize=(700, 500))
_ticks.line(100, 80, 383.44, 80)
_ticks.drawString(125, 90, "1m")
_ticks.drawString(240, 90, "5m")
_ticks.drawString(375, 90, "10m")
_ticks.save()
_tick_k, _tick_info = detect_scale_bar("/tmp/_sb_fused_ticks.pdf")
ck("scale bar uses terminal 10m tick, not intermediate 1m",
   _tick_k is not None and abs(_tick_k - 10 / 283.44) < 1e-5 and "10 m" in _tick_info,
   (_tick_k, _tick_info))

