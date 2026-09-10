#!/usr/bin/env python3
"""stipple_surfaces: measure a surface that is drawn as a STIPPLE, on a sheet where
every build-up shares one CAD layer.

The sheet this exists for (2105) defeats every identity signal we had:
  - the layer cannot separate the build-ups: `RL_Surface` carries all eight of them
  - colour cannot: every mark is pure black (0,0,0)
  - the PDF clip paths cannot: they are viewport boxes (141,794 m2), not outlines

What CAN separate them is the mark itself. The sheet draws its own legend chips in the
same pattern as the ground each one denotes, so the chips are a key we can read:

    Concrete Service Yard        seg p90   2.75 pt   <- dots
    Grasscrete                             7.24 pt      hatch
    Car Parking                           29.98 pt      hatch
    ...

A stipple is short marks; a hatch is long parallel strokes. So identity comes from the
legend chip, exactly as it does for a colour swatch — we just match a PATTERN instead
of an RGB. That is the whole idea, and it is why this refuses when the sheet does not
carry exactly one stipple chip: with two, the pattern no longer names one surface.

WHAT THIS DELIBERATELY DOES NOT DO. On 2105 this finds the client's two marked regions
at IoU 0.920 / 0.912 and 1.3% under his figure -- and also a third 345 m2 region he did
not mark, drawn in the yard's own stipple, beside the staff car parking. Nothing on the
sheet says whether that strip is concrete to be priced. This module does not invent a
rule to delete it: a region that looks unlike the others is REPORTED with a low-solidity
flag so the assessor sees the doubt, never dropped silently. Deleting ground because a
threshold we chose says so is how 6,510 m2 of car park got priced as a service yard.

Areas from this method read UNDER the truth: a stipple stops short of the boundary it is
drawn inside, so the reconstructed outline sits inside the real edge. Callers must treat
the number as a minimum and must not present it as verified.

Not yet wired into the pipeline -- see the module note in ground_truth_polygons.json.
"""
from __future__ import annotations

import math

import numpy as np

try:                                            # cv2 is required; fail loudly, not silently
    import cv2
except ImportError as _e:                       # pragma: no cover - dependency guard
    raise ImportError("stipple_surfaces requires opencv (opencv-python-headless)") from _e

import layer_surfaces as _ls

# ── what counts as a stipple mark ────────────────────────────────────────────────────
# A stipple dot on a 1:500 sheet is ~0-3 pt; the shortest hatch chip measured is 7.24 pt.
# 4.0 sits in that gap with room on both sides. It is a property of draughting practice,
# not a value fitted to one drawing's answer.
STIPPLE_MAX_SEG_PT = 4.0

# A legend chip is small. Real ground is not: the smallest region we would ever price is
# 200 m2, which at 1:500 is ~2,500 pt across. 80 pt keeps chips and ground far apart.
CHIP_MAX_PT = 80.0
CHIP_MIN_MARKS = 3
MIN_CHIPS_FOR_LEGEND = 3
MAX_CHIPS_IN_LEGEND = 25       # a key, not a field of dots
CHIP_COLUMN_TOL_PT = 40.0      # chips in one legend share a left edge, within this
CHIP_MIN_GAP_PT = 25.0         # blank paper between chips; a stipple has none
CHIP_SIZE_SPREAD = 40.0        # chips in one legend are drawn the same size

# The hatch is subtracted after dilating it by HALF ITS OWN PITCH, so the parallel
# strokes close into the solid ground they represent. The pitch is measured from the
# sheet; these bounds only stop a nonsense measurement running away.
MIN_LONG_FOR_HATCH = 40        # below this the plan simply isn't hatched
MIN_PITCH_PT = 0.5
MAX_PITCH_PT = 40.0

MIN_REGION_M2 = _ls.MIN_REGION_M2
MIN_INK_RETENTION = _ls.MIN_INK_RETENTION

# How WIDE a region is at its widest point, in metres -- not how concave it is. Solidity
# was the first attempt and it is wrong: a straight thin bar is convex, so it scores ~1.0
# and would be counted as ground. (The 2105 strip only scored 14% because it happens to
# wind; a straight one would have walked straight through.) Width is the property that
# actually matters and it is interpretable: on 2105 the two yards are 51 m across at their
# widest and the stray strip is 2.8 m. A service yard 3 m wide is not a service yard.
# Nothing is ever deleted by this -- it decides
# whether a region is reported as MEASURED or as a CANDIDATE the assessor must opt into.
# The client asked for exactly that split on 9 Sep 2026: "the two main regions should be
# detected as normal, and any additional stipple regions should appear separately as
# candidate areas. Don't automatically include them in the final total, but also don't
# reject the whole sheet because of them."
# The asymmetry is what makes a shape threshold safe here: a real region misfiled as a
# candidate under-counts until a human opts in, and nothing misfiled can inflate a price.
MIN_REGION_WIDTH_M = 6.0

# A stipple is not "a short mark" -- it is short marks REPEATED at a density. Dashes,
# symbols and the stub ends of hatch strokes are short too, and they scatter across a
# sheet without ever being ground. So a mark only counts as stipple if it also has
# company: at least MIN_NEIGHBOURS other short marks within NEIGHBOUR_RADII times the
# dot spacing the legend chip itself is drawn at. Both properties come from the chip.
# "Not alone" is the whole test. Asking for 4 neighbours deleted 19% of the real stipple
# on 2105 and made things WORSE: the field went ragged, so closing it needed a 3.88 m
# bridge instead of 2.65 m -- a bigger assumption about blank paper, for less area. A
# stipple dot always has a neighbour; a stray dash does not.
NEIGHBOUR_RADII = 2.5
MIN_NEIGHBOURS = 1


def _seg_len(item_dict):
    """Longest straight segment in one drawing item, in points."""
    best = 0.0
    for it in item_dict.get("items") or ():
        if it[0] == "l":
            dx, dy = it[2].x - it[1].x, it[2].y - it[1].y
            best = max(best, math.hypot(dx, dy))
        elif it[0] in ("c", "qu", "re"):
            # curves and rects are never stipple dots; treat as long so they can't be
            # mistaken for one. A rect's diagonal is a fair proxy for its extent.
            r = it[1] if hasattr(it[1], "width") else None
            best = max(best, math.hypot(r.width, r.height) if r is not None else MAX_PITCH_PT)
    return best


def _raster(items, rot, scale, shape):
    """Draw the ACTUAL path geometry of each item (never its bounding box).

    A bounding box turned a diagonal leader line into a solid ~300 m2 block once and
    collapsed a 10,511 m2 sheet to 603 m2. Same rule here.
    """
    h, w = shape
    m = np.zeros((h, w), np.uint8)
    for d in items:
        pts = _ls._item_points(d, rot, scale)
        if not pts:
            continue
        poly = np.round(np.asarray(pts, dtype=float)).astype(np.int32)
        solid = "f" in (d.get("type") or "") or d.get("closePath")
        if solid and len(poly) >= 3:
            cv2.fillPoly(m, [poly], 1)
        else:
            width = max(1, int(round(float(d.get("width") or 0) * scale)))
            cv2.polylines(m, [poly], False, 1, width)
    return m


def _chips(items, cents, rot, scale, shape):
    """Legend chips: a ROW or COLUMN of small, widely-spaced, similar patches.

    "Small and isolated" alone does not find them. The plan's own stipple breaks into
    small clusters, and a regular grid of lone dots looks like a column if you only ask
    about arrangement. Two things separate a legend from both:

      - a chip is a PATCH of pattern, not one mark (>= CHIP_MIN_MARKS marks), and
      - its chips line up, separated by tens of points of blank paper.

    Chips are matched on their shared EDGE, not their centroids: a 6-dot stipple chip is
    narrower than a 7-stroke hatch chip, so their centres do not line up even though the
    legend clearly does. Both orientations are tried, so a legend that reads as a row on
    a rotated sheet is found too.

    Returns [(component_id, label_image, stat_row), ...] for the chosen legend.
    """
    m = _raster(items, rot, scale, shape)
    k = max(1, int(round(3.0 * scale)))
    closed = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1,) * 2))
    n, lab, st, _cent = cv2.connectedComponentsWithStats(closed, 8)
    lim = CHIP_MAX_PT * scale

    marks = _count_by_label(cents, lab, shape, n)
    small = [i for i in range(1, n)
             if st[i, cv2.CC_STAT_WIDTH] <= lim and st[i, cv2.CC_STAT_HEIGHT] <= lim
             and marks[i] >= CHIP_MIN_MARKS]
    if len(small) < MIN_CHIPS_FOR_LEGEND:
        return []

    def _line_up(align_stat, spread_stat):
        tol = CHIP_COLUMN_TOL_PT * scale
        order = sorted(small, key=lambda i: st[i, align_stat])
        groups, cur = [], [order[0]]
        for i in order[1:]:
            if st[i, align_stat] - st[cur[-1], align_stat] <= tol:
                cur.append(i)
            else:
                groups.append(cur); cur = [i]
        groups.append(cur)
        best = []
        for ids in groups:
            if not (MIN_CHIPS_FOR_LEGEND <= len(ids) <= MAX_CHIPS_IN_LEGEND):
                continue
            vs = sorted(st[i, spread_stat] for i in ids)
            gaps = [b - a for a, b in zip(vs, vs[1:])]
            if not gaps or float(np.median(gaps)) < CHIP_MIN_GAP_PT * scale:
                continue                          # a field of marks, not a legend
            areas = sorted(st[i, cv2.CC_STAT_AREA] for i in ids)
            if areas[-1] > areas[0] * CHIP_SIZE_SPREAD:
                continue                          # chips in one legend are drawn alike
            if len(ids) > len(best):
                best = ids
        return best

    col = _line_up(cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP)
    row = _line_up(cv2.CC_STAT_TOP, cv2.CC_STAT_LEFT)
    best = col if len(col) >= len(row) else row
    return [(i, lab, st[i]) for i in best]


def _count_by_label(cents, lab, shape, n):
    """How many marks fall in each connected component."""
    h, w = shape
    out = [0] * n
    for c in cents:
        if c is None:
            continue
        x, y = c
        if 0 <= y < h and 0 <= x < w:
            v = int(lab[y, x])
            if v:
                out[v] += 1
    return out


def _centroids(items, rot, scale):
    """Every item's centroid once. Recomputing per chip made this O(chips x items)."""
    out = []
    for d in items:
        pts = _ls._item_points(d, rot, scale)
        if not pts:
            out.append(None)
            continue
        a = np.asarray(pts, dtype=float).mean(axis=0)
        out.append((int(round(a[0])), int(round(a[1]))))
    return out


def _group_by_label(items, cents, lab, shape):
    """Bucket items by the connected-component label their centroid lands in."""
    h, w = shape
    buckets = {}
    for d, c in zip(items, cents):
        if c is None:
            continue
        x, y = c
        if 0 <= y < h and 0 <= x < w:
            v = int(lab[y, x])
            if v:
                buckets.setdefault(v, []).append(d)
    return buckets


def _centroid_of(d, rot, scale):
    pts = _ls._item_points(d, rot, scale)
    if not pts:
        return None
    a = np.asarray(pts, dtype=float).mean(axis=0)
    return float(a[0]), float(a[1])


def _spacing_pt(cents, scale):
    """Median nearest-neighbour distance among marks, in points."""
    pts = np.asarray([c for c in cents if c is not None], dtype=float)
    if len(pts) < 4:
        return None
    from scipy.spatial import cKDTree
    d = cKDTree(pts).query(pts, k=2)[0][:, 1]
    d = d[np.isfinite(d) & (d > 0)]
    return float(np.median(d)) / scale if len(d) else None


def _dense_only(items, rot, scale, radius_pt, min_neighbours):
    """Keep only marks that repeat -- a lone short dash is not a stipple."""
    cents = [_centroid_of(d, rot, scale) for d in items]
    idx = [i for i, c in enumerate(cents) if c is not None]
    if len(idx) < min_neighbours + 1:
        return []
    from scipy.spatial import cKDTree
    pts = np.asarray([cents[i] for i in idx], dtype=float)
    tree = cKDTree(pts)
    counts = np.asarray([len(x) for x in tree.query_ball_point(pts, radius_pt * scale)]) - 1
    return [items[i] for i, c in zip(idx, counts) if c >= min_neighbours]


def _hatch_pitch_pt(long_items, rot, scale):
    """Perpendicular spacing of the dominant hatch direction, in points.

    Nearest-neighbour on centroids does NOT work: collinear strokes on the same hatch
    line sit far closer to each other than the pitch, which reported 3.97 m where the
    truth was 0.28 m. Project onto the hatch normal and take the median gap instead.
    """
    angs, segs = [], []
    for d in long_items:
        for it in d.get("items") or ():
            if it[0] != "l":
                continue
            dx, dy = it[2].x - it[1].x, it[2].y - it[1].y
            if math.hypot(dx, dy) <= STIPPLE_MAX_SEG_PT:
                continue
            angs.append(math.degrees(math.atan2(dy, dx)) % 180.0)
            segs.append((it[1], it[2]))
    if len(angs) < 8:
        return None
    dom = float(np.bincount(np.asarray(angs).astype(int), minlength=180).argmax())
    th = math.radians(dom)
    nx, ny = -math.sin(th), math.cos(th)
    proj = []
    for (p0, p1), a in zip(segs, angs):
        if abs(((a - dom + 90.0) % 180.0) - 90.0) >= 10.0:
            continue
        mid = _fitz_point((p0.x + p1.x) / 2.0, (p0.y + p1.y) / 2.0, rot)
        proj.append(mid[0] * nx + mid[1] * ny)
    if len(proj) < 8:
        return None
    gaps = np.diff(np.sort(np.asarray(proj)))
    gaps = gaps[gaps > 0.3]
    if not len(gaps):
        return None
    pitch = float(np.median(gaps))
    if not (MIN_PITCH_PT <= pitch <= MAX_PITCH_PT):
        return None
    return pitch


def _fitz_point(x, y, rot):
    import fitz
    p = fitz.Point(x, y) * rot
    return p.x, p.y


def _outline(mask_u8, rot, scale):
    """The region's outline in RENDERED PDF points -- mask pixel / scale, nothing else.

    This is the canonical space in this codebase (see _hatch_contour in takeoff_unmarked):
    the same space render_snapshot() uses, which the portal converts to canvas pixels once
    by multiplying by snapScale. It is the VISUALLY ROTATED page, because get_pixmap()
    applies the page rotation while get_drawings() does not.

    An earlier version of this function transformed back to UNROTATED points. On the two
    sheets that measure today that is invisible -- both are /Rotate 0 -- but 2105 is
    /Rotate 270, so it would have drawn the outline in the wrong place on the assessor's
    screen while every server-side number stayed correct. Do not "fix" this by rotating.
    """
    cs, _h = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return []
    c = max(cs, key=cv2.contourArea)
    eps = 0.002 * cv2.arcLength(c, True)
    c = cv2.approxPolyDP(c, eps, True)
    return [[round(float(pt[0]) / scale, 3), round(float(pt[1]) / scale, 3)]
            for pt in c.reshape(-1, 2)]


def _max_width_m(mask_u8, k, scale):
    """Width of the widest circle that fits inside the region, in metres."""
    dt = cv2.distanceTransform(mask_u8, cv2.DIST_L2, 5)
    return float(dt.max()) * 2.0 * k / scale


def _solidity(mask_u8, per_px_m2):
    cs, _h = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return 0.0
    c = max(cs, key=cv2.contourArea)
    hull = cv2.contourArea(cv2.convexHull(c))
    return float(mask_u8.sum()) / hull if hull > 0 else 0.0


def measure(doc, page, k, S=2.0, layer_suffix=None):
    """Measure the stipple-drawn surface. Never raises; refuses with a reason."""
    try:
        return _measure(doc, page, k, S=S, layer_suffix=layer_suffix)
    except Exception as e:                       # pragma: no cover - defensive
        return {"ok": False, "reason": f"stipple measurement failed: {type(e).__name__}: {e}"}


def _measure(doc, page, k, S=2.0, layer_suffix=None):
    import fitz  # noqa: F401  (imported for the rotation matrix type)

    rot = page.rotation_matrix
    W = int(math.ceil(page.rect.width * S))
    H = int(math.ceil(page.rect.height * S))
    shape = (H, W)
    per_px_m2 = (k / S) ** 2

    draw = [d for d in page.get_drawings(extended=True) if d.get("rect") is not None]
    by_layer = {}
    for d in draw:
        by_layer.setdefault(d.get("layer") or "", []).append(d)
    if layer_suffix:
        cand = {n: v for n, v in by_layer.items() if n.endswith(layer_suffix)}
    else:
        cand = {n: v for n, v in by_layer.items() if n}
    if not cand:
        return {"ok": False, "reason": "no CAD layers on this page to read a pattern from"}
    layer = max(cand, key=lambda n: len(cand[n]))
    items = cand[layer]
    if len(items) < 200:
        return {"ok": False, "reason": f"layer '{layer}' carries only {len(items)} marks"}

    # ── identity: read the legend chips, and require exactly one stipple ──────────────
    cents = _centroids(items, rot, S)
    chips = _chips(items, cents, rot, S, shape)
    if len(chips) < MIN_CHIPS_FOR_LEGEND:
        return {"ok": False,
                "reason": f"only {len(chips)} legend chips found on layer '{layer}' — "
                          "no pattern key to identify the surface against"}
    _lab = chips[0][1]
    buckets = _group_by_label(items, cents, _lab, shape)
    chip_ids = {ch[0] for ch in chips}
    sig = []
    for cid in sorted(chip_ids):
        mem = buckets.get(cid) or []
        if len(mem) < CHIP_MIN_MARKS:
            continue
        p90 = float(np.percentile([_seg_len(d) for d in mem], 90))
        sig.append({"id": cid, "marks": len(mem), "seg_p90_pt": round(p90, 2),
                    "kind": "stipple" if p90 < STIPPLE_MAX_SEG_PT else "hatch"})
    stipples = [s for s in sig if s["kind"] == "stipple"]
    stipple_id = stipples[0]["id"] if len(stipples) == 1 else None
    if len(sig) < MIN_CHIPS_FOR_LEGEND:
        return {"ok": False, "reason": f"only {len(sig)} readable legend chips on '{layer}'"}
    if len(stipples) != 1:
        return {"ok": False,
                "reason": (f"{len(stipples)} of the {len(sig)} legend chips are stipples — "
                           "the pattern does not name one surface, so it cannot identify it"),
                "chips": sig}

    # ── measure: split the plan's marks by the pattern the chip taught us ─────────────
    plan = []
    for d, c in zip(items, cents):
        if c is None:
            continue
        x, y = c
        if 0 <= y < H and 0 <= x < W and int(_lab[y, x]) in chip_ids:
            continue                              # a legend chip is not ground
        plan.append(d)

    short_all = [d for d in plan if _seg_len(d) <= STIPPLE_MAX_SEG_PT]
    long_ = [d for d in plan if _seg_len(d) > STIPPLE_MAX_SEG_PT]
    if len(short_all) < 200:
        return {"ok": False, "reason": f"only {len(short_all)} short marks outside the legend"}

    # the dot spacing the chip is drawn at -- the second half of the pattern key
    chip_marks = buckets.get(stipple_id) or []
    chip_sp = _spacing_pt([_centroid_of(d, rot, S) for d in chip_marks], S)
    if chip_sp is None:
        return {"ok": False, "reason": "could not read the stipple chip's dot spacing"}
    short = _dense_only(short_all, rot, S, chip_sp * NEIGHBOUR_RADII, MIN_NEIGHBOURS)
    if len(short) < 200:
        return {"ok": False,
                "reason": (f"only {len(short)} of {len(short_all)} short marks repeat at the "
                           "legend chip's density — no stipple field on this sheet")}

    sm = _raster(short, rot, S, shape)
    lm = _raster(long_, rot, S, shape)

    # A sheet may carry no hatched build-up at all on the plan. That is not a failure --
    # there is simply nothing to subtract. Only refuse when the plan clearly IS hatched
    # and we cannot read its pitch, because then we would be leaving another build-up's
    # ground inside the answer.
    if len(long_) < MIN_LONG_FOR_HATCH:
        pitch = None
        hatched = np.zeros(shape, bool)
    else:
        pitch = _hatch_pitch_pt(long_, rot, S)
        if pitch is None:
            return {"ok": False,
                    "reason": (f"the plan carries {len(long_)} long strokes but their pitch "
                               "could not be measured, so the hatched ground cannot be "
                               "subtracted from the stipple")}
        # Dilate the hatch by half its own pitch: that is exactly what turns parallel
        # strokes into the solid ground they denote. Derived from the sheet, not chosen.
        grow = max(1, int(round((pitch / 2.0) * S)))
        hatched = cv2.dilate(lm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                           (2 * grow + 1,) * 2)).astype(bool)

    closer = _ls._Closer(sm, _ls.SEARCH_MAX_PT * S)
    radius_pt, bridge_diag = _ls._bridge_radius_pt(closer, S, k)
    if radius_pt is None:
        return {"ok": False, "reason": "the stipple never closed into a stable outline"}
    # Closing by a disc of radius R spans a GAP of 2R -- that much blank paper is being
    # ASSUMED to be surface. Past the cap we are inventing ground, not measuring it.
    bridge_m = 2.0 * radius_pt * k
    if bridge_m > _ls.MAX_BRIDGE_M:
        return {"ok": False,
                "reason": (f"closing the stipple would assume {bridge_m:.2f} m of blank paper "
                           f"is surface (limit {_ls.MAX_BRIDGE_M:.1f} m)")}
    field = _ls._fill_holes(closer.close(radius_pt * S), per_px_m2, _ls.HOLE_KEEP_M2)[0]
    cand = field.astype(bool) & ~hatched

    ink = sm.astype(bool)
    built_from = ink & ~hatched
    total_ink = float(built_from.sum()) or 1.0

    n, lab, st, _c = cv2.connectedComponentsWithStats(cand.astype(np.uint8), 8)
    main, candidates, kept = [], [], np.zeros(shape, bool)
    seen = [False] * n
    for i in range(1, n):
        a_m2 = float(st[i, cv2.CC_STAT_AREA]) * per_px_m2
        cm = (lab == i)
        share = float((built_from & cm).sum()) / total_ink
        # The post-Roscoe rule: an absolute floor alone threw away a real 168 m2 third of
        # that yard as "noise". A patch carrying a real share of the surface's own marks
        # is a region however small it is.
        if a_m2 < MIN_REGION_M2 and share < _ls.MIN_REGION_INK_SHARE:
            continue
        seen[i] = True
        cmu = cm.astype(np.uint8)
        sol = _solidity(cmu, per_px_m2)
        width_across = _max_width_m(cmu, k, S)
        rec = {
            "area_m2": round(a_m2, 1),
            "ink_share": round(share, 3),
            "solidity": round(sol, 3),
            "width_across_m": round(width_across, 1),
            "width_m": round(st[i, cv2.CC_STAT_WIDTH] * k / S, 1),
            "height_m": round(st[i, cv2.CC_STAT_HEIGHT] * k / S, 1),
            "polygon_pts": _outline(cm.astype(np.uint8), rot, S),
        }
        if width_across < MIN_REGION_WIDTH_M:
            rec["candidate_reason"] = (
                f"only {width_across:.1f} m across at its widest — a strip, not a piece of "
                "ground, so it is offered for review rather than counted")
            candidates.append(rec)
        else:
            main.append(rec)
            kept |= cm
    if not main:
        return {"ok": False,
                "reason": ("no stipple region is wider than "
                           f"{MIN_REGION_WIDTH_M:.0f} m — {len(candidates)} narrow strips only")}

    # How much of the stipple ended up in the measured total. On a sheet where ONE layer
    # carries every build-up this will not reach the layer-method's 95% floor, and the
    # client has said explicitly that it should not reject the sheet. So for this method
    # it is DISCLOSED with its number rather than gated -- and the number reaches the
    # assessor and the quotation, the same way AREA IS A MINIMUM does. It is not a
    # licence to ignore it: a low figure means most of the pattern is unaccounted for.
    retention = float((built_from & kept).sum()) / total_ink
    hatch_excluded = 1.0 - (total_ink / float(ink.sum() or 1))
    cand_ink = round(sum(r["ink_share"] for r in candidates), 3)
    # The honest disclosure is the GROUND that failed to qualify as a region, not the
    # area of the ink itself -- ink is thin, and quoting its extent as though it were
    # ground would understate what is being left out by an order of magnitude.
    unformed = [i for i in range(1, n)
                if not seen[i]
                and float(st[i, cv2.CC_STAT_AREA]) * per_px_m2 > 0.0]
    unformed_m2 = round(sum(float(st[i, cv2.CC_STAT_AREA]) * per_px_m2 for i in unformed), 1)

    flags = []
    if retention < MIN_INK_RETENTION:
        flags.append(
            f"ONLY {retention:.0%} OF THE STIPPLE IS IN THE TOTAL — the rest is offered as "
            f"{len(candidates)} candidate area(s), plus {len(unformed)} patches covering about "
            f"{unformed_m2:,.0f} m2 that were too small or too scattered to qualify as regions. "
            "None of that is counted, and no area is claimed for it.")
    if candidates:
        flags.append(
            f"{len(candidates)} CANDIDATE AREA(S) NOT IN THE TOTAL — review and include "
            "each one deliberately; they are shaped unlike the measured ground.")
    flags.append("AREA IS A MINIMUM — OUTLINE RECONSTRUCTED from a stipple, which stops "
                 "short of the edge it is drawn inside.")

    total = round(sum(r["area_m2"] for r in main), 1)
    return {
        "ok": True,
        "layer": layer,
        "area_m2": total,
        "regions": sorted(main, key=lambda r: -r["area_m2"]),
        "candidate_regions": sorted(candidates, key=lambda r: -r["area_m2"]),
        "candidate_area_m2": round(sum(r["area_m2"] for r in candidates), 1),
        "unformed_stipple_m2": unformed_m2,
        "unformed_patches": len(unformed),
        "candidate_ink_share": cand_ink,
        "flags": flags,
        "chips": sig,
        "hatch_pitch_pt": round(pitch, 2) if pitch else None,
        "hatch_pitch_m": round(pitch * k, 3) if pitch else None,
        "bridge_gap_m": round(bridge_m, 2),
        "ink_retention": round(retention, 3),
        "short_ink_on_hatched_ground": round(hatch_excluded, 3),
        "area_is_a_minimum": True,
        "identified_by": ("the only stipple pattern in the sheet's own legend "
                          f"({stipples[0]['seg_p90_pt']} pt strokes)"),
        "diagnostics": {"bridge": bridge_diag, "short_marks": len(short),
                        "long_marks": len(long_)},
    }
