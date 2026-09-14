#!/usr/bin/env python3
"""A schedule of constructions is not a contradiction.

Radlett WP5 Surface Finishes Plan 0700/0701 lists four slab thicknesses — 180, 200, 225 and
375 mm — each naming its own construction and its own build-up reference. The spec reader
called that a SPEC CONFLICT, and on 14 Sep I repeated the claim to Aryan: that the sheet
contradicted itself. It does not; multi-thickness is normal on a terminal this size.

Two things are guarded here:
  - the classification: enumerated-with-own-build-up reads as a SCHEDULE, while genuine
    cross-sheet disagreement and values with no build-up of their own stay a CONFLICT;
  - the quote printed beside each value, which used to be a raw window spanning neighbouring
    legend rows — the 90 characters shown next to "180 mm" were the row that says 225mm,
    which is precisely how the misread happened.

Routing does not change either way: nothing is assumed, and the assessor still picks.

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import inspect
from pathlib import Path
from reportlab.pdfgen import canvas
from tests import _FixtureNotPresent, _require_fixture, ck

print("spec schedule vs spec conflict")
import takeoff_pipeline
from takeoff_pipeline import _is_enumerated_schedule

_LEGEND = (
    "Intermodal Terminal HGV Slab Construction 200mm thick "
    "Refer to RAD-BWB-A1EX-IT1-DD-C-1010 "
    "Intermodal Terminal HGV Slab Construction 225mm thick (Channelised Traffic along Slab Edge) "
    "Refer to RAD-BWB-A1EX-IT1-DD-C-1010 "
    "Intermodal Terminal Container Slab Construction 375mm thick "
    "Refer to RAD-BWB-A1EX-IT1-DD-C-1020-1023"
)
_schedule_records = [
    {"value": value, "file": "sfp.pdf", "page": 1, "text": _LEGEND}
    for value in (200, 225, 375)
]
ck("a legend enumerating constructions, each with its own build-up, is a SCHEDULE",
   _is_enumerated_schedule("depth_mm", _schedule_records) is True)

_cross_sheet = [dict(record) for record in _schedule_records]
_cross_sheet[0]["file"] = "other_sheet.pdf"
ck("two sheets disagreeing is still a CONFLICT, not a schedule",
   _is_enumerated_schedule("depth_mm", _cross_sheet) is False)

_no_buildup = [
   {"value": 180, "file": "s.pdf", "page": 1,
    "text": "slab 180mm thick generally and 225mm thick at the edge"},
   {"value": 225, "file": "s.pdf", "page": 1,
    "text": "slab 180mm thick generally and 225mm thick at the edge"},
]
ck("thicknesses with no build-up reference of their own stay a CONFLICT",
   _is_enumerated_schedule("depth_mm", _no_buildup) is False)

ck("a single stated thickness is neither",
   _is_enumerated_schedule("depth_mm", _schedule_records[:1]) is False)
ck("the schedule reading is scoped to slab thickness only",
   _is_enumerated_schedule("mesh", _schedule_records) is False)

_source = inspect.getsource(takeoff_pipeline)
ck("the CONFLICT wording survives for the genuine case",
   "SPEC CONFLICT — " in _source and "SPEC SCHEDULE — " in _source)
ck("the schedule flag says it is not a contradiction, and who picks",
   "not a contradiction" in _source and
   "the assessor selects the construction being priced" in _source)
ck("the printed quote is re-centred on the value it sits beside",
   'rf"{value}\\s*mm"' in _source and "stated.start() - 70" in _source)

_radlett = Path("drawings/radlett_wp5/wp5_24.pdf")
try:
    _require_fixture(_radlett, "Radlett WP5 Surface Finishes Plan not present")
    _result = takeoff_pipeline.takeoff(str(_radlett), send_approval=False)
    _spec_flags = [flag for flag in (_result.get("flags") or []) if "SPEC " in flag]
    _flag = _spec_flags[0] if _spec_flags else ""
    ck("the real Surface Finishes Plan reads as a schedule, not a conflict",
       _flag.startswith("SPEC SCHEDULE") and "SPEC CONFLICT" not in _flag, _flag[:200])
    ck("...naming all four constructions it lists",
       "lists 4 constructions" in _flag, _flag[:200])
    ck("...with each quote carrying its own thickness, not the neighbouring row's",
       all(f"{value}mm thick" in _flag.split(f"{value} mm “")[1][:120]
           for value in (180, 200, 225, 375)), _flag)
    ck("...and the sheet still refuses and still routes to an assessor",
       _result.get("measurement_state") == "UNMEASURED" and
       _result.get("needs_assessor") is True and
       bool(_result.get("spec_conflicts")),
       {"state": _result.get("measurement_state"),
        "needs_assessor": _result.get("needs_assessor")})
except _FixtureNotPresent as _e:
    print(f"  [SKIP] Radlett schedule guard — {_e} — fixture not present")
