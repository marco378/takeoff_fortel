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
ck("a thickness legend does not accidentally read as a mesh schedule",
   _is_enumerated_schedule("mesh", _schedule_records) is False)

_source = inspect.getsource(takeoff_pipeline)
ck("the CONFLICT wording survives for the genuine case",
   "SPEC CONFLICT — " in _source and "SPEC SCHEDULE — " in _source)
ck("the schedule flag says it is not a contradiction, and who picks",
   "not a contradiction" in _source and
   "the assessor selects the construction being priced" in _source)
ck("the printed quote is re-centred on the value it sits beside",
   "stated.start() - lead" in _source and "_SCHEDULE_ENTRY_PATTERNS.get(field)" in _source)
ck("...with a lead chosen per field, since a thickness is named before it and a mesh inside it",
   '"depth_mm": (r"{value}\\s*mm\\s+thick", 80, 70)' in _source and
   '"mesh": (r"additional\\s+layer\\s+of\\s+{value}\\b", 140, 5)' in _source)

_MESH = (
    "Day Joint with 1200mm restriction either side of joint. "
    "Additional layer of A393 mesh to be provided 1.2m either side of joint - refer to "
    "details on drawing RAD-BWB-A1EX-IT1-DD-C-1020 "
    "Additional layer of A252 mesh along edge of slab, refer to RAD-BWB-A1EX-IT1-DD-C-1021"
)
_mesh_records = [
    {"value": value, "file": "slab_layout.pdf", "page": 1, "text": _MESH}
    for value in ("A393", "A252")
]
ck("two additional mesh layers, each at its own location with its own detail, is a SCHEDULE",
   _is_enumerated_schedule("mesh", _mesh_records) is True)
ck("a base mesh competing with another base mesh is still a CONFLICT",
   _is_enumerated_schedule("mesh", [
       {"value": "A252", "file": "s.pdf", "page": 1, "text": "slab reinforced with A252 mesh"},
       {"value": "A393", "file": "s.pdf", "page": 1, "text": "slab reinforced with A393 mesh"},
   ]) is False)
ck("a field with no schedule pattern of its own is never reclassified",
   _is_enumerated_schedule("conc_mix", _schedule_records) is False)

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

_slab_layout = Path("drawings/inderjit_13sep/radlett_4_Slab_Layout.pdf")
try:
    _require_fixture(_slab_layout, "Radlett slab layout not present")
    _mesh_result = takeoff_pipeline.takeoff(str(_slab_layout), send_approval=False)
    _mesh_flags = [flag for flag in (_mesh_result.get("flags") or []) if "SPEC " in flag]
    _mesh_flag = _mesh_flags[0] if _mesh_flags else ""
    ck("the real slab layout's two additional mesh layers read as a schedule",
       _mesh_flag.startswith("SPEC SCHEDULE — mesh") and "SPEC CONFLICT" not in _mesh_flag,
       _mesh_flag[:200])
    ck("...with each quote naming its own mesh and its own detail sheet",
       "A252 mesh along edge of slab" in _mesh_flag and
       "A393 mesh to be provided" in _mesh_flag, _mesh_flag[:400])
    ck("...and the sheet still refuses and still routes to an assessor",
       _mesh_result.get("measurement_state") == "UNMEASURED" and
       _mesh_result.get("needs_assessor") is True,
       {"state": _mesh_result.get("measurement_state")})
except _FixtureNotPresent as _e:
    print(f"  [SKIP] slab-layout mesh guard — {_e} — fixture not present")
