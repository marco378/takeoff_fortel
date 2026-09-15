#!/usr/bin/env python3
"""Surface Finishes Plan — the priced surface named by its own legend, matched by hatch colour.

Radlett WP5 (SEGRO / BWB) is the sheet that forced this. Its Surface Finishes Plan
`RAD-BWB-A1EX-IT1-DP-C-0700` / `-0701` carries a seven-row legend: each row names a
construction, gives its thickness, points at its build-up detail, and shows a hatch swatch.
That legend is how the priced surface is identified on this project — Aryan confirmed it on
14 Sep, and confirmed the five concrete rows as Fortel's scope.

What this module does NOT do, deliberately:

  - It does not read the CAD layer names. On these sheets they are measurably wrong:
    `BWB_Bituminous Area` draws 871 concrete hatch strokes against 112 bituminous ones, and
    `BWB_HGV Concrete Slab` draws the Container crosshatch. Aryan's ruling is that the layer
    is supporting information and never the decider. The layer GROUP is still used, as
    provenance — "this mark came from the Surface Finishes Plan drawing" — because the xref
    prefix is trustworthy even where the sub-layer name is not.
  - It does not choose. Every row is offered; none is counted. Which construction is being
    priced is the assessor's decision, exactly as with the closed-boundary offer.
  - It does not claim an exact area. There is no closed outline anywhere on these sheets —
    the slab outline is 53 open fragments and the constructions are loose hatch strokes with
    nothing enclosing them. Extent is RECONSTRUCTED from the strokes and carries the same
    +/-2 m edge as 2105. Saying "exact" here would be the boundary offer's promise made
    about geometry that has not earned it.
"""
import math
import re

import cv2
import fitz
import numpy as np

import layer_surfaces

# The sheet must say what it is. Narrow on purpose: across the 668-file corpus this matches
# four sheets — WP5 0700/0701 and the same two drawings as sent separately — and nothing
# else. A legend alone is not enough of a gate; every slab layout has one, and theirs
# describes day joints and mesh zones rather than surfaces.
SHEET_TITLE_MARKER = "SURFACE FINISHES PLAN"

# The layer group that carries this sheet's own surfacing. Matching the group, not the leaf.
SFP_LAYER_MARKER = "Surface Finishes Plan"

# Two legend rows whose swatches are closer than this are not separately identifiable by
# colour, and pattern discrimination is not built. Same tolerance the construction-joint
# legend path uses.
COLOUR_TOLERANCE = 0.03

# A row needs its own build-up reference to be a construction rather than a note.
ROW_MARKER = "Refer to"

_THICKNESS = re.compile(r"(\d{2,4})\s*mm\s+thick", re.I)
_DETAIL_REF = re.compile(r"Refer\s+to\s+(\S+)", re.I)


def _display_rect(rect, rotation_matrix):
    out = rect * rotation_matrix
    out.normalize()
    return out


def _colour_of(item):
    colour = item.get("color") or item.get("fill")
    if not colour:
        return None
    rounded = tuple(round(float(channel), 3) for channel in colour)
    # Black and white are linework and paper, never a surfacing tint.
    if rounded in ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)):
        return None
    return rounded


def _same_colour(one, other):
    return max(abs(one[index] - other[index]) for index in range(3)) <= COLOUR_TOLERANCE


def is_surface_finishes_plan(page) -> bool:
    """Does this sheet say it is a Surface Finishes Plan?"""
    try:
        return SHEET_TITLE_MARKER in (page.get_text() or "").upper()
    except Exception:                                          # pragma: no cover - defensive
        return False


def read_legend(page, drawings=None) -> dict:
    """The legend's rows: name, thickness, detail reference, and hatch swatch colour.

    Returns ``{"ok": bool, "rows": [...], "reason": str}``. ``ok`` False means the legend did
    not resolve cleanly and the caller must refuse rather than guess — there is no half-read
    legend that is safe to price from.
    """
    rotation = page.rotation_matrix
    blocks = []
    for block in page.get_text("blocks"):
        text = " ".join(str(block[4]).split())
        if text:
            blocks.append((_display_rect(fitz.Rect(*block[:4]), rotation), text))

    headings = [rect for rect, text in blocks if text.strip().lower() == "legend"]
    if not headings:
        return {"ok": False, "rows": [], "reason": "no legend heading on the sheet"}
    heading = headings[0]

    # Rows sit under the heading, in its column, and each carries its own build-up reference.
    rows = sorted(
        [(rect, text) for rect, text in blocks
         if rect.y0 > heading.y1 - 2 and abs(rect.x0 - heading.x0) < 260 and ROW_MARKER in text],
        key=lambda row: row[0].y0)
    if len(rows) < 2:
        return {"ok": False, "rows": [],
                "reason": f"the legend resolved {len(rows)} construction row(s); at least 2 are "
                          "needed before offering a choice"}

    marks = []
    for item in layer_surfaces._drawings(page, drawings):
        colour = _colour_of(item)
        if colour is not None:
            marks.append((_display_rect(item["rect"], rotation), colour))

    # The swatch sits immediately left of its row, on the same line.
    bands = []
    for rect, _text in rows:
        band = {}
        for mark_rect, colour in marks:
            if not (rect.x0 - 160 < mark_rect.x1 < rect.x0 + 2):
                continue
            if not (rect.y0 - 10 <= (mark_rect.y0 + mark_rect.y1) / 2 <= rect.y1 + 10):
                continue
            band[colour] = band.get(colour, 0) + 1
        bands.append(band)

    # A colour that turns up beside MOST rows is the sheet's own linework crossing the legend
    # column, not any row's swatch. On 0701 the plan runs under the legend and put a grey at
    # 6-11 strokes in every band, which outvoted the real swatch on two rows and made two
    # constructions look like they shared a colour. A swatch belongs to one row.
    seen = {}
    for band in bands:
        for colour in band:
            seen[colour] = seen.get(colour, 0) + 1
    background = {colour for colour, count in seen.items()
                  if count >= max(2, len(bands) * 0.5)}

    resolved = []
    for (rect, text), band in zip(rows, bands):
        choices = [(count, colour) for colour, count in band.items() if colour not in background]
        if not choices:
            return {"ok": False, "rows": [],
                    "reason": f"no hatch swatch resolved beside the legend row "
                              f"'{text.split(ROW_MARKER)[0].strip()[:60]}'"}
        colour = max(choices)[1]
        thickness = _THICKNESS.search(text)
        detail = _DETAIL_REF.search(text)
        resolved.append({
            "name": text.split(ROW_MARKER)[0].strip(),
            "colour": colour,
            "depth_mm": int(thickness.group(1)) if thickness else None,
            "detail_ref": detail.group(1) if detail else None,
            "row_rect": rect,
        })

    # Colour is the whole of identity here; pattern discrimination is not built. If two rows
    # share a swatch we cannot tell their ground apart, and offering either would be a guess.
    for index, row in enumerate(resolved):
        for other in resolved[index + 1:]:
            if _same_colour(row["colour"], other["colour"]):
                return {"ok": False, "rows": [],
                        "reason": (f"legend rows '{row['name'][:40]}' and '{other['name'][:40]}' "
                                   "share a swatch colour; telling their ground apart needs "
                                   "hatch-pattern matching, which is not built")}

    return {"ok": True, "rows": resolved, "reason": ""}


def _legend_exclusion(rows, rotation_matrix):
    """The legend block itself, in DISPLAY coordinates, generously padded.

    The swatches are drawn in the very colours we are about to hunt for, and they sit on the
    Surface Finishes layer group like everything else. Left in, each one adds a small
    disconnected component to its own construction's mask.
    """
    if not rows:
        return None
    box = fitz.Rect(rows[0]["row_rect"])
    for row in rows[1:]:
        box |= row["row_rect"]
    return fitz.Rect(box.x0 - 200, box.y0 - 40, box.x1 + 60, box.y1 + 40)


def _has_surface_finishes_layers(drawings) -> bool:
    """Does this sheet carry the Surface Finishes layer group at all?

    On the real BWB sheets it does, and filtering to it is worth keeping: it is provenance
    ("this mark came from the Surface Finishes drawing") and it keeps a stray cyan from the
    Rail Alignment xref out of the HGV 200mm candidate. But a Surface Finishes Plan is not
    obliged to carry optional content at all, and requiring it would refuse a perfectly
    readable sheet for having no layers. Where there is nothing to filter by, the legend
    exclusion and the colour still constrain what is collected.
    """
    return any(SFP_LAYER_MARKER in (item.get("layer") or "") for item in drawings)


def _colour_ink(page, colour, exclusion, scale, drawings=None, layer_scoped=None):
    """Raster this sheet's Surface-Finishes marks drawn in ``colour``.

    The ink is drawn, never the bounding box around it — filling boxes once turned a single
    leader line into a solid 300 m2 block (see layer_surfaces._layer_ink).
    """
    rotation = page.rotation_matrix
    width = int(math.ceil(page.rect.width * scale))
    height = int(math.ceil(page.rect.height * scale))
    mask = np.zeros((height, width), np.uint8)
    marks = 0
    items = layer_surfaces._drawings(page, drawings)
    if layer_scoped is None:
        layer_scoped = _has_surface_finishes_layers(items)
    for item in items:
        if layer_scoped and SFP_LAYER_MARKER not in (item.get("layer") or ""):
            continue
        item_colour = _colour_of(item)
        if item_colour is None or not _same_colour(item_colour, colour):
            continue
        if exclusion is not None and exclusion.contains(
                fitz.Point(*_display_rect(item["rect"], rotation).tl)):
            continue
        points = layer_surfaces._item_points(item, rotation, scale)
        if not points:
            continue
        polygon = np.round(np.array(points, dtype=float)).astype(np.int32)
        thickness = max(1, int(round(float(item.get("width") or 0.0) * scale)))
        cv2.polylines(mask, [polygon], False, 1, thickness)
        marks += 1
    return mask, marks


# How far we will believe a bridge, per row: the hatch's OWN period times this. 2.0 is not a
# taste: a thin strip like the 225mm channelised edge arrives as 36 separate pieces and does
# not become one field until ~1.5x its period, so a 1.2x ceiling cut the search off just
# before the merge and reported 0 m2 for a real 1,569 m2 strip. Past the merge the area only
# creeps (+7% per half-period), so the plateau search still returns the smallest radius that
# works and the ceiling only bounds what we will believe. The drawing
# sets the minimum bridge (the smallest disc that makes its marks solid) and the drawing's own
# period sets the maximum we will believe — both ends come from the sheet. A fixed metre limit
# cannot work here: layer_surfaces' 3 m ceiling is right for CAD layer marks and wrong for
# hatch, whose period on these sheets runs from 0.7 m to 4.2 m.
PERIOD_CEILING = 2.0

# Retention is the "solid" test: the outline must still contain the hatch it claims to measure.
SOLID_RETENTION = 0.95


def _hatch_period_px(ink):
    """The hatch's period in pixels, measured on the plan itself.

    NOT measured on the legend swatch. The swatch is drawn about twice as dense as the plan on
    these sheets (0.79 m against 1.68 m, five rows agreeing at ~x2) so it identifies the
    construction but does not calibrate its spacing.

    Gaps are counted along rows AND columns and the smaller median is taken. For hatch at
    angle theta the horizontal gap is period/sin(theta) and the vertical period/cos(theta), so
    the smaller of the two is the closer to the true perpendicular period — exactly equal when
    the hatch runs square, and at worst 1.41x it at 45 degrees. Over-estimating only widens a
    ceiling; the search still returns the smallest radius that works.
    """
    medians = []
    for field in (ink, ink.T):
        gaps = []
        for line in field:
            marks = np.flatnonzero(line)
            if len(marks) < 3:
                continue
            steps = np.diff(marks)
            gaps.extend(steps[steps > 1].tolist())
        if gaps:
            medians.append(float(np.median(gaps)))
    return min(medians) if medians else None


# Closing radius as a multiple of the hatch's own period. NOT searched: hatch is a step
# function, not a curve. Every row on these sheets is still in pieces at 1.0x its period and
# becomes one field at 1.5x, after which the area only creeps (~7% per half period). Three
# attempts at a growth-peak-then-plateau search each landed on a different step and moved
# which rows worked whenever the ceiling moved — that search is built for stipple, where the
# spacing is unknowable and the curve shape is the only signal. Here the drawing states the
# spacing, so the radius is derived, not hunted.
#
# The 1.41x between a perpendicular gap and the horizontal run-length that _hatch_period_px
# measures at 45 degrees is why this is 1.5 and not 0.5: the two nearly cancel.
CLOSE_AT_PERIOD = 1.5

# Simplify the traced outline no finer than this fraction of the closing radius. The radius is
# how well the extent is located at all, so detail below it is stroke-end wobble rather than
# the drawn edge. Measured on Radlett 0701 at a quarter of the radius: the Container slab goes
# from 97 vertices to 10 for +0.7% area, HGV 180mm from 67 to 5 for +3.0%, while the 225mm
# channelised strip -- a genuinely thin ring round the slab -- keeps 95 of its 149 and stays a
# ring. Coarser than this starts eating that strip.
OUTLINE_EPS_OF_RADIUS = 0.25


def _row_regions(closed, ink, scale, k):
    """Every component of the closed field that holds some of this row's hatch.

    layer_surfaces._regions applies MIN_REGION_M2 to each component separately, which is right
    for one surface on one layer and wrong for a construction the drawing legitimately draws in
    pieces: the 225mm channelised edge runs round the slab as 36 fragments, and a per-component
    floor discards every one of them and reports 0 m2 for a real strip. The floor belongs to the
    ROW, applied to its total, so a construction is judged as the thing it is.

    Returns ``(kept, areas, parts)``. ``parts`` holds each component's own mask alongside its
    own area, which is what lets a construction drawn in separate pieces be offered piece by
    piece when no single outline can reproduce the whole.
    """
    count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(closed, 8)
    per_px_m2 = (k / scale) ** 2
    ink_bool = ink.astype(bool)
    kept, areas, parts = np.zeros_like(ink_bool), [], []
    for index in range(1, count):
        component = (labels == index)
        if not (component & ink_bool).any():
            continue
        kept |= component
        area = float(component.sum()) * per_px_m2
        areas.append(area)
        parts.append((component, area))
    return kept, areas, parts


def candidates(page, k, S=2.0, drawings=None) -> dict:
    """Offer one candidate per legend row. Choose nothing.

    ``k`` is metres per point from the caller's own scale work; it is NOT verified here, and
    every area returned converts through it. Returns
    ``{"ok": bool, "rows": [...], "reason": str}`` and never raises — this sits on the
    measurement path, where a drawing ends in a state and not in a traceback.
    """
    try:
        return _candidates(page, k, S, drawings)
    except Exception as exc:                                   # pragma: no cover - defensive
        return {"ok": False, "rows": [],
                "reason": (f"reading the Surface Finishes legend failed "
                           f"({type(exc).__name__}: {exc}) — no area is claimed from it")}


def _candidates(page, k, S, drawings):
    import hatch_legend_raster

    drawings = layer_surfaces._drawings(page, drawings)
    legend = read_legend(page, drawings=drawings)
    if not legend["ok"]:
        return {"ok": False, "rows": [], "reason": legend["reason"]}

    rows = legend["rows"]
    exclusion = _legend_exclusion(rows, page.rotation_matrix)
    per_px_m2 = (k / S) ** 2
    layer_scoped = _has_surface_finishes_layers(drawings)

    inks = {}
    for row in rows:
        inks[row["name"]] = _colour_ink(page, row["colour"], exclusion, S,
                                        drawings=drawings, layer_scoped=layer_scoped)

    offered = []
    for ordinal, row in enumerate(rows, 1):
        ink, marks = inks[row["name"]]
        # The row's place in the legend, not its place in the output: one construction drawn in
        # two places emits two entries, and numbering by output position would label the second
        # piece as if it were a different construction.
        entry = {"name": row["name"], "colour": row["colour"], "depth_mm": row["depth_mm"],
                 "detail_ref": row["detail_ref"], "marks": marks, "area_m2": None,
                 "polygon_pts": None, "components": 0, "edge_uncertainty_m": None,
                 "row_ordinal": ordinal, "reason": ""}
        if marks == 0 or not ink.any():
            # Normal, not a failure: 0700 is sheet 1 of 2 and each draws only its own portion.
            entry["reason"] = ("named in this sheet's legend but not drawn on it — its ground "
                               "is on a companion sheet")
            offered.append(entry)
            continue

        period_px = _hatch_period_px(ink)
        if not period_px:
            entry["reason"] = (f"{marks:,} strokes whose spacing could not be read, so no "
                               "extent is proposed for it")
            offered.append(entry)
            continue

        radius_px = period_px * CLOSE_AT_PERIOD
        entry["edge_uncertainty_m"] = radius_px * k / S
        closer = layer_surfaces._Closer(ink, radius_px + 2)
        closed, _voids_m2, _voids_n = layer_surfaces._fill_holes(
            closer.close(radius_px), per_px_m2=per_px_m2, keep_m2=layer_surfaces.HOLE_KEEP_M2)

        # The bridge crossed ground the drawing left blank between strokes. Ask the OTHER
        # constructions on this sheet whether it crossed ground already spoken for — a
        # question answered by the sheet, not by a constant.
        others = np.zeros_like(closed, dtype=bool)
        for other in rows:
            if other["name"] != row["name"]:
                others |= inks[other["name"]][0].astype(bool)
        bleed_px = int((closed & others).sum())
        closed = (closed & ~others).astype(np.uint8)

        kept, areas, parts = _row_regions(closed, ink, S, k)
        area_m2 = sum(areas)
        if area_m2 < layer_surfaces.MIN_REGION_M2:
            entry["reason"] = (f"its hatch closes into {area_m2:,.0f} m², under the "
                               f"{layer_surfaces.MIN_REGION_M2:.0f} m² floor for a surface")
            offered.append(entry)
            continue

        ink_bool = ink.astype(bool)
        retention = float((kept & ink_bool).sum()) / float(ink_bool.sum())
        if retention < SOLID_RETENTION:
            entry["reason"] = (f"the outline built from its hatch holds only "
                               f"{100 * retention:.0f}% of that hatch — it is not the ground "
                               "the engineer drew, so no area is claimed")
            offered.append(entry)
            continue

        bleed_m2 = bleed_px * per_px_m2
        if bleed_m2 > layer_surfaces.MAX_BLEED_FRACTION * area_m2:
            entry["reason"] = (f"closing its hatch ran {bleed_m2:,.0f} m² over the other "
                               "constructions on this sheet — the bridge is crossing real "
                               "boundaries, so no area is claimed")
            offered.append(entry)
            continue

        # Simplify no finer than this row's own closing radius. The outline is only located
        # to within that radius, so any wobble below it is the stroke ends showing through the
        # mask, not the engineer's boundary -- and it is exactly the zigzag Aryan reported on
        # 15 Sep. A quarter of the radius is enough to cut across the sawtooth while leaving
        # real corners alone; the fidelity check still refuses anything that stops reproducing
        # the measured area.
        _eps_floor = radius_px * OUTLINE_EPS_OF_RADIUS
        polygon, _holes, fidelity = hatch_legend_raster._outline_for(
            kept.astype(np.uint8), area_m2, S, k, min_eps_px=_eps_floor)
        if not polygon and len(parts) > 1:
            # A construction the drawing puts in two places is still one construction, and the
            # only thing missing is a single outline around both. Trace each piece on its own
            # and offer them as parts: every part is a shape the assessor can see, and including
            # all of them gives the construction's area on this sheet. 0701's Container slab is
            # 17,689 m2 in two pieces, and withholding it hid the biggest thing on the sheet.
            piece_entries, remainder_m2, remainder_n = [], 0.0, 0
            for part_mask, part_area in sorted(parts, key=lambda p: -p[1]):
                if part_area < layer_surfaces.MIN_REGION_M2:
                    remainder_m2 += part_area
                    remainder_n += 1
                    continue
                part_poly, _part_holes, _part_fid = hatch_legend_raster._outline_for(
                    part_mask.astype(np.uint8), part_area, S, k, min_eps_px=_eps_floor)
                if not part_poly:
                    remainder_m2 += part_area
                    remainder_n += 1
                    continue
                piece_entries.append((part_area, part_poly))
            if piece_entries:
                offered_total = sum(area for area, _poly in piece_entries)
                for part_index, (part_area, part_poly) in enumerate(piece_entries, 1):
                    part = dict(entry)
                    part["area_m2"] = round(part_area, 1)
                    part["polygon_pts"] = part_poly
                    part["components"] = 1
                    part["retention"] = round(retention, 3)
                    part["part_index"] = part_index
                    part["part_count"] = len(piece_entries)
                    part["part_total_m2"] = round(offered_total, 1)
                    part["remainder_m2"] = round(remainder_m2, 1)
                    part["remainder_n"] = remainder_n
                    offered.append(part)
                continue

        if not polygon:
            # An offer the assessor cannot see on the sheet is not an offer. The area may well
            # be right, but a candidate with no outline cannot be reviewed, included, or drawn
            # on the markup, and a number with no shape beside it is the thing this pipeline
            # exists to stop.
            entry["reason"] = (
                "its ground measures "
                f"{area_m2:,.0f} m² across {len(areas)} separate pieces, but a single outline "
                "that reproduces that area could not be traced"
                + (f" (off by {100 * fidelity:.0f}%)" if fidelity is not None else "")
                + ", so nothing is offered for it")
            offered.append(entry)
            continue
        entry["area_m2"] = round(area_m2, 1)
        entry["components"] = len(areas)
        entry["polygon_pts"] = polygon
        entry["retention"] = round(retention, 3)
        offered.append(entry)

    measurable = [entry for entry in offered if entry["area_m2"] is not None]
    if not measurable:
        return {"ok": False, "rows": offered,
                "reason": ("the legend read cleanly but none of its constructions produced a "
                           "measurable extent on this sheet")}
    return {"ok": True, "rows": offered, "reason": ""}
