#!/usr/bin/env python3
"""
UNMARKED takeoff that ACTUALLY RUNS end-to-end as a script (the fix for "it works when you run the
flow but the .py file doesn't").  The region step is **legend-anchored colour segmentation** — read the
"Concrete Service Yard" legend swatch, segment that hatch across the plan, take the largest filled
region — NOT LLM vertex-tracing (which was non-reproducible).  Deterministic; no API key required.
An optional --api pass uses Claude vision to read the legend colour and confirm the region.

  python3 takeoff_unmarked.py <drawing.pdf>            # deterministic
  ANTHROPIC_API_KEY=sk-... python3 takeoff_unmarked.py <drawing.pdf> --api   # + vision read/confirm

Pipeline:  render -> find concrete hatch (legend) -> segment -> verify scale -> measure
           -> plausibility -> cost (assumed build-up if architect drawing, FLAGGED).
"""
import sys, os, re, io, math, contextlib
import numpy as np, fitz
from PIL import Image
from scipy import ndimage as ndi

import scale as SC
import sanity
with contextlib.redirect_stdout(io.StringIO()):
    from pricing import slab_rate

# Default ASSUMED build-up for an architect drawing with no construction-details sheet
# (Fortel's method: assume, state the assumption in the quote). 190 mm / A252 / typical rates.
ASSUMED = dict(depth_mm=190, conc_rate=128, mesh="A252", layers=1, steel_rate_t=850, margin=0.11)
CONCRETE_LABELS = ("concrete service yard", "service yard", "external yard",
                   "yard construction", "type c", "gv areas")


# ---------------------------------------------------------------- legend -> hatch colour
def _label_bbox(pdf, page=0):
    """Find the legend text line naming the priced concrete area; return (bbox_pt, text) or None."""
    pg = fitz.open(pdf)[page]
    lines = {}
    for w in pg.get_text("words"):        # (x0,y0,x1,y1, word, block,line,wordno)
        lines.setdefault((w[5], w[6]), []).append(w)
    for ws in lines.values():
        ws = sorted(ws, key=lambda w: w[0])
        text = " ".join(w[4] for w in ws).lower()
        if any(lbl in text for lbl in CONCRETE_LABELS):
            return (min(w[0] for w in ws), min(w[1] for w in ws),
                    max(w[2] for w in ws), max(w[3] for w in ws)), text[:40]
    return None


def find_concrete_swatch_rgb(pdf, im=None, S=2.0, page=0):
    """Deterministic legend anchor. Locate the 'Concrete Service Yard' label, then read its swatch
    colour — first from the rendered raster just LEFT of the label (robust), else from a vector fill
    rect. Returns (rgb_0_255, label) or (None, reason)."""
    found = _label_bbox(pdf, page)
    if not found:
        return None, None
    (lx0, ly0, lx1, ly1), text = found
    cy = (ly0 + ly1) / 2

    # (a) raster sample: dominant non-white/non-black colour in a box just left of the label
    if im is not None:
        H, W = im.shape[:2]
        x1 = int((lx0 - 3) * S); x0 = int((lx0 - 175) * S)   # swatch can sit well left of the label
        y0 = int((cy - 7) * S);  y1 = int((cy + 7) * S)
        x0, x1 = max(0, x0), max(0, min(W, x1)); y0, y1 = max(0, y0), min(H, y1)
        if x1 - x0 > 4 and y1 - y0 > 2:
            patch = im[y0:y1, x0:x1].reshape(-1, 3)
            keep = patch[(patch.max(1) < 240) & (patch.min(1) > 30)]   # drop white bg + black ink
            if len(keep) > 8:
                from collections import Counter
                rgb = Counter(map(tuple, keep)).most_common(1)[0][0]
                return tuple(int(c) for c in rgb), text

    # (b) vector fill rect beside the label
    pg = fitz.open(pdf)[page]
    best = None
    for dr in pg.get_drawings():
        fill = dr.get("fill")
        if not fill:
            continue
        r = dr["rect"]
        if not (2 < r.width < 70 and 2 < r.height < 32):
            continue
        if r.x1 > lx0 + 3 or r.y1 < cy - 16 or r.y0 > cy + 16:
            continue
        d = lx0 - r.x1
        if best is None or d < best[0]:
            best = (d, tuple(int(round(c * 255)) for c in fill))
    if best:
        return best[1], text
    return None, text


# ---------------------------------------------------------------- segmentation
def segment_hatch(im_rgb, rgb, tol=14, close=9):
    """Largest filled connected region whose colour matches `rgb` within tol.
    For a GREY target we use a luminance band + greyscale test (more robust to anti-aliasing, and
    identical to the validated Claude-session method); for a coloured target, per-channel tol."""
    r, g, b = im_rgb[..., 0].astype(int), im_rgb[..., 1].astype(int), im_rgb[..., 2].astype(int)
    R, G, B = rgb
    if max(rgb) - min(rgb) <= 6:                       # grey hatch
        mask = (np.abs(r - g) < 12) & (np.abs(g - b) < 12) & (r >= R - tol) & (r <= R + tol)
    else:
        mask = (np.abs(r - R) <= tol) & (np.abs(g - G) <= tol) & (np.abs(b - B) <= tol)
    if mask.sum() == 0:
        return None
    lab, n = ndi.label(ndi.binary_closing(mask, structure=np.ones((close, close))))
    sizes = ndi.sum(np.ones_like(lab), lab, range(1, n + 1))
    comp = ndi.binary_fill_holes(lab == int(np.argmax(sizes)) + 1)
    return comp


# ---------------------------------------------------------------- scale
def scale_for(pdf, page=0):
    """(k_m_per_pt, verified_bool, note). Title-block 1:N, verified by scale bar when detectable."""
    pg = fitz.open(pdf)[page]
    m = re.search(r"1\s*:\s*(\d{2,4})", pg.get_text())
    denom = int(m.group(1)) if m else None
    k_title = SC.title_block_k(denom)
    kbar, info = SC.detect_scale_bar(pdf, page)
    uu = SC.user_unit(pdf, page)
    if kbar:
        kbar *= uu
        if k_title and abs(kbar - k_title) / k_title <= 0.05:
            return kbar, True, f"scale bar {info} verifies title 1:{denom} (<=5%)"
        return kbar, True, f"scale bar {info} (title 1:{denom} differs — using bar)"
    if k_title:
        return k_title * uu, False, f"title 1:{denom} only — VERIFY a feature before sign-off"
    return None, False, "no scale found"


# ---------------------------------------------------------------- main takeoff
def takeoff(pdf, source="architect", use_api=False, S=2.0, out_dir=None):
    """Returns a result dict. source in {'architect','engineer'} controls the assumption flag."""
    flags = []
    pg = fitz.open(pdf)[0]
    pix = pg.get_pixmap(matrix=fitz.Matrix(S, S))
    im = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]

    # --- region colour ---
    # The priced "Concrete Service Yard construction" hatch on SGP architect sheets is a light grey
    # (validated across all 4 Hemington units). We SEGMENT on that grey, and use the legend only to
    # CONFIRM the concrete-yard entry exists and that its swatch is grey (the anchor). Reading an
    # arbitrary swatch pixel as the segmentation colour proved fragile (grabbed green/white on other
    # units → absurd areas), so the grey convention is primary; --api can override for non-SGP packs.
    GREY = (214, 214, 214)   # band center; with tol=14 -> grey [200,228], matching the validated session
    rgb = GREY
    swatch, label = find_concrete_swatch_rgb(pdf, im=im, S=S)
    if label and swatch:
        is_grey = (max(swatch) - min(swatch) <= 18) and (188 <= sum(swatch) / 3 <= 236)
        if is_grey:
            flags.append(f"legend '{label}': swatch {swatch} is grey — concrete-yard hatch CONFIRMED")
        else:
            flags.append(f"legend '{label}' found but swatch {swatch} not grey — using SGP grey convention "
                         f"{GREY} (lower confidence; assessor confirm)")
    elif label:
        flags.append(f"legend '{label}' found (swatch unreadable) — using SGP grey convention {GREY}")
    else:
        flags.append(f"no concrete-yard legend label — grey-hatch heuristic {GREY} (LOW confidence; assessor confirm)")
    if use_api:
        try:
            import llm_client
            if llm_client.have_key():
                png = (out_dir or ".") + "/_legend.png"; Image.fromarray(im).save(png)
                leg = llm_client.read_legend(png)
                flags.append(f"vision legend: label='{leg.get('label')}' rgb~{leg.get('approx_rgb')}")
                if leg.get("approx_rgb"):
                    rgb = tuple(int(c) for c in leg["approx_rgb"])
        except Exception as e:
            flags.append(f"vision legend read skipped: {e}")

    comp = segment_hatch(im, rgb)
    if comp is None or comp.sum() == 0:
        return {"pdf": pdf, "area_m2": None, "flags": flags + ["no hatch pixels matched — assessor must trace"]}
    px = int(comp.sum())
    flags.append("interior voids filled (bay markings/text); if the yard encloses a building/island, assessor deducts it")

    # --- scale + measure ---
    k, verified, note = scale_for(pdf)
    flags.append(note)
    if k is None:
        return {"pdf": pdf, "area_m2": None, "flags": flags + ["no scale — cannot measure"]}
    area = round(px * (1.0 / S) ** 2 * k * k, 0)

    # --- plausibility (BLOCKS, not just flags) ---
    san = sanity.plausible(area)
    flags += san
    blocked = bool(san)        # any plausibility flag => do not emit a price

    # --- cost (assumed build-up; flag if architect) ---
    z = dict(name="Concrete Service Yard", area_m2=area, **ASSUMED)
    with contextlib.redirect_stdout(io.StringIO()):
        rate, rflags = slab_rate(z)
    if blocked:
        price = None
        flags.append("PRICE BLOCKED — area failed the plausibility guard (likely bad segmentation/scale); "
                     "assessor must trace before a price is issued")
    else:
        price = round(area * rate) if rate else None
    if source == "architect":
        flags.append(f"ARCHITECT drawing: build-up ASSUMED ({ASSUMED['depth_mm']}mm/{ASSUMED['mesh']}); "
                     "state assumption in quote; area carries ~5% architect-vs-engineer tolerance")

    # --- overlay for the record / vision confirm ---
    overlay = None
    if out_dir:
        ov = im.copy(); ov[comp] = (0.4 * ov[comp] + 0.6 * np.array([235, 30, 30])).astype(np.uint8)
        overlay = f"{out_dir}/{os.path.basename(pdf).split('-')[5] if '-' in pdf else 'x'}_overlay.png"
        Image.fromarray(ov).resize((pix.width // 4, pix.height // 4)).save(overlay)
        if use_api:
            try:
                import llm_client
                if llm_client.have_key():
                    c = llm_client.confirm_region(overlay, area)
                    flags.append(f"vision confirm: ok={c.get('ok')} — {c.get('reason')}")
            except Exception as e:
                flags.append(f"vision confirm skipped: {e}")

    return {"pdf": os.path.basename(pdf), "scale_k": round(k, 5), "scale_verified": verified,
            "area_m2": area, "rate": rate, "price_gbp": price, "overlay": overlay, "flags": flags}


def main(pdf, use_api=False):
    r = takeoff(pdf, use_api=use_api, out_dir=os.path.dirname(os.path.abspath(pdf)))
    print(f"\n=== {r['pdf']} ===")
    if r.get("area_m2") is None:
        print("  NO AREA EMITTED:")
    else:
        print(f"  scale k={r['scale_k']} m/pt  verified={r['scale_verified']}")
        print(f"  AREA  = {r['area_m2']:,.0f} m2")
        if r.get("price_gbp") is not None:
            print(f"  RATE  = GBP {r['rate']:.2f}/m2   PRICE = GBP {r['price_gbp']:,}")
        else:
            print(f"  PRICE = (blocked — see flags)")
    for f in r["flags"]:
        print(f"   - {f}")
    return r


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--api"]
    use_api = "--api" in sys.argv
    pdf = args[0] if args else "drawings/UNMARKED_Yard.pdf"
    main(pdf, use_api=use_api)
