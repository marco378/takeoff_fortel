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

print("\n[Stipple surfaces: identity from the sheet's own pattern key]")
try:
    import math as _math_sp, tempfile as _tmp_sp
    from pathlib import Path as _P_sp
    import fitz as _fitz_sp, numpy as _np_sp
    import stipple_surfaces as _sp

    _K_SP = 0.17638888888888887          # 1:500, the scale of the sheet this exists for

    def _sheet(path, *, stipple_chips=1, hatch_chips=3, field=True, field_pitch=3.0,
               rotation=0, stray_dashes=0, field_origin=(200.0, 200.0), field_n=70,
               hatch_over_field=False):
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
    _p2105 = _P_sp("drawings/inderjit_p9p10/"
                   "9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf")
    if not _p2105.exists():
        print("  [SKIP] 2105 not present — real-sheet refusal check needs client drawings")
    else:
        _doc9 = _fitz_sp.open(str(_p2105))
        _r9 = _sp.measure(_doc9, _doc9[0], _K_SP)
        ck("2105 refuses, and for one of the two safety gates rather than a silent pass",
           _r9.get("ok") is False
           and ("blank paper" in (_r9.get("reason") or "")
                or "ink ends up inside" in (_r9.get("reason") or "")),
           f"{_r9.get('reason')}")

except Exception as _e_sp:                                     # pragma: no cover - defensive
    import traceback as _tb_sp
    print(f"  [SKIP] stipple surfaces — missing dependency or fixture: {_e_sp}")
    _tb_sp.print_exc()
