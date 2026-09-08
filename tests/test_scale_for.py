#!/usr/bin/env python3
"""scale_for: scale bar vs title block, and when to refuse.

Sections in this module (printed in this order):
  - scale_for verification logic (scale bar vs title block)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from reportlab.pdfgen import canvas
from tests import ck

print("scale_for verification logic (scale bar vs title block)")
try:
    from takeoff_unmarked import (scale_for as _scale_for, SCALE_BAR_AGREE_TOL as _TOL,
                                  boundary_precision_risk as _boundary_precision_risk)
    import scale as _SC
    from sanity import (measurement_state as _precision_measurement_state,
                        MEASURED_UNVERIFIED as _PRECISION_UNVERIFIED)
    # PT_PER_M = 0.0254/72; k for 1:500 = 500 * PT_PER_M ≈ 0.176389 m/pt
    _PT_PER_M = 0.0254 / 72
    _k500 = 500 * _PT_PER_M   # ≈ 0.176389 m/pt

    # --- CASE 1: scale bar AGREES with title block (bar within ±3%) -> verified=True ---
    # Bar: 88 m / 500 pt = 0.176 m/pt; diff vs k500 ≈ 0.22% < 3%
    _c1 = canvas.Canvas("/tmp/_sf_agree.pdf", pagesize=(1400, 2200))
    _c1.drawString(100, 2100, "Drawing Scale 1:500")   # title-block text
    _c1.drawString(200, 120, "0          88 m")        # scale-bar label (88 m over 500 pt bar)
    _c1.line(100, 110, 600, 110)                        # 500 pt horizontal bar
    _c1.save()
    _k1, _v1, _n1, _src1 = _scale_for("/tmp/_sf_agree.pdf")
    ck("bar agrees with title -> verified=True",  _v1 is True, f"k={_k1:.5f} note={_n1[:60]}")
    ck("agree: bar in scale_sources",             "scale_bar" in _src1)
    ck("agree: title_block in scale_sources",     "title_block" in _src1)
    ck("agree: returned k close to bar",          _k1 is not None and abs(_k1 - 88/500) < 1e-6)

    # --- CASE 2: scale bar DISAGREES with title block (>3%) -> verified=False. Bar: 150 m / 500 pt
    # = 0.30 m/pt (implies ~1:850, an individually PLAUSIBLE drawing ratio) vs title k500 ≈ 70% off.
    # Both sources are plausible on their own -> this is the MIXED/DISAGREE branch (CLAUDE.md
    # invariant 3: disagreement -> refuse, don't auto-pick). Neither is silently adopted; the
    # title-block k is used for display and the assessor must set the scale explicitly.
    _c2 = canvas.Canvas("/tmp/_sf_disagree.pdf", pagesize=(1400, 2200))
    _c2.drawString(100, 2100, "Drawing Scale 1:500")
    _c2.drawString(200, 120, "0         150 m")
    _c2.line(100, 110, 600, 110)
    _c2.save()
    _k2, _v2, _n2, _src2 = _scale_for("/tmp/_sf_disagree.pdf")
    ck("bar disagrees with title -> verified=False", _v2 is False, f"k={_k2:.5f} note={_n2[:60]}")
    ck("disagree: note flags MIXED/DISAGREE",        "MIXED/DISAGREE" in _n2)
    ck("disagree: NOT auto-picked to bar k (title k used instead)",
       _k2 is not None and abs(_k2 - _k500) < 1e-6)

    # --- CASE 3: no scale bar, title block only -> verified=False ---
    _c3 = canvas.Canvas("/tmp/_sf_titleonly.pdf", pagesize=(1400, 2200))
    _c3.drawString(100, 2100, "Drawing Scale 1:500")   # title-block only, no bar line or label
    _c3.save()
    _k3, _v3, _n3, _src3 = _scale_for("/tmp/_sf_titleonly.pdf")
    ck("title-only -> verified=False",            _v3 is False, f"note={_n3[:60]}")
    ck("title-only: title_block in scale_sources", "title_block" in _src3)
    ck("title-only: no scale_bar in scale_sources", "scale_bar" not in _src3)
    ck("title-only: k close to k500",            _k3 is not None and abs(_k3 - _k500) < 1e-5)

    # --- CASE 4: bar DISAGREES with title AND the bar-implied ratio is IMPLAUSIBLE (false
    # scale-bar anchor, e.g. an unrelated dimension callout mis-paired to a nearby short line
    # fragment) -> reject the bar entirely, fall back to title-block k, still UNVERIFIED.
    # Reproduces the real corpus incident: Proposed_Gatehouse's "7016 m / 34 pt" bar candidate
    # implies k=205.868 m/pt (~1:583,563) which is nowhere near a real drawing scale.
    # Bar: 7016 m / 34 pt = 206.35 m/pt -> implied ~1:584,000, way outside 1:20-1:5000.
    _c4 = canvas.Canvas("/tmp/_sf_implausible.pdf", pagesize=(1400, 2200))
    _c4.drawString(100, 2100, "Drawing Scale 1:1250")
    _c4.drawString(200, 120, "0          7016 m")
    _c4.line(100, 110, 134, 110)                        # 34 pt bar
    _c4.save()
    _k4, _v4, _n4, _src4 = _scale_for("/tmp/_sf_implausible.pdf")
    _k1250 = 1250 * _PT_PER_M
    ck("implausible bar -> verified=False",        _v4 is False, f"k={_k4:.5f} note={_n4[:70]}")
    ck("implausible bar -> note says rejected",     "rejected as implausible" in _n4)
    ck("implausible bar -> falls back to title k",  _k4 is not None and abs(_k4 - _k1250) < 1e-5)
    ck("implausible bar -> sources still recorded", "scale_bar" in _src4 and "title_block" in _src4)

    # --- CASE 5: bar DISAGREES with title but BOTH are individually plausible drawing ratios
    # (e.g. a genuine 1:2500 site-location viewport vs a stale 1:1500 title block) -> MIXED/
    # DISAGREE. Must NOT auto-pick either side; verified stays False; title k shown for display.
    # Reproduces the real corpus incident: Site_Location_Plan's "100 m / 113 pt" bar (k=0.882,
    # ~1:2500 — a perfectly plausible ratio) disagreeing with the sheet's stated title 1:1500.
    _c5 = canvas.Canvas("/tmp/_sf_mixed.pdf", pagesize=(1400, 2200))
    _c5.drawString(100, 2100, "Drawing Scale 1:1500")
    _c5.drawString(200, 120, "0          100 m")
    _c5.line(100, 110, 213, 110)                        # 113 pt bar -> k=0.885 (~1:2504, plausible)
    _c5.save()
    _k5, _v5, _n5, _src5 = _scale_for("/tmp/_sf_mixed.pdf")
    _k1500 = 1500 * _PT_PER_M
    ck("mixed/disagree -> verified=False",          _v5 is False, f"k={_k5:.5f} note={_n5[:70]}")
    ck("mixed/disagree -> note says MIXED/DISAGREE", "MIXED/DISAGREE" in _n5)
    ck("mixed/disagree -> returns title k (no auto-pick of bar)",
       _k5 is not None and abs(_k5 - _k1500) < 1e-5)
    ck("mixed/disagree -> sources still recorded",  "scale_bar" in _src5 and "title_block" in _src5)

    # --- CASE 6: multiple viewport scales.  A bar agreeing with one printed denominator
    # does not prove that denominator belongs to the slab viewport being segmented.  Inderjit
    # clarified on 7 Aug that each layout can carry its own scale; without a spatial
    # bar/title/region association the sheet must remain assessor-gated.
    _c6 = canvas.Canvas("/tmp/_sf_multiple_viewports.pdf", pagesize=(1400, 2200))
    _c6.drawString(100, 2100, "GROUND FLOOR LAYOUT  Scale 1:500")
    _c6.drawString(800, 2100, "SECTION DETAIL  Scale 1:100")
    _c6.drawString(200, 120, "0          88 m")
    _c6.line(100, 110, 600, 110)
    _c6.save()
    _k6, _v6, _n6, _src6 = _scale_for("/tmp/_sf_multiple_viewports.pdf")
    ck("multiple viewport scales cannot be globally VERIFIED without spatial association",
       _v6 is False and "MULTIPLE VIEWPORT SCALES" in _n6 and
       _src6.get("title_block_candidates") == [500, 100],
       {"verified":_v6, "note":_n6, "sources":_src6})

    _c7 = canvas.Canvas("/tmp/_sf_a0_nts.pdf", pagesize=(1400, 2200))
    _c7.drawString(100, 2100, "SHEET SIZE A0   SCALE AS INDICATED   NTS")
    _c7.save()
    _k7, _v7, _n7, _src7 = _scale_for("/tmp/_sf_a0_nts.pdf")
    ck("A0 is sheet size and NTS never becomes a numeric scale",
       _k7 is None and _v7 is False and _src7 == {} and "no scale" in _n7,
       {"k":_k7, "verified":_v7, "note":_n7, "sources":_src7})

    _risk_1500 = _boundary_precision_risk({"title_block":{"denom":1500}})
    _risk_2000 = _boundary_precision_risk({"title_block":{"denom":2000}})
    ck("1:1500 and 1:2000 sheets carry a visible boundary-click precision risk",
       "1:1500" in _risk_1500 and "no numeric adjustment" in _risk_1500 and
       "1:2000" in _risk_2000)
    ck("ordinary 1:1000 sheet does not gain the large-denominator precision flag",
       _boundary_precision_risk({"title_block":{"denom":1000}}) is None)
    ck("large-denominator risk caps an otherwise verified number at assessor-gated state",
       _precision_measurement_state(
           1000, scale_verified=True,
           confidence="low" if _risk_1500 else None)[0] == _PRECISION_UNVERIFIED)

except ImportError as _e:
    print(f"  [SKIP] scale_for tests — missing dependency: {_e}")

