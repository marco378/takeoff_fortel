#!/usr/bin/env python3
"""takeoff_unmarked legend-anchored colour segmentation.

Sections in this module (printed in this order):
  - unmarked pipeline (legend-anchored colour segmentation)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import _FixtureNotPresent, _require_fixture, ck
from sanity import plausible

print("unmarked pipeline (legend-anchored colour segmentation)")
try:
    import numpy as _np
    from takeoff_unmarked import (segment_hatch, find_concrete_swatch_rgb,
                                   _choose_vector_swatch, _choose_raster_swatch,
                                   _is_plausible_surface_tint,
                                   _swatch_body_agrees)
    _im = _np.full((200, 300, 3), 255, _np.uint8); _im[50:150, 60:210] = (216, 216, 216)  # 100x150 grey
    _comp = segment_hatch(_im, (216, 216, 216))
    ck("segment grey hatch ~15,000 px", _comp is not None and abs(int(_comp.sum()) - 15000) < 900)
    ck("segment ignores white background", int(_comp.sum()) < 200 * 300 * 0.4)
    _px = int(_comp.sum()); _area = _px * (1 / 2.0) ** 2 * 0.1 * 0.1   # S=2 (1px=0.5pt), k=0.1 m/pt
    ck("unmarked area math (px->m2)", abs(_area - _px * 0.0025) < 1e-6)
    ck("white-segmentation blowup blocked by plausibility", len(plausible(279905)) >= 1)
    _w = segment_hatch(_im, (255, 0, 0))   # colour not present
    ck("absent hatch colour -> no region", _w is None or int(_w.sum()) == 0)

    print("client legend-swatch regressions (rotation, bright tints, pattern overlays)")
    import fitz as _fitz_swatch
    _swatch_pdf = "/tmp/_ci_rotated_bright_swatch.pdf"
    _sd = _fitz_swatch.open()
    _sp = _sd.new_page(width=400, height=600)
    # Text and chip are authored in unrotated PDF coordinates; get_pixmap() applies /Rotate.
    # The regression is the same coordinate-space mismatch as the real 270-degree sheet.
    _sp.insert_text((100, 450), "Concrete Service Yard", fontsize=12, rotate=90)
    _sp.draw_rect(_fitz_swatch.Rect(92, 300, 108, 340),
                  color=None, fill=(1.0, 180 / 255, 1.0))
    _sp.set_rotation(270)
    _sd.save(_swatch_pdf)
    _sd.close()
    with _fitz_swatch.open(_swatch_pdf) as _rd:
        _rp = _rd[0]
        _rpx = _rp.get_pixmap(matrix=_fitz_swatch.Matrix(2, 2), alpha=False)
        _rim = _np.frombuffer(_rpx.samples, _np.uint8).reshape(
            _rpx.height, _rpx.width, _rpx.n)[..., :3]
        _raw_bbox = _rp.search_for("Concrete Service Yard")[0]
        _rendered_bbox = _raw_bbox * _rp.rotation_matrix
        ck("rotated label bbox transformed into rendered-page coordinates",
           _rendered_bbox != _raw_bbox and _rendered_bbox.y1 <= _rp.rect.height)
    _rrgb, _rlabel = find_concrete_swatch_rgb(_swatch_pdf, im=_rim, S=2.0)
    ck("rotation-corrected raster swatch reads saturated bright tint",
       _rrgb == (255, 180, 255) and "concrete service yard" in _rlabel,
       f"rgb={_rrgb} label={_rlabel}")
    ck("surface-tint plausibility accepts bright hue and 239 tint, rejects ink/paper",
       _is_plausible_surface_tint((255, 180, 255)) and
       _is_plausible_surface_tint((239, 239, 240)) and
       not _is_plausible_surface_tint((0, 0, 0)) and
       not _is_plausible_surface_tint((255, 255, 255)))

    _vr = _fitz_swatch.Rect(10, 10, 40, 24)
    _overlay_candidates = [
        (2.0, (0, 0, 0), _fitz_swatch.Rect(_vr)),
        (2.1, (239, 239, 240), _fitz_swatch.Rect(_vr)),
    ]
    ck("co-located black pattern overlay yields to its non-black base tint",
       _choose_vector_swatch(_overlay_candidates) == (239, 239, 240))
    _two_row_patch = _np.full((20, 120, 3), 255, _np.uint8)
    _two_row_patch[:, 85:105] = (239, 239, 240)  # target chip nearest its label
    _two_row_patch[:, 0:50] = (156, 192, 207)    # larger neighbouring legend chip
    ck("wide raster window chooses nearest legend chip, not a larger neighbouring row",
       _choose_raster_swatch(_two_row_patch) == (239, 239, 240))
    ck("legend/body agreement is a strict gate, not an informational warning",
       _swatch_body_agrees((239, 239, 240), (238, 238, 240)) and
       not _swatch_body_agrees((239, 239, 240), (214, 214, 214)))
    _rgb_lock = _np.full((80, 120, 3), 255, _np.uint8)
    _rgb_lock[10:70, 10:55] = (239, 239, 240)
    _rgb_lock[10:70, 65:110] = (239, 220, 240)  # same R/B; G is outside the lock
    _strict_rgb = segment_hatch(_rgb_lock, (239, 239, 240), tol=5,
                                exclude_border=False, full_rgb=True)
    ck("swatch lock constrains every RGB channel, including near-grey tints",
       _strict_rgb is not None and int(_strict_rgb.sum()) < 60 * 55)

    print("team feedback fixes (DEMO4)")
    from takeoff_unmarked import drawing_style
    # (a) drawing-style guard: solid fill = colour-coded; thin lines = line/hatch (don't guess on engineer sheets)
    _solid = _np.full((300, 300, 3), 255, _np.uint8); _solid[40:260, 40:260] = (120, 170, 90)
    ck("colour-coded sheet detected", drawing_style(_solid)[0] == "colour-coded")
    _lines = _np.full((300, 300, 3), 255, _np.uint8)
    for _i in range(0, 300, 12):
        _lines[:, _i] = (80, 80, 80)
    ck("line/hatch sheet detected", drawing_style(_lines)[0] == "line/hatch")

    print("separate structural light-fill slab measurement")
    from structural_light_fill import detect_structural_light_fill as _detect_light_fill
    _light_text = (
        "MEZZANINE SUSPENDED SLAB LAYOUT\n"
        "COMPOSITE METAL DECK CONSTRUCTION"
    )
    _light_im = _np.full((600, 1200, 3), 255, _np.uint8)
    _light_im[100:400, 100:1000] = (235, 235, 235)
    _light_result = _detect_light_fill(
        _light_im, _light_text, scale_k=0.1, scale_verified=False, S=2.0)
    ck("near-white structural fill stays line/hatch under the unchanged legacy guard",
       drawing_style(_light_im)[0] == "line/hatch")
    ck("separate structural mode measures one locally solid light-fill plate",
       _light_result.get("area_m2") == 675.0 and
       _light_result.get("measurement_state") == "MEASURED_UNVERIFIED" and
       _light_result.get("needs_assessor") is True and
       _light_result.get("perimeter_measurement_allowed") is False and
       len(_light_result.get("polygon_pts") or []) >= 4,
       {key:_light_result.get(key) for key in (
           "area_m2", "measurement_state", "needs_assessor", "flags")})
    ck("light-fill appearance alone cannot activate the structural path",
       _detect_light_fill(
           _light_im, "GENERAL ARRANGEMENT PLAN", scale_k=0.1,
           scale_verified=False, S=2.0).get("applicable") is False)
    _ambiguous_light = _np.full((800, 1200, 3), 255, _np.uint8)
    _ambiguous_light[100:350, 100:500] = (235, 235, 235)
    _ambiguous_light[450:700, 700:1100] = (235, 235, 235)
    _ambiguous_result = _detect_light_fill(
        _ambiguous_light, _light_text, scale_k=0.1,
        scale_verified=False, S=2.0)
    ck("competing structural light-fill plates refuse instead of guessing",
       _ambiguous_result.get("area_m2") is None and
       _ambiguous_result.get("measurement_state") == "UNMEASURED" and
       _ambiguous_result.get("terminal_measurement_refusal") is True and
       any("ambiguous" in flag.lower() or "compete" in flag.lower()
           for flag in _ambiguous_result.get("flags", [])),
       _ambiguous_result)
    _no_scale_light = _detect_light_fill(
        _light_im, _light_text, scale_k=None, scale_verified=False, S=2.0)
    ck("resolved structural light-fill geometry emits no number without a scale",
       _no_scale_light.get("area_m2") is None and
       _no_scale_light.get("measurement_state") == "UNMEASURED" and
       any("no usable scale" in flag for flag in _no_scale_light.get("flags", [])),
       _no_scale_light)

    try:
        _mezz_pdf = (
            "drawings/inderjit_p7/"
            "7_25195-MJM-ZZ-ZZ-DR-S-2300-D2-P02-Mezzanine_Suspended_Slab_Layout.pdf"
        )
        _require_fixture(_mezz_pdf, "092 Mezzanine structural light-fill IoU test")
        _require_fixture("ground_truth_polygons.json",
                         "092 Mezzanine polygon ground truth")
        import json as _json_light
        from shapely.geometry import Polygon as _Polygon_light
        from takeoff_unmarked import takeoff as _takeoff_light
        _light_truth = _json_light.loads(
            Path("ground_truth_polygons.json").read_text()
        )["092_25195-MJM-ZZ-ZZ-DR-S-2300-D2-P02-Mezzanine_Suspended_Slab_Layout.pdf"]
        _mezz_result = _takeoff_light(_mezz_pdf)
        with _fitz_swatch.open(_mezz_pdf) as _mezz_doc:
            _mezz_page = _mezz_doc[_light_truth["page"]]
            _truth_rotated = [
                tuple(_fitz_swatch.Point(*point) * _mezz_page.rotation_matrix)
                for point in _light_truth["polygon_pts"]
            ]
        _truth_polygon = _Polygon_light(_truth_rotated)
        _measured_polygon = _Polygon_light(_mezz_result.get("polygon_pts") or [])
        _mezz_iou = (
            _measured_polygon.intersection(_truth_polygon).area /
            _measured_polygon.union(_truth_polygon).area
            if _measured_polygon.is_valid and not _measured_polygon.is_empty else 0.0
        )
        _mezz_diff_pct = abs(
            float(_mezz_result.get("area_m2") or 0) - _light_truth["area_m2"]
        ) / _light_truth["area_m2"] * 100
        ck("092 Mezzanine separate path finds the correct region by IoU",
           _mezz_iou >= _light_truth["min_iou"], round(_mezz_iou, 4))
        ck("092 Mezzanine gross area is within 2% without using truth in production code",
           _mezz_diff_pct <= 2.0,
           {"actual":_mezz_result.get("area_m2"),
            "truth":_light_truth["area_m2"], "diff_pct":round(_mezz_diff_pct, 3)})
        ck("092 Mezzanine is visibly separate and assessor-gated, never VERIFIED",
           _mezz_result.get("method") == "structural light-fill segmentation" and
           _mezz_result.get("measurement_state") == "MEASURED_UNVERIFIED" and
           _mezz_result.get("needs_assessor") is True and
           _mezz_result.get("scale_verified") is False and
           any("MEASUREMENT MODE: structural light-fill" in flag
               for flag in _mezz_result.get("flags", [])),
           {key:_mezz_result.get(key) for key in (
               "method", "area_m2", "measurement_state", "scale_verified")})
    except _FixtureNotPresent as _e:
        print(f"  [SKIP] {_e} — fixture not present")
    except (FileNotFoundError, KeyError) as _e:
        print(f"  [SKIP] 092 Mezzanine structural light-fill IoU test — "
              f"missing fixture data: {_e} — fixture not present")

    # (b) dock-bay/void fix: a large interior void is kept as a DEDUCTION, not filled (team: D77 dock bays)
    _v = _np.full((400, 400, 3), 255, _np.uint8); _v[40:360, 40:360] = (214, 214, 214); _v[150:250, 150:250] = 255
    _kept = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=1.0)   # void=6.25 m² > 1 -> kept out
    _fill = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=999)   # huge thresh -> filled
    ck("large interior void kept as deduction", int(_kept.sum()) < int(_fill.sum()))
    ck("void filled only when below threshold", int(_fill.sum()) - int(_kept.sum()) > 8000)

    print("polygon contour (fan/spoke regression)")
    import math as _math
    from takeoff_unmarked import _hatch_contour
    # Non-convex yard: rectangle with a deep notch cut from the top edge (loading dock).
    # The old angular-sort-from-centroid tracer produced spokes radiating across the slab
    # (lines from a corner) because rays from the centroid cross the boundary >2 times.
    # cv2.findContours walks the perimeter in order, so the outline must be clean.
    _cmp = _np.zeros((700, 1000), bool)
    _cmp[120:560, 160:840] = True
    _cmp[120:340, 480:680] = False          # deep top-edge notch -> strongly non-star-shaped
    _poly = _hatch_contour(_cmp, S=2.0, max_pts=80)
    ck("hatch contour returned", _poly is not None and len(_poly) >= 4)
    _xs = [q[0] for q in _poly]; _ys = [q[1] for q in _poly]
    # Bounding box must match the slab extent in PDF pt (mask px / S): x 80..420, y 60..280.
    ck("contour bbox matches slab extent",
       abs(min(_xs)-80) < 3 and abs(max(_xs)-420) < 3 and
       abs(min(_ys)-60) < 3 and abs(max(_ys)-280) < 3)
    # Perimeter sanity: true outer+notch boundary ~1440 pt. The fan bug inflated this to
    # ~2450+ pt (spokes shooting across the shape). Require it within ~25% of truth.
    _seg = [_math.hypot(_xs[i]-_xs[i-1], _ys[i]-_ys[i-1]) for i in range(1, len(_xs))]
    _seg.append(_math.hypot(_xs[0]-_xs[-1], _ys[0]-_ys[-1]))
    ck("contour perimeter not inflated by spokes", 1100 < sum(_seg) < 1800)
    # Spoke signature: count radius oscillations (near->far->near) about the centroid.
    # A clean traced outline has very few; the fan pattern had ~23/78.
    _cx = sum(_xs)/len(_xs); _cy = sum(_ys)/len(_ys)
    _rad = [_math.hypot(x-_cx, y-_cy) for x, y in zip(_xs, _ys)]
    _osc = sum(1 for i in range(1, len(_rad)-1)
               if (_rad[i] > _rad[i-1]) != (_rad[i+1] > _rad[i]))
    ck("no fan/spoke oscillation", _osc <= 6)
    # Plain convex rectangle -> exactly its 4 corners; degenerate masks -> None.
    _rect = _np.zeros((600, 800), bool); _rect[150:450, 200:650] = True
    ck("rectangle -> 4 corners", len(_hatch_contour(_rect, S=2.0)) == 4)
    ck("empty mask -> None", _hatch_contour(_np.zeros((40, 40), bool), S=2.0) is None)
    _tiny = _np.zeros((40, 40), bool); _tiny[10:12, 10:12] = True
    ck("sub-pixel blob -> None", _hatch_contour(_tiny, S=2.0) is None)
except ImportError as _e:
    print(f"  [SKIP] takeoff_unmarked tests — missing dependency: {_e}")

