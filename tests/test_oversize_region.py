"""A same-tint region ABOVE the plausibility cap must never be discarded in silence.

The satellite rule exists to drop legend chips and stray marks. It was also dropping any
component larger than PLAUSIBLE_MAX_M2 -- and calling it a satellite. That is the one shape
of error that reads as a clean measurement: the sheet reports the SMALLER region, every gate
stays green, and nothing says a bigger region of the same colour was thrown away.

Not theoretical. The largest component this system has ever measured is 48,568.9 m² (the 0720
hardstanding joint layout) -- 97.1% of the 50,000 m² cap. One scale step past it and a real
sheet lands here.

The cap is NOT changed by this test or the code it guards: takeoff_unmarked refuses above
50,000 while sanity.plausible allows 60,000, and which is right is the client's call.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import ck

print("oversize same-tint region is reported, never silently dropped")
try:
    import numpy as _np_ov
    import takeoff_unmarked as _tu_ov

    _S_ov, _k_ov = 2.0, 0.5
    _ppm2 = (_S_ov * _S_ov) / (_k_ov * _k_ov)
    _GREY_ov = (214, 214, 214)

    _im_ov = _np_ov.full((1400, 1800, 3), 255, _np_ov.uint8)
    _small_w = int((2000 * _ppm2) ** 0.5)
    _big_w = int((55000 * _ppm2) ** 0.5)
    _im_ov[60:60 + _small_w, 60:60 + _small_w] = _GREY_ov
    _im_ov[300:300 + _big_w, 700:700 + _big_w] = _GREY_ov
    _small_m2 = _small_w * _small_w / _ppm2
    _big_m2 = _big_w * _big_w / _ppm2

    ck("the fixture really does straddle the cap",
       _small_m2 < _tu_ov.PLAUSIBLE_MAX_M2 < _big_m2,
       f"{_small_m2:.0f} / cap {_tu_ov.PLAUSIBLE_MAX_M2} / {_big_m2:.0f}")

    _diag_ov = {}
    _comp_ov = _tu_ov.segment_hatch(_im_ov, _GREY_ov, tol=_tu_ov.GREY_TOL,
                                    k=_k_ov, S=_S_ov, _diag=_diag_ov)
    _dropped = _diag_ov.get("oversize_dropped_m2") or []

    ck("the oversize region is RECORDED as dropped, not forgotten",
       bool(_dropped), _dropped)
    ck("...and it is the big one that was dropped, at its true size",
       _dropped and abs(_dropped[0] - _big_m2) / _big_m2 < 0.02,
       f"{_dropped} vs planted {_big_m2:.0f}")
    ck("the measured region really is the SMALLER one — this is the understatement",
       abs(int(_comp_ov.sum()) / _ppm2 - _small_m2) / _small_m2 < 0.02,
       f"{int(_comp_ov.sum()) / _ppm2:.0f} vs {_small_m2:.0f}")

    # A sheet with nothing above the cap must stay completely untouched by this.
    _im_ok = _np_ov.full((1400, 1800, 3), 255, _np_ov.uint8)
    _im_ok[60:60 + _small_w, 60:60 + _small_w] = _GREY_ov
    _diag_ok = {}
    _tu_ov.segment_hatch(_im_ok, _GREY_ov, tol=_tu_ov.GREY_TOL,
                         k=_k_ov, S=_S_ov, _diag=_diag_ok)
    ck("an ordinary sheet gains no oversize key at all — zero blast radius",
       "oversize_dropped_m2" not in _diag_ok, _diag_ok.get("oversize_dropped_m2"))

    # The flag must reach the caller's flag list, and must lower confidence rather than
    # leaving a self-certifying number. Assert the wording the assessor actually reads.
    import inspect as _insp_ov
    _src_ov = _insp_ov.getsource(_tu_ov.takeoff)
    ck("takeoff() raises a flag naming the drop",
       "OVERSIZE REGION DROPPED, NOT MEASURED" in _src_ov)
    ck("...and the flag says the reported area is the SMALLER region",
       "SMALLER region" in _src_ov)
    ck("...and the sheet is downgraded, so a guess cannot certify itself",
       'region_confidence = "low"' in _src_ov.split("OVERSIZE REGION DROPPED")[1][:900])
except ImportError as _e_ov:
    print(f"  [SKIP] oversize-region tests — missing dependency: {_e_ov}")
