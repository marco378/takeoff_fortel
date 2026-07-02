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
import cv2

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
# Fraction of the rendered page treated as "outer margin" — a sheet-frame border strip
# or ruled border line living out here is never part of the priced yard hatch. Kept small
# deliberately: real yards routinely run close to the page edge on tightly-cropped sheets,
# so this must stay narrow enough to never clip genuine yard geometry (see MARGIN_FRAC note
# below and the _int_d77 regression guard in ci_tests.py / robustness_tests.py).
MARGIN_FRAC = 0.025
# A component smaller than this fraction of the largest plausible component's area is
# treated as a legend swatch / title-block chip / stray glyph, not a second yard region.
SATELLITE_FRAC = 0.015


def segment_hatch(im_rgb, rgb, tol=14, close=9, k=None, S=2.0, max_void_m2=1.0,
                  title_block_frac=0.0, exclude_border=True, _diag=None):
    """Best-plausible connected region of the concrete-yard hatch.

    Changes vs original:
    - Best-plausible selection: components are sorted largest-first; the first one whose
      area falls in the plausible service-yard range (200–50,000 m²) is chosen.  Falls
      back to the absolute largest if none pass (e.g. no scale yet).
    - Small interior holes (paint blocks, text) are still filled; large voids (dock bays,
      islands) are left as deductions — unchanged from original.
    - Optional title-block exclusion (`title_block_frac` > 0): mask out the bottom fraction
      of the sheet before segmentation so a legend swatch / title-block panel can't be
      selected.  DEFAULT 0.0 (OFF) — on real yard sheets the concrete slab routinely runs
      into the bottom 12% of the page, and a 0.12 cut silently deleted that area (Demo-4
      regression: D77-style yards lost ~13% / returned no plausible component).  The
      best-plausible component selector already rejects the small title-block blob, so the
      crop is not needed for correctness; leave it OFF unless a specific sheet needs it.
    - Border/legend exclusion (`exclude_border=True`, DEFAULT ON): fixes the real-sheet
      over-measurement Aryan found on the SGP architect PDFs (D77 measured 3,172 vs gold
      3,156; D219 similarly over-inclusive). A sheet-frame border strip is drawn as a ruled
      line/rect running along the page edge and is frequently the SAME grey as the yard
      hatch, and a legend colour swatch is a small isolated chip near the title block —
      both get picked up by the grey mask. Two passes:
        1. MARGIN STRIP: any mask pixel inside the outer MARGIN_FRAC of the page (border
           frame lives here almost by definition) is dropped from the mask BEFORE labeling.
           This has to happen pre-closing/pre-labeling, not as a post-hoc component filter,
           because a border frame that touches/overlaps the yard's own bounding edge would
           otherwise fuse into the same connected component via binary_closing and inflate
           its area directly rather than appearing as a separate small blob.
        2. SATELLITE COMPONENTS: after labeling, any component whose area is <SATELLITE_FRAC
           of the chosen (best-plausible) component's area is dropped — legend swatches and
           stray title-block chips are a tiny fraction of the yard; a genuine multi-part yard
           is not (kept deliberately generous so multi-region yards survive).
      Excluded pixels are reported via `_diag['excluded_components']` / `_diag['excluded_m2']`
      so the caller can flag what was dropped for the assessor.
    """
    r, g, b = im_rgb[..., 0].astype(int), im_rgb[..., 1].astype(int), im_rgb[..., 2].astype(int)
    R, G, B = rgb
    if max(rgb) - min(rgb) <= 6:                       # grey hatch
        mask = (np.abs(r - g) < 12) & (np.abs(g - b) < 12) & (r >= R - tol) & (r <= R + tol)
    else:
        mask = (np.abs(r - R) <= tol) & (np.abs(g - G) <= tol) & (np.abs(b - B) <= tol)

    # ── Exclude sheet-frame border strip (outer margin band) ──────────────────
    # Must run BEFORE closing/labeling — see docstring. Pixels here are zeroed outright,
    # not just excluded from being "the chosen component", so a border strip that runs
    # up to (or over) the yard's own edge can't bridge into the yard's connected component.
    margin_excluded_px = 0
    if exclude_border:
        H, W = mask.shape
        my = max(1, int(round(H * MARGIN_FRAC)))
        mx = max(1, int(round(W * MARGIN_FRAC)))
        border_band = np.zeros_like(mask)
        border_band[:my, :] = True
        border_band[-my:, :] = True
        border_band[:, :mx] = True
        border_band[:, -mx:] = True
        margin_excluded_px = int((mask & border_band).sum())
        mask = mask & ~border_band

    # ── Exclude title block / legend panel (bottom of drawing) ───────────────
    # Closing is applied only to the active (non-title-block) rows so the kernel
    # cannot create boundary artefacts at the cutoff edge (fixes ~6 m² over-count
    # that was introduced when title-block masking was added in 512b982).
    if title_block_frac > 0:
        cutoff = int(im_rgb.shape[0] * (1.0 - title_block_frac))
        active = mask[:cutoff, :]
        closed_active = ndi.binary_closing(active, structure=np.ones((close, close)))
        mask = np.zeros_like(mask)
        mask[:cutoff, :] = closed_active
    else:
        mask = ndi.binary_closing(mask, structure=np.ones((close, close)))

    if mask.sum() == 0:
        return None
    lab, n = ndi.label(mask)
    sizes = ndi.sum(np.ones_like(lab), lab, range(1, n + 1))

    # ── Pick the best plausible component (not just the largest) ─────────────
    # pixels → m²: area = px * (1/S)² * k²  → px_per_m2 = S²/k²
    order = list(np.argsort(sizes)[::-1])   # indices sorted largest-first
    best_idx = order[0]                      # fallback: absolute largest
    if k is not None:
        px_per_m2 = (S * S) / (k * k)
        _MIN_M2, _MAX_M2 = 200, 50_000      # plausible single service-yard range
        for idx in order:
            cand_m2 = sizes[idx] / px_per_m2
            if _MIN_M2 <= cand_m2 <= _MAX_M2:
                best_idx = idx
                break
    comp = lab == best_idx + 1              # NOT hole-filled yet

    # ── Drop satellite components (legend swatches, stray chips) ─────────────
    # Keep the chosen component plus any OTHER component that is a meaningful fraction
    # of its area (a real multi-part yard); drop the rest. Report what was excluded.
    if exclude_border and n > 1:
        best_size = sizes[best_idx]
        satellite_ids = [i + 1 for i in range(n)
                         if i != best_idx and sizes[i] < SATELLITE_FRAC * best_size]
        excluded_satellite_px = int(sum(sizes[i - 1] for i in satellite_ids))
    else:
        excluded_satellite_px = 0

    if _diag is not None:
        total_excluded_px = margin_excluded_px + excluded_satellite_px
        n_excluded = (1 if margin_excluded_px > 0 else 0) + \
                     (len(satellite_ids) if exclude_border and n > 1 else 0)
        if k is not None and total_excluded_px > 0:
            px_per_m2 = (S * S) / (k * k)
            _diag['excluded_components'] = n_excluded
            _diag['excluded_m2'] = round(total_excluded_px / px_per_m2, 1)
            _diag['excluded_margin_m2'] = round(margin_excluded_px / px_per_m2, 1)
            _diag['excluded_satellite_m2'] = round(excluded_satellite_px / px_per_m2, 1)

    # ── Size-limited fill: paint/text holes filled; dock bays / islands kept ─
    if k:
        px_per_m2 = (S * S) / (k * k)
        if _diag is not None:
            _diag['raw_hatch_m2'] = round(int(comp.sum()) / px_per_m2, 1)
        filled = ndi.binary_fill_holes(comp)
        hl, hn = ndi.label(filled & ~comp)
        if hn:
            hsz = ndi.sum(np.ones_like(hl), hl, range(1, hn + 1))
            small_ids = [i + 1 for i in range(hn) if hsz[i] < max_void_m2 * px_per_m2]
            small = np.isin(hl, small_ids)
            if _diag is not None:
                _diag['void_fill_m2'] = round(int(small.sum()) / px_per_m2, 1)
                _diag['void_count'] = len(small_ids)
            comp = comp | small
    return comp


# ---------------------------------------------------------------- polygon contour helper
def _hatch_contour(comp, S, max_pts=180):
    """Outer contour of hatch mask -> [[x,y]] in PDF-POINT coordinates, or None.

    Coordinate space: PDF points -- the SAME canonical space used by render_snapshot()
    (which multiplies by the render scale), the vision path, and measure_regions(). The
    portal converts these to canvas pixels once, by x snapScale. The mask was rendered at
    S px per PDF point, so mask-pixel -> PDF-point is simply / S.

    Approach: trace the actual outer boundary with cv2.findContours (RETR_EXTERNAL), which
    walks pixel adjacency and returns vertices in path order, then simplify with
    Douglas-Peucker (approxPolyDP) down to <= max_pts vertices.

    Why not angular sort from the centroid (the previous approach): sorting boundary pixels
    by angle and decimating only yields a clean outline for strictly star-shaped regions.
    A real service yard is non-convex (dock-bay notches, L-shapes), so a ray from the
    centroid crosses the boundary 2-4 times; angular order then interleaves near and far
    pixels and the decimated polygon zig-zags across the slab -- the "lines radiate from a
    corner / fan-star" rendering bug. Boundary tracing follows the perimeter in order, so
    concavities are traced correctly instead of being bridged by spokes."""
    try:
        mask = (np.asarray(comp) > 0).astype(np.uint8) * 255
        if mask.sum() == 0:
            return None
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        c = max(cnts, key=cv2.contourArea)            # largest external boundary
        if len(c) < 3 or cv2.contourArea(c) < 6:      # too small / degenerate
            return None
        # Douglas-Peucker: start tight, loosen until vertex count fits max_pts.
        peri = cv2.arcLength(c, True)
        eps = 0.001 * peri
        approx = cv2.approxPolyDP(c, eps, True)
        while len(approx) > max_pts and eps < 0.05 * peri:
            eps *= 1.5
            approx = cv2.approxPolyDP(c, eps, True)
        pts = approx.reshape(-1, 2)
        if len(pts) < 3:
            return None
        inv = 1.0 / S   # mask pixel -> PDF point (mask was rendered at S px/pt)
        return [[float(x * inv), float(y * inv)] for x, y in pts]
    except Exception:
        return None


# ---------------------------------------------------------------- manhole detector (UNMARKED, conservative)
# Real manhole covers/chambers on a site plan are typically drawn ~0.6-1.5 m diameter.
MANHOLE_DIAM_M_MIN = 0.5
MANHOLE_DIAM_M_MAX = 1.8


def detect_manholes(im_rgb, comp, k, S=2.0):
    """Conservative small near-circular contour detector INSIDE the measured yard polygon.

    This is an ESTIMATE, never authoritative — the unmarked path has no reliable way to
    distinguish a manhole cover symbol from a gully, a stray annotation circle, or a dimension
    bubble on a rendered raster, so the result is always surfaced as manhole_count_estimate
    with a flag telling the assessor to confirm it, never as a bare manhole_count (that field
    is reserved for the MARKED path where Fortel has placed an explicit marker).

    Method: cv2.HoughCircles on the greyscale render, restricted to a radius band scaled by k
    (m/pt) so only real-manhole-sized circles (MANHOLE_DIAM_M_MIN..MAX diameter) are candidates,
    and restricted to centres that fall INSIDE the measured yard mask `comp` (so kerb radii,
    dimension arrows, and title-block symbols outside the yard are never counted).

    Returns (count, centres_px) — centres_px is a list of (x, y) in mask-pixel space (S px/pt),
    for overlay/debugging; count is the conservative estimate.
    """
    if k is None or comp is None or comp.sum() == 0:
        return 0, []
    px_per_m = S / k
    r_min_px = max(1, int(round((MANHOLE_DIAM_M_MIN / 2) * px_per_m)))
    r_max_px = max(r_min_px + 1, int(round((MANHOLE_DIAM_M_MAX / 2) * px_per_m)))

    gray = cv2.cvtColor(np.ascontiguousarray(im_rgb), cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    try:
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(4, r_min_px * 2),
            param1=80, param2=28, minRadius=r_min_px, maxRadius=r_max_px)
    except cv2.error:
        return 0, []

    if circles is None:
        return 0, []

    H, W = comp.shape
    centres = []
    for cxf, cyf, rf in circles[0]:
        cx, cy = int(round(cxf)), int(round(cyf))
        if 0 <= cy < H and 0 <= cx < W and comp[cy, cx]:
            centres.append((cx, cy))
    return len(centres), centres


# ---------------------------------------------------------------- drawing style guard
def drawing_style(im, white_thresh=233, thresh=0.03):
    """Colour-coded (solid fills, e.g. SGP architect) vs line/hatch (engineer kerbing drawings: mostly
    white with thin coloured lines + diagonal hatching). Team feedback: solid-fill colour segmentation
    gives 'entirely wrong area' on line/hatch sheets, so we detect and refuse rather than guess.
    Metric = fraction of SOLID fill (erode 2px: solid fills survive, thin lines/hatching vanish). This
    is robust to white margin — a small colour-coded drawing on a sparse 1:750 sheet still passes,
    whereas dense line-art does not. Returns (style, solid_fill_fraction)."""
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    nonwhite = ~((r > white_thresh) & (g > white_thresh) & (b > white_thresh))
    solid = float(ndi.binary_erosion(nonwhite, iterations=2).mean())
    return ("colour-coded" if solid > thresh else "line/hatch"), solid


# ---------------------------------------------------------------- scale
SCALE_BAR_AGREE_TOL = 0.03   # ±3 % — bar and title-block must agree within this to verify

def scale_for(pdf, page=0):
    """(k_m_per_pt, verified_bool, note, sources).

    verified_bool is True ONLY when a physical scale bar is detected AND it agrees with the
    title-block stated scale within SCALE_BAR_AGREE_TOL (±3 %).  In all other cases it is False
    and `note` explains why (no bar found / bar disagrees by X%).

    sources: dict with keys 'title_block' and/or 'scale_bar' recording the contributing values.
    """
    pg = fitz.open(pdf)[page]
    m = re.search(r"1\s*:\s*(\d{2,4})", pg.get_text())
    denom = int(m.group(1)) if m else None
    k_title = SC.title_block_k(denom)
    kbar, info = SC.detect_scale_bar(pdf, page)
    uu = SC.user_unit(pdf, page)

    sources = {}
    if k_title:
        sources["title_block"] = {"denom": denom, "k": round(k_title * uu, 6)}

    if kbar:
        kbar *= uu
        sources["scale_bar"] = {"info": info, "k": round(kbar, 6)}
        if k_title:
            # Two independent sources — run them through scale.scale_consensus (same tolerance
            # mechanism as the multi-reference guard that fixed the 95,463 m² incident) rather
            # than a bespoke pct-diff check. consensus expects (real_metres, span_units) pairs;
            # both sources already reduce to a single k (m/pt), so use span=1 for each and let
            # consensus do the agree/disagree math at SCALE_BAR_AGREE_TOL.
            k_title_full = k_title * uu
            k_consensus, cflags = SC.scale_consensus([(kbar, 1), (k_title_full, 1)], tol=SCALE_BAR_AGREE_TOL)
            pct_diff = abs(kbar - k_title_full) / k_title_full
            if k_consensus is not None:
                note = (f"scale bar ({info}) AGREES with title 1:{denom} "
                        f"(diff {pct_diff*100:.1f}% ≤ {SCALE_BAR_AGREE_TOL*100:.0f}%) — VERIFIED "
                        f"[{cflags[0]}]")
                return kbar, True, note, sources
            else:
                note = (f"scale bar ({info}) DISAGREES with title 1:{denom}: "
                        f"bar k={kbar:.5f} vs title k={k_title_full:.5f} "
                        f"({pct_diff*100:.1f}% > {SCALE_BAR_AGREE_TOL*100:.0f}%) — "
                        "using bar scale; assessor should confirm which is correct")
                return kbar, False, note, sources
        else:
            # Bar found but no title-block scale to compare against
            note = f"scale bar {info} (no title-block 1:N found to cross-check) — unverified"
            return kbar, False, note, sources

    if k_title:
        return k_title * uu, False, f"title 1:{denom} only — no scale bar detected; VERIFY a feature before sign-off", sources
    return None, False, "no scale found", {}


# ---------------------------------------------------------------- main takeoff
def takeoff(pdf, source="architect", use_api=False, S=2.0, out_dir=None):
    """Returns a result dict. source in {'architect','engineer'} controls the assumption flag."""
    flags = []
    pg = fitz.open(pdf)[0]
    pix = pg.get_pixmap(matrix=fitz.Matrix(S, S))
    im = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]

    # --- drawing-style guard (team feedback #2: don't give a wrong number on non-colour-coded sheets) ---
    style, solid = drawing_style(im)
    flags.append(f"drawing style: {style} (solid-fill {solid*100:.0f}%)")
    if style == "line/hatch":
        return {"pdf": os.path.basename(pdf), "area_m2": None, "style": style, "price_gbp": None,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + [
                    "NON-COLOUR-CODED (line/hatch) drawing — solid-fill colour segmentation does NOT apply "
                    "(it scrapes stray grey -> wrong area). Route to hatch-mode / Claude vision / assessor "
                    "trace. No area emitted (this is the fix for the 'entirely wrong area' the team hit)."]}

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
            # Keep SEGMENTING on the validated GREY band center (214). Do NOT switch rgb to the
            # raw swatch pixel: the swatch can read anywhere in [195,199]∪[229,236] and still pass
            # is_grey, but a ±tol band centred there misses the actual 214 hatch → 0 px → area=None
            # (Demo-4 regression that made colour-coded D77 return no area). The legend swatch is
            # used only to CONFIRM the concrete-yard entry is grey, exactly as in the validated run.
            flags.append(f"legend '{label}': swatch {swatch} is grey — concrete-yard hatch CONFIRMED "
                         f"(segmenting on validated grey band {GREY})")
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

    # --- scale FIRST (segmentation needs k for scale-aware dock-bay/void handling) ---
    k, verified, note, scale_sources = scale_for(pdf)
    flags.append(note)
    if k is None:
        return {"pdf": pdf, "area_m2": None,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + ["no scale — cannot measure"]}

    _seg_diag = {}
    comp = segment_hatch(im, rgb, k=k, S=S, _diag=_seg_diag)
    if comp is None or comp.sum() == 0:
        return {"pdf": pdf, "area_m2": None,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + ["no hatch pixels matched — assessor must trace"]}
    px = int(comp.sum())
    flags.append("dock-bay recesses & interior islands kept as DEDUCTIONS (not filled); thin paint bridged by closing")
    if _seg_diag.get('void_fill_m2', 0) > 0:
        flags.append(f"void-fill: +{_seg_diag['void_fill_m2']} m² from {_seg_diag['void_count']} "
                     f"paint/text hole(s) (each < 1.0 m²) — included in measured area")
    if _seg_diag.get('excluded_m2', 0) > 0:
        flags.append(f"excluded {_seg_diag['excluded_components']} border/legend component(s) "
                     f"({_seg_diag['excluded_m2']} m² equivalent: {_seg_diag.get('excluded_margin_m2', 0)} m² "
                     f"sheet-frame/border strip + {_seg_diag.get('excluded_satellite_m2', 0)} m² legend/satellite "
                     f"chip(s)) — not part of the measured yard region")

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

    # --- polygon contour for portal canvas overlay ---
    # Coordinates stored in PDF-point space — the canonical space shared by render_snapshot()
    # (email + /snapshot overlay), the vision path, and measure_regions(). The portal scales
    # them to canvas pixels once (× snapScale). Storing snapshot pixels here used to double-scale
    # the overlay and mis-place the polygon on capped wide sheets.
    polygon_pts = _hatch_contour(comp, S)

    # --- manhole count ESTIMATE (unmarked path — conservative, never authoritative) ---
    manhole_count_estimate, _mh_centres = detect_manholes(im, comp, k, S=S)
    if manhole_count_estimate > 0:
        flags.append(f"manhole_count_estimate={manhole_count_estimate} (small near-circular "
                     f"features inside the measured yard, {MANHOLE_DIAM_M_MIN}-{MANHOLE_DIAM_M_MAX} m "
                     "diameter band) — this is an ESTIMATE, assessor confirm before pricing E/O manhole details")

    # --- measurement_state: the four-state contract (sanity.py) so downstream (pipeline,
    # portal, approve endpoint) never has to re-derive verified/plausible logic itself. ---
    state, state_flags = sanity.measurement_state(area, scale_verified=verified)
    flags += state_flags
    needs_assessor = state != sanity.MEASURED_VERIFIED

    return {"pdf": os.path.basename(pdf), "scale_k": round(k, 5), "scale_verified": verified,
            "scale_src": note, "scale_sources": scale_sources,
            "area_m2": area, "rate": rate, "price_gbp": price, "overlay": overlay,
            "polygon_pts": polygon_pts, "flags": flags,
            "manhole_count_estimate": manhole_count_estimate,
            "measurement_state": state, "needs_assessor": needs_assessor}


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
