"""Measure a surface from the CAD layer its engineer drew it on.

A vector site plan carries the draughtsman's own optional-content groups — layer names,
preserved through the PDF export.  On 49 of the 187 drawings in this repo's corpus a layer
names the surface outright: ``RL_Surfacing_Service Yard`` (RLRE), ``hatch-ServiceYard``
(Volumetric), ``- BMP - Proposed Concrete Yard``, ``PRP_Hatch Concrete Service Yard``.  That
name is evidence of a different kind from colour.  Colour is what the renderer painted; the
layer name is what the engineer declared.

It settles cases colour provably cannot.  On Indurent Park 22513-RLL-25-00-DR-C-3151 the
C1 Service Yard and the F1 Footway (Vehicle Overrun) are drawn in the SAME ink,
RGB (118,118,118).  No colour method can separate them.  The layer name separates them
exactly, and Aryan asked for "the c1 only".

WHAT THIS MODULE DOES NOT DO.  It is reachable from exactly one place: the
``SURFACE NOT IDENTIFIED`` refusal in ``takeoff_unmarked``, where the legend named a tint
and no region of that tint was found.  A sheet that measures today keeps measuring by the
path it measures by now.  Measured across the whole corpus either side of the change:
617 files, ONE row different — Indurent, REFUSED_CLEANLY to MEASURED_OK.

THE BRIDGE.  These layers hold no boundary — on 3151 the Service Yard layer is 15,634
stipple glyphs, zero closed paths, and every clip on the page is a viewport rectangle.  A
scatter of glyphs has to be closed into an enclosure, and the closing radius decides the
answer.  It is NOT tuned against the client's markup: it is grown from the sheet itself
until the measured area stops growing (``_bridge_radius_pt``).  Closing by a disc of radius
R spans a GAP of up to 2R, and 2R is the number disclosed and the number bounded — an
earlier revision disclosed R and told the assessor half the truth.

KNOWN AND DELIBERATE LIMITS (each of these REFUSES; none of them invents a number):
  * The label is whatever text the legend reader returns.  When that text is a sentence
    ("service yard (cl. 5.1 of the prologis ba") rather than a name it will not match a
    layer called ``C-EF_30_60_95-H_ServiceYard``, and the sheet refuses.  Measured: 9 of
    47 layered corpus sheets refuse for this reason.  Loosening the match is the direction
    that produces WRONG surfaces, so it is not done without a client markup to check against.
  * Only the page the caller hands us is read.  A layered surface on page 2 of a pack is
    not found.
  * A label that matches two visible layers refuses and names both.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

# The bridge search.  Radii in PDF points; the sheet decides which one is used.
SEARCH_MIN_PT = 2.0
SEARCH_MAX_PT = 20.0
SEARCH_STEP_PT = 0.5
CONVERGENCE_WINDOW_PT = 1.0     # look this far ahead for continued growth
CONVERGENCE_GROWTH = 0.02       # ...and call it converged below this fraction

# A closing of radius R spans a gap of 2R. Beyond this the outline stops closing a stipple
# and starts merging whatever is nearby — two surfaces either side of a verge become one.
MAX_BRIDGE_M = 3.0

MIN_REGION_M2 = 200.0           # below this a component is stipple noise, not a surface
# The closed mask may not swallow this much of another surface. KNOW WHAT THIS DOES AND DOES
# NOT CATCH. It compares against sibling layers' INK, not against the ground those strokes
# hatch, so on a sparse hatch it is far weaker than the 5% suggests: on a sheet whose siblings
# ink 1.5% of what they cover, the mask must swallow several times the measured area before it
# fires. Closing the siblings first to get their real extent was tried and is WRONG — it
# inflates a neighbouring hatch across its own boundary (measured on 3151: siblings 10,781 m2
# of ink close to 40,842 m2 and produce 59.9% false bleed, refusing a sheet that is correct).
# So read a low bleed number as ABSENCE OF EVIDENCE, not as proof of no overlap. The guards
# actually carrying the weight here are the single-visible-layer rule, the wall/kerb denylist
# and MIN_INK_RETENTION; this one only catches gross spillage.
MAX_BLEED_FRACTION = 0.05

# A void larger than this is an ISLAND — a building, a pond, a planter the surface goes
# around — and is kept as a deduction, the way the colour path already keeps them. Anything
# smaller is a perforation in the stipple itself. Measured on 3151: 296 voids, largest
# 11.3 m2, and 99.7% of the filled area falls inside the client's own polygons.
HOLE_KEEP_M2 = 200.0

# The final mask must still contain the layer's own ink. Any mechanism that throws the
# surface away — a bridge that converged on a solid fragment, a component filter that
# dropped the field — fails this regardless of HOW it went wrong. Measured across 431
# invocations on 51 layered drawings: minimum retention 0.98.
MIN_INK_RETENTION = 0.95

# Layer-name noise that carries no surface meaning: CAD prefixes and export decorations.
# Stripped from the START of the reduced string only. "ap" removed anywhere would turn
# "dockapron" into "dockron"; as a prefix it correctly reduces AEW's "ap_Parking".
_STRIP_PREFIXES = (
    "rlsurfacing", "rl", "prp", "bmp", "htc", "ap", "os", "ug", "hatch", "hatching",
    "proposed", "existing", "surfacing", "surface",
)

# Below this many letters a containment match is a coincidence, not an identification:
# "yard" alone would match both "serviceyard" and "concreteyard".
MIN_MATCH_LETTERS = 6

# A layer whose name says it is a LINE or a NOTE is not a surface, however well its name
# matches. "Service Yard Retaining Wall" contains "service yard"; measuring it would price
# a wall as a slab.
_NOT_A_SURFACE = (
    "wall", "kerb", "curb", "edging", "joint", "text", "label", "dimension", "drain",
    "channel", "gully", "duct", "fence", "line", "boundary", "level", "setting out",
)


def normalise(name: str) -> str:
    """Reduce a layer or legend string to comparable letters."""
    if not name:
        return ""
    tail = name.split("|")[-1]          # drop the parent OCG path
    tail = tail.split("$")[-1]          # drop Volumetric's $0$A010H_ export decoration
    letters = "".join(ch for ch in tail.lower() if ch.isalnum())
    changed = True
    while changed:
        changed = False
        for token in _STRIP_PREFIXES:
            if letters.startswith(token) and len(letters) > len(token):
                letters = letters[len(token):]
                changed = True
                break
    return letters


def names_same_surface(label: str, layer: str) -> bool:
    """Do a legend label and a layer name identify the same surface?

    Containment both ways, because neither side is reliably the longer one: the legend
    carries a drawing code the layer omits (``C1 - Service Yard`` vs ``Service Yard``) and
    the layer carries qualifiers the legend omits (``Service Yard (Planning)``).
    """
    if any(token in (layer or "").lower() for token in _NOT_A_SURFACE):
        return False
    a, b = normalise(label), normalise(layer)
    if len(a) < MIN_MATCH_LETTERS or len(b) < MIN_MATCH_LETTERS:
        return False
    return a in b or b in a


def _drawings(page, cache=None):
    """Parse the page's vector items ONCE. Three separate parses cost 3x for no gain."""
    if cache is not None:
        return cache
    return page.get_drawings(extended=True)


def _visible_layer_names(doc) -> set:
    """Layer names the default configuration actually renders.

    A sheet carrying ``(Option 2)`` and ``(Option 3)`` variants would match the same surface
    twice; measuring a hidden option would measure a surface nobody is building.
    """
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:
        return set()
    return {info.get("name") for info in ocgs.values()
            if info.get("on", True) and info.get("name")}


def find_surface_layer(doc, page, label, drawings=None):
    """Return ``(layer_name, candidates, reason)`` for the layer that names ``label``.

    ``layer_name`` is None unless EXACTLY ONE visible layer matches. Zero and two matches
    are both refusals, and both name what was seen so the assessor can judge.
    """
    if len(normalise(label)) < MIN_MATCH_LETTERS:
        return None, [], (f"legend label {label!r} is too short to identify a CAD layer by "
                          "name without guessing")
    visible = _visible_layer_names(doc)
    if not visible:
        return None, [], "the drawing carries no CAD layers (no optional-content groups)"

    present = set()
    try:
        for item in _drawings(page, drawings):
            name = item.get("layer")
            if name:
                present.add(name)
    except Exception as exc:                                   # pragma: no cover - defensive
        return None, [], f"could not read layered geometry: {exc}"
    if not present:
        return None, [], "the drawing declares CAD layers but draws nothing on them"

    candidates = sorted(
        name for name in present
        if name in visible and names_same_surface(label, name)
    )
    if len(candidates) == 1:
        return candidates[0], candidates, ""
    if not candidates:
        return None, [], (f"no visible CAD layer names '{label}' "
                          f"({len(present)} layers on the sheet)")
    return None, candidates, (
        f"{len(candidates)} visible CAD layers name '{label}' — "
        + "; ".join(candidates) + " — which one is the surface cannot be decided from the "
        "drawing alone")


def _item_points(item, rot, scale):
    """Every vertex of one drawing item, transformed to raster space."""
    pts = []
    for element in item.get("items") or ():
        op = element[0]
        if op == "re":
            rect = element[1] * rot
            pts += [(rect.x0 * scale, rect.y0 * scale), (rect.x1 * scale, rect.y0 * scale),
                    (rect.x1 * scale, rect.y1 * scale), (rect.x0 * scale, rect.y1 * scale)]
        elif op == "qu":
            quad = element[1]
            for corner in (quad.ul, quad.ur, quad.lr, quad.ll):
                point = corner * rot
                pts.append((point.x * scale, point.y * scale))
        else:
            # "l" gives two points, "c" a cubic's four control points. At stipple scale the
            # control polygon and the curve are the same handful of pixels.
            for corner in element[1:]:
                if hasattr(corner, "x"):
                    point = corner * rot
                    pts.append((point.x * scale, point.y * scale))
    return pts


def _layer_ink(page, keep, scale, drawings=None, size_limit_pt2=10000.0):
    """Raster the marks on the layers ``keep(name)`` accepts, at ``scale`` px per point.

    The INK is drawn, not the bounding box around it. Filling bounding boxes turned a single
    diagonal leader line sharing the layer into a solid 300 m2 block, which was enough to
    collapse 3151 from 10,511 m2 to 603 m2 — a wrong number, reported as a measurement, with
    every gate passed. A stroke is a stroke: it contributes a line, not the square it spans.

    ``get_drawings`` reports UNROTATED coordinates while the caller's raster is the rotated
    page, so every point goes through ``page.rotation_matrix`` first. Painting unrotated
    points onto a rotated frame put the mask somewhere else on the sheet entirely.
    """
    rot = page.rotation_matrix
    width = int(math.ceil(page.rect.width * scale))
    height = int(math.ceil(page.rect.height * scale))
    mask = np.zeros((height, width), np.uint8)
    marks = 0
    for item in _drawings(page, drawings):
        if not keep(item.get("layer") or ""):
            continue
        raw = item["rect"]
        # A whole-sheet path on a surface layer is a frame or a section line, never a mark.
        if raw.width * raw.height > size_limit_pt2:
            continue
        pts = _item_points(item, rot, scale)
        if not pts:
            continue
        poly = np.round(np.array(pts, dtype=float)).astype(np.int32)
        solid = "f" in (item.get("type") or "") or item.get("closePath")
        if solid and len(poly) >= 3:
            cv2.fillPoly(mask, [poly], 1)
        else:
            thickness = max(1, int(round(float(item.get("width") or 0.0) * scale)))
            cv2.polylines(mask, [poly], False, 1, thickness)
        marks += 1
    return mask, marks


class _Closer:
    """Morphological closing by a Euclidean disc, at any radius, via distance transforms.

    ``cv2.morphologyEx`` with a 40-pixel ellipse on a 16-megapixel page is O(N.k^2) and takes
    minutes (it segfaulted a probe on this very page); a distance transform is O(N) and gives
    the same disc exactly. The outer field does not depend on the radius, so the bridge
    search computes it once and pays one transform per candidate radius instead of two.
    """

    def __init__(self, mask, max_radius_px):
        self.pad = int(math.ceil(max_radius_px)) + 2
        padded = cv2.copyMakeBorder(mask, self.pad, self.pad, self.pad, self.pad,
                                    cv2.BORDER_CONSTANT, value=0)
        self.outside = cv2.distanceTransform(1 - padded, cv2.DIST_L2, 5)

    def close(self, radius_px):
        grown = (self.outside <= radius_px).astype(np.uint8)
        shrunk = (cv2.distanceTransform(grown, cv2.DIST_L2, 5) > radius_px).astype(np.uint8)
        return shrunk[self.pad:-self.pad, self.pad:-self.pad]


def _enclosed(mask):
    """Return the mask of voids fully enclosed by ``mask``.

    The flood must start on a pixel that is certainly OUTSIDE. Seeding (0,0) of the mask
    itself is not safe: one foreground pixel in that corner makes the flood a no-op and
    ``mask | (1 - flooded)`` becomes ALL ONES — the whole page reported as the surface.
    Padding by one guarantees a background seed.
    """
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    flooded = padded.copy()
    height, width = flooded.shape
    cv2.floodFill(flooded, np.zeros((height + 2, width + 2), np.uint8), (0, 0), 1)
    return (1 - flooded)[1:-1, 1:-1]


def _fill_holes(mask, per_px_m2=None, keep_m2=None):
    """Fill perforations; keep real islands as deductions.

    Returns ``(filled, kept_voids_m2, kept_void_count)``. A void bigger than ``keep_m2`` is
    a building or a pond that the surface goes around — the colour path already keeps those
    as deductions, and filling them would quote ground nobody is paving.
    """
    voids = _enclosed(mask)
    if per_px_m2 is None or keep_m2 is None:
        return (mask | voids), 0.0, 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(voids, 8)
    fill = np.zeros_like(mask)
    kept_m2, kept_n = 0.0, 0
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA] * per_px_m2
        if area < keep_m2:
            fill |= (labels == index).astype(np.uint8)
        else:
            kept_m2 += area
            kept_n += 1
    return (mask | fill), kept_m2, kept_n


def _regions(mask, scale, k):
    """Connected components above ``MIN_REGION_M2``, as (label index, area_m2) pairs."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    per_px_m2 = (k / scale) ** 2
    out = []
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA] * per_px_m2
        if area >= MIN_REGION_M2:
            out.append((index, area))
    return labels, out, per_px_m2


def _bridge_radius_pt(closer, scale, k, ink=None):
    """The smallest disc that makes the field of marks solid, grown from the sheet itself.

    Nothing here consults a known answer. As the radius rises the closed area is flat while
    the marks are still separate, climbs steeply as they merge, then flattens again once the
    field is solid and only the outer boundary is creeping. The radius wanted is the start of
    that second plateau.

    The FIRST plateau is why this is anchored on the peak. An earlier revision took the first
    radius whose area stopped changing, and on a regular lattice — where the area really is
    flat until the marks touch — that was the very first radius tried: the field was still in
    pieces, every piece fell under MIN_REGION_M2, and the whole surface was discarded. On an
    irregular stipple like 3151's the area creeps from the start, which hid it. So: find where
    the area grows fastest (the merge), and only look for a plateau after that.
    """
    steps = int(round((SEARCH_MAX_PT - SEARCH_MIN_PT) / SEARCH_STEP_PT)) + 1
    radii = [round(SEARCH_MIN_PT + i * SEARCH_STEP_PT, 2) for i in range(steps)]
    per_px_m2 = (k / scale) ** 2
    areas = {}

    def area_at(radius):
        if radius not in areas:
            closed, _, _ = _fill_holes(closer.close(radius * scale))
            areas[radius] = float(closed.sum()) * per_px_m2
        return areas[radius]

    curve = [area_at(r) for r in radii]
    growth = [curve[i + 1] - curve[i] for i in range(len(curve) - 1)]
    if not growth or max(growth) <= 0:
        return None, areas
    merged_at = radii[int(np.argmax(growth)) + 1]   # the radius the merge completes at

    for radius in radii:
        if radius < merged_at:
            continue
        ahead = round(radius + CONVERGENCE_WINDOW_PT, 2)
        if ahead > SEARCH_MAX_PT:
            break
        here = area_at(radius)
        if here <= 0:
            continue
        if (area_at(ahead) - here) / here < CONVERGENCE_GROWTH:
            return radius, areas
    return None, areas


def _is_surfacing(name, like):
    """Is ``name`` a sibling surface layer of ``like``?

    Siblings share the parent OCG and a surface-family marker, so a kerb, a text layer or a
    drainage run is not mistaken for a competing surface. A flat layer name has NO parent —
    ``split("|")[0]`` returns the whole name, so comparing those made every sibling test
    false and silently switched the overlap guard off on half the layered corpus.
    """
    lowered = name.lower()
    if not any(token in lowered for token in ("surfac", "hatch", "pavement", "parking",
                                              "carriageway", "footway", "concrete")):
        return False
    parent = lambda n: n.split("|")[0] if "|" in n else ""
    return parent(name) == parent(like)


def measure(doc, page, label, k, S=2.0):
    """Measure ``label`` from the CAD layer that names it.

    Returns a dict. ``ok`` False means this method does not apply and the caller should
    refuse exactly as it would have; ``reason`` says why, in words an assessor can act on.
    NEVER raises: this sits on the measurement path, and the four-state contract says a
    drawing ends in a state, not in a traceback.
    """
    try:
        return _measure(doc, page, label, k, S)
    except Exception as exc:                                   # pragma: no cover - defensive
        return {"ok": False, "candidates": [],
                "reason": (f"reading the CAD layers failed ({type(exc).__name__}: {exc}) — "
                           "no measurement is claimed from them")}


def _measure(doc, page, label, k, S=2.0):
    drawings = page.get_drawings(extended=True)
    layer, candidates, reason = find_surface_layer(doc, page, label, drawings=drawings)
    if not layer:
        return {"ok": False, "reason": reason, "candidates": candidates}

    ink, marks = _layer_ink(page, lambda n: n == layer, S, drawings=drawings)
    if marks == 0 or not ink.any():
        return {"ok": False, "candidates": candidates,
                "reason": f"CAD layer '{layer}' names the surface but draws nothing on it"}

    per_px_m2 = (k / S) ** 2
    closer = _Closer(ink, SEARCH_MAX_PT * S)
    radius_pt, _ = _bridge_radius_pt(closer, S, k, ink)
    if radius_pt is None:
        return {"ok": False, "candidates": candidates,
                "reason": (f"the {marks} marks on CAD layer '{layer}' never close into a "
                           f"region: no bridge up to {2 * SEARCH_MAX_PT * k:.1f} m makes "
                           "them one surface")}
    # Closing by a disc of radius R spans a GAP of up to 2R. That is the number that matters
    # to the assessor and the number that must be bounded.
    bridge_m = 2 * radius_pt * k
    if bridge_m > MAX_BRIDGE_M:
        return {"ok": False, "candidates": candidates,
                "reason": (f"closing CAD layer '{layer}' needs a {bridge_m:.1f} m bridge, "
                           f"beyond the {MAX_BRIDGE_M:.0f} m limit — at that distance the "
                           "outline would merge surfaces the drawing keeps apart")}

    closed, kept_void_m2, kept_void_n = _fill_holes(
        closer.close(radius_pt * S), per_px_m2=per_px_m2, keep_m2=HOLE_KEEP_M2)

    # The bridge crossed ground the drawing left blank. Ask the OTHER surfacing layers
    # whether it crossed ground that was already spoken for — derived from the sheet.
    others, _ = _layer_ink(page, lambda n: n != layer and _is_surfacing(n, layer), S,
                           drawings=drawings)
    bleed_px = int((closed & others).sum())
    if bleed_px:
        closed = (closed & ~others.astype(bool)).astype(np.uint8)

    labels, regions, _ = _regions(closed, S, k)
    if not regions:
        return {"ok": False, "candidates": candidates,
                "reason": (f"CAD layer '{layer}' closes into nothing larger than "
                           f"{MIN_REGION_M2:.0f} m²")}
    regions.sort(key=lambda pair: -pair[1])
    region_masks = [(labels == index) for index, _ in regions]
    kept = np.zeros_like(closed, dtype=bool)
    for region_mask in region_masks:
        kept |= region_mask

    # Independent net: whatever the mechanism, the answer must still contain the marks it
    # claims to have measured.
    ink_bool = ink.astype(bool)
    retention = float((kept & ink_bool).sum()) / float(ink_bool.sum())
    if retention < MIN_INK_RETENTION:
        return {"ok": False, "candidates": candidates,
                "reason": (f"the outline built from CAD layer '{layer}' contains only "
                           f"{100 * retention:.0f}% of that layer's own marks — it is not "
                           "the surface the engineer drew, so no area is claimed")}

    area_m2 = sum(area for _, area in regions)
    bleed_m2 = bleed_px * per_px_m2
    if bleed_m2 > MAX_BLEED_FRACTION * area_m2:
        return {"ok": False, "candidates": candidates,
                "reason": (f"closing CAD layer '{layer}' ran {bleed_m2:.0f} m² over other "
                           f"surfacing layers ({100 * bleed_m2 / area_m2:.0f}% of the "
                           "result) — the bridge is crossing real boundaries")}

    flags = [
        f"SURFACE FROM CAD LAYER: measured '{label}' as the {marks:,} marks the engineer "
        f"drew on layer '{layer}'. The surface is identified by the name the drawing gives "
        f"it, not by matching a colour.",
        f"OUTLINE BRIDGED {bridge_m:.2f} m: the layer holds marks, not a boundary, so the "
        f"outline is those marks closed across gaps up to {bridge_m:.2f} m (a disc of "
        f"radius {radius_pt * k:.2f} m). That radius is grown from the sheet: it is the "
        f"smallest at which the marks have merged AND a further {CONVERGENCE_WINDOW_PT:g} pt "
        f"of closing changes the area by less than {100 * CONVERGENCE_GROWTH:g}%. Ground "
        f"within {bridge_m:.2f} m of the marks is treated as surface.",
        f"AREA IS A MINIMUM: the marks stop short of the edge they are drawn inside, so "
        f"{area_m2:,.0f} m² is the extent of the marks, not the edge of the surface. "
        f"Assessor: check the outline against the drawing and treat this as a floor.",
    ]
    if kept_void_n:
        flags.append(
            f"assessor: {kept_void_n} void(s) totalling {kept_void_m2:,.0f} m² inside the "
            f"surface were KEPT AS DEDUCTIONS, not filled — each is larger than "
            f"{HOLE_KEEP_M2:.0f} m² and so is an island the surface goes around rather than "
            "a gap in the pattern. Confirm each before approval.")
    if bleed_m2:
        flags.append(
            f"assessor: the closed outline overlapped {bleed_m2:.0f} m² of other surfacing "
            f"layers ({100 * bleed_m2 / area_m2:.1f}%); that overlap was removed from the "
            "measurement rather than claimed.")
    return {"ok": True, "layer": layer, "candidates": candidates, "mask": closed,
            "region_masks": region_masks, "area_m2": area_m2, "bridge_m": bridge_m,
            "disc_radius_m": radius_pt * k, "bleed_m2": bleed_m2,
            "kept_void_m2": kept_void_m2, "kept_void_count": kept_void_n,
            "ink_retention": retention, "marks": marks, "flags": flags}
