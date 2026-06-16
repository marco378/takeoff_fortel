#!/usr/bin/env python3
"""
Generate a synthetic CAD-style site plan with KNOWN geometry, so we can prove the
engine measures correctly before the real Winvic PDFs are mounted.

Scenario (mirrors TC1/TC5/TC9):
  - External yard slab: 200.0 m x 130.4 m = 26,080 m2  (gross)
  - One dock-leveller void: 20 m x 8 m = 160 m2          (to be EXCLUDED)  -> net 25,920
  - 26 manhole markers (0.6 m)                            (to be COUNTED)
  - "Scale 1:200" label                                   (to be auto-detected)
"""
from reportlab.pdfgen import canvas

SCALE = 200
def m2pt(x_m):                      # real metres -> paper points at SCALE
    return (x_m / SCALE) / 0.0254 * 72

W, H, MARGIN = 200.0, 130.4, 20.0
pw, ph = m2pt(W + 2 * MARGIN), m2pt(H + 2 * MARGIN + 30)
c = canvas.Canvas("drawings/synthetic_yard.pdf", pagesize=(pw, ph))
ox, oy = m2pt(MARGIN), m2pt(MARGIN)

# Yard slab (gross 26,080 m2)
c.setStrokeColorRGB(0, 0, 0); c.setLineWidth(1); c.setFillColorRGB(0.85, 0.9, 0.95)
c.rect(ox, oy, m2pt(W), m2pt(H), stroke=1, fill=1)

# Dock-leveller void (160 m2) — excluded by the engine
c.setFillColorRGB(1, 1, 1)
c.rect(ox + m2pt(150), oy + m2pt(5), m2pt(20.0), m2pt(8.0), stroke=1, fill=1)

# 26 manhole markers (0.6 m squares) — counted
c.setFillColorRGB(0.2, 0.2, 0.2)
for i in range(26):
    rx, ry = 10 + (i % 13) * 14.0, 30 + (i // 13) * 60.0
    c.rect(ox + m2pt(rx), oy + m2pt(ry), m2pt(0.6), m2pt(0.6), stroke=0, fill=1)

c.setFillColorRGB(0, 0, 0)
c.drawString(ox, oy + m2pt(H) + 20, "PROPOSED SITE PLAN - EXTERNAL YARD    Scale 1:200")
c.save()
print("wrote drawings/synthetic_yard.pdf  (gross 26,080  void 160  net 25,920  manholes 26)")
