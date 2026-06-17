#!/usr/bin/env python3
"""
LLM-in-the-loop unmarked takeoff — the identification step is a REAL Claude vision call
(this replaces the hardcoded-polygon stub the team correctly flagged).

  render(pdf)            -> PNG with a PDF-point coordinate grid
  identify_slab(png)     -> Claude VISION returns {vertices, voids, scale_check}
  measure(vertices,k)    -> exact area (requires an explicit calibrated k)
  main(pdf)              -> full hardened pipeline: render -> identify -> calibrate -> measure

Run:  ANTHROPIC_API_KEY=sk-... python3 vision_llm.py drawings/UNMARKED_Yard.pdf
      python3 vision_llm.py drawings/UNMARKED_Yard.pdf --demo   # offline demo (yard only)
"""
import sys, os, json, base64, math, fitz
from shapely.geometry import Polygon
from PIL import Image, ImageDraw

PROMPT = """You are a concrete-takeoff estimator working a construction / external-works drawing
(NOT a generic 'site plan'). The image has a red coordinate grid labelled in PDF points (every 250).
This mirrors how Fortel's estimators actually work:

STEP 1 - read the LEGEND/key. Find which hatch is the priced CONCRETE service/external yard
(named 'service yard', 'Type C', 'GV areas', 'external yard construction' - it varies per drawing).
Distinguish it from tarmac/bituminous, car park, and landscaping.

STEP 2 - trace the boundary of the CONCRETE-hatch area ONLY (the priced slab). Exclude tarmac,
landscaping, the roundabout, and anything outside that hatch. Trace any VOIDS to deduct.

STEP 3 - give a SCALE-CHECK feature so the scale can be VERIFIED against a known real size (Fortel
verify every drawing this way, because the PDF is often not at its stated scale): the two corners of a
CAR-PARKING BAY (real width 2.5 m), or a printed DIMENSION line + value, or a SCALE BAR + its metres.

Reply with ONLY JSON:
{{"vertices": [[x,y],...], "voids": [[[x,y],...], ...],
  "scale_check": {{"feature": "parking_bay|dimension|scale_bar", "p1": [x,y], "p2": [x,y], "metres": 2.5}}}}
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
    """REAL Claude vision call -> {vertices, voids, scale_check} dict.
    Requires ANTHROPIC_API_KEY. Parses the full JSON schema including scale_check."""
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    b64 = base64.b64encode(open(png, "rb").read()).decode()
    msg = client.messages.create(
        model="claude-opus-4-8", max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": PROMPT.format(zone=zone)}]}])
    txt = msg.content[0].text
    parsed = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    # Return full dict: vertices, voids, scale_check (all fields may be absent if LLM omits them)
    return {
        "vertices": parsed.get("vertices", []),
        "voids": parsed.get("voids", []),
        "scale_check": parsed.get("scale_check"),  # None if LLM did not provide one
    }


def measure(vertices, k):
    """k MUST be a per-viewport calibrated scale (m/pt). NEVER hardcode it — adversarial
    testing showed a constant k gives 63-900% errors across drawings, and the title-block
    scale is unreliable (the yard reads 26,080 at k=0.108 but the sheet says 1:500=k=0.176)."""
    if k is None:
        raise ValueError("scale required — calibrate per viewport (scale bar / known dimension); flag for assessor")
    return Polygon(vertices).area * k * k


def main(pdf, zone="external yard"):
    """Full hardened pipeline for a single PDF drawing.

    With ANTHROPIC_API_KEY:
      render -> identify_slab (gets vertices + voids + scale_check)
      -> calibrate_verified (feature WINS over title-block, Fortel's rule)
      -> measure_regions (with voids) -> sanity.plausible
      -> prints scale source, area, ALL flags

    Without ANTHROPIC_API_KEY:
      Prints an honest 'no key / cannot identify' message. Does NOT fabricate any area
      number. The bare title-block scale must never be trusted for an arbitrary drawing.
    """
    import re as _re
    from scale import calibrate_verified, user_unit
    from geometry import measure_regions
    import sanity

    png = render(pdf)
    print(f"[vision_llm] rendered: {png}")

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "\nERROR: ANTHROPIC_API_KEY is not set.\n"
            "Live vision identification is unavailable without it.\n"
            "Cannot identify the slab region for this drawing.\n"
            "Cannot emit an area — the bare title-block scale must NEVER be trusted\n"
            "for an arbitrary drawing (it is wrong on ~40% of Fortel PDFs).\n"
            "ACTION: set ANTHROPIC_API_KEY and re-run, or have an assessor trace\n"
            "the boundary manually and supply vertices + a verified scale feature."
        )
        return

    # --- live path ---
    result = identify_slab(png, zone)
    vertices = result["vertices"]
    voids_list = result["voids"]        # list of void vertex-lists
    scale_check = result["scale_check"] # {feature, p1, p2, metres} or None

    print(f"[Claude vision]  {len(vertices)} boundary vertices, {len(voids_list)} voids")

    if not scale_check:
        print(
            "\nWARNING: Claude did not return a scale_check feature.\n"
            "Scale cannot be verified — area NOT emitted.\n"
            "ACTION: assessor must supply a known-size feature (parking bay / dimension / scale bar)."
        )
        return

    # Distance between the two feature points (in PDF points)
    p1, p2 = scale_check["p1"], scale_check["p2"]
    span_pt = math.dist(p1, p2)
    feature = scale_check["feature"]
    metres = scale_check.get("metres", 2.5)

    # Pull title-block denominator for comparison only (Fortel's rule: feature wins)
    sm = _re.search(r"1\s*:\s*(\d{2,4})", fitz.open(pdf)[0].get_text())
    title_denom = int(sm.group(1)) if sm else None

    if feature == "parking_bay":
        k, flags = calibrate_verified(
            title_denominator=title_denom,
            bay_width_pt=span_pt,
        )
    else:
        # dimension or scale_bar: use span_pt as the dimension span
        k, flags = calibrate_verified(
            title_denominator=title_denom,
            dim_span_pt=span_pt,
            dim_m=metres,
        )

    # Apply PDF /UserUnit multiplier (large drawings often have UserUnit != 1)
    uu = user_unit(pdf)
    k = k * uu
    if uu != 1.0:
        flags.append(f"UserUnit={uu} applied")

    print(f"  scale source:  {feature} ({metres} m / {span_pt:.0f} pt)  k={k:.5f} m/pt")
    for f in flags:
        print(f"  FLAG: {f}")

    # Build holes dict for measure_regions: {0: [void_verts, ...]}
    holes = {0: voids_list} if voids_list else {}
    area, geo_flags = measure_regions([vertices], k, holes)

    san_flags = sanity.plausible(area)
    all_flags = flags + geo_flags + san_flags

    print(f"\n  slab area:  {area:,.0f} m2")
    if all_flags:
        print("  FLAGS:")
        for f in all_flags:
            print(f"    - {f}")
    else:
        print("  (no flags — result looks plausible)")


# ---------------------------------------------------------------------------
# DEMO constants (ONLY used when --demo flag is passed AND file is the yard)
# ---------------------------------------------------------------------------
_DEMO_YARD_FILE = "drawings/UNMARKED_Yard.pdf"
_DEMO_YARD_VERTS = [
    [545, 610], [900, 595], [1400, 595], [1915, 600], [1925, 1000], [1915, 1500],
    [1880, 1760], [1790, 1985], [1650, 2150], [1430, 2250], [1150, 2278], [850, 2268],
    [620, 2140], [540, 1860], [515, 1400], [515, 950], [530, 720],
]
_DEMO_SCALE_CHECK = {
    "feature": "parking_bay",
    "p1": [545, 610], "p2": [568, 610],   # ~23.1 pt span -> k ~0.108 m/pt
    "metres": 2.5,
}


def _run_demo(pdf):
    """Offline demo: only valid for the yard drawing. Uses the in-session LLM identification."""
    import re as _re
    from scale import calibrate_verified, user_unit
    from geometry import measure_regions
    import sanity

    if not os.path.abspath(pdf).endswith(os.path.abspath(_DEMO_YARD_FILE).lstrip("/")):
        # Safety: refuse to apply the yard polygon to a different drawing
        if _DEMO_YARD_FILE not in os.path.abspath(pdf):
            print(
                f"ERROR: --demo is only valid for '{_DEMO_YARD_FILE}'.\n"
                f"Refusing to apply the hardcoded yard polygon to '{pdf}' — it would be meaningless."
            )
            return

    print("[DEMO MODE — yard only, no API key needed]")
    verts = _DEMO_YARD_VERTS
    scale_check = _DEMO_SCALE_CHECK

    p1, p2 = scale_check["p1"], scale_check["p2"]
    span_pt = math.dist(p1, p2)
    metres = scale_check.get("metres", 2.5)

    sm = _re.search(r"1\s*:\s*(\d{2,4})", fitz.open(pdf)[0].get_text())
    title_denom = int(sm.group(1)) if sm else None

    k, flags = calibrate_verified(title_denominator=title_denom, bay_width_pt=span_pt)
    uu = user_unit(pdf)
    k = k * uu

    print(f"  {len(verts)} demo vertices (in-session Claude identification)")
    print(f"  scale: parking_bay {metres} m / {span_pt:.1f} pt  k={k:.5f} m/pt")
    for f in flags:
        print(f"  FLAG: {f}")

    area, geo_flags = measure_regions([verts], k)
    san_flags = sanity.plausible(area)
    all_flags = flags + geo_flags + san_flags

    print(f"\n  slab area:  {area:,.0f} m2")
    for f in all_flags:
        print(f"  FLAG: {f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    demo = "--demo" in args
    args = [a for a in args if a != "--demo"]

    pdf = args[0] if args else "drawings/UNMARKED_Yard.pdf"
    zone = args[1] if len(args) > 1 else "external yard"

    if demo:
        _run_demo(pdf)
    else:
        main(pdf, zone)
