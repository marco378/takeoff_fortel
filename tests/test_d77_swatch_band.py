#!/usr/bin/env python3
"""takeoff_unmarked swatch-locked grey band vs the ancillary-concrete strip.

Sections in this module (printed in this order):
  - D77 swatch-locked grey band vs 'Footpaths (ancillary): Concrete' annexation (Aryan field report: real SGP sheet measured 3,172 vs Smita gold 3,156 — root cause was the generic 214±14 grey band admitting a darker, adjacent ancillary-concrete legend colour and binary_closing fusing it into the yard's own connected component)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import _FixtureNotPresent, _require_fixture, ck
from sanity import MEASURED_UNVERIFIED, MEASURED_VERIFIED

print("D77 swatch-locked grey band vs 'Footpaths (ancillary): Concrete' annexation "
      "(Aryan field report: real SGP sheet measured 3,172 vs Smita gold 3,156 — root cause was "
      "the generic 214±14 grey band admitting a darker, adjacent ancillary-concrete legend "
      "colour and binary_closing fusing it into the yard's own connected component)")
try:
    _require_fixture("drawings/_int_d77.pdf", "D77 swatch-locked grey band test")
    import fitz as _fitz_fp
    from takeoff_unmarked import (takeoff as _tu_takeoff_fp, segment_hatch as _seg_fp,
                                   PLAUSIBLE_MIN_M2 as _PMIN_FP, PLAUSIBLE_MAX_M2 as _PMAX_FP)

    def _gen_d77_footpath(out_path, chip_grey=0.878, yard_grey=0.878):
        """Same D77 yard geometry (page 1067.766x824.854pt, scale bar, 1:250 title) plus:
          - a darker 'Footpaths (ancillary): Concrete' strip (204 grey) sitting 0.65pt below
            the yard's bottom edge — close enough for binary_closing (any close>=2) to bridge,
            reproducing the real-sheet CONNECTED over-measure (not a satellite blob).
          - a legend swatch chip + label 'Concrete Service Yard construction' (readable by
            find_concrete_swatch_rgb) so the swatch-lock path engages.
          - a second, non-matching legend line 'Footpaths (ancillary): Concrete' with its own
            (darker) swatch chip — must NOT be picked up as the concrete-yard label anchor.
        Title text deliberately avoids CONCRETE_LABELS substrings (unlike drawings/_int_d77.pdf,
        whose title text IS the label match and has no nearby swatch chip -> unreadable swatch,
        which is why that fixture stays on the generic-band fallback path untouched by this fix).
        """
        d = _fitz_fp.open()
        W, H = 1067.7659912109375, 824.853515625
        pg = d.new_page(width=W, height=H)
        pg.insert_text((130.0, 80), "PROPOSED HARD LANDSCAPING - UNIT 1 SITE PLAN    Scale 1:250",
                       fontsize=13)
        pg.draw_line(_fitz_fp.Point(130.0, 714.853515625), _fitz_fp.Point(696.9290771484375, 714.853515625),
                     color=(0, 0, 0), width=2.0)
        pg.insert_text((126.0, 731), "0", fontsize=11)
        pg.insert_text((678.9, 731), "50 m", fontsize=11)

        yg = (yard_grey, yard_grey, yard_grey)
        pg.draw_rect(_fitz_fp.Rect(130.0, 120.0, 937.765625, 624.853515625),
                     color=(0, 0, 0), fill=yg, width=1.0)

        # Ancillary footpath strip: 230x9pt = 16.1 m² at k=0.08819, darker grey (204), 0.65pt
        # gap below the yard's own bottom edge (bridged by binary_closing regardless of the
        # exact close value, same mechanism as the real sheet's kerb-line gap).
        strip_grey = (0.80, 0.80, 0.80)
        pg.draw_rect(_fitz_fp.Rect(350.0, 625.503515625, 580.0, 634.503515625),
                     color=None, fill=strip_grey, width=0)

        # Legend: matching swatch chip + label (concrete-yard anchor).
        cg = (chip_grey, chip_grey, chip_grey)
        pg.draw_rect(_fitz_fp.Rect(330.0, 762.0, 360.0, 776.0), color=(0, 0, 0), fill=cg, width=0.5)
        pg.insert_text((400.0, 772.0), "Concrete Service Yard construction", fontsize=9)

        # Second legend line: non-matching label + its own (darker) swatch chip — must not be
        # mistaken for the concrete-yard anchor, and is small/isolated -> satellite-dropped.
        pg.draw_rect(_fitz_fp.Rect(330.0, 784.0, 360.0, 796.0), color=(0, 0, 0), fill=strip_grey, width=0.5)
        pg.insert_text((400.0, 792.0), "Footpaths (ancillary): Concrete", fontsize=9)

        d.save(out_path)
        d.close()

    _p_fp = "drawings/_int_d77_footpath.pdf"
    _gen_d77_footpath(_p_fp)

    # BEFORE: old generic-band segmentation (direct segment_hatch call, mirroring the borders
    # test's "WITHOUT exclusion" pattern) — proves the annexation is real and CONNECTED (not
    # something the satellite-fraction filter would already have dropped).
    _pgfp = _fitz_fp.open(_p_fp)[0]
    _pixfp = _pgfp.get_pixmap(matrix=_fitz_fp.Matrix(2.0, 2.0))
    import numpy as _np_fp
    _imfp = _np_fp.frombuffer(_pixfp.samples, _np_fp.uint8).reshape(_pixfp.height, _pixfp.width, _pixfp.n)[..., :3]
    _k_fp = 0.08819
    _comp_old_fp = _seg_fp(_imfp, (214, 214, 214), k=_k_fp, S=2.0, exclude_border=True)
    _area_old_fp = round(int(_comp_old_fp.sum()) * (1 / 2.0) ** 2 * _k_fp * _k_fp, 0)
    ck("BEFORE fix (generic 214 band): footpath strip annexed, area > 3,159 + 10 m² "
       "(connected over-measure, not a dropped satellite)",
       _area_old_fp > 3159.0 + 10, f"got {_area_old_fp}")

    # AFTER: full takeoff() with the swatch-lock fix — flags show the lock, area back to gold.
    _r_fp = _tu_takeoff_fp(_p_fp)
    _area_fp = _r_fp.get("area_m2")
    ck("AFTER fix: swatch (224ish) LOCKED — footpath strip excluded, area within 0.5% of 3,159 m²",
       _area_fp is not None and abs(_area_fp - 3159.0) / 3159.0 <= 0.005, f"got {_area_fp}")
    ck("AFTER fix: flags show the swatch-locked band",
       any("LOCKED" in f for f in _r_fp.get("flags", [])), _r_fp.get("flags"))
    ck("AFTER fix: measurement_state MEASURED_VERIFIED",
       _r_fp.get("measurement_state") == MEASURED_VERIFIED, _r_fp.get("measurement_state"))

    # DEMO-4 REGRESSION GUARD: swatch reads far enough from the yard's own fill (232 vs 214)
    # that the locked band [218,246] misses the 214 yard entirely -> must FALL BACK, never
    # silently return area=None on a perfectly measurable sheet.
    _p_fp_d4 = "/tmp/_ci_d77_footpath_demo4.pdf"
    _gen_d77_footpath(_p_fp_d4, chip_grey=0.910, yard_grey=0.84)
    _r_fp_d4 = _tu_takeoff_fp(_p_fp_d4)
    ck("DEMO-4 GUARD: swatch-locked band misses the yard fill -> FELL BACK (flag present)",
       any("FELL BACK" in f for f in _r_fp_d4.get("flags", [])), _r_fp_d4.get("flags"))
    ck("DEMO-4 GUARD: fallback still produces a measurable area (never area=None)",
       _r_fp_d4.get("area_m2") is not None, _r_fp_d4.get("area_m2"))
    ck("DEMO-4 GUARD: low-confidence fallback is MEASURED_UNVERIFIED (measurable, approve-blocked)",
       _r_fp_d4.get("measurement_state") == MEASURED_UNVERIFIED
       and _r_fp_d4.get("needs_assessor") is True,
       f"state={_r_fp_d4.get('measurement_state')} needs_assessor={_r_fp_d4.get('needs_assessor')}")

    # GOLD GUARDS unchanged: both pre-existing synthetic fixtures have unreadable swatches
    # (title text IS the label match, no nearby swatch chip) -> always take the fallback path,
    # golds untouched by this change.
    _d77_regress = _tu_takeoff_fp("drawings/_int_d77.pdf")
    ck("GOLD GUARD: _int_d77.pdf still exactly 3,159 m² (swatch-lock did not touch it)",
       _d77_regress.get("area_m2") == 3159.0, _d77_regress.get("area_m2"))
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 swatch-locked grey band test — missing dependency or file: {_e}")

