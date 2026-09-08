#!/usr/bin/env python3
"""prose metre tokens and BS standards citations are not scales.

Sections in this module (printed in this order):
  - [a metre token buried in prose is not a scale bar]
  - [standards citations are not scales]

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck

print("\n[a metre token buried in prose is not a scale bar]")
try:
    import os as _os_pb
    from scale import detect_scale_bar as _dsb_pb
    import takeoff_unmarked as _tu_pb

    # Three real sheets were being handed invented scale bars because detect_scale_bar paired a
    # prose "Nm" token to whatever line ran past it:
    #   0710/0711  "a 25m x 25m grid" in a construction note -> 25 m / 318 pt, 25 m / 43 pt
    #   092        "SPEC. CLAUSE No. 130L 130M"              -> read as 130 METRES
    # Each then disagreed with a good title block, so the sheet fell into MIXED/DISAGREE and
    # Inderjit had to set the scale by hand on a drawing that never had a bar.
    _pb_false = [
        ("0710", "drawings/inderjit_p6/6_31941-TTE-ZF-762-DR-C-0710-P01-Construction_Thicknessess_Plan.pdf", 0.176389),
        ("0711", "drawings/inderjit_p6/6_31941-TTE-ZF-762-DR-C-0711-P01-Construction_Thicknessess_Plan.pdf", 0.176389),
        ("092",  "drawings/inderjit_p7/7_25195-MJM-ZZ-ZZ-DR-S-2300-D2-P02-Mezzanine_Suspended_Slab_Layout.pdf", 0.070556),
    ]
    for _nm, _pp, _expect_k in _pb_false:
        if not _os_pb.path.exists(_pp):
            print(f"  [SKIP] {_nm} false-scale-bar guard — client fixture not present")
            continue
        _kb, _ib = _dsb_pb(_pp)
        ck(f"{_nm}: the prose 'Nm' token is no longer read as a scale bar",
           _kb is None, f"got k={_kb} from {_ib!r}")
        _tk, _tv, _tn, _ = _tu_pb.scale_for(_pp)
        ck(f"{_nm}: falls back to its title block cleanly, no MIXED/DISAGREE",
           abs(_tk - _expect_k) < 1e-4 and "DISAGREE" not in _tn, f"k={_tk} note={_tn[:80]}")
        # INVARIANT 3: removing a false bar must never manufacture a verified scale.
        ck(f"{_nm}: still UNVERIFIED — killing a false bar cannot verify a sheet",
           _tv is False, _tv)

    # Positive controls: a REAL bar must survive the prose filter untouched.
    for _nm, _pp in (("real SGP D77", "drawings/D77-REAL_D77_Hard_Landscaping.pdf"),
                     ("_int_d77", "drawings/_int_d77.pdf")):
        if not _os_pb.path.exists(_pp):
            print(f"  [SKIP] {_nm} real-scale-bar positive control — fixture not present")
            continue
        _kb, _ib = _dsb_pb(_pp)
        ck(f"{_nm}: its genuine scale bar still detected", _kb is not None, _ib)
        _tk, _tv, _tn, _ = _tu_pb.scale_for(_pp)
        ck(f"{_nm}: still VERIFIED by bar/title agreement", _tv is True, _tn[:90])
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] false-scale-bar regression — missing dependency or file: {_e}")

print("\n[standards citations are not scales]")
from takeoff_unmarked import _title_scale_denominators as _tsd, FOUR_DIGIT_SCALE_SERIES as _series
_bs_note = "75mm sand:cement screed to BS 8204 Part 1: 2003 on Rockwool RockFloor"
_title_line = "Construction Thicknessess Plan Sheet 2 S2 1:500 A1 17.08.26"
ck("a BS standard year is not accepted as a scale", _tsd(_bs_note) == [], _tsd(_bs_note))
ck("the real title-block 1:500 wins over the BS year on the same sheet",
   _tsd(f"{_bs_note} {_title_line}") == [500], _tsd(f"{_bs_note} {_title_line}"))
ck("the hyphenated standard form is rejected too (BS 8204-1:2003)",
   _tsd("to BS 8204-1:2003 on") == [], _tsd("to BS 8204-1:2003 on"))
ck("BS EN / ISO variants are rejected",
   _tsd("to BS EN 1234-1:2004 spec") == [] and _tsd("per ISO 9001 Part 2: 1999") == [])
for _s, _want in (("Site plan 1:2000", 2000), ("Masterplan 1:1500", 1500),
                  ("Layout 1:1250", 1250), ("Plan 1:1000", 1000), ("Detail 1:2500", 2500)):
    ck(f"real large scale {_want} still parses — banning years must not ban these",
       _tsd(_s) == [_want], _tsd(_s))
for _s, _want in (("Yard 1:500", 500), ("Plan 1:200", 200), ("Detail 1:75", 75)):
    ck(f"ordinary scale {_want} unaffected", _tsd(_s) == [_want], _tsd(_s))
# 1:2000 is BOTH a real drawing scale and a plausible year, so the series whitelist alone
# cannot separate them — the standards-citation strip has to carry that case. Assert the hard
# one directly rather than assuming the whitelist covers it.
ck("a standards citation quoting the year 2000 is still rejected, even though 1:2000 is a "
   "real scale the whitelist allows",
   _tsd("screed to BS 8204 Part 1: 2000 on insulation") == [],
   _tsd("screed to BS 8204 Part 1: 2000 on insulation"))
ck("...and a genuine 1:2000 site plan on the same sheet still wins",
   _tsd("screed to BS 8204 Part 1: 2000 on insulation. Site plan 1:2000") == [2000],
   _tsd("screed to BS 8204 Part 1: 2000 on insulation. Site plan 1:2000"))

# The real client sheet, when present (drawings/ is gitignored — skip cleanly if absent).
_p6 = Path("drawings/inderjit_p6/"
           "6_31941-TTE-ZF-762-DR-C-0711-P01-Construction_Thicknessess_Plan.pdf")
if _p6.exists():
    import fitz as _fitz_p6
    _txt_p6 = _fitz_p6.open(str(_p6))[0].get_text() or ""
    ck("Inderjit's real project-6 sheet now yields 1:500, not the BS year 2003",
       _tsd(_txt_p6)[:1] == [500], _tsd(_txt_p6)[:3])
else:
    print(f"  [SKIP] real project-6 sheet not present — {_p6}")


# ── a legend tint we cannot find is a REFUSAL, not a substitution ─────────────────────────
# Indurent Park (22513-RLL-3151): the legend names "C1 - Service Yard" and the surface is drawn
# as a stipple on white paper. We locked its tint, failed to find it, and answered by
# re-segmenting on a constant from a different client's site plans — landing on the car park.
# 98.6% of that sheet's own `RL_Surfacing_Service Yard` CAD items lie inside the client's own
# markup; 0.2% lie inside what we measured. IoU 0.0006. It quoted GBP 335,518 as yard concrete.
_ind = Path("drawings/inderjit_p9p10/11_Indurent_Park_Newport_22513-RLL-25-00-DR-C-3151"
            "_P02_Proposed_Pavement_Construction.pdf")
if _ind.exists():
    from takeoff_pipeline import takeoff as _tk_ind
    _r_ind = _tk_ind(str(_ind))
    _fl_ind = " ".join(_r_ind.get("flags") or [])
    # The refusal was correct and is now unnecessary: the surface is identified from the
    # engineer's own CAD layer instead. What must never come back is the substitution — the
    # number below has to come from `RL_Surfacing_Service Yard`, never from another client's
    # grey. "FELL BACK" appearing here would mean the guess is back.
    ck("a legend tint that cannot be found never substitutes another client's grey",
       "FELL BACK" not in _fl_ind, _fl_ind[:160])
    ck("...it identifies the surface from the CAD layer the engineer drew it on",
       "SURFACE FROM CAD LAYER" in _fl_ind
       and "RL_Surfacing_Service Yard" in _fl_ind,
       _fl_ind[:200])
    ck("...and it never reports the car park it used to report",
       str(_r_ind.get("area_m2")) not in ("6510.0", "2519.8"), str(_r_ind.get("area_m2")))
    # No scale bar on this sheet, so a measured number is the assessor's to verify, never
    # approvable on its own.
    ck("...capped at MEASURED_UNVERIFIED because the sheet carries no scale bar",
       _r_ind.get("area_m2") is not None
       and _r_ind.get("measurement_state") == "MEASURED_UNVERIFIED"
       and _r_ind.get("needs_assessor") is True,
       f"area={_r_ind.get('area_m2')} state={_r_ind.get('measurement_state')}")
    # An outline reconstructed by closing a stipple is an assumption about blank ground, and
    # it under-measures. Both must reach the assessor in metres, not be buried in a constant.
    ck("...and it discloses the bridge it closed and that the area is a floor",
       "OUTLINE BRIDGED" in _fl_ind and " m:" in _fl_ind and "AREA IS A FLOOR" in _fl_ind,
       _fl_ind[:200])
else:
    print(f"  [SKIP] Indurent sheet not present — {_ind}")

# The two gold sheets that legitimately depend on the generic grey convention reach it by the
# OTHER gates (swatch unreadable / no plausible region), which the refusal above must not touch.
for _gp, _want in (("drawings/real_sgp/D77_Hard_Landscaping.pdf", 3138.0),
                   ("drawings/_int_d77.pdf", 3159.0)):
    if Path(_gp).exists():
        from takeoff_pipeline import takeoff as _tk_g
        _rg = _tk_g(_gp)
        ck(f"the grey-convention gold sheet {Path(_gp).name} is untouched by that refusal",
           _rg.get("area_m2") == _want, f"{_rg.get('area_m2')} (want {_want})")
    else:
        print(f"  [SKIP] gold sheet not present — {_gp}")

# ── surface-identity doubt must survive EXPORT, not just reach the portal ─────────────────
# The Indurent quotation exported GBP 335,518.20 headed "EXTERNAL YARD SLABS", naming
# "Service Yard" four times, with no trace in txt/html/xlsx that the surface was never
# identified. A caveat the client document does not carry is not a caveat.
from quotation import (generate_quotation as _gq_sd, quotation_text as _qt_sd,
                       quotation_html as _qh_sd, SURFACE_DOUBT_LABEL as _SDL)
_fake_sd = {"file": "sheet.pdf", "type": "UNMARKED vector", "area_m2": 1000.0,
            "measurement_state": "MEASURED_UNVERIFIED", "needs_assessor": True,
            "scale_k": 0.1, "confidence": "low",
            "flags": ["legend/body colour DISAGREE: swatch (1,1,1), selected component dominant "
                      "RGB (255,255,255), max channel difference 254 > 5"],
            "brief_spec": {"depth_mm": 190, "mesh": "A252", "layers": 1, "conc_mix": "C32/40"}}
_q_sd = _gq_sd(_fake_sd, project="CI", client="CI")
ck("a surface-identity flag becomes a client-facing declaration",
   any(_SDL in d for d in _q_sd["declarations"]),
   str(_q_sd["declarations"])[:160])
ck("...and it reaches the exported text quotation",
   _SDL in _qt_sd(_q_sd), "not in txt")
ck("...and the exported html quotation",
   _SDL in _qh_sd(_q_sd), "not in html")

