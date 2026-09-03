#!/usr/bin/env python3
"""Assessor-gated measurement of hatch-drawn surfaces from a HATCH LEGEND CHIP.

Why this exists (Inderjit's project-8 sheet, 14173-TCG CONCRETE SLAB MSA, 2 Sep 2026): a drawing
whose surfaces are ONLY hatch strokes plus linework scores ~0.4% solid fill, so drawing_style()
refuses it as line/hatch before segment_hatch() — and the closing-growth hatch router lives inside
segment_hatch(), so it never ran.  MJM 9000 only reached that router by accident of its solid
landscaping fill.  This module is the path AROUND that guard, gated on evidence the sheet itself
carries: a legend chip that is a STROKE PATTERN, and plan strokes that match it.

It was chosen over two alternatives by adversarial review (three designs, four refuters each, on
the real sheet and on constructed fusion fixtures): it is the only one that keeps two separately
drafted same-pattern surfaces apart across a corridor of clean paper narrower than the closing
kernel, because it partitions BEFORE closing.  Its declared blind window is stated below and in the
flag it emits, and every number it produces is capped at MEASURED_UNVERIFIED for the assessor.

takeoff_unmarked is imported read-only for its helpers (label lookup, router, contour, native
acceptor); it imports this module lazily inside the line/hatch branch, so there is no cycle.

Where it runs: inside takeoff()'s line/hatch branch, AFTER structural_light_fill has declined, i.e.
on sheets the shipped colour path already refuses.  It never touches the colour-coded path, so
gold sheets (D77, _int_d77, MJM 9000) cannot reach it.

What it does, in order — every step can decline, and a decline is the existing UNMEASURED refusal:
  1. LEGEND TIER (own tier, never appended to YARD_LABELS/DOCK_APRON_LABELS): locate the surface
     label, then the chip left of it.  The chip is accepted ONLY as a STROKE PATTERN (boxed chip,
     interior ink 3-45 %, >= 3 parallel strokes at one angle theta explaining >= 60 % of the
     interior ink, measurable normal spacing s).  A solid chip declines: this path is unreachable
     on solid-swatch sheets, which is what keeps the widened kernel window closed on them.
  2. PLAN INK = (stroke colour band read from the chip's own rendered pixels) INTERSECT
     (21 px morphological line-opening at theta).  The opening drops building grids, boundary
     lines and text by construction because they are not at theta.
  3. KERNEL: the shipped _hatch_closing_kernel() on that ink with k=None (growth ratio, row-gap
     stats, kernel from the sheet's own spacing, 120 px hard cap; the real-space 8 m cap does not
     apply) under a PAPER-SPACE cap of 3x the chip's own row spacing.  Plan gap median must agree
     with the chip spacing within 25 % (the pattern on the plan is the pattern in the legend).
  4. FUSION GUARD — AND-CELL PARTITION (structural, kernel-independent).  A pixel is COVERED only
     if the stroke lines on BOTH sides of it carry ink at the same along-stroke position:
        I1 = close(ink, line(theta, 0.5 s))           bridge crossing linework (<= 0.5 s)
        I2 = erode(I1, line(theta, 2m+1)), m = s/2   shorten every stroke end by m
        cover = dilate(I2, halfline(+n, 1.15 s)) AND dilate(I2, halfline(-n, 1.15 s))
     Any unhatched corridor of width w at angle phi to the strokes cuts every stroke it crosses
     over an along-stroke gap g = w/sin(phi); with the erosion m, neither the inter-line cell
     slivers nor the on-line filaments survive once g + 2m >= s/tan(phi), i.e. whenever
     w >= s(cos(phi) - sin(phi)).  The cover therefore disconnects across the corridor whatever
     the closing kernel is.  The closing is applied PER PART afterwards, so K can never bridge
     two parts.  Declared blind window: w < s(cos(phi) - sin(phi)) — zero for phi >= 45 deg,
     at most 0.41 s for a corridor nearly parallel to the strokes (a gap the pattern itself
     cannot resolve).
  5. PARTS: ink is assigned to the nearest cover component; each part is closed with K on its
     own; parts < 200 m² are dropped; a merged closed component that straddles >= 2 parts is
     REFUSED and the parts are emitted separately only if each stands alone (pairwise overlap
     of their closures <= 5 % of the smaller); otherwise the surface is refused to the assessor.
  6. ACCEPTOR: the shipped native closed-boundary acceptor where drawings exist; else the part is
     an assessor-gated PREFILL polygon.  State is capped at MEASURED_UNVERIFIED in all cases.
"""
import math
import os
import sys

import cv2
import fitz
import numpy as np
from scipy import ndimage as ndi

sys.path.insert(0, "/Users/jas/fortel-takeoff-repo")
import takeoff_unmarked as T   # noqa: E402  (read-only helpers)
import sanity                  # noqa: E402

MEASUREMENT_MODE = "hatch-legend raster line-opening (angle R)"

# ── Legend tier (OWN tier; the shipped tuples are read, never extended) ──────────────────────
# Surface key -> (label phrases tried in order, zone category, subject text)
HATCH_LEGEND_SURFACES = (
    ("yard", ("concrete slab for yard",) , "external_yard", "CONCRETE SLAB FOR YARD"),
    ("road", ("concrete slab for road",), "unclassified", "CONCRETE SLAB FOR ROAD"),
)
# A generic yard label (the shipped YARD_LABELS vocabulary, read-only) is consulted for the
# yard surface ONLY when no hatch-specific phrase exists and ONLY if its chip is a stroke
# pattern.  Consulting a tuple is not appending to it.
GENERIC_YARD_LABELS = T.YARD_LABELS

# ── Chip reader gates ────────────────────────────────────────────────────────────────────────
CHIP_WINDOW_PT = 175          # same reach left of the label as the shipped swatch reader
CHIP_HALF_HEIGHT_PT = 16      # chips are taller than the shipped +-7 pt raster window
CHIP_MIN_W_PT, CHIP_MIN_H_PT = 8.0, 4.0
CHIP_OUTLINE_INSET_PX = 4     # drop the chip's own box outline before reading strokes
CHIP_MIN_STROKES = 3
CHIP_MIN_DENSITY, CHIP_MAX_DENSITY = 0.03, 0.45   # a solid chip (~1.0) declines here
CHIP_ANGLE_EXPLAINED = 0.60   # best-angle opening must explain this much of interior ink
CHIP_MIN_ABS_SIN = 0.30       # row-sampled gap statistics need slanted strokes
CHIP_OPEN_LEN_PX = 15
COLOUR_BAND_PAD = 10
NEUTRAL_SPREAD = 12

# ── Plan / partition constants (all derived from s, the chip's normal spacing) ───────────────
OPEN_LEN_PX = 21
OPEN_ANGLE_TOL_DEG = 5
PAPER_CAP_ROW_SPACINGS = 3.0
SPACING_AGREE_TOL = 0.25
PLAN_LATTICE_MIN_AGREE = 0.70  # share of sampled plan cells that must match a chip cell
ALONG_GAP_FRAC = 0.5          # L_gap = 0.5 s
ERODE_FRAC = 0.5              # m = 0.5 s
REACH_FRAC = 1.15             # 1.15 s
ASSIGN_REACH_FRAC = 1.5
SEED_MIN_M2 = 20.0
PART_MIN_M2 = float(T.PLAUSIBLE_MIN_M2)
PART_OVERLAP_MAX = 0.05
MAX_PARTS = 8
MAX_VOID_M2 = 1.0
HOLE_MIN_M2 = 25.0            # a void smaller than this is raster noise, not a courtyard
OUTLINE_FIDELITY_TOL = 0.15   # drawn outline must reproduce the measured hatch within this


def _poly_area_m2(pts, k):
    """Shoelace area of a PDF-point polygon, in m². The area the ASSESSOR sees enclosed."""
    if not pts or len(pts) < 3:
        return 0.0
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    total = 0.0
    for i in range(len(xs)):
        j = (i + 1) % len(xs)
        total += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(total) * 0.5 * k * k


def _contour_with_holes(comp, S, k, max_pts=180):
    """Outer boundary AND its holes, in PDF points: ([[x, y], ...], [[[x, y], ...], ...]).

    T._hatch_contour is RETR_EXTERNAL and stays untouched — it is on the shipped MJM path.
    This module needs holes because a road drawn as a loop around a yard has an outer contour
    that encloses the yard as well: on Inderjit's project-8 sheet the road's outer boundary
    encloses 48,729 m² beside a measured 14,267 m². Handing an assessor an outline 3.4x the
    number it is labelled with is how a wrong extent gets approved.
    """
    try:
        mask = (np.asarray(comp) > 0).astype(np.uint8) * 255
        if mask.sum() == 0:
            return None, []
        cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts or hier is None:
            return None, []
        hier = hier[0]
        outer_idx = [i for i in range(len(cnts)) if hier[i][3] == -1]
        if not outer_idx:
            return None, []
        oi = max(outer_idx, key=lambda i: cv2.contourArea(cnts[i]))
        if len(cnts[oi]) < 3 or cv2.contourArea(cnts[oi]) < 6:
            return None, []

        def simplify(c):
            peri = cv2.arcLength(c, True)
            eps = 0.001 * peri
            approx = cv2.approxPolyDP(c, eps, True)
            while len(approx) > max_pts and eps < 0.05 * peri:
                eps *= 1.5
                approx = cv2.approxPolyDP(c, eps, True)
            pts = approx.reshape(-1, 2)
            if len(pts) < 3:
                return None
            inv = 1.0 / S
            return [[float(x * inv), float(y * inv)] for x, y in pts]

        outer = simplify(cnts[oi])
        if outer is None:
            return None, []
        holes = []
        for i in range(len(cnts)):
            if hier[i][3] != oi or len(cnts[i]) < 3:
                continue
            hp = simplify(cnts[i])
            if hp is not None and _poly_area_m2(hp, k) >= HOLE_MIN_M2:
                holes.append(hp)
        return outer, holes
    except Exception:
        return None, []


def _outline_for(mask, area_m2, S, k):
    """(polygon, holes, fidelity) for a hatch mask, or (None, [], fidelity) when the traced
    outline cannot reproduce the measured quantity within OUTLINE_FIDELITY_TOL."""
    poly, holes = _contour_with_holes(mask, S, k)
    if not poly:
        return None, [], None
    drawn = _poly_area_m2(poly, k) - sum(_poly_area_m2(h, k) for h in holes)
    fid = abs(drawn - area_m2) / max(area_m2, 1e-9)
    if fid > OUTLINE_FIDELITY_TOL:
        return None, [], fid
    return poly, holes, fid


# ─────────────────────────────────────────────────────────────────────────────── kernels
def line_se(length, ang_deg):
    length = max(3, int(length) | 1)
    k = np.zeros((length, length), np.uint8)
    c = (length - 1) / 2.0
    dx, dy = math.cos(math.radians(ang_deg)), -math.sin(math.radians(ang_deg))
    cv2.line(k, (int(round(c - dx * c)), int(round(c - dy * c))),
             (int(round(c + dx * c)), int(round(c + dy * c))), 1, 1)
    return k


def half_line_se(reach, ang_deg):
    reach = max(1, int(reach))
    size = 2 * reach + 1
    k = np.zeros((size, size), np.uint8)
    dx, dy = math.cos(math.radians(ang_deg)), -math.sin(math.radians(ang_deg))
    cv2.line(k, (reach, reach), (int(round(reach + dx * reach)), int(round(reach + dy * reach))), 1, 1)
    return k


def oriented_keep(mask, theta, length=OPEN_LEN_PX, tol=OPEN_ANGLE_TOL_DEG):
    """Pixels of ``mask`` that survive a line-opening at theta (+-tol)."""
    d = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))
    keep = np.zeros_like(d)
    for a in (theta - tol, theta, theta + tol):
        keep |= cv2.morphologyEx(d, cv2.MORPH_OPEN, line_se(length, a))
    return mask & cv2.dilate(keep, np.ones((3, 3), np.uint8)).astype(bool)


def _close(mask, K):
    if K <= 1:
        return mask.copy()
    return cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((K, K), np.uint8)).astype(bool)


# ─────────────────────────────────────────────────────────────────────────────── chip reader
def read_hatch_chip(pdf, pg, im, labels, S, page=0):
    """Locate ``labels`` and read the chip left of it as a STROKE PATTERN.

    Returns a dict with ``ok`` True and the pattern (theta_deg, s_perp_px, row_spacing_px,
    colour band, chip bbox in raster px, label text), or ``ok`` False with a reason.  A solid chip
    is a decline, not an error.
    """
    found = T._label_bbox_for(pdf, labels, page)
    if not found:
        return {"ok": False, "reason": "label not found", "label": None}
    raw_bbox, text = found
    R = pg.rotation_matrix
    rb = fitz.Rect(raw_bbox) * R
    lx0, cy = rb.x0, (rb.y0 + rb.y1) / 2.0
    H, W = im.shape[:2]
    x0 = max(0, int((lx0 - CHIP_WINDOW_PT) * S)); x1 = max(0, min(W, int((lx0 - 3) * S)))
    y0 = max(0, int((cy - CHIP_HALF_HEIGHT_PT) * S)); y1 = min(H, int((cy + CHIP_HALF_HEIGHT_PT) * S))
    if x1 - x0 < 8 or y1 - y0 < 4:
        return {"ok": False, "reason": "no room left of the label for a chip", "label": text}
    patch = im[y0:y1, x0:x1]
    ink = patch.min(2) < 235
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
    cands = []
    for i in range(1, n):
        x, y, w, h, a = [int(v) for v in st[i]]
        if w >= CHIP_MIN_W_PT * S and h >= CHIP_MIN_H_PT * S and a >= 20:
            cands.append((x + w, i, x, y, w, h, a))
    if not cands:
        return {"ok": False, "reason": "no chip-sized ink component left of the label", "label": text}
    cands.sort(reverse=True)               # nearest the label first
    _, cid, cx, cyy, cw, ch, ca = cands[0]
    box_ink = ink[cyy:cyy + ch, cx:cx + cw]
    density = float(box_ink.mean())
    if density > CHIP_MAX_DENSITY:
        return {"ok": False, "reason": f"chip is a SOLID fill (ink density {density:.2f}); not a stroke pattern",
                "label": text, "chip_kind": "solid"}
    if density < CHIP_MIN_DENSITY:
        return {"ok": False, "reason": f"chip ink density {density:.3f} too sparse to be a pattern", "label": text}
    i0 = CHIP_OUTLINE_INSET_PX
    if ch <= 2 * i0 + 4 or cw <= 2 * i0 + 4:
        return {"ok": False, "reason": "chip too small to read strokes inside its outline", "label": text}
    inner = box_ink[i0:ch - i0, i0:cw - i0]
    inner_rgb = patch[cyy + i0:cyy + ch - i0, cx + i0:cx + cw - i0]
    if inner.sum() < 12:
        return {"ok": False, "reason": "chip interior carries no strokes", "label": text}
    inner_u8 = inner.astype(np.uint8)
    d = cv2.dilate(inner_u8, np.ones((3, 3), np.uint8))
    L = min(CHIP_OPEN_LEN_PX, min(inner.shape) - 1) | 1
    # Angle from each stroke's own second-order moments (major axis), NOT from an opening
    # response: a short line SE fits inside a thick short stroke over a 30-degree plateau, so
    # the response cannot rank angles.  The opening is used afterwards only to VALIDATE.
    nc, clab, cst, _ = cv2.connectedComponentsWithStats(inner_u8, 8)
    angles = []
    for i in range(1, nc):
        if cst[i, 4] < 6:
            continue
        mom = cv2.moments((clab == i).astype(np.uint8), binaryImage=True)
        if mom["m00"] <= 0:
            continue
        mu20, mu02, mu11 = mom["mu20"] / mom["m00"], mom["mu02"] / mom["m00"], mom["mu11"] / mom["m00"]
        elong = (mu20 + mu02 + math.hypot(mu20 - mu02, 2 * mu11)) / max(1e-9, mu20 + mu02 - math.hypot(mu20 - mu02, 2 * mu11))
        if elong < 4.0:                       # not a stroke (blob/text)
            continue
        theta_img = 0.5 * math.degrees(math.atan2(2 * mu11, mu20 - mu02))   # image coords, y down
        angles.append((-theta_img) % 180.0)                                  # CCW from +x, y up
    if len(angles) < CHIP_MIN_STROKES:
        return {"ok": False, "reason": f"only {len(angles)} elongated strokes inside the chip", "label": text}
    # circular median over the 180-degree range
    ang_arr = np.asarray(angles)
    ref = ang_arr[0]
    rel = ((ang_arr - ref + 90.0) % 180.0) - 90.0
    theta = int(round((ref + float(np.median(rel))) % 180.0))
    if float(np.percentile(np.abs(rel - np.median(rel)), 90)) > 8.0:
        return {"ok": False, "reason": f"chip strokes are not one parallel family (angles {sorted(round(a) for a in angles)})",
                "label": text}

    def resp(a):
        o = cv2.morphologyEx(d, cv2.MORPH_OPEN, line_se(L, a))
        return int((cv2.dilate(o, np.ones((3, 3), np.uint8)).astype(bool) & inner).sum())
    explained = resp(theta) / float(inner.sum())
    if explained < CHIP_ANGLE_EXPLAINED:
        return {"ok": False, "reason": f"chip strokes are not one parallel family (best angle {theta} deg "
                f"explains {explained:.2f} of interior ink)", "label": text}
    if abs(math.sin(math.radians(theta))) < CHIP_MIN_ABS_SIN:
        return {"ok": False, "reason": f"chip strokes at {theta} deg are too flat for row-sampled spacing",
                "label": text}
    keep = oriented_keep(inner, theta, length=L, tol=OPEN_ANGLE_TOL_DEG)
    ns, slab, sst, scen = cv2.connectedComponentsWithStats(keep.astype(np.uint8), 8)
    strokes = [(float(scen[i][0]), float(scen[i][1])) for i in range(1, ns) if sst[i, 4] >= 6]
    if len(strokes) < CHIP_MIN_STROKES:
        return {"ok": False, "reason": f"only {len(strokes)} chip strokes at {theta} deg (< {CHIP_MIN_STROKES})",
                "label": text}
    nx, ny = -math.sin(math.radians(theta)), -math.cos(math.radians(theta))   # normal in (x, y-down)
    proj = sorted(x * nx + y * ny for x, y in strokes)
    diffs = [b - a for a, b in zip(proj, proj[1:]) if b - a > 2.0]
    if len(diffs) < 2:
        return {"ok": False, "reason": "chip stroke spacing unmeasurable", "label": text}
    # The chip declares a LATTICE, not one spacing: a paired-line pattern (P8 yard: cells 18/36 px)
    # is as legitimate as a single-line one (P8 road: 18 px).  Keep the smallest and largest
    # cell; the partition sizes itself from s_max and bridges linework from s_min.
    diffs = sorted(diffs)
    s_min = float(diffs[0])
    s_max = float(diffs[-1])
    if s_max > 3.0 * s_min:
        return {"ok": False, "reason": f"chip stroke cells too irregular ({[round(d, 1) for d in diffs]} px)", "label": text}
    if REACH_FRAC * s_max >= s_min + s_max:
        return {"ok": False, "reason": "chip lattice too uneven for a safe AND-cell reach", "label": text}
    s_perp = s_max
    row_spacing = s_max / abs(math.sin(math.radians(theta)))
    row_spacing_min = s_min / abs(math.sin(math.radians(theta)))
    px = inner_rgb[keep]
    # The band is the chip strokes' CENTRAL rendered range (p10-p90): the tail above p90 is the
    # antialias fringe, which on a neutral chip would admit every grey line on the sheet.
    p_lo = np.percentile(px, 10, axis=0); p_hi = np.percentile(px, 90, axis=0)
    spread = np.percentile(np.abs(px[:, 0].astype(int) - px[:, 1]), 95), \
        np.percentile(np.abs(px[:, 1].astype(int) - px[:, 2]), 95)
    neutral = bool(max(spread) <= NEUTRAL_SPREAD)
    band = [[int(max(0, p_lo[c] - COLOUR_BAND_PAD)), int(min(255, p_hi[c] + COLOUR_BAND_PAD))] for c in range(3)]
    mode = T._dominant_rgb(inner_rgb, keep)
    return {
        "ok": True, "label": text, "chip_kind": "stroke-pattern",
        "theta_deg": int(theta), "explained": round(float(explained), 3),
        "n_strokes": len(strokes), "s_perp_px": round(s_perp, 2), "row_spacing_px": round(row_spacing, 2),
        "s_min_px": round(s_min, 2), "s_max_px": round(s_max, 2), "row_spacing_min_px": round(row_spacing_min, 2),
        "cell_diffs_px": [round(d, 1) for d in diffs],
        "density": round(density, 3), "band": band, "neutral": neutral, "mode_rgb": mode,
        "stroke_angles_deg": [round(a, 1) for a in angles],
        "chip_bbox_px": [x0 + cx, y0 + cyy, cw, ch],
        "window_bbox_px": [x0, y0, x1 - x0, y1 - y0],
    }


# ─────────────────────────────────────────────────────────────────────────────── plan ink
def plan_ink(im, chip, exclude_boxes_px):
    r, g, b = im[..., 0].astype(int), im[..., 1].astype(int), im[..., 2].astype(int)
    (r0, r1), (g0, g1), (b0, b1) = chip["band"]
    mask = (r >= r0) & (r <= r1) & (g >= g0) & (g <= g1) & (b >= b0) & (b <= b1)
    mask &= ~((r > 233) & (g > 233) & (b > 233))            # never paper
    if chip["neutral"]:
        mask &= (np.abs(r - g) <= NEUTRAL_SPREAD) & (np.abs(g - b) <= NEUTRAL_SPREAD)
    H, W = mask.shape
    my = max(1, int(round(H * T.MARGIN_FRAC))); mx = max(1, int(round(W * T.MARGIN_FRAC)))
    mask[:my, :] = False; mask[-my:, :] = False; mask[:, :mx] = False; mask[:, -mx:] = False
    for (x, y, w, h) in exclude_boxes_px:
        mask[max(0, y):y + h, max(0, x):x + w] = False
    colour_px = int(mask.sum())
    ink = oriented_keep(mask, chip["theta_deg"])
    return ink, colour_px


# ─────────────────────────────────────────────────────────────────────────────── plan lattice
def plan_cell_widths(ink, theta, s_max, n_lines=300, seed=0):
    """Perpendicular stroke-to-stroke cell widths sampled along the normal through random ink pixels."""
    H, W = ink.shape
    nx, ny = -math.sin(math.radians(theta)), -math.cos(math.radians(theta))
    ys, xs = np.where(ink)
    if len(xs) < 10:
        return np.zeros(0)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(xs), min(n_lines, len(xs)), replace=False)
    span = int(4 * s_max)
    ts = np.arange(-span, span + 1)
    cells = []
    for i in idx:
        px = np.round(xs[i] + ts * nx).astype(int); py = np.round(ys[i] + ts * ny).astype(int)
        ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        t_hit = ts[ok][ink[py[ok], px[ok]]]
        if len(t_hit) < 3:
            continue
        groups = np.split(t_hit, np.where(np.diff(t_hit) > 3)[0] + 1)
        cells.extend(np.diff([g.mean() for g in groups]).tolist())
    return np.asarray(cells)


def plan_lattice_agreement(cells, s_min, s_max, tol=SPACING_AGREE_TOL):
    if not len(cells):
        return 0.0
    ok = (np.abs(cells - s_min) <= tol * s_min) | (np.abs(cells - s_max) <= tol * s_max)
    return float(ok.mean())


# ─────────────────────────────────────────────────────────────────────────────── partition
def and_cover_partition(ink, theta, s_min, s_max, px_per_m2):
    """AND-cell cover -> parts.  Returns (parts, info); parts = list of (seed_id, part_ink_mask).

    reach must cover the WIDEST cell (s_max) and must never reach the second neighbour line
    (s_min + s_max), which the chip reader guarantees; the erosion m is half the widest cell so
    the cut condition g + 2m > s_max/tan(phi) holds for every cell.
    """
    s_perp = s_max
    L_gap = max(3, int(round(ALONG_GAP_FRAC * s_min)) | 1)
    m = max(1, int(round(ERODE_FRAC * s_max)))
    reach = max(2, int(round(REACH_FRAC * s_max)))
    I1 = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE, line_se(L_gap, theta))
    I2 = cv2.erode(I1, line_se(2 * m + 1, theta))
    Dp = cv2.dilate(I2, half_line_se(reach, theta + 90))
    Dm = cv2.dilate(I2, half_line_se(reach, theta - 90))
    cover = cv2.morphologyEx((Dp & Dm), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(cover, 8)
    seed_min_px = SEED_MIN_M2 * px_per_m2
    seed_ids = [i for i in range(1, n) if st[i, 4] >= seed_min_px]
    info = {"L_gap_px": L_gap, "erode_m_px": m, "reach_px": reach, "cover_px": int(cover.sum()),
            "cover_components": int(n - 1), "seed_components": len(seed_ids)}
    cover_bool = cover.astype(bool)
    if not seed_ids:
        return [], info, cover_bool
    seed_mask = np.isin(lab, seed_ids)
    dist, pix_lab = cv2.distanceTransformWithLabels(
        (~seed_mask).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    # map each DIST_LABEL_PIXEL id (one per seed pixel) to its seed component id
    seed_ys, seed_xs = np.where(seed_mask)
    pix_ids = pix_lab[seed_ys, seed_xs]
    lut = np.zeros(int(pix_lab.max()) + 1, np.int32)
    lut[pix_ids] = lab[seed_ys, seed_xs]
    ink_ys, ink_xs = np.where(ink)
    assign = lut[pix_lab[ink_ys, ink_xs]]
    assign[dist[ink_ys, ink_xs] > ASSIGN_REACH_FRAC * s_max] = 0
    part_of = np.zeros_like(lab)
    part_of[ink_ys, ink_xs] = assign
    info["stray_ink_px"] = int((assign == 0).sum())
    parts = []
    for sid in seed_ids:
        pm = part_of == sid
        if pm.any():
            parts.append((int(sid), pm))
    return parts, info, cover_bool


def _fill_small_holes(region, px_per_m2):
    filled = ndi.binary_fill_holes(region)
    hl, hn = ndi.label(filled & ~region)
    if not hn:
        return region, 0
    hsz = ndi.sum(np.ones_like(hl), hl, range(1, hn + 1))
    small_ids = [i + 1 for i in range(hn) if hsz[i] < MAX_VOID_M2 * px_per_m2]
    small = np.isin(hl, small_ids)
    return region | small, int(small.sum())


# ─────────────────────────────────────────────────────────────────────────────── one surface
def measure_surface(pdf, pg, im, key, labels, chip, S, k, drawings, drawings_reason, out_dir=None):
    """Returns (surface_result, flags).  surface_result is None on a decline (flags say why)."""
    flags = []
    px_per_m2 = (S * S) / (k * k)
    exclude = [tuple(chip["window_bbox_px"])]
    ink, colour_px = plan_ink(im, chip, exclude)
    diag = {"chip": chip, "colour_px": colour_px, "ink_px": int(ink.sum())}
    if int(ink.sum()) < T.HATCH_MIN_INK_PX:
        flags.append(f"{key}: REFUSED — only {int(ink.sum())} plan px at the chip angle (< {T.HATCH_MIN_INK_PX})")
        return None, flags, diag
    # The realised lattice on the plan may leave wider cells than the chip declares (missing strokes);
    # the partition must reach across the widest REAL cell, bounded by the chip's own irregularity guard.
    cells = plan_cell_widths(ink, chip["theta_deg"], chip["s_max_px"])
    agree = plan_lattice_agreement(cells, chip["s_min_px"], chip["s_max_px"])
    s_min = chip["s_min_px"]
    s_max = chip["s_max_px"]
    if len(cells):
        p90 = float(np.percentile(cells, 90))
        if p90 > s_max and p90 <= 3.0 * s_min and REACH_FRAC * p90 < s_min + p90:
            s_max = p90
    diag["plan_cells"] = {"n": int(len(cells)), "p10_p50_p90": [round(float(v), 1) for v in np.percentile(cells, [10, 50, 90])] if len(cells) else None,
                          "frac_matching_chip_cells": round(agree, 3), "s_min_used": round(s_min, 1), "s_max_used": round(s_max, 1)}
    if len(cells) < 50 or agree < PLAN_LATTICE_MIN_AGREE:
        flags.append(f"{key}: REFUSED — the plan's stroke lattice does not match the legend chip's "
                     f"(chip cells {chip['cell_diffs_px']} px; {agree*100:.0f}% of {len(cells)} sampled plan cells within "
                     f"{SPACING_AGREE_TOL*100:.0f}% of a chip cell, need {PLAN_LATTICE_MIN_AGREE*100:.0f}%)")
        return None, flags, diag
    parts, pinfo, cover = and_cover_partition(ink, chip["theta_deg"], s_min, s_max, px_per_m2)
    diag["partition"] = pinfo
    # ROUTER on LATTICE ink (strokes that have a parallel neighbour on both sides).  Ink at the legend
    # angle that is not part of a lattice — a boundary line, a 45-degree building element, grid
    # junctions — inflates the K=6 base component and would fail the growth gate for a real hatch.
    lattice_ink = ink & cv2.dilate(cover.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    _raw_kernel, raw_info = T._hatch_closing_kernel(ink, 6, None, S)
    kernel, rinfo = T._hatch_closing_kernel(lattice_ink, 6, None, S)   # k=None: no real-space cap; 120 px hard cap
    diag["router_raw_ink"] = raw_info
    diag["router"] = rinfo
    if not kernel:
        flags.append(f"{key}: REFUSED — lattice ink at the chip angle does not read as a hatch: {rinfo.get('reason')}")
        return None, flags, diag
    cap_px = int(round(PAPER_CAP_ROW_SPACINGS * chip["row_spacing_px"]))
    diag["paper_cap_px"] = cap_px
    if kernel > cap_px:
        flags.append(f"{key}: REFUSED — kernel {kernel}px exceeds the paper-space cap {cap_px}px "
                     f"({PAPER_CAP_ROW_SPACINGS:.0f}x the legend chip's own row spacing {chip['row_spacing_px']:.1f}px)")
        return None, flags, diag
    merged = _close(ink, kernel)
    mn, mlab, mst, _ = cv2.connectedComponentsWithStats(merged.astype(np.uint8), 8)
    regions = []
    small_parts_m2 = []
    for sid, pink in parts:
        reg = _close(pink, kernel)
        reg, void_px = _fill_small_holes(reg, px_per_m2)
        area_m2 = reg.sum() / px_per_m2
        if area_m2 < PART_MIN_M2:
            if area_m2 >= SEED_MIN_M2:
                small_parts_m2.append(round(float(area_m2), 1))
            continue
        ys, xs = np.where(reg)
        regions.append({"seed": sid, "mask": reg, "ink_px": int(pink.sum()), "area_m2": round(float(area_m2), 1),
                        "void_fill_px": void_px,
                        "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]})
    regions.sort(key=lambda r: -r["area_m2"])
    diag["n_parts_ge_min"] = len(regions)
    diag["small_hatched_parts_m2"] = sorted(small_parts_m2, reverse=True)
    if small_parts_m2:
        flags.append(f"{key}: {len(small_parts_m2)} separately-hatched part(s) below the {PART_MIN_M2:.0f} m² floor NOT emitted "
                     f"({', '.join(f'{a:,.0f}' for a in sorted(small_parts_m2, reverse=True)[:6])} m²); assessor may trace them")
    if not regions:
        flags.append(f"{key}: REFUSED — no separately-hatched part reaches {PART_MIN_M2:.0f} m² "
                     f"(largest merged closed component {mst[1:, 4].max() / px_per_m2 if mn > 1 else 0:,.0f} m²)")
        return None, flags, diag
    # Shipped retention rule: the largest part is primary; a part below SATELLITE_FRAC of it is a
    # satellite (stray chip/element) and is dropped with a report; larger co-parts are retained for
    # review and feed the total only when their bbox is disjoint from the primary's (segment_hatch /
    # takeoff precedent for multi-region same-tint sets).
    primary = regions[0]
    satellites = [r for r in regions[1:] if r["area_m2"] < T.SATELLITE_FRAC * primary["area_m2"]]
    regions = [r for r in regions if r is primary or r["area_m2"] >= T.SATELLITE_FRAC * primary["area_m2"]]
    if satellites:
        flags.append(f"{key}: {len(satellites)} satellite part(s) below {T.SATELLITE_FRAC*100:.1f}% of the primary dropped "
                     f"({', '.join('{:,.0f}'.format(r['area_m2']) for r in satellites[:6])} m²); not part of the measured surface")
    diag["satellites_m2"] = [r["area_m2"] for r in satellites]
    if len(regions) > MAX_PARTS:
        flags.append(f"{key}: REFUSED — hatch partitions into {len(regions)} parts (> {MAX_PARTS}); too fragmented "
                     "to prefill honestly; assessor must trace")
        return None, flags, diag

    # Every retained part feeds the candidate total.  The shipped bbox-overlap hold-out exists for
    # same-tint regions whose CLASS is ambiguous (AEW: yard/building/road in one grey).  Here every
    # part already carries the legend's colour, angle AND lattice, so class is not the question;
    # the seam split exists only so that no two parts are ever MERGED.  Per-part keep/exclude
    # review remains mandatory (review_required, MEASURED_UNVERIFIED).
    for r in regions:
        r["included"] = True
    # stands-alone test: pairwise overlap of the per-part closures
    seams = []
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            ov = int((regions[i]["mask"] & regions[j]["mask"]).sum()) / px_per_m2
            smaller = min(regions[i]["area_m2"], regions[j]["area_m2"])
            if ov <= PART_OVERLAP_MAX * smaller:
                if ov > 0:
                    gap_px = 0.0
                else:
                    di = cv2.distanceTransform((~regions[i]["mask"]).astype(np.uint8), cv2.DIST_L2, 5)
                    gap_px = float(di[regions[j]["mask"]].min()) if regions[j]["mask"].any() else None
                if gap_px is not None and gap_px <= 3.0 * s_max:
                    seams.append((i + 1, j + 1, round(gap_px, 1)))
            if ov > PART_OVERLAP_MAX * smaller:
                flags.append(f"{key}: REFUSED — parts {regions[i]['area_m2']:,.0f} and {regions[j]['area_m2']:,.0f} m² "
                             f"cannot stand alone (closures overlap {ov:,.0f} m² > {PART_OVERLAP_MAX*100:.0f}% of the "
                             "smaller); the raster cannot separate them without guessing; assessor must trace")
                return None, flags, diag
    # straddle report: merged closed components containing >= 2 parts
    straddles = []
    for mi in range(1, mn):
        if mst[mi, 4] < PART_MIN_M2 * px_per_m2:
            continue
        inside = [r for r in regions if (r["mask"] & (mlab == mi)).sum() > 0.5 * r["mask"].sum()]
        if len(inside) >= 2:
            straddles.append((round(mst[mi, 4] / px_per_m2, 1), [r["area_m2"] for r in inside]))
    diag["straddles"] = straddles
    diag["seams_px"] = seams
    for i, j, gap_px in seams:
        flags.append(f"{key}: parts {i} and {j} abut across a {gap_px:.0f}px ({gap_px * k / S:.1f} m) unhatched seam — "
                     + ("0-px seam = the strokes stop and restart with a phase break (typically two hatch objects of one "
                        "surface); assessor may merge" if gap_px < 2 else
                        "a paper corridor the hatch does not cross; assessor decides whether it separates two surfaces"))
    for m_area, part_areas in straddles:
        flags.append(f"{key}: SPLIT — the merged {kernel}px-closed component of {m_area:,.0f} m² straddles "
                     f"{len(part_areas)} separately-hatched parts ({', '.join(f'{a:,.0f}' for a in part_areas)} m²) "
                     "across an unhatched corridor; the merged region is REFUSED and the parts are emitted "
                     "separately (each stands alone); assessor decides whether they are one surface")
    # acceptor + records
    records = []
    for rank, r in enumerate(regions, 1):
        native, reason = (None, drawings_reason) if drawings is None else \
            T._native_boundary_for_mask(pg, r["mask"], S, k, drawings=drawings)
        # The outline the assessor is shown must reproduce the quantity it is labelled with.
        # A loop-shaped surface (a road around a yard) fails that before its holes are cut and
        # passes after; anything that still fails is emitted as a number with NO prefill, because
        # a misleading polygon is worse than none — an assessor approves what they can see.
        if native:
            poly, holes, per, src, conf = native["polygon_pts"], [], round(native["perimeter_lm"], 2), \
                f"native closed CAD boundary independently matched (IoU {native['iou']:.3f})", "high"
            fidelity = None
        else:
            poly, holes, fidelity = _outline_for(r["mask"], r["area_m2"], S, k)
            per, src, conf = None, f"unresolved: {reason}", "unresolved" if poly else "no drawable outline"
            if not poly:
                flags.append(f"{key} part {rank}: NO PREFILL OUTLINE — the traced boundary "
                             + (f"is {fidelity * 100:.0f}% away from the measured {r['area_m2']:,.0f} m² "
                                f"(tolerance {OUTLINE_FIDELITY_TOL * 100:.0f}%)" if fidelity is not None
                                else "could not be formed")
                             + "; the quantity stands from the hatch itself, but no polygon is issued — "
                               "assessor must trace this part")
        records.append({
            "region_id": f"{key}-part-{rank}", "area_m2": r["area_m2"], "ink_px": r["ink_px"],
            "bbox_pdf_pts": [round(v / S, 1) for v in r["bbox_px"]],
            "polygon_pts": poly, "hole_polygons_pts": holes,
            "outline_fidelity": None if fidelity is None else round(float(fidelity), 4),
            "perimeter_lm": per, "perimeter_source": src, "perimeter_confidence": conf,
            "included": bool(r["included"]), "classification_source": f"hatch legend chip '{chip['label']}' + {chip['theta_deg']} deg line-opening",
            "needs_assessor": True,
        })
        held = "" if r["included"] else "; HELD OUT of the total: bbox overlaps the primary part, assessor keep/exclude"
        flags.append(f"{key} part {rank}: {r['area_m2']:,.1f} m² (UNVERIFIED{held}) at bbox {records[-1]['bbox_pdf_pts']} PDF pt; "
                     + (f"perimeter {per:.2f} Lm from native boundary" if native else
                        f"extent unconfirmed, no native boundary ({reason}); raster outline retained as assessor prefill"))
    union = np.zeros_like(ink, dtype=bool)
    for r in regions:
        if r["included"]:
            union |= r["mask"]
    result = {"key": key, "chip": chip, "kernel_px": kernel, "router": rinfo, "partition": pinfo,
              "regions": records, "masks": [r["mask"] for r in regions], "union_mask": union,
              "included": [bool(r["included"]) for r in regions],
              "total_m2": round(float(union.sum() / px_per_m2), 1), "ink_mask": ink, "diag": diag}
    flags.insert(0, f"{key}: legend chip '{chip['label']}' is a STROKE PATTERN ({chip['n_strokes']} strokes at "
                    f"{chip['theta_deg']} deg, lattice cells {chip['cell_diffs_px']} px perp, mode RGB {chip['mode_rgb']}); "
                    f"plan lattice matches ({agree*100:.0f}% of sampled cells; cells used {s_min:.0f}/{s_max:.0f}px); "
                    f"plan ink {int(ink.sum())} px at that angle, {int(lattice_ink.sum())} px in a lattice "
                    f"(raw-ink growth {raw_info.get('growth_ratio')}x, lattice-ink {rinfo.get('reason')}); paper-space cap {cap_px}px; "
                    f"AND-cell partition -> {len(regions)} part(s) >= {PART_MIN_M2:.0f} m² (L_gap {pinfo['L_gap_px']}px, "
                    f"erode {pinfo['erode_m_px']}px, reach {pinfo['reach_px']}px)")
    return result, flags, diag


# ─────────────────────────────────────────────────────────────────────────────── entry point
def detect_hatch_legend_surfaces(pdf, pg, im, k, scale_verified=False, S=2.0, out_dir=None, page=0,
                                 debug_masks=False):
    """Assessor-gated hatch-legend measurement.  ``applicable`` False when no stroke-pattern chip exists."""
    flags = []
    chips = {}
    for key, labels, category, subject in HATCH_LEGEND_SURFACES:
        chip = read_hatch_chip(pdf, pg, im, labels, S, page)
        if not chip.get("ok") and key == "yard" and chip.get("label") is None:
            chip = read_hatch_chip(pdf, pg, im, GENERIC_YARD_LABELS, S, page)
            if chip.get("label"):
                chip["via_generic_yard_label"] = True
        if chip.get("label") is None:
            continue
        if not chip.get("ok"):
            flags.append(f"{key}: legend '{chip['label']}' found but its chip is not a stroke pattern — {chip['reason']}")
            continue
        chips[key] = (chip, category, subject)
    if not chips:
        return {"applicable": False, "flags": flags}

    flags = [f"MEASUREMENT MODE: {MEASUREMENT_MODE} (separate assessor-gated path inside the line/hatch branch; "
             "the colour-coded Yard path stays blocked; every number is a PREFILL for the assessor)"] + flags
    refusal = {"applicable": True, "method": MEASUREMENT_MODE, "measurement_mode": MEASUREMENT_MODE,
               "terminal_measurement_refusal": True, "area_m2": None, "measurement_state": sanity.UNMEASURED,
               "needs_assessor": True, "perimeter_measurement_allowed": False}
    try:
        kf = float(k)
    except (TypeError, ValueError):
        kf = None
    if not kf or not math.isfinite(kf) or kf <= 0:
        return dict(refusal, flags=flags + ["REFUSED — stroke-pattern legend found but no usable scale; no number emitted"])
    drawings, drawings_reason = T._native_boundary_drawings(pg)
    surfaces = {}
    diags = {}
    for key, (chip, category, subject) in chips.items():
        res, sflags, diag = measure_surface(pdf, pg, im, key, None, chip, S, kf, drawings, drawings_reason, out_dir)
        flags += sflags
        diags[key] = {kk: vv for kk, vv in diag.items() if kk != "chip"}
        if res:
            res["category"], res["subject"] = category, subject
            surfaces[key] = res
    if not surfaces:
        return dict(refusal, flags=flags + ["REFUSED — every stroke-pattern surface declined (see reasons above); "
                                            "no number emitted, assessor must trace"], hatch_legend_diagnostics=diags)

    # mutual exclusivity between surfaces (shipped precedent: Yard/Dock overlap removed from Yard)
    if "yard" in surfaces and "road" in surfaces:
        ov = surfaces["yard"]["union_mask"] & surfaces["road"]["union_mask"]
        ov_m2 = float(ov.sum()) * (kf / S) ** 2
        if ov_m2 > 0:
            px_per_m2 = (S * S) / (kf * kf)
            for mask, rec in zip(surfaces["yard"]["masks"], surfaces["yard"]["regions"]):
                before = int(mask.sum())
                mask &= ~ov
                rec["area_m2"] = round(float(mask.sum() / px_per_m2), 1)
                # The outline was traced BEFORE the seam was taken off the yard. Re-trace the
                # parts that actually changed, or the yard's polygons keep enclosing a strip the
                # road was given — picture and number must not drift apart.
                if int(mask.sum()) != before and rec.get("perimeter_source", "").startswith("unresolved"):
                    _poly, _holes, _fid = _outline_for(mask, rec["area_m2"], S, kf)
                    rec["polygon_pts"], rec["hole_polygons_pts"] = _poly, _holes
                    rec["outline_fidelity"] = None if _fid is None else round(float(_fid), 4)
                    if not _poly:
                        rec["perimeter_confidence"] = "no drawable outline"
                        flags.append(f"yard {rec['region_id']}: NO PREFILL OUTLINE after the road seam "
                                     "was removed; quantity stands, assessor must trace this part")
            surfaces["yard"]["union_mask"] &= ~ov
            surfaces["yard"]["total_m2"] = round(float(surfaces["yard"]["union_mask"].sum() / px_per_m2), 1)
            flags.append(f"yard/road overlap {ov_m2:,.1f} m² removed from the yard (surfaces are mutually exclusive; "
                         "the road keeps it, as the shipped Yard/Dock rule does); assessor confirms the seam")
    zones = []
    for key in ("yard", "road"):
        if key not in surfaces:
            continue
        s = surfaces[key]
        # Geometry travels WITH the zone. marked_pdf/the portal draw per-zone polygons; a zone
        # that carries only a number would put the road's 23,000 m² on the quote with nothing
        # for the assessor to look at. Held-out parts are excluded so the drawn outline always
        # sums to the stated area.
        drawn = [r for r in s["regions"] if r.get("included") and r.get("polygon_pts")]
        primary_part = max(drawn, key=lambda r: r["area_m2"]) if drawn else None
        zones.append({
            "zone_key": f"{s['category']}:hatch-legend-{key}", "category": s["category"], "subjects": [s["subject"]],
            "measurement_kind": "area", "area_m2": s["total_m2"], "length_lm": None, "perimeter_lm": None,
            "annotation_count": 0, "cutout_count": sum(len(r.get("hole_polygons_pts") or []) for r in drawn),
            "part_count": len(s["regions"]),
            "classification_source": f"hatch legend chip '{s['chip']['label']}' (stroke pattern) + line-opening at "
                                     f"{s['chip']['theta_deg']} deg; extent unconfirmed",
            "needs_assessor": True, "rate_note": ("own spec on the sheet; unpriced assessor-rate line" if key == "road" else None),
            "polygon_pts": primary_part["polygon_pts"] if primary_part else None,
            "region_polygons": [r["polygon_pts"] for r in drawn],
            "region_holes": [list(r.get("hole_polygons_pts") or []) for r in drawn],
            "region_ids": [r["region_id"] for r in drawn],
            "region_areas_m2": [r["area_m2"] for r in drawn],
        })
    yard_total = surfaces["yard"]["total_m2"] if "yard" in surfaces else None
    zones_total = round(sum(z["area_m2"] for z in zones), 1)
    state_basis = yard_total if yard_total is not None else zones_total
    state, state_flags = sanity.measurement_state(state_basis, scale_verified=bool(scale_verified), confidence="low")
    state = sanity.MEASURED_UNVERIFIED if state != sanity.REJECTED else state
    flags += state_flags
    flags.append("NO NATIVE CLOSED BOUNDARY CORROBORATION for parts marked 'extent unconfirmed': emitted only as "
                 "MEASURED_UNVERIFIED prefill polygons; assessor must confirm scale, extent, seams and cut-outs")
    all_regions = [r for key in ("yard", "road") if key in surfaces for r in surfaces[key]["regions"]]
    # The top-level polygon is the biggest part that actually HAS a drawable outline — a part
    # whose outline was withheld by the fidelity guard must not leave the sheet with polygon_pts
    # of None where every previous path put a polygon.
    _drawable = [r for r in (surfaces["yard"]["regions"] if "yard" in surfaces else all_regions)
                 if r.get("included") and r.get("polygon_pts")] or \
                [r for r in all_regions if r.get("included") and r.get("polygon_pts")]
    primary_poly = max(_drawable, key=lambda r: r["area_m2"])["polygon_pts"] if _drawable else None
    out = {
        "applicable": True, "method": MEASUREMENT_MODE, "measurement_mode": MEASUREMENT_MODE,
        "terminal_measurement_refusal": False,
        "area_m2": yard_total, "polygon_pts": primary_poly,
        "regions": [r["polygon_pts"] for r in all_regions if r["polygon_pts"]],
        "zones": zones, "zones_total_area_m2": zones_total,
        "yard_regions": [dict(r, chosen_primary=(i == 0)) for i, r in enumerate(surfaces["yard"]["regions"])] if "yard" in surfaces else [],
        "road_regions": surfaces["road"]["regions"] if "road" in surfaces else [],
        "yard_region_review_required": True,
        "extent_corroborated": False,
        "extent_corroboration_reason": "hatch-legend prefill; no native closed boundary matched",
        "measurement_state": state, "needs_assessor": True, "region_confidence": "low",
        "legend_found": True, "perimeter_measurement_allowed": False, "flags": flags,
        "hatch_legend_diagnostics": diags,
    }
    # Masks are numpy and the portal writes every job through json.dumps: returning them by
    # default would take the whole job store down on the first sheet that reached this path.
    # Scoring code asks for them explicitly; the live path never does.
    if debug_masks:
        out["_surfaces"] = surfaces
    return out
