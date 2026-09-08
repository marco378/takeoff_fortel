"""Measure a surface from the CAD layer its engineer drew it on.

A vector site plan carries the draughtsman's own optional-content groups — layer names,
preserved through the PDF export.  On 49 of the 187 drawings in this repo's corpus a layer
names the surface outright: ``RL_Surfacing_Service Yard`` (RLRE), ``hatch-ServiceYard``
(Volumetric), ``- BMP - Proposed Concrete Yard``, ``PRP_Concrete``.  That name is evidence
of a different kind from colour.  Colour is what the renderer painted; the layer name is
what the engineer declared.

It settles cases colour provably cannot.  On Indurent Park 22513-RLL-25-00-DR-C-3151 the
C1 Service Yard and the F1 Footway (Vehicle Overrun) are drawn in the SAME ink,
RGB (118,118,118).  No colour method can separate them.  The layer name separates them
exactly, and Aryan asked for "the c1 only".

WHAT THIS MODULE DOES NOT DO.  It is reachable from exactly one place: the
``SURFACE NOT IDENTIFIED`` refusal in ``takeoff_unmarked``, where the legend named a tint
and no region of that tint was found.  A sheet that measures today keeps measuring by the
path it measures by now.  This turns a refusal into a measurement; it never turns a
measurement into a different measurement.

THE BRIDGE.  These layers hold no boundary — on 3151 the Service Yard layer is 15,634
stipple glyphs, zero closed paths, and every clip on the page is a viewport rectangle.  A
scatter of glyphs has to be closed into an enclosure, and the closing radius decides the
answer.  It is NOT tuned against the client's markup.  It is grown from the sheet itself
until the measured area stops growing (``_bridge_radius_pt``): below convergence the field
is still perforated and the area climbs steeply; above it, only the outer boundary creeps.
The radius that survives is disclosed to the assessor in metres, because a 0.9 m bridge is
a 0.9 m assumption about ground the drawing left blank.

THE FIGURE IS A FLOOR.  A stipple stops short of its own clip boundary — the generator
drops glyphs whose mark would cross the edge — so the closed envelope sits inside the true
surface by up to half a stipple pitch.  Measured against the client's own markup on 3151
the three regions come in 5.4% low, never high.  V1 reports the floor and says so rather
than inflating toward an answer it cannot independently check.
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

# A bridge wider than this stops being "close the stipple" and starts being "merge whatever
# is nearby" — two surfaces separated by a 3 m verge would be joined into one.
MAX_BRIDGE_M = 3.0

MIN_REGION_M2 = 200.0           # below this a component is stipple noise, not a surface
MAX_BLEED_FRACTION = 0.05       # closed mask may not swallow this much of another surface

# Layer-name noise that carries no surface meaning: CAD prefixes and export decorations.
# Stripped from the START of the reduced string only.  "ap" removed anywhere would turn
# "dockapron" into "dockron"; as a prefix it correctly reduces AEW's "ap_Parking".
_STRIP_PREFIXES = (
    "rlsurfacing", "rl", "prp", "bmp", "htc", "ap", "os", "ug", "hatch", "hatching",
    "proposed", "existing", "surfacing", "surface",
)

# Below this many letters a containment match is a coincidence, not an identification:
# "yard" alone would match both "serviceyard" and "concreteyard".
MIN_MATCH_LETTERS = 6

def normalise(name: str) -> str:
    """Reduce a layer or legend string to comparable letters.

    ``x_Pavement Construction (Option 3)|RL_Surfacing_Service Yard`` and
    ``...|11735_Proposed Site$0$A010H_hatch-ServiceYard`` and a legend row reading
    ``C1 - Service Yard`` must all reduce to something that identifies the same surface.
    """
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

    Containment both ways, because neither side is the longer one reliably: the legend
    carries a drawing code the layer omits (``C1 - Service Yard`` vs ``Service Yard``) and
    the layer carries qualifiers the legend omits (``Service Yard (Planning)``).
    """
    a, b = normalise(label), normalise(layer)
    if len(a) < MIN_MATCH_LETTERS or len(b) < MIN_MATCH_LETTERS:
        return False
    return a in b or b in a


def _visible_layer_names(doc) -> set:
    """Layer names the default configuration actually renders.

    A sheet carrying ``(Option 2)`` and ``(Option 3)`` variants would match the same surface
    twice; measuring a hidden option would measure a surface nobody is building.  Read the
    per-OCG ``on`` flag rather than inferring "all on" from an empty on/off config.
    """
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:
        return set()
    return {info.get("name") for info in ocgs.values()
            if info.get("on", True) and info.get("name")}


def find_surface_layer(doc, page, label):
    """Return ``(layer_name, candidates, reason)`` for the layer that names ``label``.

    ``layer_name`` is None unless EXACTLY ONE visible layer matches.  Zero matches and two
    matches are both refusals, and both name what was seen so the assessor can judge.
    """
    if len(normalise(label)) < MIN_MATCH_LETTERS:
        return None, [], (f"legend label {label!r} is too short to identify a CAD layer by "
                          "name without guessing")
    visible = _visible_layer_names(doc)
    if not visible:
        return None, [], "the drawing carries no CAD layers (no optional-content groups)"

    present = set()
    try:
        for item in page.get_drawings(extended=True):
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


def _layer_ink(page, keep, scale, size_limit_pt2=10000.0):
    """Raster the glyphs on the layers ``keep(name)`` accepts, at ``scale`` px per point."""
    width = int(math.ceil(page.rect.width * scale))
    height = int(math.ceil(page.rect.height * scale))
    mask = np.zeros((height, width), np.uint8)
    marks = 0
    for item in page.get_drawings(extended=True):
        name = item.get("layer") or ""
        if not keep(name):
            continue
        rect = item["rect"]
        # A stipple glyph is a few points across.  The occasional full-width stroke that
        # shares the layer (a section line, a leader) is not the surface and would drag the
        # envelope across the sheet.
        if rect.width * rect.height > size_limit_pt2:
            continue
        cv2.rectangle(mask,
                      (int(rect.x0 * scale), int(rect.y0 * scale)),
                      (int(math.ceil(rect.x1 * scale)), int(math.ceil(rect.y1 * scale))),
                      1, -1)
        marks += 1
    return mask, marks


class _Closer:
    """Morphological closing by a Euclidean disc, at any radius, via distance transforms.

    ``cv2.morphologyEx`` with a 40-pixel ellipse on a 16-megapixel page is O(N·k²) and takes
    minutes; a distance transform is O(N) and gives the same disc exactly.  The outer field
    does not depend on the radius, so the bridge search computes it once and pays for one
    transform per candidate radius instead of two.
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


def _fill_holes(mask):
    flooded = mask.copy()
    height, width = flooded.shape
    cv2.floodFill(flooded, np.zeros((height + 2, width + 2), np.uint8), (0, 0), 1)
    return mask | (1 - flooded)


def _regions(mask, scale, k):
    """Connected components above ``MIN_REGION_M2``, as (mask, area_m2) pairs."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    per_px_m2 = (k / scale) ** 2
    out = []
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA] * per_px_m2
        if area >= MIN_REGION_M2:
            out.append((index, area))
    return labels, out, per_px_m2


def _closed_area(closer, radius_pt, scale, k):
    closed = _fill_holes(closer.close(radius_pt * scale))
    _, regions, _ = _regions(closed, scale, k)
    return sum(area for _, area in regions), len(regions)


def _bridge_radius_pt(closer, scale, k):
    """Grow the bridge until the measured area stops growing.

    Nothing here consults a known answer.  A stipple that is still perforated gains area
    fast as the radius rises (on 3151: 599 m² at 3.5 pt, 8,748 at 4.0, 10,156 at 4.5); once
    the field is solid the only remaining growth is the outer boundary creeping outward
    (10,523 at 5.0, 10,715 at 6.0 — 1.8%).  The first radius whose area is within
    ``CONVERGENCE_GROWTH`` of the area a further ``CONVERGENCE_WINDOW_PT`` buys is the
    smallest radius that solidifies the field, and the smallest is the safest.

    The search runs on the SAME raster the measurement is reported from.  Sizing the bridge
    on a coarser raster picks the radius that closes a coarser mask: at 1 px/pt this sheet
    converged 0.5 pt early and came out 12% low instead of 5%.
    """
    steps = int(round((SEARCH_MAX_PT - SEARCH_MIN_PT) / SEARCH_STEP_PT)) + 1
    radii = [round(SEARCH_MIN_PT + i * SEARCH_STEP_PT, 2) for i in range(steps)]
    areas = {}

    def area_at(radius):
        if radius not in areas:
            areas[radius] = _closed_area(closer, radius, scale, k)[0]
        return areas[radius]

    for radius in radii:
        ahead = round(radius + CONVERGENCE_WINDOW_PT, 2)
        if ahead > SEARCH_MAX_PT:
            break
        here = area_at(radius)
        if here < MIN_REGION_M2:
            continue
        if (area_at(ahead) - here) / here < CONVERGENCE_GROWTH:
            return radius, areas
    return None, areas


def measure(doc, page, label, k, S=2.0):
    """Measure ``label`` from the CAD layer that names it.

    Returns a dict.  ``ok`` False means this method does not apply and the caller should
    refuse exactly as it would have; ``reason`` says why, in words an assessor can act on.
    On success: ``mask`` (uint8, page rendered at ``S`` px per point — the caller's raster
    space), ``region_masks`` (boolean, largest first), ``area_m2``, ``bridge_m``,
    ``layer``, and ``flags`` stating the bridge and the floor.
    """
    layer, candidates, reason = find_surface_layer(doc, page, label)
    if not layer:
        return {"ok": False, "reason": reason, "candidates": candidates}

    ink, marks = _layer_ink(page, lambda n: n == layer, S)
    if marks == 0 or not ink.any():
        return {"ok": False, "candidates": candidates,
                "reason": f"CAD layer '{layer}' names the surface but draws nothing on it"}

    closer = _Closer(ink, SEARCH_MAX_PT * S)
    radius_pt, _ = _bridge_radius_pt(closer, S, k)
    if radius_pt is None:
        return {"ok": False, "candidates": candidates,
                "reason": (f"the {marks} marks on CAD layer '{layer}' never close into a "
                           f"region: no bridge up to {SEARCH_MAX_PT * k:.1f} m makes them "
                           "one surface")}
    bridge_m = radius_pt * k
    if bridge_m > MAX_BRIDGE_M:
        return {"ok": False, "candidates": candidates,
                "reason": (f"closing CAD layer '{layer}' needs a {bridge_m:.1f} m bridge, "
                           f"beyond the {MAX_BRIDGE_M:.0f} m limit — at that distance the "
                           "outline would merge surfaces the drawing keeps apart")}

    closed = _fill_holes(closer.close(radius_pt * S))

    # The bridge crossed ground the drawing left blank.  Ask the OTHER surfacing layers
    # whether it crossed ground that was already spoken for — derived from the sheet, not
    # from any external answer.
    others, _ = _layer_ink(page, lambda n: n != layer and _is_surfacing(n, layer), S)
    bleed_px = int((closed & others).sum())
    if bleed_px:
        closed = (closed & ~others.astype(bool)).astype(np.uint8)

    labels, regions, per_px_m2 = _regions(closed, S, k)
    if not regions:
        return {"ok": False, "candidates": candidates,
                "reason": (f"CAD layer '{layer}' closes into nothing larger than "
                           f"{MIN_REGION_M2:.0f} m²")}
    regions.sort(key=lambda pair: -pair[1])
    region_masks = [(labels == index) for index, _ in regions]
    area_m2 = sum(area for _, area in regions)
    bleed_m2 = bleed_px * per_px_m2
    if bleed_m2 > MAX_BLEED_FRACTION * area_m2:
        return {"ok": False, "candidates": candidates,
                "reason": (f"closing CAD layer '{layer}' ran {bleed_m2:.0f} m² over other "
                           f"surfacing layers ({100 * bleed_m2 / area_m2:.0f}% of the "
                           "result) — the bridge is crossing real boundaries")}

    flags = [
        f"SURFACE FROM CAD LAYER: measured '{label}' as the {marks:,} marks the engineer "
        f"drew on layer '{layer}' — not by colour. On this sheet colour cannot do it: the "
        f"surface shares its ink with other surfaces, and the layer name is the only thing "
        f"that separates them.",
        f"OUTLINE BRIDGED {bridge_m:.2f} m: the layer holds marks, not a boundary, so the "
        f"outline is those marks closed by {bridge_m:.2f} m — the smallest bridge that makes "
        f"the field solid, grown from the sheet until the area stopped changing. Ground "
        f"within {bridge_m:.2f} m of the surface is treated as part of it.",
        f"AREA IS A FLOOR: a stipple stops short of its own boundary, so {area_m2:,.0f} m² is "
        f"the inside of the marks, not the edge of the surface. Where this has been checked "
        f"against a client markup it read 5% low, never high. Assessor: the true figure is "
        f"this or larger.",
    ]
    if bleed_m2:
        flags.append(
            f"assessor: the closed outline overlapped {bleed_m2:.0f} m² of other surfacing "
            f"layers ({100 * bleed_m2 / area_m2:.1f}%); that overlap was removed from the "
            "measurement rather than claimed.")
    return {"ok": True, "layer": layer, "candidates": candidates, "mask": closed,
            "region_masks": region_masks, "area_m2": area_m2, "bridge_m": bridge_m,
            "bleed_m2": bleed_m2, "marks": marks, "flags": flags}


def _is_surfacing(name, like):
    """Is ``name`` a sibling surface layer of ``like``?

    Siblings share the parent OCG and the ``Surfacing``/``hatch`` family marker, so a kerb,
    a text layer or a drainage run is not mistaken for a competing surface.
    """
    lowered = name.lower()
    if not any(token in lowered for token in ("surfac", "hatch", "pavement", "parking",
                                              "carriageway", "footway", "concrete")):
        return False
    return name.split("|")[0] == like.split("|")[0]
