"""Deterministic vector candidates for assessor-assisted Office GA tracing.

This module deliberately does *not* measure an office slab.  Architectural GA plans have
open door breaks, room partitions, cores and stair/lift voids, so a closed vector face is
only a tracing aid until an assessor selects/edits it.  The pipeline therefore carries the
returned polygons while keeping ``area_m2=None`` and ``measurement_state=UNMEASURED``.

Candidate coordinates are rotated PDF points: the same coordinate space as the rendered
page returned by PyMuPDF and the portal snapshot canvas before applying ``snapScale``.
"""

from __future__ import annotations

import math
import re
import fitz
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union


DARK_STROKE_MAX = 0.10
MIN_SEGMENT_PT = 5.0
MIN_CANDIDATE_M2 = 20.0
MAX_CANDIDATE_M2 = 500.0
MAX_CANDIDATES_PER_LEVEL = 3


def _level_titles(page) -> list[dict]:
    """Return level labels and their raw-content-stream positions."""
    titles = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            match = re.search(r"Office\s+Plan\s+Level\s*(\d+)", text, re.I)
            if match:
                level = int(match.group(1))
            elif re.search(r"\bThird\s+Floor\b", text, re.I):
                # Unit 3 calls the last plan "Third Floor" instead of "Level 03".
                level = 3
            else:
                continue
            x0, y0, x1, y1 = line["bbox"]
            titles.append({
                "level": level,
                "text": text.strip(),
                "x": (x0 + x1) / 2,
                "y": (y0 + y1) / 2,
            })
    # A duplicated extracted title must not create a duplicated candidate group.
    unique = {}
    for title in titles:
        unique.setdefault(title["level"], title)
    return list(unique.values())


def _dark_vector_lines(page) -> list[LineString]:
    """Extract dark vector strokes/rectangles; ignore grey hatch and fine colour fills."""
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


def _rotated_points(page, polygon) -> list[list[float]]:
    """Convert raw drawing coordinates to the rotated coordinates used by snapshots."""
    simplified = polygon.simplify(0.5, preserve_topology=True)
    points = list(simplified.exterior.coords)[:-1]
    matrix = page.rotation_matrix
    return [[round((fitz.Point(x, y) * matrix).x, 2),
             round((fitz.Point(x, y) * matrix).y, 2)] for x, y in points]


def detect_office_candidates(pdf: str, page: int = 0, *, scale_k: float | None = None,
                             scale_verified: bool = False) -> dict:
    """Find closed Office GA vector faces for assessor-assisted tracing.

    ``scale_k`` is used only to reject obviously tiny/huge faces.  It must come from the
    existing independently-verified scale machinery.  If it is absent/unverified, a
    conservative page-relative geometric filter is used instead and no quantity is emitted.
    """
    doc = fitz.open(pdf)
    try:
        pg = doc[page]
        titles = _level_titles(pg)
        if not titles:
            return {"candidate_polygons": [], "flags": []}

        lines = _dark_vector_lines(pg)
        if not lines:
            return {
                "candidate_polygons": [],
                "flags": ["OFFICE ASSISTED TRACE: level labels found but no dark vector linework"],
            }

        faces = list(polygonize(unary_union(lines)))
        page_area = pg.mediabox.get_area()
        eligible = []
        for face in faces:
            if face.is_empty or not face.is_valid or face.area <= 0:
                continue
            if scale_verified and scale_k:
                diagnostic_m2 = face.area * scale_k * scale_k
                keep = MIN_CANDIDATE_M2 <= diagnostic_m2 <= MAX_CANDIDATE_M2
            else:
                # Geometry-only fallback: enough to offer an outline, never enough to issue
                # a number.  The bounds mirror the validated 20-500 m2 faces on A0/A1 sheets.
                frac = face.area / page_area
                keep = 0.002 <= frac <= 0.15
            if keep:
                eligible.append(face)

        # Plans can be stacked along either raw PDF axis.  Associate each closed face with
        # the nearest extracted level title along the axis on which those titles vary.
        xspread = max(t["x"] for t in titles) - min(t["x"] for t in titles)
        yspread = max(t["y"] for t in titles) - min(t["y"] for t in titles)
        title_axis = "x" if xspread > yspread else "y"
        grouped = {title["level"]: [] for title in titles}
        for face in eligible:
            pos = face.centroid.x if title_axis == "x" else face.centroid.y
            nearest = min(titles, key=lambda t: abs(pos - t[title_axis]))
            grouped[nearest["level"]].append(face)

        candidates = []
        missing_levels = []
        for title in sorted(titles, key=lambda t: t["level"]):
            level = title["level"]
            faces_for_level = sorted(grouped[level], key=lambda face: face.area, reverse=True)
            if not faces_for_level:
                missing_levels.append(f"Level {level:02d}")
                continue
            category = "ground_floor" if level == 0 else "upper_floor"
            for index, face in enumerate(faces_for_level[:MAX_CANDIDATES_PER_LEVEL], 1):
                polygon_pts = _rotated_points(pg, face)
                if len(polygon_pts) < 3:
                    continue
                candidates.append({
                    "candidate_id": f"office-p{page}-level-{level:02d}-{index}",
                    "page": page,
                    "level": level,
                    "level_label": f"Level {level:02d}",
                    "source_label": title["text"],
                    "category": category,
                    "polygon_pts": polygon_pts,
                    "coordinate_space": "rotated_pdf_points",
                    "source": "office-vector-closed-loop",
                    "confidence": "low",
                    "flags": [
                        "ASSISTED TRACE candidate only — exterior doors, partitions and voids "
                        "can make a closed face smaller than the slab; assessor selects/edits it"
                    ],
                })

        flags = []
        if candidates:
            flags.append(
                f"OFFICE ASSISTED TRACE: {len(candidates)} closed vector candidate(s) found; "
                "no area emitted — assessor must select/edit all required level/core regions"
            )
        if missing_levels:
            flags.append(
                "OFFICE ASSISTED TRACE: no closed exterior candidate for "
                + ", ".join(missing_levels)
                + " — assessor must trace those regions manually"
            )
        if not scale_verified:
            flags.append(
                "OFFICE ASSISTED TRACE: scale is not independently verified; candidates carry "
                "geometry only and assessor must calibrate before adjustment"
            )
        return {"candidate_polygons": candidates, "flags": flags}
    finally:
        doc.close()


__all__ = ["detect_office_candidates"]
