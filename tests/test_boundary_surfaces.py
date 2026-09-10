#!/usr/bin/env python3
"""boundary_surfaces: closed CAD boundaries OFFERED, never chosen.

Sections in this module (printed in this order):
  - closed CAD boundaries offered as candidates

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck

print("closed CAD boundaries offered as candidates")
try:
    import math as _math_bs
    import fitz as _fitz_bs
    import boundary_surfaces as _bs

    # ── the vocabulary gate ───────────────────────────────────────────────────────────
    ck("a layer named for a surface qualifies",
       _bs._looks_like_a_surface("Zz_20_40_60-M_Onsite_Dock_(Rigid)"), "dock/rigid")
    # An underlay is not this engineer's statement about this scheme. On 2105 the
    # illustrative masterplan X-Ref supplied 278 of 709 "kerb" lines and trimming against
    # it moved nothing; the same underlay must never supply a priceable boundary either.
    ck("...but an illustrative masterplan X-Ref does NOT",
       not _bs._looks_like_a_surface(
           "X-Ref 7431 - 059 rev H Illustrative Masterplan|Proposed Concrete Yard"),
       "underlay rejected")
    ck("...nor OS mapping or a topographic survey",
       not _bs._looks_like_a_surface("OS Mapping|Topo Area - Road Or Track Fill"),
       "topo rejected")
    # Joints, kerblines and setting-out lines close into loops and are not the surface.
    # The corpus sweep found exactly these picked as the LARGEST loop on real sheets:
    # isolation joints on one drawing and a kerbline on Indurent.
    ck("...nor isolation joints, which close into loops but are not ground",
       not _bs._looks_like_a_surface("RPS-L-Hardstanding Isolation Joints (IJ)"),
       "joints rejected")
    ck("...nor a kerbline",
       not _bs._looks_like_a_surface("RL_Kerbline"), "kerbline rejected")

    # ── the Bezier trap, pinned ───────────────────────────────────────────────────────
    # Pushing a cubic's four CONTROL points into a polygon uses the control hull, which
    # lies outside the arc at a convex corner. That inflated LDSS2's dock loop by 23.9 m2
    # and turned a 0.05% under-read into a spurious 2.09% OVER -- the wrong direction.
    _q = _fitz_bs.Point
    _cubic = ("c", _q(0, 0), _q(0, 55.23), _q(44.77, 100), _q(100, 100))
    _pts = _bs._flatten(_cubic)
    ck("a cubic is FLATTENED, not hulled — every sample lies on the arc, not outside it",
       len(_pts) > 8
       and all(_math_bs.hypot(x - 100.0, y - 0.0) <= 100.5 for x, y in _pts),
       f"{len(_pts)} samples, max radius "
       f"{max(_math_bs.hypot(x - 100.0, y) for x, y in _pts):.2f} of 100")
    _hull_area = _bs._shoelace([(0, 0), (0, 55.23), (44.77, 100), (100, 100)])
    _flat_area = _bs._shoelace(_pts)
    ck("...and the hull would have over-read the corner, which is why we do not use it",
       _flat_area < _hull_area, f"flat {_flat_area:.1f} vs hull {_hull_area:.1f}")

    # ── the real sheet ────────────────────────────────────────────────────────────────
    _p_ldss2 = Path("drawings/aryan_10sep/LDSS2-01-DR-C-151-XX-ZZ-PVMT-ARP.pdf")
    if not _p_ldss2.exists():
        print("  [SKIP] LDSS2 not present — real-sheet checks need client drawings")
    else:
        _d = _fitz_bs.open(str(_p_ldss2))
        _k = (1.0 / 72.0) * 0.0254 * 250          # the sheet's own 1:250
        _got = _bs.find(_d[0], _k)
        ck("LDSS2 offers the closed boundaries its engineer drew",
           len(_got) >= 3, f"{len(_got)} candidates")
        ck("...every one of them actually closes",
           all(g["closing_gap_pt"] <= 1.0 for g in _got),
           f"max gap {max((g['closing_gap_pt'] for g in _got), default=0):.2f} pt")
        # The client confirmed on 10 Sep 2026 that P1 Heavy Duty Concrete Build-up is the
        # Dock (Rigid) layer. His traced outline shoelaces to 1,114.55 m2, matching his own
        # printed label to the centimetre.
        _dock = [g for g in _got if g["short_name"].endswith("Onsite_Dock_(Rigid)")]
        ck("...including the one the client confirmed is his concrete",
           bool(_dock), f"layers={[g['short_name'][-24:] for g in _got]}")
        if _dock:
            ck("...measured EXACTLY, not reconstructed — within 1% of his traced 1,114.55",
               abs(_dock[0]["area_m2"] - 1114.55) / 1114.55 < 0.01,
               f"{_dock[0]['area_m2']} vs 1114.55")
        # This is the whole reason the module offers instead of deciding: the biggest loop
        # on this sheet is the ROAD. A "largest wins" rule would price a carriageway.
        ck("...and the LARGEST loop is the carriageway, not the concrete — so size cannot "
           "be allowed to decide",
           _got[0]["short_name"].endswith("Onsite_Carriageway_(Flexible)"),
           f"largest={_got[0]['short_name'][-30:]} at {_got[0]['area_m2']} m2")
        # Nothing here may carry an "included" verdict. Identity is the assessor's call.
        ck("...and NOTHING is marked as chosen — the module returns candidates only",
           all("included" not in g and "chosen" not in g for g in _got),
           str(sorted(_got[0].keys())))

        # A sheet that already measures must not suddenly sprout candidates to click.
        _quiet = Path("drawings/inderjit_p9p10/11_Indurent_Park_Newport_22513-RLL-25-00-"
                      "DR-C-3151_P02_Proposed_Pavement_Construction.pdf")
        if _quiet.exists():
            _dq = _fitz_bs.open(str(_quiet))
            ck("a sheet that measures today is left alone — no boundaries offered on it",
               not _bs.find(_dq[0], 0.17638888888888887),
               f"{len(_bs.find(_dq[0], 0.17638888888888887))} offered on Indurent")

except (ImportError, FileNotFoundError) as _e_bs:
    print(f"  [SKIP] boundary surfaces — missing dependency or file: {_e_bs}")
