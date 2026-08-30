#!/usr/bin/env python3
"""Assessor-gated measurement for structural slabs drawn with near-white solid fills.

This is deliberately separate from the service-yard colour-segmentation path.  It applies
only when the sheet itself identifies both a structural slab plan and composite metal-deck
construction.  Candidate geometry is selected from one dominant *local* neutral-fill
component; whole-sheet ink percentages and filenames are never evidence.
"""
import math
import re

import cv2
import numpy as np
from shapely.geometry import Polygon

import sanity


# These gates belong only to this new measurement mode.  They do not alter drawing_style(),
# segment_hatch(), any existing gold tolerance, or the colour-coded path in any way.
LIGHT_NEUTRAL_MIN = 225          # lighter than the legacy Yard band, darker than paper
LIGHT_NEUTRAL_MAX = 244          # near-white paper starts at 245 in the swatch reader too
MIN_MODE_PDF_PT2 = 1_000         # reject text antialiasing / isolated legend chips
STABLE_MODE_COVERAGE = 0.98      # one component must explain almost all pixels of its mode
STABLE_AREA_GROWTH = 0.02        # stop at the first closing plateau, before shapes overgrow
STABLE_BBOX_IOU = 0.98
MIN_LOCAL_FILL_SUPPORT = 0.80    # evaluate within the candidate viewport, never whole page
MAX_COMPETITOR_RATIO = 0.25      # two material disconnected plates are ambiguous
MAX_CLOSE_PDF_PT = 12.5          # line/text gaps only; never bridge a site-scale separation

MEASUREMENT_MODE = "structural light-fill segmentation"


def structural_sheet_evidence(sheet_text):
    """Return on-sheet evidence for this mode, or an empty list when it does not apply."""
    text = re.sub(r"\s+", " ", str(sheet_text or "")).upper()
    slab_plan = (
        "MEZZANINE SUSPENDED SLAB LAYOUT" in text
        or bool(re.search(
            r"\b(?:MEZZANINE|SUSPENDED)\b.{0,80}\bSLAB\b.{0,80}\b(?:LAYOUT|PLAN)\b",
            text,
        ))
    )
    deck_legend = (
        "COMPOSITE METAL DECK CONSTRUCTION" in text
        or ("COMPOSITE METAL DECK" in text and "COMPOSITE FLOOR" in text)
    )
    if not (slab_plan and deck_legend):
        return []
    return [
        "sheet title identifies a mezzanine/suspended slab layout",
        "sheet legend/notes identify composite metal-deck construction",
    ]


def _bbox_iou(a, b):
    ax, ay, aw, ah = [int(value) for value in a]
    bx, by, bw, bh = [int(value) for value in b]
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    intersection = ix * iy
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


def _closing_kernels(S, scale_k):
    """Odd kernels expressed in rendered pixels, bounded by PDF and real-world distance."""
    max_px = max(5, int(round(MAX_CLOSE_PDF_PT * S)))
    if scale_k and math.isfinite(float(scale_k)) and float(scale_k) > 0:
        # Never bridge more than one metre merely to connect a light fill interrupted by ink.
        max_px = min(max_px, max(5, int(round(1.0 * S / float(scale_k)))))
    if max_px % 2 == 0:
        max_px -= 1
    return list(range(3, max_px + 1, 2))


def _largest_component(binary):
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        binary.astype(np.uint8), 8)
    if count <= 1:
        return None
    component_id = int(np.argmax(stats[1:, cv2.CC_STAT_AREA]) + 1)
    return {
        "labels": labels,
        "stats": stats,
        "component_id": component_id,
        "mask": labels == component_id,
        "area_px": int(stats[component_id, cv2.CC_STAT_AREA]),
        "bbox": stats[component_id, :4].astype(int).tolist(),
    }


def _stable_mode_component(raw_mask, kernels):
    """Find the first closing plateau that yields one dominant local fill component."""
    raw_count = int(raw_mask.sum())
    records = []
    ambiguity = None
    for kernel in kernels:
        closed = cv2.morphologyEx(
            raw_mask.astype(np.uint8), cv2.MORPH_CLOSE,
            np.ones((kernel, kernel), np.uint8),
        )
        record = _largest_component(closed)
        if not record:
            continue
        component_areas = np.sort(
            record["stats"][1:, cv2.CC_STAT_AREA].astype(float))[::-1]
        if len(component_areas) > 1 and component_areas[1] >= (
                component_areas[0] * MAX_COMPETITOR_RATIO):
            ambiguity = (
                f"two disconnected components compete at {component_areas[0]:.0f} and "
                f"{component_areas[1]:.0f} px"
            )
        record["kernel_px"] = kernel
        record["modal_coverage"] = float(
            np.logical_and(record["mask"], raw_mask).sum() / max(raw_count, 1))
        records.append(record)

    for index, record in enumerate(records[:-1]):
        following = records[index + 1]
        growth = (following["area_px"] - record["area_px"]) / max(record["area_px"], 1)
        if (
            record["modal_coverage"] >= STABLE_MODE_COVERAGE
            and growth <= STABLE_AREA_GROWTH
            and _bbox_iou(record["bbox"], following["bbox"]) >= STABLE_BBOX_IOU
        ):
            record["next_kernel_growth"] = float(growth)
            return record, ambiguity
    return None, ambiguity


def _outline_from_mask(mask, S):
    """Return a valid, simplified outer contour in rotated PDF-point coordinates."""
    contours, _hierarchy = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return None
    # Start tight and simplify only enough to remove pixel-scale self-touching spikes.
    for fraction in (0.001, 0.0015, 0.002, 0.003, 0.005):
        approx = cv2.approxPolyDP(contour, fraction * perimeter, True).reshape(-1, 2)
        points = [[float(x) / S, float(y) / S] for x, y in approx]
        if len(points) >= 3:
            polygon = Polygon(points)
            if polygon.is_valid and not polygon.is_empty and polygon.area > 1:
                return points
    return None


def detect_structural_light_fill(im_rgb, sheet_text, scale_k, scale_verified=False, S=2.0):
    """Measure one unambiguous structural light-fill plate, always assessor-gated.

    Returns ``applicable=False`` when the structural text gate is absent.  Once applicable,
    weak/multiple geometry is a terminal clean refusal: no other automatic estimator should
    reinterpret the same sheet after this mode has declined it.
    """
    evidence = structural_sheet_evidence(sheet_text)
    if not evidence:
        return {"applicable": False}

    flags = [
        "MEASUREMENT MODE: structural light-fill segmentation (separate assessor-gated "
        "path; the existing colour-coded Yard path remains blocked)",
        "Mode evidence: " + "; ".join(evidence),
    ]
    refusal = {
        "applicable": True,
        "method": MEASUREMENT_MODE,
        "measurement_mode": MEASUREMENT_MODE,
        "terminal_measurement_refusal": True,
        "area_m2": None,
        "measurement_state": sanity.UNMEASURED,
        "needs_assessor": True,
        "perimeter_measurement_allowed": False,
    }

    image = np.asarray(im_rgb)
    if image.ndim != 3 or image.shape[2] < 3 or S <= 0:
        return dict(refusal, flags=flags + [
            "REFUSED — structural light-fill render is invalid; assessor must trace manually"
        ])
    rgb = image[..., :3].astype(np.int16)
    spread = rgb.max(axis=2) - rgb.min(axis=2)
    neutral = spread <= 1
    levels = np.rint(rgb.mean(axis=2)).astype(np.int16)
    min_mode_pixels = max(1, int(round(MIN_MODE_PDF_PT2 * S * S)))
    kernels = _closing_kernels(S, scale_k)
    candidates = []
    ambiguity_reasons = []
    mode_diagnostics = []

    for level in range(LIGHT_NEUTRAL_MIN, LIGHT_NEUTRAL_MAX + 1):
        raw_mask = neutral & (levels == level)
        raw_count = int(raw_mask.sum())
        if raw_count < min_mode_pixels:
            continue
        stable, ambiguity = _stable_mode_component(raw_mask, kernels)
        if ambiguity:
            ambiguity_reasons.append(f"neutral level {level}: {ambiguity}")
        if not stable:
            mode_diagnostics.append({
                "neutral_level": level, "raw_pixels": raw_count,
                "status": "no unique closing plateau",
            })
            continue

        x, y, width, height = stable["bbox"]
        component = stable["mask"]
        local = component[y:y + height, x:x + width]
        local_neutral_fill = neutral[y:y + height, x:x + width] & (
            levels[y:y + height, x:x + width] >= LIGHT_NEUTRAL_MIN) & (
            levels[y:y + height, x:x + width] <= LIGHT_NEUTRAL_MAX)
        local_support = float(local_neutral_fill[local].mean()) if local.any() else 0.0
        modal_concentration = float(
            np.logical_and(component, raw_mask).sum() / max(raw_count, 1))
        mode_diagnostics.append({
            "neutral_level": level,
            "raw_pixels": raw_count,
            "kernel_px": stable["kernel_px"],
            "modal_coverage": round(stable["modal_coverage"], 4),
            "next_kernel_growth": round(stable["next_kernel_growth"], 4),
            "local_fill_support": round(local_support, 4),
            "bbox_render_px": [x, y, width, height],
        })
        if local_support < MIN_LOCAL_FILL_SUPPORT or modal_concentration < STABLE_MODE_COVERAGE:
            continue
        outline = _outline_from_mask(component, S)
        if not outline:
            continue
        candidates.append({
            "neutral_level": level,
            "mask": component,
            "area_px": int(component.sum()),
            "kernel_px": stable["kernel_px"],
            "modal_coverage": modal_concentration,
            "local_fill_support": local_support,
            "bbox_render_px": [x, y, width, height],
            "polygon_pts": outline,
        })

    candidates.sort(key=lambda candidate: candidate["area_px"], reverse=True)
    if not candidates:
        reason = ("; ".join(ambiguity_reasons[:3]) if ambiguity_reasons
                  else "no unique locally solid near-white component reached a stable boundary")
        return dict(refusal, flags=flags + [
            "REFUSED — structural light-fill geometry is ambiguous/unresolved: " + reason
            + "; no number emitted, assessor must trace manually"
        ], light_fill_diagnostics=mode_diagnostics)

    chosen = candidates[0]
    competitors = []
    for candidate in candidates[1:]:
        intersection = int(np.logical_and(chosen["mask"], candidate["mask"]).sum())
        union = int(np.logical_or(chosen["mask"], candidate["mask"]).sum())
        mask_iou = intersection / union if union else 0.0
        if (candidate["area_px"] >= chosen["area_px"] * MAX_COMPETITOR_RATIO
                and mask_iou < 0.50):
            competitors.append(candidate)
    if competitors:
        sizes = [chosen["area_px"]] + [candidate["area_px"] for candidate in competitors]
        return dict(refusal, flags=flags + [
            "REFUSED — multiple disconnected structural light-fill regions compete "
            f"({', '.join(str(size) for size in sizes)} px); no region was guessed, "
            "assessor must trace/classify them"
        ], light_fill_diagnostics=mode_diagnostics)

    try:
        k = float(scale_k)
    except (TypeError, ValueError):
        k = None
    if not k or not math.isfinite(k) or k <= 0:
        return dict(refusal, flags=flags + [
            "REFUSED — structural light-fill region resolved but no usable scale exists; "
            "no number emitted, assessor must calibrate and trace"
        ], polygon_pts=chosen["polygon_pts"], light_fill_diagnostics=mode_diagnostics)

    area_m2 = round(chosen["area_px"] * (k / S) ** 2, 1)
    state, state_flags = sanity.measurement_state(
        area_m2, scale_verified=bool(scale_verified), confidence="low")
    # This estimator has semantic + raster corroboration but no independent native boundary;
    # it is intentionally never allowed to promote itself to VERIFIED.
    state = sanity.MEASURED_UNVERIFIED
    x, y, width, height = chosen["bbox_render_px"]
    flags += [
        f"Structural light-fill region: neutral RGB level {chosen['neutral_level']} selected "
        f"inside local plan viewport [{x / S:.1f}, {y / S:.1f}, "
        f"{(x + width) / S:.1f}, {(y + height) / S:.1f}] PDF pt; "
        f"{chosen['local_fill_support'] * 100:.1f}% local neutral-fill support",
        f"Geometry gate: {chosen['modal_coverage'] * 100:.1f}% of the selected neutral mode "
        f"belongs to one component; first stable closing plateau is "
        f"{chosen['kernel_px']} rendered px",
        "NO NATIVE CLOSED BOUNDARY CORROBORATION: area is emitted only as "
        "MEASURED_UNVERIFIED; assessor must confirm scale, extent and cut-outs",
    ] + state_flags
    zone = {
        "zone_key": "upper_floor:structural-light-fill",
        "category": "upper_floor",
        "subjects": ["Mezzanine suspended slab / composite metal deck"],
        "measurement_kind": "area",
        "area_m2": area_m2,
        "length_lm": None,
        "perimeter_lm": None,
        "annotation_count": 0,
        "cutout_count": 0,
        "classification_source": (
            "on-sheet suspended-slab title + composite-metal-deck legend + local light-fill"
        ),
        "needs_assessor": True,
    }
    serialisable_diagnostics = {
        "selected_neutral_level": chosen["neutral_level"],
        "selected_kernel_px": chosen["kernel_px"],
        "local_fill_support": round(chosen["local_fill_support"], 4),
        "modal_coverage": round(chosen["modal_coverage"], 4),
        "bbox_render_px": chosen["bbox_render_px"],
        "mode_candidates": mode_diagnostics,
    }
    return {
        "applicable": True,
        "method": MEASUREMENT_MODE,
        "measurement_mode": MEASUREMENT_MODE,
        "terminal_measurement_refusal": False,
        "area_m2": area_m2,
        "polygon_pts": chosen["polygon_pts"],
        "regions": [chosen["polygon_pts"]],
        "zones": [zone],
        "zones_total_area_m2": area_m2,
        "measurement_state": state,
        "needs_assessor": True,
        "region_confidence": "low",
        "legend_found": True,
        "perimeter_measurement_allowed": False,
        "flags": flags,
        "light_fill_diagnostics": serialisable_diagnostics,
    }
