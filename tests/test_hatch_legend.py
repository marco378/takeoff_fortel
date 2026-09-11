#!/usr/bin/env python3
"""hatch-legend surfaces: a sheet that is only hatch plus linework.

Sections in this module (printed in this order):
  - [hatch-legend surfaces: a sheet that is only hatch + linework, with a hatch legend]

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck

print("\n[hatch-legend surfaces: a sheet that is only hatch + linework, with a hatch legend]")
try:
    import os as _os_hl, json as _json_hl, tempfile as _tmp_hl, math as _math_hl
    import fitz as _fitz_hl, cv2 as _cv_hl, numpy as _np_hl
    import takeoff_unmarked as _tu_hl
    import hatch_legend_raster as _hlr

    # The tier is its own tuple — never appended to the yard/dock-apron vocabularies (appending
    # DOCK_APRON_LABELS onto CONCRETE_LABELS regressed two live project-6 sheets on 2 Sep).
    _hl_terms = [t for _, terms, _, _ in _hlr.HATCH_LEGEND_SURFACES for t in terms]
    ck("hatch-legend tier is separate from YARD_LABELS / DOCK_APRON_LABELS / CONCRETE_LABELS",
       not any(t in _tu_hl.CONCRETE_LABELS for t in _hl_terms), _hl_terms)
    ck("drawing_style defaults are untouched by this path",
       _tu_hl.drawing_style.__defaults__ == (233, 0.03), _tu_hl.drawing_style.__defaults__)

    def _hl_synth(path, legend=True, two=False, scale="1:500", spacing=9.0, angle_deg=45):
        """A1 landscape sheet: hatched rectangle(s) of black 45-deg strokes + optional legend
        row with a boxed stroke-pattern chip LEFT of the label, plus a title-block scale."""
        doc = _fitz_hl.open(); pg = doc.new_page(width=2384, height=1684)
        sh = pg.new_shape()
        def hatch(x0, y0, x1, y1):
            sh.draw_rect(_fitz_hl.Rect(x0, y0, x1, y1)); sh.finish(color=(0, 0, 0), width=0.72)
            # 45-deg strokes clipped to the rect by construction
            c = y0 - x1
            while c < y1 - x0:
                pts = []
                for (X, Y) in ((x0, x0 + c), (x1, x1 + c), (y0 - c, y0), (y1 - c, y1)):
                    if x0 - 1e-6 <= X <= x1 + 1e-6 and y0 - 1e-6 <= Y <= y1 + 1e-6: pts.append((X, Y))
                if len(pts) >= 2:
                    a, b = sorted(set(pts))[0], sorted(set(pts))[-1]
                    sh.draw_line(_fitz_hl.Point(*a), _fitz_hl.Point(*b)); sh.finish(color=(0, 0, 0), width=0.72)
                c += spacing * _math_hl.sqrt(2)
        hatch(300, 400, 1100, 1000)                       # 800 x 600 pt
        if two:
            hatch(1100 + spacing * 1.5 * _math_hl.sqrt(2) * 0 + 40, 400, 1500, 1000)   # corridor perpendicular to strokes ~40 pt
        if legend:
            hatch(1650, 300, 1690, 314)                    # the chip: a boxed stroke pattern LEFT of the label
            pg.insert_text((1700, 311), "CONCRETE SLAB FOR YARD WITH:", fontsize=9)
            pg.insert_text((1700, 325), "A393 MESH AT 11 MSA", fontsize=7)
        pg.insert_text((1900, 1600), "Scale: " + scale, fontsize=10)
        pg.insert_text((1900, 1620), "EXTERNAL WORKS", fontsize=10)
        sh.commit(); doc.save(path); doc.close()

    _hl_dir = _tmp_hl.mkdtemp(prefix="hatch_legend_")
    _p_yes = _os_hl.path.join(_hl_dir, "hatch_legend_yes.pdf"); _hl_synth(_p_yes, legend=True)
    _p_no  = _os_hl.path.join(_hl_dir, "hatch_legend_no.pdf");  _hl_synth(_p_no,  legend=False)
    _p_two = _os_hl.path.join(_hl_dir, "hatch_legend_two.pdf"); _hl_synth(_p_two, legend=True, two=True)
    _k500 = 500 * 0.0254 / 72
    _r_yes = _tu_hl.takeoff(_p_yes, source="engineer")
    _exp_m2 = 800 * 600 * _k500 * _k500
    _z_yes = [z for z in (_r_yes.get("zones") or []) if z.get("category") == "external_yard"]
    ck("synthetic hatch-only sheet WITH a hatch legend is admitted and measured",
       _r_yes.get("measurement_state") == "MEASURED_UNVERIFIED" and _z_yes,
       f"{_r_yes.get('measurement_state')} zones={[(z.get('category'), z.get('area_m2')) for z in _r_yes.get('zones') or []]}")
    _a_yes = sum(z.get("area_m2") or 0 for z in _z_yes)
    ck("...within 5% of the drawn rectangle", abs(_a_yes - _exp_m2) / _exp_m2 <= 0.05, f"{_a_yes:.1f} vs {_exp_m2:.1f}")
    _fl_yes = _r_yes.get("flags") or []
    ck("...the path names itself", any("MEASUREMENT MODE: hatch-legend" in f for f in _fl_yes))
    ck("...it states no native boundary corroborates the extent", any("NO NATIVE CLOSED BOUNDARY" in f for f in _fl_yes))
    ck("...it never claims a verified scale", _r_yes.get("scale_verified") is False and _r_yes.get("needs_assessor") is True)
    _r_no = _tu_hl.takeoff(_p_no, source="engineer")
    ck("the SAME sheet without the legend row is refused exactly as before (NON-COLOUR-CODED)",
       _r_no.get("area_m2") is None and _r_no.get("measurement_state") == "UNMEASURED"
       and any("NON-COLOUR-CODED" in f for f in _r_no.get("flags") or []), _r_no.get("measurement_state"))
    _r_two = _tu_hl.takeoff(_p_two, source="engineer")
    # The killer case from the earlier workflow: two surfaces separated by a paper corridor must
    # NOT be closed into one region. Parts live inside the zone (one zone per legend spec), so
    # count the parts, not the zones.
    _z_two = [z for z in (_r_two.get("zones") or []) if z.get("category") == "external_yard"]
    _two_polys = [poly for z in _z_two for poly in (z.get("region_polygons") or [])]
    _two_areas = sorted((a for z in _z_two for a in (z.get("region_areas_m2") or [])), reverse=True)
    ck("two hatched rectangles across a clean corridor emit TWO parts, not one fused region",
       len(_two_polys) >= 2 and len(_two_areas) >= 2,
       f"parts={[round(a) for a in _two_areas]} polys={[len(p) for p in _two_polys]}")
    _exp_two = sorted([800 * 600 * _k500 * _k500, 360 * 600 * _k500 * _k500], reverse=True)
    ck("...and each part measures its own rectangle, so nothing was bridged across the corridor",
       len(_two_areas) >= 2 and all(abs(_two_areas[i] - _exp_two[i]) / _exp_two[i] <= 0.05 for i in range(2)),
       f"{[round(a, 1) for a in _two_areas[:2]]} vs {[round(a, 1) for a in _exp_two]}")
    ck("...every drawn part carries its own polygon and its own quantity (marked-up PDF labels each)",
       len(_two_polys) == len(_two_areas) and all(len(p) >= 3 for p in _two_polys))

    # The marked-up PDF is what the client actually looks at. A multi-part zone must reach it as
    # several outlines each labelled with its own quantity, and a loop-shaped surface must reach
    # it with its hole cut, or the drawing claims an extent the number never included.
    from marked_pdf import _overlay_manifest as _om_hl
    _hl_zone = {
        "zone_key": "unclassified:hatch-legend-road", "category": "unclassified", "area_m2": 1000.0,
        "region_polygons": [[[100, 100], [400, 100], [400, 400], [100, 400]], [[500, 100], [600, 100], [600, 200], [500, 200]]],
        "region_ids": ["road-part-1", "road-part-2"], "region_areas_m2": [700.0, 300.0],
        "region_holes": [[[[200, 200], [300, 200], [300, 300], [200, 300]]], []],
    }
    _hl_man = _om_hl({"id": "hl", "result": {"pdf_path": _p_yes, "scale_k": _k500, "zones": [_hl_zone]}},
                     Path(_p_yes), 0, None)
    _hl_regions = _hl_man["geometry"]["regions"]; _hl_cuts = _hl_man["geometry"]["cutouts"]
    ck("marked-up PDF: each part of a zone is its own outline with its own quantity",
       len(_hl_regions) == 2 and sorted(r["area_m2"] for r in _hl_regions) == [300.0, 700.0],
       [(r["region_id"], r["area_m2"]) for r in _hl_regions])
    ck("marked-up PDF: a loop-shaped surface is drawn with its hole cut out",
       len(_hl_cuts) == 1 and _hl_cuts[0]["assessor_supplied"] is False,
       [(c["cutout_id"], c["assessor_supplied"]) for c in _hl_cuts])
    ck("marked-up PDF: the single-outline zones every other path emits are unchanged",
       len(_om_hl({"id": "hl2", "result": {"pdf_path": _p_yes, "scale_k": _k500,
                                           "zones": [{"zone_key": "dock", "category": "dock", "area_m2": 930.0,
                                                      "polygon_pts": [[10, 10], [90, 10], [90, 90], [10, 90]]}]}},
                  Path(_p_yes), 0, None)["geometry"]["regions"]) == 1)

    # ── Real sheet, scored by IoU against Aryan's markup (never by area alone) ──────────────
    _p8 = "drawings/inderjit_p8/8_14173-TCG-XX_XX-XX-SK-C-0003_CONCRETE_SLAB_MSA.pdf"
    _gt_key = "8_14173-TCG-XX_XX-XX-SK-C-0003_CONCRETE_SLAB_MSA.pdf"
    if not (_os_hl.path.exists(_p8) and _os_hl.path.exists("ground_truth_polygons.json")):
        print(f"  [SKIP] project-8 hatch-legend gold — client fixture not present ({_p8})")
    else:
        _e = _json_hl.loads(Path("ground_truth_polygons.json").read_text())[_gt_key]
        _S = 2.0; _doc = _fitz_hl.open(_p8); _pg = _doc[0]; _R = _pg.rotation_matrix
        _pix = _pg.get_pixmap(matrix=_fitz_hl.Matrix(_S, _S))
        _im = _np_hl.frombuffer(_pix.samples, _np_hl.uint8).reshape(_pix.height, _pix.width, _pix.n)[:, :, :3].copy()
        _H, _W = _im.shape[:2]
        def _rot(pts): return _np_hl.array([[(_fitz_hl.Point(x, y) * _R).x * _S, (_fitz_hl.Point(x, y) * _R).y * _S] for x, y in pts], _np_hl.int32)
        def _fill(arr):
            m = _np_hl.zeros((_H, _W), _np_hl.uint8); _cv_hl.fillPoly(m, [arr], 1); return m.astype(bool)
        # The ONLY documented truth on this sheet is Aryan's combined markup (road + yard in one
        # figure, 60,426 m²). There is no human road/yard split, so nothing here scores against
        # a per-surface mask of our own construction — that would be marking our own homework.
        _net = _fill(_rot(_e["polygon_pts"])) & ~_fill(_rot(_e["cutout_polygons_pts"][0]))
        _r8 = _tu_hl.takeoff(_p8, source="engineer")
        ck("p8: measured as assessor-gated prefill, not refused",
           _r8.get("measurement_state") == "MEASURED_UNVERIFIED" and _r8.get("needs_assessor") is True, _r8.get("measurement_state"))
        _zs = _r8.get("zones") or []
        _cats = {z.get("category") for z in _zs}
        ck("p8: road and yard are two zones with two legend specs", {"external_yard", "unclassified"} <= _cats,
           [(z.get("category"), z.get("subjects")) for z in _zs])

        def _part_masks(z, frame):
            """Every outline the assessor is actually shown for this zone, holes cut out."""
            out = []
            polys = list(z.get("region_polygons") or []) or ([z["polygon_pts"]] if len(z.get("polygon_pts") or []) >= 3 else [])
            holes = list(z.get("region_holes") or [])
            conv = (lambda pts: _rot(pts)) if frame == "rot" else (
                lambda pts: _np_hl.array([[x * _S, y * _S] for x, y in pts], _np_hl.int32))
            for i, pts in enumerate(polys):
                if len(pts or []) < 3: continue
                m = _fill(conv(pts))
                for h in (holes[i] if i < len(holes) else []) or []:
                    if len(h or []) >= 3: m &= ~_fill(conv(h))
                out.append(m)
            return out
        def _zone_mask(z, frame):
            u = _np_hl.zeros((_H, _W), bool)
            for m in _part_masks(z, frame): u |= m
            return u
        def _union(frame):
            u = _np_hl.zeros((_H, _W), bool)
            for z in _zs: u |= _zone_mask(z, frame)
            return u
        def _iou(a, b): return (a & b).sum() / max((a | b).sum(), 1)
        _by_frame = {f: _iou(_union(f), _net) for f in ("rot", "asis")}
        _iou_union = max(_by_frame.values())
        ck("p8: the measured surfaces TOGETHER match Aryan's markup (IoU >= 0.90)",
           _iou_union >= 0.90, f"IoU {_iou_union:.4f} {_by_frame}")
        ck("p8: the polygons live in exactly one coordinate frame (the other scores near zero)",
           min(_by_frame.values()) <= 0.30, f"{_by_frame}")

        # The ring test. A road drawn as a loop around the yard has an outer contour that
        # encloses the yard too: before holes were cut, the road's outline covered 58,177 m²
        # beside its own 23,460 m² number, and the two zones' outlines overlapped by 1.8%.
        _frame = max(_by_frame, key=_by_frame.get)
        _zm = {z["category"]: _zone_mask(z, _frame) for z in _zs}
        _ov = (_zm["external_yard"] & _zm["unclassified"]).sum()
        _smaller = max(min(_zm["external_yard"].sum(), _zm["unclassified"].sum()), 1)
        ck("p8: the yard and road outlines do not overlap (a loop-shaped road must carry its hole)",
           _ov / _smaller < 0.01, f"{_ov / _smaller * 100:.2f}% of the smaller outline")
        _px_m2 = (float(_e["k_m_per_pt"]) / _S) ** 2 if _e.get("k_m_per_pt") else (0.352778 / _S) ** 2
        for _z in _zs:
            _drawn = _zone_mask(_z, _frame).sum() * _px_m2
            ck(f"p8: what is drawn for {_z['category']} equals what is claimed for it (within 5%)",
               abs(_drawn - (_z.get("area_m2") or 0)) / max(_z.get("area_m2") or 1, 1) <= 0.05,
               f"drawn {_drawn:,.1f} m² vs stated {_z.get('area_m2'):,.1f} m²")
        # Swap guard: each surface must contain the ink its OWN legend chip declared. The yard
        # chip on this sheet is black strokes and the road chip is magenta; if the chip-to-region
        # association ever crossed, the two areas would swap silently and both would still look
        # plausible. Scored on ink, not on area.
        _mag8 = _np_hl.all(_np_hl.abs(_im.astype(_np_hl.int16) - _np_hl.array([254, 16, 254], _np_hl.int16)) <= 60, axis=2)
        _dark8 = _im.max(axis=2) < 90
        _yard_m8, _road_m8 = _zm["external_yard"], _zm["unclassified"]
        ck("p8: the yard holds the black hatch its chip declared, not the road's magenta",
           _dark8[_yard_m8].mean() > 0.02 and _mag8[_yard_m8].mean() < 0.005,
           f"dark {_dark8[_yard_m8].mean() * 100:.2f}% magenta {_mag8[_yard_m8].mean() * 100:.2f}%")
        ck("p8: the road holds the magenta hatch its chip declared, and holds most of it",
           _mag8[_road_m8].mean() > 0.02 and _mag8[_road_m8].sum() / max(_mag8.sum(), 1) > 0.75,
           f"magenta {_mag8[_road_m8].mean() * 100:.2f}% of the road, "
           f"{_mag8[_road_m8].sum() / max(_mag8.sum(), 1) * 100:.1f}% of all magenta on the sheet")
        _parts8 = list(_r8.get("yard_regions") or []) + list(_r8.get("road_regions") or [])
        ck("p8: every part that carries an outline reproduces its own quantity within 15%",
           all((r.get("outline_fidelity") is None) or r["outline_fidelity"] <= 0.15 for r in _parts8),
           [(r["region_id"], r.get("outline_fidelity"), bool(r.get("polygon_pts"))) for r in _parts8])
        _sum = sum(z.get("area_m2") or 0 for z in _zs)
        ck("p8: zones sum to Aryan's combined 60,426 within 5%", abs(_sum - 60426) / 60426 <= 0.05, f"{_sum:.1f}")
        # The portal stores each job with json.dumps. A result carrying numpy masks would take
        # the whole job store down on the first sheet that reached this path — a crash, i.e. a
        # fifth state the four-state contract does not have.
        try:
            _json_hl.dumps(_r8); _hl_serialises = True; _hl_ser_err = ""
        except TypeError as _te:
            _hl_serialises = False; _hl_ser_err = str(_te)[:90]
        ck("p8: the whole result serialises to JSON, the way the portal stores a job",
           _hl_serialises, _hl_ser_err)
        _fl8 = _r8.get("flags") or []
        ck("p8: flags name the path, the missing native boundary and the assessor gate",
           any("MEASUREMENT MODE: hatch-legend" in f for f in _fl8) and any("NO NATIVE CLOSED BOUNDARY" in f for f in _fl8)
           and any("assessor must confirm" in f for f in _fl8))
        _doc.close()

    # ── Regression guards: sheets on other paths must be byte-identical (captured from the
    #    unpatched code earlier this session).
    for _gp, _ga, _gs in (("drawings/real_sgp/D77_Hard_Landscaping.pdf", 3138.0, "MEASURED_UNVERIFIED"),
                          # 11 Sep: was MEASURED_VERIFIED. This sheet's surface identity comes
                          # from the SGP grey CONSTANT because its legend chip cannot be read --
                          # a guess. _choose_surface_band now returns "low" on that branch like
                          # its siblings, so the guess is gated instead of certified. The AREA is
                          # what this guard protects and it has not moved.
                          ("drawings/_int_d77.pdf", 3159.0, "MEASURED_UNVERIFIED"),
                          ("drawings/inderjit_p7/7_25195-MJM-00-00-DR-C-9000-D2-P04-External_Works_Layout.pdf", 9762.0, "MEASURED_UNVERIFIED"),
                          ("drawings/inderjit_p7/7_25195-MJM-ZZ-ZZ-DR-S-2300-D2-P02-Mezzanine_Suspended_Slab_Layout.pdf", 1189.8, "MEASURED_UNVERIFIED")):
        if not _os_hl.path.exists(_gp):
            print(f"  [SKIP] hatch-legend regression guard — fixture not present ({_gp})"); continue
        _rg = _tu_hl.takeoff(_gp, source="engineer")
        ck(f"unchanged by the hatch-legend path: {_os_hl.path.basename(_gp)[:40]} = {_ga} {_gs}",
           _rg.get("area_m2") == _ga and _rg.get("measurement_state") == _gs,
           f"{_rg.get('area_m2')} {_rg.get('measurement_state')}")
except (ImportError, FileNotFoundError, KeyError) as _e:
    print(f"  [SKIP] hatch-legend regression — missing dependency or file: {_e}")

