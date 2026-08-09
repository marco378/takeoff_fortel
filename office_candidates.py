"""Deterministic vector candidates for assessor-assisted Office GA tracing.

This module deliberately does *not* measure an office slab.  Architectural GA plans have
open door breaks, room partitions, cores and stair/lift voids, so a candidate remains a
tracing aid until an assessor selects/edits it.  The pipeline therefore carries geometry
while keeping ``area_m2=None`` and ``measurement_state=UNMEASURED``.

There is exactly one candidate record per detected level title.  A record can contain
several ``regions`` (Level 00 commonly has two separate cores), or be explicitly unresolved.
Candidate coordinates are rotated PDF points: the same coordinate space as the rendered
page returned by PyMuPDF and the portal snapshot canvas before applying ``snapScale``.
"""

from __future__ import annotations

import math
import re

import fitz
from shapely.affinity import translate
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union


DARK_STROKE_MAX = 0.10
MIN_SEGMENT_PT = 5.0
MIN_CANDIDATE_M2 = 20.0
MAX_CANDIDATE_M2 = 500.0
IOU_DEDUPE_THRESHOLD = 0.90
UNRESOLVED_REASON = "level detected but outline not resolved — trace manually"


def _level_titles(page) -> list[dict]:
    """Return one label per level with its raw-content-stream position and bbox."""
    titles = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            match = re.search(r"Office\s+Plan\s+Level\s*(\d+)", text, re.I)
            if match:
                level = int(match.group(1))
            elif re.search(r"\bFirst\s+Floor\s+Steelwork\s+Layout\b", text, re.I):
                # Engineer composite-deck sheets do not call the viewport an Office Plan,
                # but this title is direct evidence of an upper-floor review target.  The
                # title creates an assisted row only; fragmented deck hatch is not promoted
                # to a measured boundary.
                level = 1
            elif re.search(r"\bThird\s+Floor\b", text, re.I):
                # Unit 3 calls the last plan "Third Floor" instead of "Level 03".
                level = 3
            else:
                continue
            x0, y0, x1, y1 = line["bbox"]
            titles.append({
                "level": level,
                "text": text.strip(),
                "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                "x": (x0 + x1) / 2,
                "y": (y0 + y1) / 2,
            })
    # Repeated PDF text objects must not create duplicate level records.
    unique = {}
    for title in titles:
        unique.setdefault(title["level"], title)
    return list(unique.values())


def _dark_vector_lines(page) -> list[LineString]:
    """Extract dark vector strokes/rectangles; ignore grey hatch and colour fills."""
    lines = []
    for drawing in page.get_drawings():
        colour = drawing.get("color")
        if colour is None or max(colour) > DARK_STROKE_MAX:
            continue
        for item in drawing.get("items", []):
            if item[0] == "l":
                a, b = item[1], item[2]
                if math.dist(a, b) < MIN_SEGMENT_PT:
                    continue
                points = [(round(a.x, 1), round(a.y, 1)),
                          (round(b.x, 1), round(b.y, 1))]
            elif item[0] == "re":
                rect = item[1]
                if max(rect.width, rect.height) < MIN_SEGMENT_PT:
                    continue
                points = [
                    (round(rect.x0, 1), round(rect.y0, 1)),
                    (round(rect.x1, 1), round(rect.y0, 1)),
                    (round(rect.x1, 1), round(rect.y1, 1)),
                    (round(rect.x0, 1), round(rect.y1, 1)),
                    (round(rect.x0, 1), round(rect.y0, 1)),
                ]
            else:
                continue
            lines.append(LineString(points))
    return lines


def _white_fill_rectangles(page) -> list[dict]:
    """Return white-filled closed rectangles used by these CAD exports as floor plates.

    The Castle Donington GA exports retain several structural boundaries as white filled
    rectangles even where door breaks prevent the dark strokes from polygonizing.  They are
    evidence already in the raw drawing, not inferred dimensions.
    """
    records = []
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        items = drawing.get("items") or []
        if not fill or min(fill) < 0.98 or len(items) != 1 or items[0][0] != "re":
            continue
        rect = items[0][1]
        if min(rect.width, rect.height) < MIN_SEGMENT_PT:
            continue
        records.append({
            "geometry": Polygon([
                (rect.x0, rect.y0), (rect.x1, rect.y0),
                (rect.x1, rect.y1), (rect.x0, rect.y1),
            ]),
            "source": "office-vector-white-fill-loop",
        })
    return records


def _geometry_iou(left, right) -> float:
    """Intersection-over-union for geometry identity checks (never area-only dedupe)."""
    union = left.union(right).area
    return left.intersection(right).area / union if union else 0.0


def _dedupe_iou(records: list[dict],
                threshold: float = IOU_DEDUPE_THRESHOLD) -> list[dict]:
    """Remove overlapping near-identical loops using IoU, preserving distinct cores."""
    kept = []
    # Prefer dark closed linework, which can retain notches/voids absent from a fill rectangle.
    ordered = sorted(
        records,
        key=lambda record: (
            record.get("source") != "office-vector-closed-loop",
            -record["geometry"].area,
        ),
    )
    for record in ordered:
        if any(_geometry_iou(record["geometry"], other["geometry"]) >= threshold
               for other in kept):
            continue
        kept.append(record)
    return kept


def _rotated_ring(page, coords) -> list[list[float]]:
    matrix = page.rotation_matrix
    points = list(coords)
    if points and points[0] == points[-1]:
        points = points[:-1]
    return [[round((fitz.Point(x, y) * matrix).x, 2),
             round((fitz.Point(x, y) * matrix).y, 2)] for x, y in points]


def _rotated_geometry(page, polygon) -> tuple[list[list[float]], list[list[list[float]]]]:
    """Convert one raw polygon, including its void rings, to snapshot coordinates."""
    simplified = polygon.simplify(0.5, preserve_topology=True)
    exterior = _rotated_ring(page, simplified.exterior.coords)
    holes = [
        ring for ring in (_rotated_ring(page, interior.coords)
                          for interior in simplified.interiors)
        if len(ring) >= 3
    ]
    return exterior, holes


def _aspect_ratio(geometry) -> float:
    x0, y0, x1, y1 = geometry.bounds
    width, height = x1 - x0, y1 - y0
    return max(width, height) / max(min(width, height), 1e-9)


def _confidence(*, resolved: bool, translated: bool, scale_verified: bool,
                region_count: int) -> tuple[str, int, list[str]]:
    """Explain candidate confidence from observable geometric evidence."""
    if not resolved:
        return "low", 10, [
            "level title matched exactly",
            UNRESOLVED_REASON,
        ]
    score = 35
    reasons = ["level title matched exactly", "candidate boundary is a closed CAD vector loop"]
    if scale_verified:
        score += 20
        reasons.append("drawing scale independently verified")
    else:
        reasons.append("drawing scale not independently verified")
    if translated:
        score += 5
        reasons.append(
            "assisted prefill translated from the nearest resolved sibling-level plate; "
            "inspect and edit extent manually"
        )
    else:
        score += 20
        reasons.append("outline resolved within this level viewport")
    if region_count > 1:
        score += 5
        reasons.append(f"{region_count} disjoint core regions resolved for this level")
    label = "medium" if score >= 70 else "low"
    return label, min(score, 95), reasons


def _eligible(record: dict, page_area: float, scale_k: float | None,
              scale_verified: bool) -> bool:
    geometry = record["geometry"]
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
        return False
    if scale_verified and scale_k:
        diagnostic_m2 = geometry.area * scale_k * scale_k
        return MIN_CANDIDATE_M2 <= diagnostic_m2 <= MAX_CANDIDATE_M2
    frac = geometry.area / page_area
    return 0.002 <= frac <= 0.15


def _diagnostic_m2(record: dict, scale_k: float | None) -> float | None:
    if not scale_k:
        return None
    return record["geometry"].area * scale_k * scale_k


def _ground_regions(records: list[dict], scale_k: float | None,
                    scale_verified: bool) -> list[dict]:
    """Select one or two disjoint Level 00 core loops as one semantic candidate."""
    if not records:
        return []
    if not (scale_verified and scale_k):
        return [max(records, key=lambda record: record["geometry"].area)]

    # The client drawings show a larger core and a smaller separate core at Level 00.
    # Aspect limits exclude title strips and long internal bands without assuming an area.
    large = [
        record for record in records
        if record["source"] == "office-vector-white-fill-loop"
        and 55 <= _diagnostic_m2(record, scale_k) <= 125
        and _aspect_ratio(record["geometry"]) <= 2.5
    ]
    primary = max(large, key=lambda record: record["geometry"].area) if large else None
    small = [
        record for record in records
        if record["source"] == "office-vector-closed-loop"
        and 20 <= _diagnostic_m2(record, scale_k) <= 50
        and _aspect_ratio(record["geometry"]) <= 3.0
        and (primary is None or _geometry_iou(record["geometry"], primary["geometry"]) < 0.05)
    ]
    secondary = max(small, key=lambda record: record["geometry"].area) if small else None
    selected = [record for record in (primary, secondary) if record is not None]
    return selected or [max(records, key=lambda record: record["geometry"].area)]


def _upper_regions(records: list[dict], scale_k: float | None) -> tuple[list[dict], str | None]:
    """Select a level-local upper-floor loop, or explain why none is defensible.

    A white CAD fill rectangle is useful corroboration, but it is not itself proof of the
    slab edge: in the client GA files those rectangles can include plant/stair wings.  Dark
    closed linework retains the local notches and void topology.  If the two sources overlap
    substantially but are not the same geometry, a dark loop without any retained topology
    is ambiguous and must remain a manual trace.

    This function deliberately does not copy a sibling level as a resolved boundary.  The
    caller may separately offer a translated sibling as an explicitly low-confidence assisted
    prefill, but it remains non-measured geometry until the assessor accepts or edits it.
    """
    dark = [
        record for record in records
        if record["source"] == "office-vector-closed-loop"
        and ((scale_k and _diagnostic_m2(record, scale_k) >= 80)
             or (not scale_k and record["geometry"].area > 0))
        and _aspect_ratio(record["geometry"]) <= 5
    ]
    if not dark:
        return [], "no level-local dark closed plate loop was found"

    selected = max(dark, key=lambda record: record["geometry"].area)
    if not selected["geometry"].interiors:
        white = [
            record for record in records
            if record["source"] == "office-vector-white-fill-loop"
        ]
        overlaps = [
            _geometry_iou(selected["geometry"], record["geometry"])
            for record in white
        ]
        if any(0.50 <= overlap < IOU_DEDUPE_THRESHOLD for overlap in overlaps):
            return [], (
                "level-local dark and white CAD loops disagree, with no retained void/notch "
                "topology to identify the slab edge"
            )
    return [selected], None


def _candidate_record(page, page_number: int, title: dict, records: list[dict],
                      *, translated: bool, scale_verified: bool,
                      unresolved_detail: str | None = None) -> dict:
    level = title["level"]
    resolved = bool(records)
    confidence, score, reasons = _confidence(
        resolved=resolved,
        translated=translated,
        scale_verified=scale_verified,
        region_count=len(records),
    )
    regions = []
    region_holes = []
    for record in records:
        exterior, holes = _rotated_geometry(page, record["geometry"])
        if len(exterior) >= 3:
            regions.append(exterior)
            region_holes.append(holes)
    void_count = sum(len(holes) for holes in region_holes)
    if void_count:
        reasons.append(
            f"{void_count} enclosed void(s) found; portal trace uses the outer outline, "
            "so assessor must notch/exclude them manually"
        )
    resolved = bool(regions)
    if not resolved and UNRESOLVED_REASON not in reasons:
        confidence, score, reasons = _confidence(
            resolved=False, translated=False, scale_verified=scale_verified, region_count=0)
    if unresolved_detail:
        reasons.append(unresolved_detail)
    sources = sorted({record["source"] for record in records})
    return {
        "candidate_id": f"office-p{page_number}-level-{level:02d}",
        "page": page_number,
        "level": level,
        "level_label": f"Level {level:02d}",
        "source_label": title["text"],
        "title_bbox": title["bbox"],
        "category": "ground_floor" if level == 0 else "upper_floor",
        "boq_scope": "ground_floor_core" if level == 0 else "main_upper_floor",
        "boundary_rule": (None if level == 0 else
                          "assessor trace must follow the edge of the metal decking"),
        # First region retained for older portal clients; new clients use all regions.
        "polygon_pts": regions[0] if regions else [],
        "regions": regions,
        "region_holes": region_holes,
        "diagnostic_void_count": void_count,
        "coordinate_space": "rotated_pdf_points",
        "source": "+".join(sources) if sources else "office-level-title-only",
        "outline_status": ("prefill" if resolved and translated else
                           "resolved" if resolved else "unresolved"),
        "confidence": confidence,
        "confidence_score": score,
        "confidence_reasons": reasons,
        "flags": [
            "ASSISTED TRACE candidate only — assessor must inspect/edit exterior doors, "
            "partitions and voids before submitting an adjustment"
        ] + ([
            "LOW-CONFIDENCE PREFILL: geometry was translated from the nearest resolved "
            "level on this same sheet; it is not a measurement and must be accepted or edited"
        ] if translated else []) + ([] if level == 0 else [
            "UPPER FLOOR SCOPE CHECK: trace to the edge of the metal decking; Plant deck and "
            "POD first-floor areas require separate assessor regions/BOQ scopes"
        ] + ([] if resolved else [
            UNRESOLVED_REASON
            + (f" ({unresolved_detail})" if unresolved_detail else "")
        ])),
    }


def detect_office_candidates(pdf: str, page: int = 0, *, scale_k: float | None = None,
                             scale_verified: bool = False) -> dict:
    """Find exactly one Office GA assisted-trace record for every detected level label.

    ``scale_k`` is used only to reject obviously tiny/huge loops.  It must come from the
    existing independently-verified scale machinery.  No quantity is emitted here.
    """
    doc = fitz.open(pdf)
    try:
        pg = doc[page]
        titles = _level_titles(pg)
        if not titles:
            return {"candidate_polygons": [], "flags": []}

        raw_records = [
            {"geometry": face, "source": "office-vector-closed-loop"}
            for face in polygonize(unary_union(_dark_vector_lines(pg)))
        ]
        raw_records.extend(_white_fill_rectangles(pg))
        records = _dedupe_iou([
            record for record in raw_records
            if _eligible(record, pg.mediabox.get_area(), scale_k, scale_verified)
        ])

        # Plans can be stacked along either raw PDF axis.  Associate each loop with the
        # nearest title along the axis on which titles vary.
        xspread = max(t["x"] for t in titles) - min(t["x"] for t in titles)
        yspread = max(t["y"] for t in titles) - min(t["y"] for t in titles)
        title_axis = "x" if xspread > yspread else "y"
        grouped = {title["level"]: [] for title in titles}
        for record in records:
            centroid = record["geometry"].centroid
            pos = centroid.x if title_axis == "x" else centroid.y
            nearest = min(titles, key=lambda title: abs(pos - title[title_axis]))
            grouped[nearest["level"]].append(record)

        ordered_titles = sorted(titles, key=lambda item: item["level"])
        local_selection = {}
        local_reason = {}
        for title in ordered_titles:
            level = title["level"]
            if level == 0:
                local_selection[level] = _ground_regions(
                    grouped[level], scale_k, scale_verified)
                local_reason[level] = None
            else:
                local_selection[level], local_reason[level] = _upper_regions(
                    grouped[level], scale_k)

        # A repeated level plan with broken local exterior linework can still receive a useful
        # one-click starting outline.  Translation uses only same-sheet title spacing and a
        # genuinely resolved sibling polygon.  It never emits area and is visibly low-confidence;
        # if no sibling resolved, the level remains explicitly unresolved.
        translated_levels = set()
        resolved_upper = [
            title for title in ordered_titles
            if title["level"] > 0 and local_selection[title["level"]]
        ]
        for title in ordered_titles:
            level = title["level"]
            if level == 0 or local_selection[level] or not resolved_upper:
                continue
            sibling = min(
                resolved_upper,
                key=lambda source: abs(title[title_axis] - source[title_axis]),
            )
            dx = title["x"] - sibling["x"] if title_axis == "x" else 0.0
            dy = title["y"] - sibling["y"] if title_axis == "y" else 0.0
            translated_records = [{
                "geometry": translate(record["geometry"], xoff=dx, yoff=dy),
                "source": "office-vector-sibling-level-prefill",
            } for record in local_selection[sibling["level"]]]
            page_bounds = Polygon([
                (pg.mediabox.x0, pg.mediabox.y0), (pg.mediabox.x1, pg.mediabox.y0),
                (pg.mediabox.x1, pg.mediabox.y1), (pg.mediabox.x0, pg.mediabox.y1),
            ])
            if translated_records and all(
                    page_bounds.covers(record["geometry"]) for record in translated_records):
                local_selection[level] = translated_records
                translated_levels.add(level)

        candidates = []
        for title in ordered_titles:
            level = title["level"]
            candidates.append(_candidate_record(
                pg, page, title, local_selection[level],
                translated=level in translated_levels,
                scale_verified=scale_verified,
                unresolved_detail=local_reason[level],
            ))

        unresolved = [
            candidate["level_label"] for candidate in candidates
            if candidate["outline_status"] == "unresolved"
        ]
        prefill_count = sum(
            candidate["outline_status"] == "prefill" for candidate in candidates)
        resolved_count = sum(
            candidate["outline_status"] == "resolved" for candidate in candidates)
        flags = [
            f"OFFICE ASSISTED TRACE: {resolved_count} locally resolved, {prefill_count} "
            f"low-confidence prefill, {len(unresolved)} unresolved across {len(candidates)} "
            "level(s); exactly one candidate row per detected level; no area emitted — "
            "assessor must inspect/edit and submit required regions"
        ]
        if unresolved:
            flags.append(
                "OFFICE ASSISTED TRACE: "
                + ", ".join(unresolved)
                + f" — {UNRESOLVED_REASON}"
            )
        if not scale_verified:
            flags.append(
                "OFFICE ASSISTED TRACE: scale is not independently verified; candidates carry "
                "geometry only and assessor must calibrate before adjustment"
            )
        from measurement_rules import exclusion_review_prompts
        prompts = exclusion_review_prompts(
            {candidate["category"] for candidate in candidates}, pg.get_text() or "")
        for candidate in candidates:
            candidate["exclusion_prompts"] = [
                prompt for prompt in prompts
                if ((candidate["category"] == "ground_floor"
                     and prompt["exclusion_id"] in {
                         "lift_void", "service_data_riser", "precast_stair_foundation"})
                    or (candidate["category"] == "upper_floor"
                        and prompt["exclusion_id"] in {
                            "lift_void", "service_data_riser"}))
            ]
            candidate["flags"].append(
                "EXCLUSION CHECK: trace around lift shafts/pits and service/data risers; "
                "ground-floor stair foundations are separate work, not slab void pricing"
            )
        flags.append(
            "OFFICE EXCLUSION CHECK: candidate outlines do not prove lift/riser/stair-"
            "foundation voids; assessor must exclude them while tracing"
        )
        return {"candidate_polygons": candidates, "flags": flags,
                "exclusion_prompts": prompts}
    finally:
        doc.close()


__all__ = ["detect_office_candidates"]
