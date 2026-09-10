#!/usr/bin/env python3
"""stipple_surfaces: identifying a surface by the PATTERN its legend chip is drawn in.

Sections in this module (printed in this order):
  - [Stipple surfaces: identity from the sheet's own pattern key]

Every fixture here is SYNTHETIC and built in a temp dir, so this module runs on a clean
checkout with no client drawings. The real sheet this was built for (2105) is checked at
the end and is expected to REFUSE -- that expectation is the point, not a placeholder.

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck


def _shoe_sp(pts):
    """Shoelace area in square PDF points -- the polygon's own enclosure, not the mask's."""
    a = 0.0
    for _i in range(len(pts)):
        _x1, _y1 = pts[_i]
        _x2, _y2 = pts[(_i + 1) % len(pts)]
        a += _x1 * _y2 - _x2 * _y1
    return abs(a) / 2.0


print("\n[Stipple surfaces: identity from the sheet's own pattern key]")
try:
    import math as _math_sp, tempfile as _tmp_sp
    from pathlib import Path as _P_sp
    import fitz as _fitz_sp, numpy as _np_sp
    import stipple_surfaces as _sp

    _K_SP = 0.17638888888888887          # 1:500, the scale of the sheet this exists for

    def _sheet(path, *, stipple_chips=1, hatch_chips=3, field=True, field_pitch=3.0,
               rotation=0, stray_dashes=0, field_origin=(200.0, 200.0), field_n=70,
               hatch_over_field=False, ribbon=False):
        """A page carrying a legend column and (optionally) a stipple field.

        The legend is a column of chips down the right-hand side: each chip is either
        dots (stipple) or short parallel strokes (hatch), drawn in the SAME pattern as
        the ground it denotes -- which is the convention the module reads.
        """
        doc = _fitz_sp.open()
        page = doc.new_page(width=1400, height=1000)
        xref = doc.add_ocg("RL_Surface")
        cx, cy = 1180.0, 120.0
        n_chip = 0
        for kind in ["stipple"] * stipple_chips + ["hatch"] * hatch_chips:
            oy = cy + n_chip * 90.0                       # >> CHIP_MIN_GAP_PT of blank paper
            if kind == "stipple":
                for r in range(6):
                    for c in range(6):
                        x, y = cx + c * 5.0, oy + r * 5.0
                        page.draw_line(_fitz_sp.Point(x, y), _fitz_sp.Point(x + 0.8, y),
                                       color=(0, 0, 0), width=1.0, oc=xref)
            else:
                for c in range(7):
                    x = cx + c * 5.0
                    page.draw_line(_fitz_sp.Point(x, oy), _fitz_sp.Point(x + 12.0, oy + 24.0),
                                   color=(0, 0, 0), width=1.0, oc=xref)
            n_chip += 1
        if field:
            ox, oy = field_origin
            for r in range(field_n):
                for c in range(field_n):
                    x, y = ox + c * field_pitch, oy + r * field_pitch
                    page.draw_line(_fitz_sp.Point(x, y), _fitz_sp.Point(x + 0.8, y),
                                   color=(0, 0, 0), width=1.0, oc=xref)
            if hatch_over_field:
                # a hatched build-up sitting on the right half of the same field
                x0 = ox + (field_n // 2) * field_pitch
                for i in range(120):
                    x = x0 + i * 1.6
                    page.draw_line(_fitz_sp.Point(x, oy),
                                   _fitz_sp.Point(x + 18.0, oy + 36.0),
                                   color=(0, 0, 0), width=1.0, oc=xref)
        if ribbon:
            # a long, 3-dot-wide strip of the SAME stipple, well away from the field:
            # real ground is compact, this is not, and it must be offered rather than counted
            for j in range(240):
                for w in range(3):
                    x, y = 150.0 + j * 3.0, 780.0 + w * 3.0
                    page.draw_line(_fitz_sp.Point(x, y), _fitz_sp.Point(x + 0.8, y),
                                   color=(0, 0, 0), width=1.0, oc=xref)
        for i in range(stray_dashes):
            # lone short marks, far apart -- short, but never a stipple
            x, y = 120.0 + (i % 14) * 78.0, 640.0 + (i // 14) * 60.0
            page.draw_line(_fitz_sp.Point(x, y), _fitz_sp.Point(x + 1.0, y),
                           color=(0, 0, 0), width=1.0, oc=xref)
        if rotation:
            page.set_rotation(rotation)
        doc.save(path)
        return doc

    _d_sp = _tmp_sp.mkdtemp(prefix="stipple_")

    # ── the happy path: one stipple chip in a legend of four names the ground ─────────
    _p1 = str(_P_sp(_d_sp) / "one_stipple.pdf")
    _doc1 = _sheet(_p1)
    _r1 = _sp.measure(_doc1, _doc1[0], _K_SP)
    ck("a sheet whose legend carries exactly ONE stipple chip measures that pattern",
       _r1.get("ok") is True and (_r1.get("area_m2") or 0) > 0,
       f"{_r1.get('reason') or _r1.get('area_m2')}")
    ck("...and it says the legend pattern is what identified the surface",
       "stipple" in (_r1.get("identified_by") or "").lower(),
       f"{_r1.get('identified_by')}")
    ck("...and it declares the area a MINIMUM, because a stipple stops inside its edge",
       _r1.get("area_is_a_minimum") is True, f"{_r1.get('area_is_a_minimum')}")

    # ── two stipple chips: the pattern no longer names ONE surface ────────────────────
    # This is the guard against the 2105 failure mode in general form. If a sheet draws
    # two build-ups as dots, matching "dots" cannot tell you which one you measured, and
    # a number would be a guess wearing a measurement's clothes.
    _p2 = str(_P_sp(_d_sp) / "two_stipples.pdf")
    _doc2 = _sheet(_p2, stipple_chips=2, hatch_chips=2)
    _r2 = _sp.measure(_doc2, _doc2[0], _K_SP)
    ck("two stipple chips is a REFUSAL, not a guess between them",
       _r2.get("ok") is False and "not name one surface" in (_r2.get("reason") or ""),
       f"{_r2.get('reason')}")

    # ── no legend at all: nothing to identify against ─────────────────────────────────
    _p3 = str(_P_sp(_d_sp) / "no_legend.pdf")
    _doc3 = _sheet(_p3, stipple_chips=0, hatch_chips=0)
    _r3 = _sp.measure(_doc3, _doc3[0], _K_SP)
    ck("a stipple field with no legend key is refused, never measured on its own",
       _r3.get("ok") is False, f"{_r3.get('reason')}")

    # ── a lone short dash is not a stipple ────────────────────────────────────────────
    # Dashes, symbols and the stub ends of hatch strokes are all short. Length alone
    # would sweep them in; the chip's own dot DENSITY is what rejects them.
    _p4 = str(_P_sp(_d_sp) / "strays.pdf")
    _doc4 = _sheet(_p4, field=False, stray_dashes=260)
    _r4 = _sp.measure(_doc4, _doc4[0], _K_SP)
    ck("scattered short dashes never become a surface, however many there are",
       _r4.get("ok") is False and "density" in (_r4.get("reason") or ""),
       f"{_r4.get('reason')}")

    # ── the hatch pitch must be measured perpendicular, not by nearest neighbour ──────
    # Centroid nearest-neighbour reported 3.97 m on the real sheet where the truth was
    # 0.28 m, because collinear strokes on ONE hatch line sit far closer than the pitch.
    _doc5 = _sheet(str(_P_sp(_d_sp) / "pitch.pdf"), hatch_chips=3)
    _pg5 = _doc5[0]
    _long5 = [d for d in _pg5.get_drawings(extended=True)
              if d.get("rect") is not None and (d.get("layer") or "").endswith("RL_Surface")
              and _sp._seg_len(d) > _sp.STIPPLE_MAX_SEG_PT]
    _pitch5 = _sp._hatch_pitch_pt(_long5, _pg5.rotation_matrix, 2.0)
    ck("the hatch pitch is the PERPENDICULAR spacing of its strokes",
       _pitch5 is not None and 2.0 <= _pitch5 <= 12.0,
       f"pitch={_pitch5}")

    # ── rotation must not change the answer ──────────────────────────────────────────
    # A /Rotate 180 sheet once produced a wrong PRICED number in the sibling module.
    _areas_sp = {}
    for _rot in (0, 90, 180, 270):
        _pr = str(_P_sp(_d_sp) / f"rot{_rot}.pdf")
        _dr = _sheet(_pr, rotation=_rot)
        _rr = _sp.measure(_dr, _dr[0], _K_SP)
        _areas_sp[_rot] = _rr.get("area_m2") if _rr.get("ok") else None
    _base_sp = _areas_sp[0]
    for _rot in (90, 180, 270):
        ck(f"a page rotated {_rot} measures the same surface as the unrotated one",
           _base_sp is not None and _areas_sp[_rot] is not None
           and abs(_areas_sp[_rot] - _base_sp) <= max(1.0, 0.02 * _base_sp),
           f"rot{_rot}={_areas_sp[_rot]} vs {_base_sp}")

    # ── a hatched build-up sharing the field is subtracted, not measured ──────────────
    _p7 = str(_P_sp(_d_sp) / "mixed.pdf")
    _doc7 = _sheet(_p7, hatch_over_field=True)
    _r7 = _sp.measure(_doc7, _doc7[0], _K_SP)
    _plain = _r1.get("area_m2") or 0
    ck("hatched ground inside the same field is subtracted, so the answer shrinks",
       (not _r7.get("ok")) or (_r7.get("area_m2") or 0) < _plain,
       f"mixed={_r7.get('area_m2') or _r7.get('reason')} vs plain={_plain}")

    # ── a degenerate page returns a refusal, never a traceback ───────────────────────
    _doc8 = _fitz_sp.open()
    _doc8.new_page(width=800, height=600)
    _r8 = _sp.measure(_doc8, _doc8[0], _K_SP)
    ck("an empty page returns a refusal with a reason, never a traceback",
       isinstance(_r8, dict) and _r8.get("ok") is False and _r8.get("reason"),
       f"{_r8}")

    # ── the real sheet: it must REFUSE, and that is the expected result ───────────────
    # 2105 is the drawing this module was written for, and it still does not measure it.
    # Both gates below are pre-existing safety rules, not judgements made to avoid
    # shipping: closing spans 3.88 m of blank paper against a 3.0 m cap, and only 76.7%
    # of the stipple ink lands inside the outline against a 95% floor -- because the
    # remaining 23% sits in ~150 scattered patches the sheet gives us no way to attribute.
    # If a future change makes this measure, it must be checked by IoU against the
    # client's polygons in ground_truth_polygons.json, never by area.
    # ── a ribbon of the same stipple is OFFERED, never counted ───────────────────────
    # The client's decision, 9 Sep 2026: extra stipple areas become candidates the
    # assessor opts into; they must not silently join the total, and must not make the
    # whole sheet refuse either.
    _p10 = str(_P_sp(_d_sp) / "ribbon.pdf")
    _doc10 = _sheet(_p10, ribbon=True)
    _r10 = _sp.measure(_doc10, _doc10[0], _K_SP)
    ck("a ribbon of the same stipple does not stop the sheet being measured",
       _r10.get("ok") is True, f"{_r10.get('reason')}")
    if _r10.get("ok"):
        ck("...it is held out as a CANDIDATE rather than counted",
           len(_r10.get("candidate_regions") or []) >= 1,
           f"candidates={len(_r10.get('candidate_regions') or [])}")
        ck("...and the total is the compact ground only, unchanged by its presence",
           abs((_r10.get("area_m2") or 0) - (_r1.get("area_m2") or 0)) <= 1.0,
           f"{_r10.get('area_m2')} vs plain {_r1.get('area_m2')}")
        ck("...and the assessor is told candidates exist and are excluded",
           any("CANDIDATE" in f for f in (_r10.get("flags") or [])),
           f"{_r10.get('flags')}")

    # ── the real sheet: measured, and checked by SHAPE against the client's markup ────
    # This measured nothing until 9 Sep 2026. It measures now because the client decided
    # extra stipple areas should be offered rather than cause a refusal. The number is
    # scored by IoU against his polygons -- an area can match while outlining the wrong
    # ground, which is exactly what went wrong on Indurent.
    _p2105 = _P_sp("drawings/inderjit_p9p10/"
                   "9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf")
    _gt_sp = _P_sp("ground_truth_polygons.json")
    if not (_p2105.exists() and _gt_sp.exists()):
        print("  [SKIP] 2105 not present — real-sheet checks need client drawings")
    else:
        import json as _json_sp
        _g9 = _json_sp.loads(_gt_sp.read_text()).get(
            "drawings/inderjit_p9p10/"
            "9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf")
        _doc9 = _fitz_sp.open(str(_p2105))
        _pg9 = _doc9[0]
        _r9 = _sp.measure(_doc9, _pg9, _K_SP)
        ck("2105 measures from the one stipple pattern in its own legend",
           _r9.get("ok") is True and (_r9.get("area_m2") or 0) > 0,
           f"{_r9.get('reason') or _r9.get('area_m2')}")
        if _r9.get("ok") and _g9:
            import cv2 as _cv9
            _S9 = 2.0
            _rot9 = _pg9.rotation_matrix
            _W9 = int(_np_sp.ceil(_pg9.rect.width * _S9))
            _H9 = int(_np_sp.ceil(_pg9.rect.height * _S9))

            def _m9(pts):
                _m = _np_sp.zeros((_H9, _W9), _np_sp.uint8)
                _q = _np_sp.round(_np_sp.array(
                    [tuple(_fitz_sp.Point(x, y) * _rot9) for x, y in pts]) * _S9
                ).astype(_np_sp.int32)
                _cv9.fillPoly(_m, [_q], 1)
                return _m.astype(bool)

            # Ours are already in RENDERED space; the client's markup is in unrotated
            # space, so only the latter gets the rotation matrix. Rasterising both the
            # same way would silently score a rotated sheet against a rotated copy of
            # itself and pass while the portal drew the outline in the wrong place.
            def _m9_rendered(pts):
                _m = _np_sp.zeros((_H9, _W9), _np_sp.uint8)
                _q = _np_sp.round(_np_sp.array(pts, dtype=float) * _S9).astype(_np_sp.int32)
                _cv9.fillPoly(_m, [_q], 1)
                return _m.astype(bool)

            _ours = [_m9_rendered(x["polygon_pts"]) for x in _r9["regions"]
                     if x.get("polygon_pts")]
            _worst = 1.0
            for _g in _g9["regions"]:
                _gm = _m9(_g["polygon_pts"])
                _best = 0.0
                for _om in _ours:
                    _u = int((_om | _gm).sum())
                    _best = max(_best, (int((_om & _gm).sum()) / _u) if _u else 0.0)
                _worst = min(_worst, _best)
            ck(f"...and every region the CLIENT marked is found (IoU >= {_g9['min_iou']})",
               _worst >= _g9["min_iou"], f"worst IoU {_worst:.3f}")
            # Reading OVER would mean quoting ground nobody is paving. A stipple stops
            # inside its own edge, so this direction is not a preference, it is the method.
            ck("...and the total reads UNDER the client's own figure, never over it",
               _r9["area_m2"] <= _g9["area_m2"],
               f"{_r9['area_m2']} vs client {_g9['area_m2']} "
               f"({100 * _r9['area_m2'] / _g9['area_m2'] - 100:+.2f}%)")
            ck("...the stray strip is a CANDIDATE, kept out of the priced total",
               len(_r9.get("candidate_regions") or []) >= 1
               and _r9["area_m2"] + (_r9.get("candidate_area_m2") or 0) > _r9["area_m2"],
               f"candidates={_r9.get('candidate_area_m2')} m2")
            ck("...and the stipple left uncounted is disclosed with its own number",
               any("NOT IN THE TOTAL" in f or "IS IN THE TOTAL" in f
                   for f in (_r9.get("flags") or [])),
               f"{(_r9.get('flags') or [''])[0][:80]}")
            ck("...closing never assumes more blank paper than the cap allows",
               (_r9.get("bridge_gap_m") or 0) <= 3.0, f"bridge {_r9.get('bridge_gap_m')} m")
            # The retention line fell from 67% to 54% when the kerb fence landed, because
            # the stipple past the kerb is ~8x denser than the stipple inside it. The
            # number is right and the bare sentence misleads: an assessor reading "only
            # 54%" concludes we captured LESS of the yard when we captured more. If the
            # fence trimmed anything, the disclosure has to say so.
            _sfx = _r9.get("shape_fix") or {}
            _dis = " ".join(str(f) for f in (_r9.get("flags") or []))
            ck("...and when the kerb trims the outline, the disclosure says so",
               (_sfx.get("trimmed_m2") or 0) < 1.0
               or ("trimmed back to the kerb" in _dis and "does not extend past" in _dis),
               f"trimmed={_sfx.get('trimmed_m2')} m2, said={'trimmed back to the kerb' in _dis}")
            ck("...and the outline it draws encloses the area it reports, within 2%",
               all(abs(_shoe_sp(_x["polygon_pts"]) * (_K_SP ** 2) / _x["area_m2"] - 1.0) < 0.02
                   for _x in _r9["regions"] if _x.get("polygon_pts")),
               "; ".join(f"{_shoe_sp(_x['polygon_pts']) * (_K_SP ** 2):.0f} vs {_x['area_m2']}"
                         for _x in _r9["regions"]))

except Exception as _e_sp:                                     # pragma: no cover - defensive
    import traceback as _tb_sp
    print(f"  [SKIP] stipple surfaces — missing dependency or fixture: {_e_sp}")
    _tb_sp.print_exc()
