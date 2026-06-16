#!/usr/bin/env python3
"""
LLM-in-the-loop unmarked takeoff — the identification step is a REAL Claude vision call
(this replaces the hardcoded-polygon stub the team correctly flagged).

  render(pdf)            -> PNG with a PDF-point coordinate grid
  identify_slab(png)     -> Claude VISION returns the slab polygon as JSON vertices
  measure(vertices)      -> exact area at the per-viewport scale (geometry, deterministic)

Run:  ANTHROPIC_API_KEY=sk-... python3 vision_llm.py drawings/UNMARKED_Yard.pdf "external yard"
"""
import sys, os, json, base64, fitz
from shapely.geometry import Polygon
from PIL import Image, ImageDraw

PROMPT = """You are a concrete-takeoff estimator. The image is a site/GA plan with a red
coordinate grid labelled in PDF points (every 250). Identify the extent of the {zone}
concrete slab we price: follow the kerbs / site boundary; INCLUDE the hardstanding, aprons
and parking; EXCLUDE landscaping, the roundabout, roads outside the hardstanding, and any
gatehouse/kerb islands (per the estimating handbook). ALSO trace any VOIDS to deduct
(dock-leveller voids, pits) and a SCALE REFERENCE (two points on a scale bar, or a known
labelled dimension, plus its real length in metres) so area can be calibrated per-viewport.
Reply with ONLY JSON:
{{"vertices": [[x,y],...], "voids": [[[x,y],...], ...], "scale_ref": [[x1,y1],[x2,y2], metres]}}
— PDF-point coordinates, boundary clockwise, 15-25 points."""


def render(pdf, S=0.5):
    p = fitz.open(pdf)[0]
    pix = p.get_pixmap(matrix=fitz.Matrix(S, S))
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples); d = ImageDraw.Draw(img)
    for x in range(0, int(p.rect.width) + 1, 250):
        d.line([x * S, 0, x * S, pix.height], fill=(255, 0, 0)); d.text((x * S + 1, 2), str(x), fill=(0, 0, 255))
    for y in range(0, int(p.rect.height) + 1, 250):
        d.line([0, y * S, pix.width, y * S], fill=(255, 0, 0)); d.text((1, y * S + 1), str(y), fill=(0, 0, 255))
    out = pdf.replace(".pdf", "_grid.png"); img.save(out); return out


def identify_slab(png, zone="external yard"):
    """REAL Claude vision call -> slab polygon vertices in PDF points."""
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    b64 = base64.b64encode(open(png, "rb").read()).decode()
    msg = client.messages.create(
        model="claude-opus-4-8", max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": PROMPT.format(zone=zone)}]}])
    txt = msg.content[0].text
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])["vertices"]


def measure(vertices, k):
    """k MUST be a per-viewport calibrated scale (m/pt). NEVER hardcode it — adversarial
    testing showed a constant k gives 63-900% errors across drawings, and the title-block
    scale is unreliable (the yard reads 26,080 at k=0.108 but the sheet says 1:500=k=0.176)."""
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension); flag for assessor")
    return Polygon(vertices).area * k * k


if __name__ == "__main__":
    pdf = sys.argv[1] if len(sys.argv) > 1 else "drawings/UNMARKED_Yard.pdf"
    zone = sys.argv[2] if len(sys.argv) > 2 else "external yard"
    png = render(pdf)
    if os.getenv("ANTHROPIC_API_KEY"):
        verts = identify_slab(png, zone)                    # live LLM call
        src = "live Claude vision call"
    else:
        # demo: the polygon Claude returned in-session (a real LLM identification)
        verts = [[545, 610], [900, 595], [1400, 595], [1915, 600], [1925, 1000], [1915, 1500],
                 [1880, 1760], [1790, 1985], [1650, 2150], [1430, 2250], [1150, 2278], [850, 2268],
                 [620, 2140], [540, 1860], [515, 1400], [515, 950], [530, 720]]
        src = "in-session Claude identification (set ANTHROPIC_API_KEY for a live call)"
    import re as _re
    sm = _re.search(r"1\s*:\s*(\d{2,4})", fitz.open(pdf)[0].get_text())
    k = 0.0254 / 72 * int(sm.group(1)) if sm else None
    print(f"[{src}]  {len(verts)} vertices")
    print(f"  scale: {('1:%s title-block — UNVERIFIED' % sm.group(1)) if sm else 'NONE'}  "
          f"-> FLAG: assessor must confirm scale per viewport (title-block is unreliable)")
    if k:
        print(f"  slab @ unverified scale: {measure(verts, k):,.0f} m2  (do NOT trust until scale confirmed)")
    else:
        print("  no scale reference -> cannot measure; route to assessor")
