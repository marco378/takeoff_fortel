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

# Inderjit confirmed on 31 Jul that Fortel wants these assumptions offered.  This remains the
# single kill-switch: set CHANNEL_PROPOSALS_ENABLED=0 in the environment (or change this default
# to "0") to suppress every proposal without touching measurement, zones, costing or portal
# approval behaviour.
CHANNEL_PROPOSALS_ENABLED = os.getenv("CHANNEL_PROPOSALS_ENABLED", "1").strip().lower() \
    not in {"0", "false", "no", "off"}
CHANNEL_PROPOSAL_BASIS = (
    "ASSUMED per Inderjit's confirmed rule when channels are not drawn: two straight, "
    "non-diagonal runs adjacent to retaining walls - one between the dock retaining walls/"
    "loading face and one following the longest resolved retaining-wall/yard edge. Assessor "
    "must accept, edit or remove each proposal."
)


# ---------------------------------------------------------------- legend -> hatch colour
def _label_bbox(pdf, page=0):
    """Find the legend text line naming the priced concrete area; return (bbox_pt, text) or None."""
    with fitz.open(pdf) as doc:
        pg = doc[page]
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


def _is_plausible_surface_tint(rgb):
    """A legend surface can be any hue; reject only ink-black and paper-white.

    Saturated bright colours (for example a channel at 255) and very light architectural
    tints must survive.  This deliberately classifies no specific architect or RGB value.
    """
    return bool(rgb) and max(rgb) > 30 and min(rgb) < 245


SWATCH_BODY_AGREE_TOL = 5


def _swatch_body_agrees(swatch_rgb, body_rgb, tol=SWATCH_BODY_AGREE_TOL):
    """Require the legend proposal and selected surface to agree channel-by-channel."""
    if not swatch_rgb or not body_rgb:
        return False
    return max(abs(int(body_rgb[i]) - int(swatch_rgb[i])) for i in range(3)) <= tol


def _dominant_rgb(im_rgb, mask):
    """Return the exact modal RGB tuple inside a component without a huge 3-D histogram."""
    pixels = im_rgb[mask]
    if not pixels.size:
        return None
    packed = ((pixels[:, 0].astype(np.uint32) << 16)
              | (pixels[:, 1].astype(np.uint32) << 8)
              | pixels[:, 2].astype(np.uint32))
    values, counts = np.unique(packed, return_counts=True)
    mode = int(values[int(np.argmax(counts))])
    return ((mode >> 16) & 255, (mode >> 8) & 255, mode & 255)


def _rect_iou(a, b):
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    intersection = ix * iy
    union = a.get_area() + b.get_area() - intersection
    return intersection / union if union > 0 else 0.0


def _choose_vector_swatch(candidates):
    """Choose the nearest vector swatch, preferring its non-black co-located base fill."""
    if not candidates:
        return None
    nearest = min(candidates, key=lambda candidate: candidate[0])
    colocated = [candidate for candidate in candidates
                 if _rect_iou(candidate[2], nearest[2]) >= 0.90]
    surface_fills = [candidate for candidate in colocated
                     if _is_plausible_surface_tint(candidate[1])]
    chosen = min(surface_fills or colocated, key=lambda candidate: candidate[0])
    return chosen[1]


def _choose_raster_swatch(patch):
    """Choose the nearest solid surface chip left of a rendered legend label.

    The sample window is deliberately wide for legends with indented chips.  Choosing the
    global modal colour across that window can therefore steal a larger chip from the next
    legend row.  Connected components preserve spatial association: the nearest filled block
    wins, while small text/border antialias fragments are ignored.
    """
    if patch is None or patch.size == 0:
        return None
    keep_mask = (patch.min(2) < 245) & (patch.max(2) > 30)
    labels, count = ndi.label(keep_mask)
    candidates = []
    for component_id in range(1, count + 1):
        mask = labels == component_id
        yy, xx = np.where(mask)
        if len(xx) < 20:
            continue
        width = int(xx.max() - xx.min() + 1)
        height = int(yy.max() - yy.min() + 1)
        density = len(xx) / (width * height)
        if width < 3 or height < 3 or density < 0.15:
            continue
        rgb = _dominant_rgb(patch, mask)
        if not _is_plausible_surface_tint(rgb):
            continue
        distance_from_label = int(patch.shape[1] - 1 - xx.max())
        candidates.append((distance_from_label, -len(xx), rgb))
    return min(candidates)[2] if candidates else None


def find_concrete_swatch_rgb(pdf, im=None, S=2.0, page=0):
    """Deterministic legend anchor. Locate the 'Concrete Service Yard' label, then read its swatch
    colour — first from the rendered raster just LEFT of the label (robust), else from a vector fill
    rect. Returns (rgb_0_255, label) or (None, reason)."""
    found = _label_bbox(pdf, page)
    if not found:
        return None, None
    raw_bbox, text = found
    raw_lx0, raw_ly0, raw_lx1, raw_ly1 = raw_bbox
    raw_cy = (raw_ly0 + raw_ly1) / 2

    # get_text() / get_drawings() use unrotated coordinates, while get_pixmap() returns the
    # visually rotated page.  Transform the complete bbox before raster pixel arithmetic.
    with fitz.open(pdf) as doc:
        pg = doc[page]
        rendered_bbox = fitz.Rect(raw_bbox) * pg.rotation_matrix
        lx0, ly0, lx1, ly1 = rendered_bbox
        cy = (ly0 + ly1) / 2

    # (a) raster sample: dominant non-white/non-black colour in a box just left of the label
    if im is not None:
        H, W = im.shape[:2]
        x1 = int((lx0 - 3) * S); x0 = int((lx0 - 175) * S)   # swatch can sit well left of the label
        y0 = int((cy - 7) * S);  y1 = int((cy + 7) * S)
        x0, x1 = max(0, x0), max(0, min(W, x1)); y0, y1 = max(0, y0), min(H, y1)
        if x1 - x0 > 4 and y1 - y0 > 2:
            patch = im[y0:y1, x0:x1]
            # Drop only paper-like near-white and ink-like near-black.  The old max<240
            # predicate discarded any tint with one bright channel (including saturated
            # colours) and every 239/240 architectural tint before it could be considered.
            rgb = _choose_raster_swatch(patch)
            if rgb:
                return rgb, text

    # (b) vector fill rect beside the label
    candidates = []
    with fitz.open(pdf) as doc:
        pg = doc[page]
        for dr in pg.get_drawings():
            fill = dr.get("fill")
            if not fill:
                continue
            r = dr["rect"]
            if not (2 < r.width < 70 and 2 < r.height < 32):
                continue
            if r.x1 > raw_lx0 + 3 or r.y1 < raw_cy - 16 or r.y0 > raw_cy + 16:
                continue
            candidates.append((
                raw_lx0 - r.x1,
                tuple(int(round(c * 255)) for c in fill),
                fitz.Rect(r),
            ))
    vector_rgb = _choose_vector_swatch(candidates)
    if vector_rgb:
        return vector_rgb, text
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

# Plausible single service-yard area range (m²) — shared by segment_hatch's best-component
# selection AND the swatch-lock fallback gate below (same magic numbers, one place).
PLAUSIBLE_MIN_M2 = 200
PLAUSIBLE_MAX_M2 = 50_000


def _axis_segments(page):
    """Yield axis-aligned vector segments in PDF-point space.

    ``grey`` identifies the light-grey wall / dock-door construction lines used by the
    raw Castle Donington external drawings.  It is deliberately only an evidence
    discriminator: quantities still come from the segment geometry and verified scale.
    """
    for drawing in page.get_drawings():
        colours = [drawing.get("color"), drawing.get("fill")]
        grey_tones = [
            sum(colour) / len(colour) * 255
            for colour in colours
            if (
                colour
                and max(colour) - min(colour) <= 0.04
                and 0.55 <= sum(colour) / len(colour) <= 0.99
            )
        ]
        grey = bool(grey_tones)
        for item in drawing["items"]:
            segments = []
            if item[0] == "l":
                segments = [(item[1], item[2])]
            elif item[0] == "re":
                rect = item[1]
                segments = [
                    (fitz.Point(rect.x0, rect.y0), fitz.Point(rect.x1, rect.y0)),
                    (fitz.Point(rect.x1, rect.y0), fitz.Point(rect.x1, rect.y1)),
                    (fitz.Point(rect.x1, rect.y1), fitz.Point(rect.x0, rect.y1)),
                    (fitz.Point(rect.x0, rect.y1), fitz.Point(rect.x0, rect.y0)),
                ]
            for start, end in segments:
                dx, dy = abs(end.x - start.x), abs(end.y - start.y)
                if min(dx, dy) > 0.05 or max(dx, dy) <= 0.05:
                    continue
                if dx > dy:
                    yield {
                        "orientation": "H",
                        "a0": min(start.x, end.x),
                        "a1": max(start.x, end.x),
                        "p0": min(start.y, end.y),
                        "p1": max(start.y, end.y),
                        "grey": grey,
                        "grey_tones": grey_tones,
                    }
                else:
                    yield {
                        "orientation": "V",
                        "a0": min(start.y, end.y),
                        "a1": max(start.y, end.y),
                        "p0": min(start.x, end.x),
                        "p1": max(start.x, end.x),
                        "grey": grey,
                        "grey_tones": grey_tones,
                    }


def detect_raw_dock_zone(pdf, k, S=2.0, target_rgb=(214, 214, 214)):
    """Measure a raw external dock only when its native CAD geometry is unambiguous.

    Evidence from all four client-marked Castle Donington externals:
    - the pink/yellow presentation is a Bluebeam annotation overlay and disappears from a
      true raw upload;
    - the raw CAD retains a repeated, light-grey dock-door/reveal family running along the
      loading face;
    - that family is adjacent to the segmented yard on exactly one side and a continuous
      wall segment bounds the same run.

    The detector requires all three signals.  It never uses a fixed dock area or a drawing
    filename.  If any signal is absent/ambiguous it returns no quantity so the caller can
    retain the yard measurement and ask the assessor to classify/trace the dock.
    """
    doc = fitz.open(pdf)
    try:
        page = doc[0]
        segments = list(_axis_segments(page))
        rotation_matrix = page.rotation_matrix
    finally:
        doc.close()

    families = {}
    for segment in segments:
        if not segment["grey"]:
            continue
        depth_m = (segment["a1"] - segment["a0"]) * k
        # The proven dock-door/reveal family is approximately 3.11 m deep in every
        # client fixture. This is an identity/plausibility band, not an assumed area:
        # the emitted quantity is reconstructed from each sheet's actual vectors.
        if not (2.5 <= depth_m <= 3.7):
            continue
        key = (
            segment["orientation"],
            round(segment["a0"], 1),
            round(segment["a1"], 1),
        )
        family = families.setdefault(key, {
            "orientation": segment["orientation"],
            "a0": segment["a0"],
            "a1": segment["a1"],
            "positions": set(),
        })
        family["positions"].add(round((segment["p0"] + segment["p1"]) / 2, 2))

    candidates = []
    evidence_seen = False
    for family in families.values():
        positions = sorted(family["positions"])
        if len(positions) < 8:
            continue
        face0, face1 = positions[0], positions[-1]
        face_span_m = (face1 - face0) * k
        if face_span_m < 20:
            continue
        evidence_seen = True

        # The door/reveal segments terminate at the continuous loading-face wall on one
        # end only. This remains available on Unit 1 where loading-bay recesses leave no
        # solid yard-mask pixels immediately outside the wall.
        wall_orientation = "V" if family["orientation"] == "H" else "H"
        end_walls = []
        for end_edge in (family["a0"], family["a1"]):
            options = []
            for segment in segments:
                if segment["orientation"] != wall_orientation:
                    continue
                wall_length = segment["a1"] - segment["a0"]
                ratio = wall_length / max(face1 - face0, 1e-9)
                wall_coordinate = (segment["p0"] + segment["p1"]) / 2
                distance_m = abs(wall_coordinate - end_edge) * k
                if 0.8 <= ratio <= 1.7 and distance_m <= 1.5:
                    options.append(distance_m)
            end_walls.append(min(options) if options else None)
        if end_walls[0] is None and end_walls[1] is None:
            continue
        if end_walls[1] is None or (
                end_walls[0] is not None and end_walls[0] + 0.15 < end_walls[1]):
            near_edge, far_edge, direction = family["a0"], family["a1"], -1
        elif end_walls[0] is None or end_walls[1] + 0.15 < end_walls[0]:
            near_edge, far_edge, direction = family["a1"], family["a0"], +1
        else:
            # A long wall is equally close to both ends: identity is ambiguous.
            continue

        # Locate the full yard-hatch boundary on the identified side. The longest
        # matching-grey vector there is the outer loading-face datum; using the first
        # raster pixel is unsafe because loading-bay recesses are intentionally white.
        target_grey = sum(target_rgb) / len(target_rgb)
        boundary_options = []
        for segment in segments:
            if segment["orientation"] != wall_orientation or not segment["grey_tones"]:
                continue
            boundary_coordinate = (segment["p0"] + segment["p1"]) / 2
            signed_distance_m = (boundary_coordinate - near_edge) * direction * k
            segment_length_m = (segment["a1"] - segment["a0"]) * k
            if not (0.05 <= signed_distance_m <= 1.0):
                continue
            if segment_length_m < 0.8 * face_span_m:
                continue
            if min(abs(tone - target_grey) for tone in segment["grey_tones"]) > 14:
                continue
            boundary_options.append((segment_length_m, boundary_coordinate))
        if not boundary_options:
            continue
        _, yard_boundary = max(boundary_options, key=lambda option: option[0])
        gap_pt = abs(yard_boundary - near_edge)

        # The local continuous loading-face wall supplies the dock run; repeated door
        # positions intentionally do not, because end bays have margins.
        wall_options = []
        for segment in segments:
            if segment["orientation"] != wall_orientation:
                continue
            wall_length = segment["a1"] - segment["a0"]
            ratio = wall_length / max(face1 - face0, 1e-9)
            wall_coordinate = (segment["p0"] + segment["p1"]) / 2
            distance_m = abs(wall_coordinate - yard_boundary) * k
            if not (0.8 <= ratio <= 1.5 and distance_m <= 1.5):
                continue
            score = distance_m + abs(ratio - 1.05)
            wall_options.append((score, segment, wall_coordinate))
        if not wall_options:
            continue
        _, wall, wall_coordinate = min(wall_options, key=lambda option: option[0])

        dock_depth_pt = abs(far_edge - near_edge) + 2 * gap_pt
        dock_depth_m = dock_depth_pt * k
        wall_length_m = (wall["a1"] - wall["a0"]) * k
        if not (3.4 <= dock_depth_m <= 4.6 and wall_length_m >= 20):
            continue

        dock_far = far_edge - direction * gap_pt
        if family["orientation"] == "H":
            polygon = [
                [yard_boundary, wall["a0"]],
                [dock_far, wall["a0"]],
                [dock_far, wall["a1"]],
                [yard_boundary, wall["a1"]],
            ]
        else:
            polygon = [
                [wall["a0"], yard_boundary],
                [wall["a1"], yard_boundary],
                [wall["a1"], dock_far],
                [wall["a0"], dock_far],
            ]
        area_m2 = wall_length_m * dock_depth_m
        if family["orientation"] == "H":
            loading_face_pts = [
                [wall_coordinate, wall["a0"]],
                [wall_coordinate, wall["a1"]],
            ]
        else:
            loading_face_pts = [
                [wall["a0"], wall_coordinate],
                [wall["a1"], wall_coordinate],
            ]
        # get_drawings() returns the raw/pre-/Rotate coordinates on these landscape sheets,
        # while the rendered Yard mask, snapshot canvas and _hatch_contour use rotated page
        # coordinates. Convert once here so Dock masks and proposal overlays share the same
        # canonical coordinate space.
        rotated_polygon = []
        for x, y in polygon:
            point = fitz.Point(x, y) * rotation_matrix
            rotated_polygon.append([round(point.x, 2), round(point.y, 2)])
        rotated_loading_face = []
        for x, y in loading_face_pts:
            point = fitz.Point(x, y) * rotation_matrix
            rotated_loading_face.append([round(point.x, 2), round(point.y, 2)])
        candidates.append({
            "area_m2": round(area_m2, 1),
            "polygon_pts": rotated_polygon,
            "door_segment_count": len(positions),
            "door_depth_m": round(abs(far_edge - near_edge) * k, 3),
            "dock_depth_m": round(dock_depth_m, 3),
            "loading_face_lm": round(wall_length_m, 2),
            "loading_face_pts": rotated_loading_face,
            "score": len(positions) * wall_length_m,
        })

    # Stroke and fill paths describe the same door family independently. Collapse only
    # near-identical rectangles; genuinely separate candidates remain an assessor decision.
    deduped = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        points = candidate["polygon_pts"]
        box = (
            min(point[0] for point in points), min(point[1] for point in points),
            max(point[0] for point in points), max(point[1] for point in points),
        )
        duplicate = False
        for kept in deduped:
            kept_points = kept["polygon_pts"]
            kept_box = (
                min(point[0] for point in kept_points), min(point[1] for point in kept_points),
                max(point[0] for point in kept_points), max(point[1] for point in kept_points),
            )
            ix = max(0.0, min(box[2], kept_box[2]) - max(box[0], kept_box[0]))
            iy = max(0.0, min(box[3], kept_box[3]) - max(box[1], kept_box[1]))
            intersection = ix * iy
            union = (
                (box[2] - box[0]) * (box[3] - box[1])
                + (kept_box[2] - kept_box[0]) * (kept_box[3] - kept_box[1])
                - intersection
            )
            if union > 0 and intersection / union >= 0.8:
                duplicate = True
                break
        if not duplicate:
            deduped.append(candidate)

    if len(deduped) != 1:
        return {
            "zone": None,
            "evidence_seen": evidence_seen,
            "reason": (
                "no unique repeated dock-door family adjacent to the yard"
                if not deduped
                else f"{len(deduped)} competing dock-door families"
            ),
        }

    chosen = deduped[0]
    zone = {
        "zone_key": "dock",
        "category": "dock",
        "subjects": ["Dock slab"],
        "measurement_kind": "area",
        "area_m2": chosen["area_m2"],
        "length_lm": None,
        "perimeter_lm": None,
        "annotation_count": 0,
        "cutout_count": 0,
        "classification_source": "raw CAD dock-door family adjacent to yard hatch",
        "needs_assessor": False,
        "polygon_pts": chosen["polygon_pts"],
        # Retain the actual CAD wall evidence instead of making downstream consumers
        # reconstruct a line from the Dock rectangle.
        "loading_face_lm": chosen["loading_face_lm"],
        "loading_face_pts": chosen["loading_face_pts"],
    }
    return {"zone": zone, "diagnostics": chosen, "evidence_seen": True}


def _has_explicit_channel_linework(pdf):
    """True only for semantically labelled channel markup already carrying a real line.

    Anonymous black CAD strokes are not classified here: the supplied raw sheets contain many
    indistinguishable walls and grid lines, so treating one as a channel would violate the
    refuse-instead-of-guess rule.  Marked ``Channel`` Line/PolyLine annotations take the normal
    marked pipeline and therefore always win over this assumption layer.
    """
    try:
        with fitz.open(pdf) as doc:
            for page in doc:
                for annot in page.annots() or []:
                    subject = " ".join(str((annot.info or {}).get("subject") or "")
                                       .strip().casefold().split())
                    if subject == "channel" and annot.type[1] in {"Line", "PolyLine"}:
                        return True
    except Exception:
        return False
    return False


def _rendered_axis_wall_segments(pdf):
    """Return native straight CAD lines in the portal's rendered PDF-point space.

    The segment identity is intentionally generic: ``_axis_segments`` accepts horizontal /
    vertical native CAD strokes, and this helper only applies the page rotation.  Whether a
    stroke is usable as a retaining-wall edge is decided geometrically against the measured
    Yard boundary below -- no architect, filename, colour table or target length is involved.
    """
    try:
        with fitz.open(pdf) as doc:
            page = doc[0]
            rotation_matrix = page.rotation_matrix
            rendered = []
            for segment in _axis_segments(page):
                if segment["orientation"] == "H":
                    start = fitz.Point(segment["a0"], segment["p0"])
                    end = fitz.Point(segment["a1"], segment["p0"])
                else:
                    start = fitz.Point(segment["p0"], segment["a0"])
                    end = fitz.Point(segment["p0"], segment["a1"])
                start, end = start * rotation_matrix, end * rotation_matrix
                # Snap the sub-point floating noise introduced by the rotation matrix.  A
                # proposed line must be literally horizontal or vertical in portal space.
                if abs(end.x - start.x) <= abs(end.y - start.y):
                    x = (start.x + end.x) / 2
                    points = [[x, start.y], [x, end.y]]
                else:
                    y = (start.y + end.y) / 2
                    points = [[start.x, y], [end.x, y]]
                rendered.append({
                    "polyline_pts": points,
                    "grey_wall_evidence": bool(segment.get("grey")),
                })
            return rendered
    except Exception:
        return []


def _longest_contained_yard_run(polygon_pts, k, wall_segments=None):
    """Longest unique, non-diagonal native CAD wall beside the measured Yard boundary.

    This replaces the old maximum boundary-vertex chord: that chord could cut diagonally
    across the Yard and was therefore not a defensible channel placement.  Candidates must
    now be actual horizontal/vertical CAD lines whose complete sampled length remains within
    1 m of the measured Yard edge.  Coincident strokes and the two faces of one wall are
    collapsed by geometric overlap; competing near-equal walls are refused, not guessed.

    ``wall_segments`` is explicit so the geometry can be regression-tested without a PDF.
    The returned length always comes from the native line itself, never a fitted constant.
    """
    if not k or k <= 0 or not isinstance(polygon_pts, list) or len(polygon_pts) < 3:
        return None, "Yard outline or scale is missing"
    if not wall_segments:
        return None, "No native straight CAD wall lines were available beside the Yard"
    try:
        from shapely.geometry import LineString, Polygon

        raw = Polygon(polygon_pts)
        if raw.is_empty or abs(raw.area) <= 0:
            return None, "Yard outline is empty or degenerate"
        repaired = raw if raw.is_valid else raw.buffer(0)
        if repaired.is_empty:
            return None, "Yard outline could not be repaired into a valid polygon"
        parts = list(repaired.geoms) if repaired.geom_type == "MultiPolygon" else [repaired]
        total_area = sum(part.area for part in parts)
        yard = max(parts, key=lambda part: part.area)
        repair_delta = abs(total_area - abs(raw.area)) / max(abs(raw.area), 1e-9)
        dominance = yard.area / max(total_area, 1e-9)
        if repair_delta > 0.01 or dominance < 0.995:
            return None, (
                "Yard outline repair is ambiguous "
                f"(area change {repair_delta * 100:.2f}%, largest component "
                f"{dominance * 100:.2f}%)"
            )

        candidates = []
        tested = 0
        adjacency_limit_m = 1.0
        for evidence in wall_segments:
            points = evidence.get("polyline_pts") if isinstance(evidence, dict) else evidence
            if not isinstance(points, (list, tuple)) or len(points) != 2:
                continue
            try:
                start = (float(points[0][0]), float(points[0][1]))
                end = (float(points[1][0]), float(points[1][1]))
            except (TypeError, ValueError, IndexError):
                continue
            dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
            tested += 1
            # Literal non-diagonal constraint.  The source reader snaps rotation-matrix
            # noise; callers supplying evidence directly get the same strict contract.
            if min(dx, dy) > 0.05 or max(dx, dy) <= 0.05:
                continue
            orientation = "H" if dx > dy else "V"
            if orientation == "H":
                fixed = (start[1] + end[1]) / 2
                a0, a1 = sorted((start[0], end[0]))
                line = LineString([(a0, fixed), (a1, fixed)])
            else:
                fixed = (start[0] + end[0]) / 2
                a0, a1 = sorted((start[1], end[1]))
                line = LineString([(fixed, a0), (fixed, a1)])
            length_m = line.length * k
            if length_m < 20:
                continue
            samples = [line.interpolate(index / 8, normalized=True) for index in range(9)]
            boundary_distances_m = [yard.boundary.distance(point) * k for point in samples]
            if max(boundary_distances_m) > adjacency_limit_m:
                continue
            candidates.append({
                "line": line,
                "orientation": orientation,
                "fixed": fixed,
                "a0": a0,
                "a1": a1,
                "length_m": length_m,
                "max_boundary_distance_m": max(boundary_distances_m),
                "mean_boundary_distance_m": sum(boundary_distances_m) / len(boundary_distances_m),
                "grey_wall_evidence": bool(
                    isinstance(evidence, dict) and evidence.get("grey_wall_evidence")),
            })
        if not candidates:
            return None, (
                "No native non-diagonal CAD wall remained within 1.00 m of the complete "
                "Yard-edge run"
            )

        # Collapse repeated stroke/fill paths and nearby parallel faces describing one wall.
        # A cluster is evidence-backed if it contains a light wall stroke OR repeated CAD
        # paths; a lone anonymous black line is not enough to call a retaining wall.
        clusters = []
        for candidate in sorted(candidates, key=lambda item: item["length_m"], reverse=True):
            matched = None
            for cluster in clusters:
                representative = cluster["representative"]
                if candidate["orientation"] != representative["orientation"]:
                    continue
                overlap = max(0.0, min(candidate["a1"], representative["a1"])
                              - max(candidate["a0"], representative["a0"]))
                shorter = min(candidate["a1"] - candidate["a0"],
                              representative["a1"] - representative["a0"])
                separation_m = abs(candidate["fixed"] - representative["fixed"]) * k
                if shorter > 0 and overlap / shorter >= 0.9 and separation_m <= 1.0:
                    matched = cluster
                    break
            if matched is None:
                clusters.append({"representative": candidate, "members": [candidate]})
            else:
                matched["members"].append(candidate)
                if candidate["length_m"] > matched["representative"]["length_m"]:
                    matched["representative"] = candidate

        supported = [
            cluster for cluster in clusters
            if any(member["grey_wall_evidence"] for member in cluster["members"])
            or len(cluster["members"]) >= 2
        ]
        if not supported:
            return None, (
                "Yard-edge lines were found, but none had a light wall stroke or repeated "
                "native CAD paths to corroborate retaining-wall identity"
            )
        supported.sort(key=lambda cluster: cluster["representative"]["length_m"], reverse=True)
        if (len(supported) > 1
                and supported[1]["representative"]["length_m"]
                >= 0.95 * supported[0]["representative"]["length_m"]):
            return None, (
                "Competing non-diagonal retaining-wall/yard-edge runs are within 5% length "
                "of one another; assessor must choose the channel edge"
            )

        chosen_cluster = supported[0]
        chosen = chosen_cluster["representative"]
        start, end = list(chosen["line"].coords)
        return {
            "length_lm": round(chosen["length_m"], 2),
            "polyline_pts": [
                [round(start[0], 2), round(start[1], 2)],
                [round(end[0], 2), round(end[1], 2)],
            ],
            "orientation": chosen["orientation"],
            "tested_wall_segments": tested,
            "wall_evidence_count": len(chosen_cluster["members"]),
            "max_boundary_distance_m": round(chosen["max_boundary_distance_m"], 3),
            "mean_boundary_distance_m": round(chosen["mean_boundary_distance_m"], 3),
            "repair_delta_pct": round(repair_delta * 100, 3),
            "largest_component_pct": round(dominance * 100, 3),
        }, None
    except Exception as exc:
        return None, f"Yard straight-run geometry failed ({type(exc).__name__}: {exc})"


def propose_channels(yard_polygon_pts, dock_zone, k, *, scale_verified=False,
                     explicit_channel_linework=False, wall_segments=None):
    """Return separate, non-measured channel assumptions plus visible refusal reasons."""
    if not CHANNEL_PROPOSALS_ENABLED:
        return [], ["CHANNEL PROPOSALS disabled by CHANNEL_PROPOSALS_ENABLED"]
    if explicit_channel_linework:
        return [], [
            "CHANNEL PROPOSAL not created: explicit Channel linework exists; the real marked "
            "measurement takes precedence over an assumption"
        ]
    if not isinstance(dock_zone, dict):
        return [], [
            "CHANNEL PROPOSAL refused: no unique Dock loading face was resolved; neither of "
            "Inderjit's two assumed runs is emitted"
        ]

    loading_face_lm = dock_zone.get("loading_face_lm")
    loading_face_pts = dock_zone.get("loading_face_pts")
    if (not isinstance(loading_face_lm, (int, float)) or loading_face_lm <= 0
            or not isinstance(loading_face_pts, list) or len(loading_face_pts) != 2):
        return [], [
            "CHANNEL PROPOSAL refused: Dock exists but its loading-face line evidence is missing"
        ]
    try:
        loading_dx = abs(float(loading_face_pts[1][0]) - float(loading_face_pts[0][0]))
        loading_dy = abs(float(loading_face_pts[1][1]) - float(loading_face_pts[0][1]))
    except (TypeError, ValueError, IndexError):
        return [], [
            "CHANNEL PROPOSAL refused: Dock loading-face geometry is malformed"
        ]
    if min(loading_dx, loading_dy) > 0.05 or max(loading_dx, loading_dy) <= 0.05:
        return [], [
            "CHANNEL PROPOSAL refused: Dock loading face is not a straight non-diagonal line"
        ]
    loading_orientation = "horizontal" if loading_dx > loading_dy else "vertical"

    scale_reason = ("drawing scale independently verified" if scale_verified
                    else "drawing scale unverified; assessor must confirm proposed Lm")
    proposals = [{
        "proposal_id": "channel-dock-loading-face",
        "component": "dock_retaining_wall",
        "proposed_length_lm": round(float(loading_face_lm), 2),
        "polyline_pts": loading_face_pts,
        "orientation": loading_orientation,
        "coordinate_space": "pdf_points",
        "basis": CHANNEL_PROPOSAL_BASIS,
        "confidence": "medium" if scale_verified else "low",
        "confidence_reasons": [
            "unique loading face reconstructed from repeated dock-door/reveal vectors, "
            "adjacent Yard hatch and continuous retaining-wall CAD line",
            scale_reason,
            "placement is geometric; channel existence is an explicit client assumption",
        ],
        "assumed": True,
        "requires_assessor_confirmation": True,
    }]

    yard_run, refusal = _longest_contained_yard_run(
        yard_polygon_pts, k, wall_segments=wall_segments)
    if yard_run:
        proposals.append({
            "proposal_id": "channel-yard-longest-contained-run",
            "component": "yard_longest_contained_run",
            "proposed_length_lm": yard_run["length_lm"],
            "polyline_pts": yard_run["polyline_pts"],
            "orientation": "horizontal" if yard_run["orientation"] == "H" else "vertical",
            "coordinate_space": "pdf_points",
            "basis": CHANNEL_PROPOSAL_BASIS,
            "confidence": "medium" if scale_verified else "low",
            "confidence_reasons": [
                f"selected a native {yard_run['orientation']}-axis CAD retaining-wall/yard-edge "
                "line; diagonal chords are forbidden",
                f"tested {yard_run['tested_wall_segments']} native CAD wall segments; chosen "
                f"wall had {yard_run['wall_evidence_count']} corroborating stroke/path records",
                f"complete run stays within {yard_run['max_boundary_distance_m']:.3f} m of the "
                f"calculated Yard boundary (mean {yard_run['mean_boundary_distance_m']:.3f} m)",
                f"outline repair area change {yard_run['repair_delta_pct']:.3f}% and largest "
                f"component {yard_run['largest_component_pct']:.3f}%",
                scale_reason,
                "placement is geometric; channel existence is an explicit client assumption",
            ],
            "assumed": True,
            "requires_assessor_confirmation": True,
        })
    flags = [
        "CHANNEL PROPOSAL (ASSUMED, NOT MEASURED OR PRICED): assessor must accept, edit or "
        "remove every proposed run before approval"
    ]
    if refusal:
        flags.append(f"CHANNEL PROPOSAL Yard run refused: {refusal}; Dock run retained")
    return proposals, flags


def segment_hatch(im_rgb, rgb, tol=14, close=6, k=None, S=2.0, max_void_m2=1.0,
                  title_block_frac=0.0, exclude_border=True, full_rgb=False, _diag=None):
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
    if not full_rgb and max(rgb) - min(rgb) <= 6:      # generic grey fallback
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
        raw_mask = np.zeros_like(mask)
        raw_mask[:cutoff, :] = active
        closed_active = ndi.binary_closing(active, structure=np.ones((close, close)))
        mask = np.zeros_like(mask)
        mask[:cutoff, :] = closed_active
    else:
        raw_mask = mask.copy()
        mask = ndi.binary_closing(mask, structure=np.ones((close, close)))

    if mask.sum() == 0:
        return None
    lab, n = ndi.label(mask)
    sizes = ndi.sum(np.ones_like(lab), lab, range(1, n + 1))

    # ── Pick the best plausible component (not just the largest) ─────────────
    # pixels → m²: area = px * (1/S)² * k²  → px_per_m2 = S²/k²
    order = list(np.argsort(sizes)[::-1])   # indices sorted largest-first
    best_idx = order[0]                      # fallback: absolute largest
    no_plausible_component = False
    if k is not None:
        px_per_m2 = (S * S) / (k * k)
        _MIN_M2, _MAX_M2 = PLAUSIBLE_MIN_M2, PLAUSIBLE_MAX_M2   # plausible single service-yard range
        no_plausible_component = True
        for idx in order:
            cand_m2 = sizes[idx] / px_per_m2
            if _MIN_M2 <= cand_m2 <= _MAX_M2:
                best_idx = idx
                no_plausible_component = False
                break
    if _diag is not None:
        # Surfaced so takeoff() can REFUSE rather than emit the absolute-largest guess.
        # Found on real client tender packs (31 Jul): an External Kerbing & Surfacing layout
        # whose legend swatch read black, so segmentation chased white background scraps,
        # found nothing in the 200-50,000 m² band, fell back to the largest blob and emitted
        # 8 m² for a site-wide external works drawing. A number that small is not a yard.
        _diag["no_plausible_component"] = no_plausible_component
        if k is not None:
            px_per_m2 = (S * S) / (k * k)

            def _records(component_labels, component_sizes, component_order, chosen=None):
                slices = ndi.find_objects(component_labels)
                records = []
                for idx in component_order[:20]:
                    area_m2 = float(component_sizes[idx]) / px_per_m2
                    sl = slices[idx] if idx < len(slices) else None
                    if sl is None:
                        continue
                    ys, xs = sl
                    records.append({
                        "component_id": int(idx + 1),
                        "area_m2": round(area_m2, 1),
                        "bbox_pdf_pts": [
                            round(xs.start / S, 1), round(ys.start / S, 1),
                            round(xs.stop / S, 1), round(ys.stop / S, 1),
                        ],
                        "plausible": bool(
                            PLAUSIBLE_MIN_M2 <= area_m2 <= PLAUSIBLE_MAX_M2),
                        "chosen": bool(idx == chosen),
                    })
                return records

            _diag["component_candidates"] = _records(lab, sizes, order, best_idx)
            _diag["selected_component_m2"] = round(float(sizes[best_idx]) / px_per_m2, 1)
            _diag["plausible_component_union_m2"] = round(sum(
                float(sizes[idx]) / px_per_m2 for idx in order
                if PLAUSIBLE_MIN_M2 <= float(sizes[idx]) / px_per_m2 <= PLAUSIBLE_MAX_M2
            ), 1)

            # Preserve the unclosed evidence too. Closing is intentionally retained for the
            # measured component (thin paint/line gaps), but it must never hide a material
            # largest-component-vs-all-tint ambiguity from the assessor.
            raw_lab, raw_n = ndi.label(raw_mask)
            if raw_n:
                raw_sizes = ndi.sum(np.ones_like(raw_lab), raw_lab, range(1, raw_n + 1))
                raw_order = list(np.argsort(raw_sizes)[::-1])
                _diag["raw_component_candidates"] = _records(
                    raw_lab, raw_sizes, raw_order, raw_order[0])
                _diag["raw_largest_component_m2"] = round(
                    float(raw_sizes[raw_order[0]]) / px_per_m2, 1)
                _diag["raw_tint_union_m2"] = round(int(raw_mask.sum()) / px_per_m2, 1)
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

# Plausible drawing-scale ratio band (1:N). Derived from the realistic range of architectural /
# civil engineering drawing scales actually used on Fortel tender packs (title-block 1:N values
# seen across the corpus run from 1:20 detail blow-ups to ~1:2500 site-location plans; 1:5000 gives
# headroom above that without being so wide it admits a false detection). A detected scale bar
# implying a ratio outside this band is not "a different but real scale" — it is a mis-paired
# label/line (e.g. an unrelated "7016 m" dimension callout fused to a nearby 34pt line fragment)
# and must be rejected as a false anchor rather than trusted over the title block.
PLAUSIBLE_SCALE_RATIO_MIN = 20
PLAUSIBLE_SCALE_RATIO_MAX = 5000


def _implausible_scale_ratio(k_m_per_pt):
    """True if k (m/pt) implies a 1:N drawing ratio outside the plausible band, i.e. the value
    that PRODUCED k (a detected scale-bar length/label pairing) is almost certainly a false
    anchor rather than a genuine — if unusual — drawing scale."""
    if not k_m_per_pt or k_m_per_pt <= 0:
        return True
    implied_n = k_m_per_pt / SC.PT_PER_M
    return not (PLAUSIBLE_SCALE_RATIO_MIN <= implied_n <= PLAUSIBLE_SCALE_RATIO_MAX)


def _title_scale_denominators(text):
    """Return 1:N candidates by viewport prevalence, preserving reading order for ties.

    Tender sheets routinely carry small 1:75 details and a 1:500 yard plan.  The denominator
    repeated across the main viewport labels is the safest text candidate; an equal-frequency
    tie keeps PDF reading order (the long-established behaviour) and remains subject to the
    normal scale-consensus disagreement gate.
    """
    all_candidates = [int(value) for value in re.findall(
        r"\b1\s*:\s*(\d{2,4})\b", text)]
    plausible = [value for value in all_candidates
                 if PLAUSIBLE_SCALE_RATIO_MIN <= value <= PLAUSIBLE_SCALE_RATIO_MAX]
    first_seen = {value: plausible.index(value) for value in set(plausible)}
    return sorted(set(plausible), key=lambda value: (-plausible.count(value), first_seen[value]))


def scale_for(pdf, page=0):
    """(k_m_per_pt, verified_bool, note, sources).

    verified_bool is True ONLY when a physical scale bar is detected AND it agrees with the
    title-block stated scale within SCALE_BAR_AGREE_TOL (±3 %).  In all other cases it is False
    and `note` explains why (no bar found / bar disagrees by X% / bar rejected as implausible).

    On bar/title disagreement beyond tolerance this NEVER auto-picks a side (CLAUDE.md invariant
    3 — "disagreement -> refuse, don't auto-pick"):
      (a) if the bar-implied ratio is implausible for a drawing (see _implausible_scale_ratio),
          the bar is almost certainly a false detection (an unrelated line/label pairing) — the
          title-block k is used instead, flagged as a rejected-bar case, still UNVERIFIED.
      (b) if both sources are individually plausible but disagree, neither is picked — the
          title-block k is used for DISPLAY only, flagged MIXED/DISAGREE, still UNVERIFIED, and
          the assessor must set the scale explicitly.
    Both branches return verified=False; there is no path out of a disagreement that returns True.

    sources: dict with keys 'title_block' and/or 'scale_bar' recording the contributing values.
    """
    with fitz.open(pdf) as doc:
        page_text = doc[page].get_text()
    denoms = _title_scale_denominators(page_text)
    kbar, info = SC.detect_scale_bar(pdf, page)
    uu = SC.user_unit(pdf, page)

    # A sheet can contain detail scales as well as its yard-plan viewport scale. Candidate order
    # is determined from page-text prevalence above; the independent bar still has to pass the
    # normal consensus gate and never selects a convenient printed scale merely because it agrees.
    denom = denoms[0] if denoms else None
    k_title = SC.title_block_k(denom)

    sources = {}
    if k_title:
        sources["title_block"] = {"denom": denom, "k": round(k_title * uu, 6)}
        if len(denoms) > 1:
            sources["title_block_candidates"] = denoms

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
            elif _implausible_scale_ratio(kbar):
                # (a) bar disagrees AND is individually implausible -> false anchor. Reject the
                # bar, fall back to title-block k. Still UNVERIFIED (single uncorroborated source).
                implied_n = kbar / SC.PT_PER_M
                note = (f"scale bar candidate rejected as implausible (bar {info} implies "
                        f"~1:{implied_n:.0f}, outside plausible 1:{PLAUSIBLE_SCALE_RATIO_MIN}-"
                        f"1:{PLAUSIBLE_SCALE_RATIO_MAX}) — title-block 1:{denom} scale used, "
                        "UNVERIFIED")
                return k_title_full, False, note, sources
            else:
                # (b) both individually plausible but disagree -> MIXED/DISAGREE. Do not pick
                # either silently: keep title k for display, flag for assessor, stay UNVERIFIED.
                note = (f"MIXED/DISAGREE — scale bar ({info}, k={kbar:.5f}) and title 1:{denom} "
                        f"(k={k_title_full:.5f}) disagree by {pct_diff*100:.1f}% "
                        f"(> {SCALE_BAR_AGREE_TOL*100:.0f}%) and both are individually plausible — "
                        "assessor must set scale; title-block value shown, NOT auto-picked")
                return k_title_full, False, note, sources
        else:
            # Bar found but no title-block scale to compare against
            if _implausible_scale_ratio(kbar):
                implied_n = kbar / SC.PT_PER_M
                note = (f"scale bar candidate rejected as implausible (bar {info} implies "
                        f"~1:{implied_n:.0f}, outside plausible 1:{PLAUSIBLE_SCALE_RATIO_MIN}-"
                        f"1:{PLAUSIBLE_SCALE_RATIO_MAX}, no title-block to fall back on) — no scale")
                sources.pop("scale_bar", None)
                return None, False, note, sources
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
    sheet_identity = f"{os.path.basename(pdf)}\n{pg.get_text()}".lower()
    if re.search(r"\bsite\s+location\s+plan\b", sheet_identity):
        return {"pdf": os.path.basename(pdf), "area_m2": None, "style": style,
                "price_gbp": None, "measurement_state": sanity.UNMEASURED,
                "needs_assessor": True,
                "flags": flags + [
                    "REFUSED — sheet identifies itself as a Site Location Plan, not a slab/yard "
                    "measurement viewport. A matching colour legend cannot override drawing "
                    "identity; route the actual external-works/surfacing sheet to takeoff."]}
    if style == "line/hatch":
        return {"pdf": os.path.basename(pdf), "area_m2": None, "style": style, "price_gbp": None,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + [
                    "NON-COLOUR-CODED (line/hatch) drawing — solid-fill colour segmentation does NOT apply "
                    "(it scrapes stray grey -> wrong area). Route to hatch-mode / Claude vision / assessor "
                    "trace. No area emitted (this is the fix for the 'entirely wrong area' the team hit)."]}

    # --- region colour ---
    # The priced "Concrete Service Yard construction" hatch on SGP architect sheets is a light grey
    # (validated across all 4 Hemington units). Historically we always SEGMENTED on a hard-coded
    # generic grey band centred at 214 regardless of what the legend swatch actually read, using the
    # swatch only to CONFIRM a concrete-yard entry exists. Aryan's real SGP sheet (105301-SGP-01
    # D77) showed why that is unsafe: its legend swatch read (224,224,224), and the sheet ALSO has a
    # second, separate grey legend entry "Footpaths (ancillary): Concrete" whose darker fill lands
    # inside the generic [200,228] band and is close enough to the yard's own bottom edge that
    # binary_closing fuses it into the same connected component — a straight +16 m² over-measure
    # (3,172 vs Smita's Bluebeam 3,156) that is invisible to the border/satellite exclusion above
    # because it is CONNECTED, not a separate blob.
    #
    # Fix: LOCK the segmentation band centre to any plausible legend-confirmed surface tint — not
    # only grey.  The darker ancillary-concrete grey then falls outside the locked ±tol band and is
    # never admitted into the mask, regardless of closing. A plausibility
    # gate (same PLAUSIBLE_MIN_M2/MAX_M2 range segment_hatch already uses for best-component choice)
    # falls back to the validated generic 214 band if the locked band yields nothing plausible —
    # this is what prevents a repeat of the Demo-4 regression (swatch reads in [195,199]∪[229,236]
    # while the real hatch is 214 → a naive lock would produce area=None on a perfectly measurable
    # sheet). Synthetic gold fixtures (_int_d77*.pdf) have unreadable swatches, so they always take
    # the fallback path unchanged — their golds (3,159 / 3,159) are untouched by this change.
    GREY_FALLBACK = (214, 214, 214)   # validated SGP convention; band [200,228] with tol=14
    GREY_TOL = 14
    rgb = GREY_FALLBACK
    swatch_locked = False
    region_confidence = None
    swatch, label = find_concrete_swatch_rgb(pdf, im=im, S=S)
    legend_found = bool(label)   # True in both label branches below; False only in the no-legend else
    if label and swatch:
        if _is_plausible_surface_tint(swatch):
            # LOCK the full RGB band to the legend-confirmed tint. Other surfaces on the sheet
            # that render at a different tint fall outside the locked band and are excluded.
            rgb = swatch
            swatch_locked = True
            flags.append(f"legend '{label}': plausible surface swatch {swatch} — full RGB band "
                         f"LOCKED to swatch centre ±{GREY_TOL}")
        else:
            region_confidence = "low"
            flags.append(f"legend '{label}' found but swatch {swatch} is near-black/near-white, "
                         f"not a plausible surface tint — using SGP grey convention {GREY_FALLBACK} "
                         "(lower confidence; assessor confirm)")
    elif label:
        flags.append(f"legend '{label}' found (swatch unreadable) — using SGP grey convention {GREY_FALLBACK}")
    else:
        region_confidence = "low"
        flags.append(f"no concrete-yard legend label — grey-hatch heuristic {GREY_FALLBACK} (LOW confidence; assessor confirm)")
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
    comp = segment_hatch(im, rgb, tol=GREY_TOL, k=k, S=S,
                         full_rgb=swatch_locked, _diag=_seg_diag)

    # --- swatch-lock plausibility gate: fall back to the validated generic grey band if the
    # locked swatch band produced nothing plausible (closes the Demo-4 regression class — a
    # swatch reading in [195,199]∪[229,236] while the real hatch is 214 would otherwise lock an
    # empty band and silently turn a measurable sheet into area=None). ---
    if swatch_locked:
        px_per_m2 = (S * S) / (k * k)
        cand_m2 = (int(comp.sum()) / px_per_m2) if comp is not None else 0.0
        if comp is None or comp.sum() == 0 or not (PLAUSIBLE_MIN_M2 <= cand_m2 <= PLAUSIBLE_MAX_M2):
            flags.append(f"swatch-locked band {swatch}±{GREY_TOL} produced no plausible yard region "
                         f"(candidate {cand_m2:.0f} m²) — FELL BACK to validated SGP grey band "
                         f"{GREY_FALLBACK}±{GREY_TOL}; assessor confirm region colour")
            region_confidence = "low"
            rgb = GREY_FALLBACK
            swatch_locked = False
            _seg_diag = {}
            comp = segment_hatch(im, rgb, tol=GREY_TOL, k=k, S=S, _diag=_seg_diag)

    # The legend proposes a colour; the measured body must independently confirm it. The
    # segmentation band is intentionally wider than this agreement gate to tolerate rendering,
    # but a modal body colour more than five RGB levels away is not silently accepted.
    if swatch_locked and comp is not None and comp.sum() > 0:
        body_rgb = _dominant_rgb(im, comp)
        body_diff = max(abs(int(body_rgb[i]) - int(swatch[i])) for i in range(3)) \
            if body_rgb else 256
        if not _swatch_body_agrees(swatch, body_rgb):
            flags.append(
                f"legend/body colour DISAGREE: swatch {swatch}, selected component dominant "
                f"RGB {body_rgb}, max channel difference {body_diff} > "
                f"{SWATCH_BODY_AGREE_TOL} — FELL BACK to validated SGP grey band "
                f"{GREY_FALLBACK}±{GREY_TOL}; assessor confirm region colour"
            )
            region_confidence = "low"
            rgb = GREY_FALLBACK
            swatch_locked = False
            _seg_diag = {}
            comp = segment_hatch(im, rgb, tol=GREY_TOL, k=k, S=S, _diag=_seg_diag)
        else:
            flags.append(
                f"legend/body colour cross-check PASSED: swatch {swatch}, selected component "
                f"dominant RGB {body_rgb} (max channel difference {body_diff} ≤ "
                f"{SWATCH_BODY_AGREE_TOL})"
            )

    if comp is None or comp.sum() == 0:
        return {"pdf": pdf, "area_m2": None,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + ["no hatch pixels matched — assessor must trace"]}

    # --- zone-aware raw external measurement ----------------------------------------------
    # Yard remains the proven legend-swatch segmentation. Dock is added only when native
    # loading-face CAD geometry passes detect_raw_dock_zone's three-signal gate.
    dock_detection = detect_raw_dock_zone(pdf, k, S=S, target_rgb=rgb)
    dock_zone = dock_detection.get("zone")
    yard_comp = comp
    overlap_m2 = 0.0
    if dock_zone:
        dock_mask = np.zeros_like(comp, dtype=np.uint8)
        dock_points = np.asarray(dock_zone["polygon_pts"], dtype=float)
        dock_points = np.rint(dock_points * S).astype(np.int32)
        cv2.fillPoly(dock_mask, [dock_points], 1)
        overlap_px = int((comp & dock_mask.astype(bool)).sum())
        if overlap_px:
            yard_comp = comp & ~dock_mask.astype(bool)
            overlap_m2 = overlap_px * (1.0 / S) ** 2 * k * k
    px = int(yard_comp.sum())

    body_rgb = _dominant_rgb(im, comp)
    flags.append(f"selected surface component dominant RGB {body_rgb}; segmentation centre {rgb}")

    component_evidence = {
        key: _seg_diag.get(key)
        for key in (
            "selected_component_m2", "plausible_component_union_m2",
            "raw_largest_component_m2", "raw_tint_union_m2",
            "component_candidates", "raw_component_candidates",
        )
        if _seg_diag.get(key) is not None
    }
    component_records = component_evidence.get("component_candidates") or []
    selected_record = next((record for record in component_records if record.get("chosen")), None)
    co_components = [record for record in component_records
                     if not record.get("chosen") and record.get("plausible")]
    if selected_record:
        flags.append(
            "surface-tint component chosen (pre-void-fill): "
            f"{selected_record['area_m2']:,.1f} m² at bbox {selected_record['bbox_pdf_pts']} PDF pt"
        )
    if co_components:
        component_text = "; ".join(
            f"{record['area_m2']:,.1f} m² bbox {record['bbox_pdf_pts']}"
            for record in co_components[:10]
        )
        flags.append(
            "assessor: co-components matching the legend tint were NOT summed into the measured "
            f"total — {component_text}"
        )
        region_confidence = "low"

    raw_largest = component_evidence.get("raw_largest_component_m2") or 0
    raw_union = component_evidence.get("raw_tint_union_m2") or 0
    raw_records = component_evidence.get("raw_component_candidates") or []
    raw_co_components = [record for record in raw_records if not record.get("chosen")]
    if raw_co_components:
        raw_component_text = "; ".join(
            f"{record['area_m2']:,.1f} m² bbox {record['bbox_pdf_pts']}"
            for record in raw_co_components[:10]
        )
        flags.append(
            "assessor: pre-closing matching-tint co-components (not summed separately) — "
            f"{raw_component_text}"
        )
    if raw_largest > 0 and raw_union > raw_largest * 1.05:
        swing_pct = (raw_union - raw_largest) / raw_largest * 100
        flags.append(
            "assessor: matching tint is materially fragmented before line-gap closing — raw "
            f"largest component {raw_largest:,.1f} m² vs all matching-tint pixels "
            f"{raw_union:,.1f} m² ({swing_pct:.1f}% difference). The deterministic chosen "
            "component is reported above; assessor decides whether separated co-components "
            "belong to the Yard."
        )
        region_confidence = "low"

    flags.append("dock-bay recesses & interior islands kept as DEDUCTIONS (not filled); thin paint bridged by closing")
    if _seg_diag.get('void_fill_m2', 0) > 0:
        flags.append(f"void-fill: +{_seg_diag['void_fill_m2']} m² from {_seg_diag['void_count']} "
                     f"paint/text hole(s) (each < 1.0 m²) — included in measured area")
    if _seg_diag.get('excluded_m2', 0) > 0:
        flags.append(f"excluded {_seg_diag['excluded_components']} border/legend component(s) "
                     f"({_seg_diag['excluded_m2']} m² equivalent: {_seg_diag.get('excluded_margin_m2', 0)} m² "
                     f"sheet-frame/border strip + {_seg_diag.get('excluded_satellite_m2', 0)} m² legend/satellite "
                     f"chip(s)) — not part of the measured yard region")

    yard_area = round(px * (1.0 / S) ** 2 * k * k, 0)

    # --- refuse the absolute-largest guess (invariant 5) --------------------------------
    # If NO connected component fell inside the plausible service-yard band, segment_hatch
    # fell back to "largest blob on the page", which is a guess, not a measurement. Real
    # client tender packs (31 Jul: External Kerbing & Surfacing Layout, Proposed External
    # Works Plan) hit exactly this and produced 8 m² and 7 m² for site-wide drawings — a
    # confidently wrong small number is the same failure class as a confidently wrong large
    # one. Emit NO area and send it to the assessor instead.
    if _seg_diag.get("no_plausible_component") and not (PLAUSIBLE_MIN_M2 <= yard_area <= PLAUSIBLE_MAX_M2):
        return {"pdf": os.path.basename(pdf), "area_m2": None,
                "scale_k": round(k, 5), "scale_verified": verified,
                "scale_src": note, "scale_sources": scale_sources,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + [
                    f"REFUSED — no region fell inside the plausible service-yard band "
                    f"({PLAUSIBLE_MIN_M2:,.0f}-{PLAUSIBLE_MAX_M2:,.0f} m²); the largest candidate was "
                    f"only {yard_area:,.0f} m², i.e. the colour segmentation did not find a yard on "
                    f"this sheet (wrong legend swatch, or this is not a slab drawing). Assessor must "
                    f"confirm the region and scale — no area issued."]}

    zones = [{
        "zone_key": "external_yard",
        "category": "external_yard",
        "subjects": [label or "Concrete Service Yard construction"],
        "measurement_kind": "area",
        "area_m2": yard_area,
        "length_lm": None,
        "perimeter_lm": None,
        "annotation_count": 0,
        "cutout_count": 0,
        "classification_source": "legend-swatch colour segmentation",
        "needs_assessor": region_confidence == "low",
    }]
    # Preserve the historic top-level area_m2 contract: on this raw path it is the
    # grey service-yard quantity (and is what the pre-zone golds validate).  The
    # explicit zone total below is what new zone-aware consumers compare with a
    # marked drawing's all-area total.  In particular, Dock must never inherit the
    # Yard rate merely because it was added to a top-level priced quantity.
    area = yard_area
    zones_total_area = yard_area
    if dock_zone:
        zones.append(dock_zone)
        dock_diag = dock_detection["diagnostics"]
        zones_total_area = round(yard_area + dock_zone["area_m2"], 1)
        flags.append(
            "raw Dock zone measured from native repeated dock-door/reveal geometry: "
            f"{dock_diag['area_m2']:.1f} m² "
            f"({dock_diag['loading_face_lm']:.2f} Lm loading face × "
            f"{dock_diag['dock_depth_m']:.3f} m structural dock depth; "
            f"{dock_diag['door_segment_count']} repeated vector segments)"
        )
        if overlap_m2 > 0:
            flags.append(
                f"Yard/Dock overlap removed from Yard: {overlap_m2:.1f} m² "
                "(zones are mutually exclusive; zone total contains no double counting)"
            )
        flags.append(
            f"raw zone total {zones_total_area:.1f} m² = Yard {yard_area:.1f} m² + "
            f"Dock {dock_zone['area_m2']:.1f} m²; Dock remains an unpriced assessor-rate line"
        )
        flags.append(
            "raw Channel MEASUREMENT not attempted — any channel_proposals are separate "
            "assumptions requiring assessor review; Transition length NOT ATTEMPTED because "
            "line identity is not reliable without marked subjects"
        )
    else:
        if dock_detection.get("evidence_seen"):
            region_confidence = "low"
            zones.append({
                "zone_key": "unclassified:raw-dock-candidate",
                "category": "unclassified",
                "subjects": ["Raw dock candidate"],
                "measurement_kind": "area",
                "area_m2": None,
                "length_lm": None,
                "perimeter_lm": None,
                "annotation_count": 0,
                "cutout_count": 0,
                "classification_source": "ambiguous native loading-face geometry",
                "needs_assessor": True,
            })
            flags.append(
                f"assessor: classify/trace Dock zone — {dock_detection['reason']}; "
                "Yard retained, no Dock area guessed"
            )

    # --- refuse instead of guess (invariant 5) ------------------------------------------------
    # No concrete-yard legend label AND no verified scale means BOTH the region identity and the
    # scale are guesses — the resulting number is meaningless. Elevation, gatehouse and
    # location-plan sheets land here and used to emit confident 5,000-6,000 m² garbage (gated
    # behind the assessor, but still misleading). Emit NO area; route to the assessor with the
    # candidate figure in the flag so they can judge quickly. Inderjit's real D77 gold is
    # UNAFFECTED: it carries a legend label (legend_found=True) even though its scale bar is
    # unverified, so this guard never fires on it.
    if not legend_found and not verified:
        return {"pdf": os.path.basename(pdf), "area_m2": None,
                "scale_k": round(k, 5), "scale_verified": verified,
                "scale_src": note, "scale_sources": scale_sources,
                "measurement_state": sanity.UNMEASURED, "needs_assessor": True,
                "flags": flags + [
                    f"REFUSED — no concrete-yard legend label AND scale unverified: the candidate "
                    f"{area:,.0f} m² is a shape on an unidentified sheet (elevation / section / "
                    f"location plan measure here), not a verified slab. Assessor must confirm the "
                    f"drawing type, region and scale before any area is issued."]}

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
        ov = im.copy()
        ov[yard_comp] = (0.4 * ov[yard_comp] + 0.6 * np.array([235, 30, 30])).astype(np.uint8)
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
    polygon_pts = _hatch_contour(yard_comp, S)

    # --- unpriced channel ASSUMPTIONS -------------------------------------------------------
    # Kept deliberately outside zones[] and every measured/costed total.  The Dock loading-face
    # placement and Yard wall-edge run are geometric; the existence of two channels is Inderjit's
    # supplied assumption and must be reviewed in the portal before approval.
    channel_proposals, channel_proposal_flags = propose_channels(
        polygon_pts, dock_zone, k,
        scale_verified=verified,
        explicit_channel_linework=_has_explicit_channel_linework(pdf),
        wall_segments=_rendered_axis_wall_segments(pdf),
    )
    flags += channel_proposal_flags

    # --- manhole count ESTIMATE (unmarked path — conservative, never authoritative) ---
    manhole_count_estimate, _mh_centres = detect_manholes(im, yard_comp, k, S=S)
    if manhole_count_estimate > 0:
        flags.append(f"manhole_count_estimate={manhole_count_estimate} (small near-circular "
                     f"features inside the measured yard, {MANHOLE_DIAM_M_MIN}-{MANHOLE_DIAM_M_MAX} m "
                     "diameter band) — this is an ESTIMATE, assessor confirm before pricing E/O manhole details")

    # --- manhole count ASSUMPTION (Inderjit, last Fortel call) ------------------------------
    # When there is no drainage layout and no manhole symbol was detected, Fortel's rule is to
    # ASSUME 1 manhole per 1,000 m² (placed corner-to-corner) so the assessor starts from a
    # figure rather than a bare zero. Kept in its OWN field (never manhole_count_estimate, which
    # auto-prices via price_with_defaults) so it stays a COUNT ASSUMPTION + flag and never feeds
    # the £75/Nr E/O line automatically — the assessor confirms the count first. Gated on a found
    # legend label so a mis-segmented non-yard sheet can't sprout phantom manholes. round() with a
    # floor of 1 matches the real Winvic sheet (26,080 m² → 26 Nr; ceil would over-count at 27).
    manhole_count_assumed = None
    if manhole_count_estimate == 0 and area and not blocked and legend_found:
        manhole_count_assumed = max(1, round(yard_area / 1000.0))
        flags.append(f"manhole_count_assumed={manhole_count_assumed} — ASSUMPTION per Inderjit's rule "
                     f"(1 per 1,000 m², placed corner-to-corner), applied because no drainage layout / "
                     f"no manhole symbols were detected: round({yard_area:,.0f} yard m² / 1,000), min 1. Assessor "
                     "confirms the count before any E/O manhole line is priced.")

    # --- measurement_state: the four-state contract (sanity.py) so downstream (pipeline,
    # portal, approve endpoint) never has to re-derive verified/plausible logic itself. ---
    # A sheet measured WITHOUT a legend label (generic grey-hatch guess) can never be
    # approvable even if its scale happens to verify — the region identity is still unconfirmed.
    # Feed confidence="low" in that case so the state machine caps it at MEASURED_UNVERIFIED
    # (approve-blocked) rather than MEASURED_VERIFIED. A labelled sheet (e.g. D77) is unaffected.
    state, state_flags = sanity.measurement_state(
        area, scale_verified=verified,
        confidence=("low" if region_confidence == "low" or not legend_found else None))
    flags += state_flags
    needs_assessor = state != sanity.MEASURED_VERIFIED

    return {"pdf": os.path.basename(pdf), "scale_k": round(k, 5), "scale_verified": verified,
            "scale_src": note, "scale_sources": scale_sources,
            "area_m2": area, "rate": rate, "price_gbp": price, "overlay": overlay,
            "polygon_pts": polygon_pts, "flags": flags,
            "zones": zones,
            "zones_total_area_m2": zones_total_area,
            "segmentation_components": component_evidence,
            "channel_proposals": channel_proposals,
            "manhole_count_estimate": manhole_count_estimate,
            "manhole_count_assumed": manhole_count_assumed,
            "legend_found": legend_found,
            "region_confidence": region_confidence,
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
