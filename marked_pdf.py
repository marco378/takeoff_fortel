#!/usr/bin/env python3
"""Create a client-shareable PDF with Fortel assessor geometry burned into the drawing.

The visible markup is permanent page content so the result opens consistently in Bluebeam,
email viewers and ordinary PDF readers.  A versioned JSON manifest is embedded as an attachment
so a later, separately-reviewed importer can recover the exact job/page/geometry without trying
to reverse-engineer coloured pixels.  This module does not implement that importer.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import fitz


MANIFEST_NAME = "fortel-markup-v1.json"
MANIFEST_SCHEMA = "fortel.markup.v1"


class MarkedPdfError(ValueError):
    """The source or saved job has no safe, exportable marked-PDF representation."""


def _safe_filename_part(value: object, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return cleaned[:120] or fallback


def marked_pdf_filename(job: dict) -> str:
    """Stable client filename: project + source drawing + commercial revision."""
    result = job.get("result") or {}
    source_name = result.get("file") or result.get("pdf_path") or job.get("pdf_path")
    drawing = Path(str(source_name or "drawing.pdf")).stem
    project_ref = _safe_filename_part(job.get("project_ref"), "PROJECT")
    drawing = _safe_filename_part(drawing, "drawing")
    stored_prefix = f"{project_ref}_"
    if drawing.casefold().startswith(stored_prefix.casefold()):
        # Portal storage already prefixes uploaded files for collision safety. Do not repeat
        # that implementation detail in the client-facing download name.
        drawing = _safe_filename_part(drawing[len(stored_prefix):], "drawing")
    revision = int(job.get("quotation_revision") or 1)
    return f"{project_ref}_{drawing}_REV_{revision:02d}_MARKED.pdf"


def _valid_points(value, minimum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= minimum
        and all(
            isinstance(point, (list, tuple))
            and len(point) == 2
            and all(
                isinstance(coordinate, (int, float))
                and not isinstance(coordinate, bool)
                and math.isfinite(coordinate)
                for coordinate in point
            )
            for point in value
        )
    )


def _normalise_points(points, divisor: float = 1.0) -> list[list[float]]:
    return [[round(float(x) / divisor, 6), round(float(y) / divisor, 6)] for x, y in points]


def _polyline_length(points: list[list[float]], scale_k: float | None) -> float | None:
    if not scale_k or scale_k <= 0 or len(points) < 2:
        return None
    return round(sum(math.dist(points[index - 1], points[index])
                     for index in range(1, len(points))) * scale_k, 2)


def _polygon_area(points: list[list[float]], scale_k: float | None) -> float | None:
    if not scale_k or scale_k <= 0 or len(points) < 3:
        return None
    twice_area = sum(
        points[index - 1][0] * points[index][1]
        - points[index][0] * points[index - 1][1]
        for index in range(len(points))
    )
    return round(abs(twice_area) * 0.5 * scale_k * scale_k, 2)


def _label_for_category(category: str | None) -> str:
    labels = {
        "external_yard": "Service yard",
        "dock": "Dock slab",
        "ground_floor": "Ground-floor core",
        "upper_floor": "Upper floor",
        "channel": "Channel",
        "transition": "Transition",
        "construction_joint": "Construction joint",
        "unclassified": "Unclassified area",
        "other": "Measured area",
    }
    return labels.get(str(category or "").strip().lower(), "Measured area")


def _centroid(points: list[list[float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _midpoint(points: list[list[float]]) -> tuple[float, float]:
    distances = [math.dist(points[index - 1], points[index])
                 for index in range(1, len(points))]
    total = sum(distances)
    if total <= 0:
        return points[0][0], points[0][1]
    target = total / 2
    travelled = 0.0
    for index, segment in enumerate(distances, 1):
        if travelled + segment >= target:
            fraction = (target - travelled) / segment
            start, end = points[index - 1], points[index]
            return (
                start[0] + (end[0] - start[0]) * fraction,
                start[1] + (end[1] - start[1]) * fraction,
            )
        travelled += segment
    return points[-1][0], points[-1][1]


def _page_point(page: fitz.Page, rotated_point) -> fitz.Point:
    point = fitz.Point(float(rotated_point[0]), float(rotated_point[1]))
    return point * page.derotation_matrix if page.rotation else point


def _draw_label(page: fitz.Page, anchor, label: str, color,
                occupied: list[fitz.Rect] | None = None) -> None:
    """Place a legible horizontal label in the page's displayed coordinate system."""
    width = min(max(78.0, len(label) * 5.7 + 8), max(90.0, page.rect.width - 8))
    height = 15.0
    occupied = occupied if occupied is not None else []
    base_x = float(anchor[0]) - width / 2
    base_y = float(anchor[1]) - height / 2

    def box_at(dx, dy):
        x = min(max(4.0, base_x + dx), max(4.0, page.rect.width - width - 4))
        y = min(max(4.0, base_y + dy), max(4.0, page.rect.height - height - 4))
        return fitz.Rect(x, y, x + width, y + height)

    # Independently named elements often sit inside the main slab. Keep every burned-in label
    # readable by stacking nearby boxes before accepting an overlap. This is presentation only;
    # the exact geometry remains untouched in the manifest.
    candidates = [box_at(0, 0)]
    for distance in (18, 36, 54, 72, 90, 108):
        candidates.extend((box_at(0, -distance), box_at(0, distance)))
    candidates.extend((box_at(-width * 0.55, 0), box_at(width * 0.55, 0)))
    box = next((candidate for candidate in candidates
                if not any(candidate.intersects(other) for other in occupied)), candidates[0])
    occupied.append(box)
    x, y = box.x0, box.y0
    rotated_box = [
        [x, y], [x + width, y], [x + width, y + height], [x, y + height]
    ]
    page.draw_polyline(
        [_page_point(page, point) for point in rotated_box],
        color=color, fill=(1, 1, 1), width=0.6, closePath=True,
        fill_opacity=0.88, overlay=True,
    )
    baseline = _page_point(page, [x + 4, y + 11])
    page.insert_text(
        baseline, label, fontsize=8.5, fontname="helv", color=color,
        rotate=page.rotation, overlay=True,
    )


def _draw_polygon(page: fitz.Page, points, label: str, color, *, cutout=False,
                  occupied=None) -> None:
    page_points = [_page_point(page, point) for point in points]
    page.draw_polyline(
        page_points, color=color, fill=(1, 1, 1) if cutout else color,
        dashes="[5 3] 0" if cutout else None, width=1.8, closePath=True,
        fill_opacity=0.42 if cutout else 0.13, stroke_opacity=0.95, overlay=True,
    )
    _draw_label(page, _centroid(points), label, color, occupied)


def _draw_line(page: fitz.Page, points, label: str, color, *, dashed=False,
               occupied=None) -> None:
    page.draw_polyline(
        [_page_point(page, point) for point in points],
        color=color, dashes="[6 4] 0" if dashed else None,
        width=2.2, lineCap=1, lineJoin=1, overlay=True,
    )
    _draw_label(page, _midpoint(points), label, color, occupied)


def _effective_source(job: dict, explicit_path=None) -> Path:
    result = job.get("result") or {}
    value = explicit_path or result.get("pdf_path") or job.get("pdf_path") or job.get("pdf")
    if not value:
        raise MarkedPdfError("job has no source PDF path")
    path = Path(str(value))
    if not path.exists():
        raise MarkedPdfError(f"source PDF is not available: {path}")
    return path


def _overlay_manifest(job: dict, source_path: Path, page_index: int,
                      snapshot_scale_value: float | None) -> dict:
    """Normalise every exportable geometry into displayed/rotated PDF points."""
    result = job.get("result") or {}
    adjusted = job.get("adjusted") or {}
    scale_k_pdf = result.get("scale_k")
    if isinstance(job.get("scale_k"), (int, float)) and job.get("scale_k") > 0:
        scale_k_pdf = job["scale_k"]

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "job_id": str(job.get("id") or ""),
        "project_ref": job.get("project_ref"),
        "project_name": job.get("project_name"),
        "quotation_revision": int(job.get("quotation_revision") or 1),
        "source_pdf": source_path.name,
        "source_page_index": page_index,
        "export_coordinate_space": "rotated_pdf_points",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "geometry": {
            "regions": [], "cutouts": [], "channels": [], "transitions": []
        },
    }
    geometry = manifest["geometry"]

    adjusted_regions = adjusted.get("regions") or (
        [adjusted.get("vertices")] if _valid_points(adjusted.get("vertices"), 3) else []
    )
    adjusted_snapshot_scale = adjusted.get("snapshot_scale") or snapshot_scale_value
    if adjusted_regions:
        if not isinstance(adjusted_snapshot_scale, (int, float)) or adjusted_snapshot_scale <= 0:
            raise MarkedPdfError("assessor geometry has no recoverable snapshot scale")
        divisor = float(adjusted_snapshot_scale)
        assessor_scale = adjusted.get("scale_k")
        valid_cutouts = [points for points in (adjusted.get("cutout_regions") or [])
                         if _valid_points(points, 3)]
        categories = adjusted.get("region_categories") or []
        scopes = adjusted.get("region_scopes") or []
        for index, raw_points in enumerate(adjusted_regions):
            if not _valid_points(raw_points, 3):
                continue
            points = _normalise_points(raw_points, divisor)
            area = _polygon_area(raw_points, assessor_scale)
            if valid_cutouts and assessor_scale:
                try:
                    from geometry import measure_regions
                    # The same geometry operation as /adjust: cut-outs outside this region
                    # subtract nothing, while contained/intersecting cut-outs produce its net
                    # priced area. The label must match that assessor truth, not gross outline.
                    area = round(measure_regions(
                        [raw_points], assessor_scale, holes={0: valid_cutouts})[0], 2)
                except Exception:
                    # Export remains available with the gross label plus separately labelled
                    # cut-outs; the manifest still preserves exact geometry for human review.
                    pass
            category = categories[index] if index < len(categories) else "unclassified"
            geometry["regions"].append({
                "region_id": f"assessor-region-{index + 1}",
                "name": _label_for_category(category),
                "category": category,
                "boq_scope": scopes[index] if index < len(scopes) else "main",
                "area_m2": area,
                "points": points,
                "source_coordinate_space": "snapshot_pixels",
                "source_snapshot_scale_px_per_pdf_point": divisor,
                "assessor_supplied": True,
            })
        for index, raw_points in enumerate(valid_cutouts):
            # Compute the actual removed area (intersection with measured regions)
            # rather than just the polygon area
            cutout_removed_area = _polygon_area(raw_points, assessor_scale)
            if adjusted_regions and assessor_scale:
                try:
                    from geometry import measure_regions_with_cutouts
                    # Compute how much of this cutout actually intersects the measured regions
                    _, removed_m2, _, _ = measure_regions_with_cutouts(
                        adjusted_regions, [raw_points], assessor_scale)
                    if removed_m2 > 0:
                        cutout_removed_area = removed_m2
                except Exception:
                    # Fall back to polygon area if geometry fails
                    pass
            geometry["cutouts"].append({
                "cutout_id": f"assessor-cutout-{index + 1}",
                "name": f"Cut-out {index + 1}",
                "area_m2": cutout_removed_area,
                "polygon_area_m2": _polygon_area(raw_points, assessor_scale),
                "points": _normalise_points(raw_points, divisor),
                "source_coordinate_space": "snapshot_pixels",
                "source_snapshot_scale_px_per_pdf_point": divisor,
                "assessor_supplied": True,
            })
        for index, raw_points in enumerate(adjusted.get("user_channels") or []):
            if not _valid_points(raw_points, 2):
                continue
            geometry["channels"].append({
                "channel_id": f"assessor-channel-{index + 1}",
                "name": f"Channel {index + 1}",
                "length_lm": _polyline_length(raw_points, assessor_scale),
                "points": _normalise_points(raw_points, divisor),
                "source_coordinate_space": "snapshot_pixels",
                "source_snapshot_scale_px_per_pdf_point": divisor,
                "assessor_supplied": True,
                "provisional": False,
            })
    else:
        # Prefer individually surfaced native Yard/zone geometry. Fall back to the job's
        # assessor-approved top-level polygon when the pipeline exposes only one region.
        seen = set()
        native_regions = job.get("yard_regions") or result.get("yard_regions") or []
        for index, region in enumerate(native_regions):
            points = region.get("polygon_pts") if isinstance(region, dict) else None
            if not _valid_points(points, 3) or region.get("included") is False:
                continue
            normalised = _normalise_points(points)
            key = tuple(tuple(point) for point in normalised)
            seen.add(key)
            geometry["regions"].append({
                "region_id": str(region.get("region_id") or f"yard-region-{index + 1}"),
                "name": "Service yard",
                "category": "external_yard",
                "area_m2": region.get("area_m2"),
                "points": normalised,
                "source_coordinate_space": "rotated_pdf_points",
                "assessor_supplied": False,
                "assessor_approved": job.get("decision") in {"approved", "adjusted"},
            })
        for index, zone in enumerate(job.get("zones") or result.get("zones") or []):
            if not isinstance(zone, dict):
                continue
            # A zone can be drawn in several separately-hatched parts (the hatch-legend path
            # emits a yard or a road as its parts). Draw EVERY part, each labelled with its own
            # quantity: one outline beside a number covering three parts is how an assessor is
            # led to approve an extent nobody looked at.
            polygons = list(zone.get("region_polygons") or [])
            part_ids = list(zone.get("region_ids") or [])
            part_areas = list(zone.get("region_areas_m2") or [])
            part_holes = list(zone.get("region_holes") or [])
            zone_id = str(zone.get("zone_key") or f"zone-region-{index + 1}")
            if polygons:
                parts = [(polygons[i],
                          str(part_ids[i]) if i < len(part_ids) else f"{zone_id}-part-{i + 1}",
                          part_areas[i] if i < len(part_areas) else None,
                          part_holes[i] if i < len(part_holes) else [])
                         for i in range(len(polygons))]
            else:
                parts = [(zone.get("polygon_pts"), zone_id, zone.get("area_m2"), [])]
            for points, region_id, part_area, holes in parts:
                if not _valid_points(points, 3):
                    continue
                normalised = _normalise_points(points)
                key = tuple(tuple(point) for point in normalised)
                if key in seen:
                    continue
                seen.add(key)
                geometry["regions"].append({
                    "region_id": region_id,
                    "name": _label_for_category(zone.get("category")),
                    "category": zone.get("category"),
                    "area_m2": part_area,
                    "points": normalised,
                    "source_coordinate_space": "rotated_pdf_points",
                    "assessor_supplied": False,
                    "assessor_approved": job.get("decision") in {"approved", "adjusted"},
                })
                # A surface drawn as a loop (a road around a yard) has holes. Draw them, or the
                # outline claims everything it encircles and the picture contradicts the number
                # printed on it. These are already excluded from the measured quantity.
                for hole_index, hole in enumerate(holes or []):
                    if not _valid_points(hole, 3):
                        continue
                    hole_points = _normalise_points(hole)
                    hole_key = tuple(tuple(point) for point in hole_points)
                    if hole_key in seen:
                        continue
                    seen.add(hole_key)
                    geometry["cutouts"].append({
                        "cutout_id": f"{region_id}-void-{hole_index + 1}",
                        "name": "Not in this surface",
                        "area_m2": None,
                        "points": hole_points,
                        "source_coordinate_space": "rotated_pdf_points",
                        "assessor_supplied": False,
                    })
        top_points = result.get("polygon_pts")
        if not geometry["regions"] and _valid_points(top_points, 3):
            geometry["regions"].append({
                "region_id": "measured-region-1",
                "name": "Measured area",
                "category": None,
                "area_m2": result.get("area_m2") or job.get("area_m2"),
                "points": _normalise_points(top_points),
                "source_coordinate_space": "rotated_pdf_points",
                "assessor_supplied": False,
                "assessor_approved": job.get("decision") in {"approved", "adjusted"},
            })

    # Independently named ``+ Area`` elements share the assessor snapshot coordinate space
    # but are never merged into the main measured region. Burn each with its client-entered
    # name and preserve that distinction in the future-import manifest.
    area_elements = job.get("area_elements")
    if not isinstance(area_elements, list):
        area_elements = adjusted.get("area_elements") if isinstance(
            adjusted.get("area_elements"), list) else []
    if area_elements:
        if not isinstance(adjusted_snapshot_scale, (int, float)) or adjusted_snapshot_scale <= 0:
            raise MarkedPdfError("named assessor area geometry has no recoverable snapshot scale")
        divisor = float(adjusted_snapshot_scale)
        for index, element in enumerate(area_elements, 1):
            if not isinstance(element, dict):
                continue
            points = element.get("polygon_pts") or element.get("points")
            if not _valid_points(points, 3):
                continue
            geometry["regions"].append({
                "region_id": str(element.get("element_id") or f"area-element-{index}"),
                "name": str(element.get("name") or f"Area element {index}"),
                "category": element.get("category"),
                "boq_scope": element.get("boq_scope") or "main",
                "area_m2": element.get("area_m2"),
                "points": _normalise_points(points, divisor),
                "source_coordinate_space": "snapshot_pixels",
                "source_snapshot_scale_px_per_pdf_point": divisor,
                "assessor_supplied": True,
                "independent_area_element": True,
            })

    # Accepted AI proposals remain explicitly provisional assumptions. Geometry edits made by
    # the assessor are preserved on the decision record and win over the original proposal.
    channel_proposals = {
        str(item.get("proposal_id")): item
        for item in (job.get("channel_proposals") or result.get("channel_proposals") or [])
        if isinstance(item, dict) and item.get("proposal_id")
    }
    channel_decisions = job.get("channel_proposal_decisions") or result.get(
        "channel_proposal_decisions") or {}
    for proposal_id, decision in channel_decisions.items():
        if not isinstance(decision, dict) or decision.get("decision") != "accepted":
            continue
        proposal = channel_proposals.get(str(proposal_id), {})
        points = decision.get("polyline_pts") or proposal.get("polyline_pts")
        if not _valid_points(points, 2):
            continue
        geometry["channels"].append({
            "channel_id": str(proposal_id),
            "name": proposal.get("component") or "Proposed channel",
            "length_lm": decision.get("length_lm"),
            "points": _normalise_points(points),
            "source_coordinate_space": "rotated_pdf_points",
            "assessor_supplied": True,
            "provisional": True,
            "basis": proposal.get("basis"),
        })

    transition_candidates = {
        str(item.get("candidate_id")): item
        for item in (job.get("transition_candidates") or result.get("transition_candidates") or [])
        if isinstance(item, dict) and item.get("candidate_id")
    }
    transition_decisions = job.get("transition_candidate_decisions") or result.get(
        "transition_candidate_decisions") or {}
    for candidate_id, decision in transition_decisions.items():
        if not isinstance(decision, dict) or decision.get("decision") != "accepted":
            continue
        candidate = transition_candidates.get(str(candidate_id), {})
        points = candidate.get("polyline_pts")
        if not _valid_points(points, 2):
            continue
        geometry["transitions"].append({
            "transition_id": str(candidate_id),
            "name": "Transition",
            "length_lm": decision.get("length_lm"),
            "points": _normalise_points(points),
            "source_coordinate_space": "rotated_pdf_points",
            "assessor_supplied": True,
            "provisional": True,
            "basis": candidate.get("basis"),
        })

    return manifest


def build_marked_pdf(job: dict, pdf_path=None, *, snapshot_scale_value=None) -> tuple[bytes, dict]:
    """Return ``(pdf_bytes, manifest)`` without mutating the source PDF or job store."""
    source_path = _effective_source(job, pdf_path)
    result = job.get("result") or {}
    page_index = int(result.get("page") or 0)
    try:
        document = fitz.open(source_path)
    except Exception as exc:
        raise MarkedPdfError(f"source PDF could not be opened: {exc}") from exc
    if not 0 <= page_index < document.page_count:
        document.close()
        raise MarkedPdfError(f"measured page {page_index} is outside the source PDF")

    manifest = _overlay_manifest(job, source_path, page_index, snapshot_scale_value)
    geometry = manifest["geometry"]
    source_annotation_count = sum(
        1 for page in document for _annot in (page.annots() or [])
    )
    if not any(geometry.values()) and not source_annotation_count:
        document.close()
        raise MarkedPdfError("job has no measured/assessor markup geometry to export")

    # Existing Bluebeam annotations must look identical but be independent of a viewer's
    # annotation settings. Bake first, then draw Fortel's canonical vectors as page content.
    if source_annotation_count:
        document.bake(annots=True, widgets=True)
    page = document[page_index]
    area_color = (0.88, 0.0, 0.48)
    cutout_color = (0.90, 0.30, 0.02)
    channel_color = (0.75, 0.12, 0.10)
    transition_color = (0.92, 0.45, 0.04)
    occupied_labels = []

    for region in geometry["regions"]:
        quantity = region.get("area_m2")
        label = region.get("name") or "Measured area"
        if isinstance(quantity, (int, float)):
            label += f" - {quantity:,.2f} m2"
        _draw_polygon(page, region["points"], label, area_color,
                      occupied=occupied_labels)
    for cutout in geometry["cutouts"]:
        quantity = cutout.get("area_m2")
        label = cutout.get("name") or "Cut-out"
        if isinstance(quantity, (int, float)):
            label += f" - {quantity:,.2f} m2"
        _draw_polygon(page, cutout["points"], label, cutout_color, cutout=True,
                      occupied=occupied_labels)
    for channel in geometry["channels"]:
        label = channel.get("name") or "Channel"
        if isinstance(channel.get("length_lm"), (int, float)):
            label += f" - {channel['length_lm']:,.2f} Lm"
        if channel.get("provisional"):
            label += " - PROVISIONAL"
        _draw_line(page, channel["points"], label, channel_color,
                   dashed=bool(channel.get("provisional")), occupied=occupied_labels)
    for transition in geometry["transitions"]:
        label = transition.get("name") or "Transition"
        if isinstance(transition.get("length_lm"), (int, float)):
            label += f" - {transition['length_lm']:,.2f} Lm"
        if transition.get("provisional"):
            label += " - PROVISIONAL"
        _draw_line(page, transition["points"], label, transition_color,
                   dashed=bool(transition.get("provisional")), occupied=occupied_labels)

    manifest["source_annotations_burned_in"] = source_annotation_count
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    document.embfile_add(
        MANIFEST_NAME, manifest_bytes, filename=MANIFEST_NAME,
        ufilename=MANIFEST_NAME,
        desc="Fortel assessor markup geometry - versioned re-import manifest",
    )
    metadata = dict(document.metadata or {})
    metadata.update({
        "title": marked_pdf_filename(job).removesuffix(".pdf"),
        "subject": "Fortel assessor marked-up takeoff drawing",
        "keywords": (
            f"Fortel; {MANIFEST_SCHEMA}; job_id={manifest['job_id']}; "
            f"revision={manifest['quotation_revision']}"
        ),
        "producer": "Fortel AI Takeoff assessor portal",
    })
    document.set_metadata(metadata)
    output = document.tobytes(garbage=4, deflate=True, deflate_images=True, clean=True)
    document.close()
    return output, manifest
