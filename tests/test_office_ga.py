#!/usr/bin/env python3
"""office_candidates assisted-trace vectors + the accuracy scorecard harness.

Sections in this module (printed in this order):
  - Office GA assisted-trace vector candidates
  - accuracy scorecard harness

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from tests import _FixtureNotPresent, _require_fixture, ck

print("Office GA assisted-trace vector candidates")
from office_candidates import detect_office_candidates as _office_candidates
_office_pdf = "/tmp/_office_candidates.pdf"
_office_canvas = canvas.Canvas(_office_pdf, pagesize=(900, 500))
for _index, _level in enumerate((0, 1, 2)):
    _x = 60 + _index * 280
    _office_canvas.rect(_x, 220, 100, 100, stroke=1, fill=0)
    _office_canvas.drawString(_x, 190, f"Office Plan Level {_level:02d}")
_office_canvas.line(60, 120, 160, 120)  # open line: must not become a candidate
_office_canvas.rect(800, 100, 5, 5, stroke=1, fill=0)  # symbol: below 20 m2
_office_canvas.save()
_office_detected = _office_candidates(_office_pdf, scale_k=0.1, scale_verified=True)
_office_by_level = {candidate["level"]: candidate
                    for candidate in _office_detected["candidate_polygons"]}
ck("closed vector plates become one assisted candidate per labelled level",
   set(_office_by_level) == {0, 1, 2} and len(_office_by_level) == 3 and
   all(candidate["outline_status"] == "resolved"
       for candidate in _office_detected["candidate_polygons"]),
   _office_detected)
ck("Level 00 maps to ground and upper levels stay separate",
   _office_by_level[0]["category"] == "ground_floor" and
   all(_office_by_level[level]["category"] == "upper_floor" for level in (1, 2)))
ck("candidate records are geometry-only tracing aids",
   all("area_m2" not in candidate and
       candidate["coordinate_space"] == "rotated_pdf_points" and
       candidate["confidence"] in {"low", "medium"} and
       candidate.get("confidence_reasons")
       for candidate in _office_detected["candidate_polygons"]))
from office_candidates import _dedupe_iou as _office_dedupe_iou
from shapely.geometry import box as _office_box
_iou_records = _office_dedupe_iou([
    {"geometry": _office_box(0, 0, 100, 100), "source": "office-vector-closed-loop"},
    {"geometry": _office_box(1, 1, 101, 101), "source": "office-vector-white-fill-loop"},
    # Same area but disjoint: an area-only deduper would incorrectly remove this core.
    {"geometry": _office_box(200, 0, 300, 100), "source": "office-vector-closed-loop"},
])
ck("office loop dedupe uses IoU and preserves equal-area disjoint regions",
   len(_iou_records) == 2)

_office_unresolved_pdf = "/tmp/_office_candidates_unresolved.pdf"
_office_unresolved_canvas = canvas.Canvas(_office_unresolved_pdf, pagesize=(900, 500))
_office_unresolved_canvas.drawString(60, 190, "Office Plan Level 00")
_office_unresolved_canvas.drawString(340, 190, "Office Plan Level 01")
_office_unresolved_canvas.save()
_office_unresolved = _office_candidates(
    _office_unresolved_pdf, scale_k=0.1, scale_verified=True)
ck("detected level without a defensible outline is reported, never dropped",
   [candidate["level"] for candidate in _office_unresolved["candidate_polygons"]] == [0, 1] and
   all(candidate["outline_status"] == "unresolved"
       for candidate in _office_unresolved["candidate_polygons"]) and
   any("level detected but outline not resolved — trace manually" in flag
       for flag in _office_unresolved["flags"]),
   _office_unresolved)

_steelwork_title_pdf = "/tmp/_office_first_floor_steelwork_title.pdf"
_steelwork_title_canvas = canvas.Canvas(_steelwork_title_pdf, pagesize=(900, 500))
_steelwork_title_canvas.drawString(250, 60, "First Floor Steelwork Layout")
_steelwork_title_canvas.drawString(650, 450, "Metal Deck Notes")
_steelwork_title_canvas.setStrokeColorRGB(0, 0.59, 0)
for _deck_row in range(30):
    for _deck_col in range(40):
        _hx = 220 + _deck_col * 3
        _hy = 210 + _deck_row * 3
        _steelwork_title_canvas.line(_hx, _hy, _hx + 1, _hy + 1)
_steelwork_title_canvas.save()
_steelwork_title_result = _office_candidates(
    _steelwork_title_pdf, scale_k=0.1, scale_verified=False)
ck("First Floor Steelwork Layout creates a real quantity-free proposed polygon",
   len(_steelwork_title_result["candidate_polygons"]) == 1 and
   _steelwork_title_result["candidate_polygons"][0]["level"] == 1 and
   _steelwork_title_result["candidate_polygons"][0]["category"] == "upper_floor" and
   _steelwork_title_result["candidate_polygons"][0]["outline_status"] == "proposed" and
   len(_steelwork_title_result["candidate_polygons"][0]["polygon_pts"]) >= 4 and
   "Metal Deck" in (_steelwork_title_result["candidate_polygons"][0].get("basis") or "") and
   "area_m2" not in _steelwork_title_result["candidate_polygons"][0],
   _steelwork_title_result)
_steelwork_auto = _steelwork_title_result.get("auto_measurement") or {}
ck("corroborated repeated deck hatch measures only at MEASURED_UNVERIFIED",
   _steelwork_auto.get("area_m2", 0) > 0 and
   _steelwork_auto.get("measurement_state") == "MEASURED_UNVERIFIED" and
   _steelwork_auto.get("needs_assessor") is True and
   _steelwork_auto.get("perimeter_lm", 0) > 0 and
   len(_steelwork_auto.get("zones") or []) == 1 and
   _steelwork_auto["zones"][0]["category"] == "upper_floor",
   _steelwork_auto)
_steelwork_no_scale = _office_candidates(
    _steelwork_title_pdf, scale_k=None, scale_verified=False)
ck("deck-hatch polygon stays a quantity-free proposal when scale is absent",
   _steelwork_no_scale["candidate_polygons"][0]["outline_status"] == "proposed" and
   "auto_measurement" not in _steelwork_no_scale and
   "area_m2" not in _steelwork_no_scale["candidate_polygons"][0],
   _steelwork_no_scale)

_office_no_sibling_pdf = "/tmp/_office_candidates_sibling_prefill.pdf"
_office_no_sibling_canvas = canvas.Canvas(_office_no_sibling_pdf, pagesize=(900, 500))
_office_no_sibling_canvas.rect(60, 220, 100, 100, stroke=1, fill=0)
_office_no_sibling_canvas.drawString(60, 190, "Office Plan Level 01")
_office_no_sibling_canvas.drawString(340, 190, "Office Plan Level 02")
_office_no_sibling_canvas.save()
_office_no_sibling = _office_candidates(
    _office_no_sibling_pdf, scale_k=0.1, scale_verified=True)
_office_no_sibling_by_level = {
    candidate["level"]: candidate
    for candidate in _office_no_sibling["candidate_polygons"]
}
ck("missing local Office loop gets geometry-only sibling prefill, never a measured area",
   _office_no_sibling_by_level[1]["outline_status"] == "resolved" and
   _office_no_sibling_by_level[2]["outline_status"] == "prefill" and
   _office_no_sibling_by_level[2]["regions"] and
   "area_m2" not in _office_no_sibling_by_level[2] and
   "no level-local dark closed plate loop" in
       " ".join(_office_no_sibling_by_level[2]["confidence_reasons"]) and
   "sibling-level-prefill" in _office_no_sibling_by_level[2]["source"] and
   any("LOW-CONFIDENCE PREFILL" in flag
       for flag in _office_no_sibling_by_level[2]["flags"]),
   _office_no_sibling)

print("accuracy scorecard harness")
from accuracy_report import (
    discover_pairs as _accuracy_discover_pairs,
    normalise_drawing_name as _accuracy_normalise_name,
    pair_marked_and_raw as _accuracy_pair,
    score_drawing as _accuracy_score_drawing,
    scorecard as _accuracy_scorecard,
    signed_delta_pct as _accuracy_delta,
    within_tolerance as _accuracy_within,
)
_accuracy_marked_record = {
    "path": Path("/tmp/Markup Project-Alpha_marked.pdf"),
    "truth": {"area_m2": 100.0, "zones": []},
}
_accuracy_raw_record = {"path": Path("/tmp/raw/Project Alpha.pdf")}
_accuracy_other_raw = {"path": Path("/tmp/raw/Project Beta.pdf")}
_accuracy_pairs, _accuracy_unused = _accuracy_pair(
    [_accuracy_marked_record], [_accuracy_raw_record, _accuracy_other_raw])
ck("accuracy pairing normalises marked/raw prefixes and suffixes without cross-pairing",
   _accuracy_normalise_name(_accuracy_marked_record["path"]) == "projectalpha" and
   _accuracy_normalise_name("MarkupProject Alpha.pdf") == "projectalpha" and
   len(_accuracy_pairs) == 1 and
   _accuracy_pairs[0]["raw_path"] == _accuracy_raw_record["path"] and
   len(_accuracy_unused) == 1 and "Project Beta.pdf" in _accuracy_unused[0],
   {"pairs": _accuracy_pairs, "unused": _accuracy_unused})
ck("accuracy tolerance maths is signed and inclusive at exactly 5%",
   _accuracy_delta(95, 100) == -5 and
   _accuracy_within(105, 100, 5) and
   not _accuracy_within(105.01, 100, 5))

_accuracy_not_measured = _accuracy_score_drawing(
    marked_path=Path("/tmp/marked.pdf"),
    raw_label="temporary stripped copy",
    raw_mode="derived-stripped",
    pairing="test",
    truth={"area_m2": 100.0,
           "zones": [{"category": "external_yard", "area_m2": 100.0}]},
    pipeline_run={
        "ok": True, "elapsed_s": 0.1,
        "payload": {
            "area_m2": None, "measurement_state": "UNMEASURED",
            "flags": ["REFUSED: no reliable slab region"], "zones": [],
        },
    },
    tolerance_pct=5,
)
ck("accuracy NOT MEASURED is an explicit client miss, never a silent pass",
   _accuracy_not_measured["verdict"] == "NOT MEASURED" and
   _accuracy_not_measured["failure_mode"] == "not measured" and
   _accuracy_not_measured["measured_total_m2"] is None,
   _accuracy_not_measured)

_accuracy_summary = _accuracy_scorecard([
    {"verdict": "PASS", "failure_mode": None},
    {"verdict": "FAIL", "failure_mode": "over-measured"},
    {"verdict": "FAIL", "failure_mode": "under-measured"},
    {"verdict": "FAIL", "failure_mode": "zone mis-split"},
    {"verdict": "NOT MEASURED", "failure_mode": "not measured"},
], 5)
ck("accuracy scorecard arithmetic counts passes and each failure mode",
   _accuracy_summary["passed"] == 1 and
   _accuracy_summary["drawings"] == 5 and
   _accuracy_summary["accuracy_pct"] == 20.0 and
   _accuracy_summary["summary_line"] == "1 of 5 within 5% (20.0%)" and
   all(_accuracy_summary["breakdown"][mode] == 1 for mode in (
       "not measured", "over-measured", "under-measured", "zone mis-split")),
   _accuracy_summary)

try:
    _require_fixture("drawings/castle_donington",
                     "accuracy harness Castle Donington pairing integration")
    _accuracy_castle_pairs = _accuracy_discover_pairs(
        ["drawings/castle_donington"])["pairs"]
    ck("accuracy harness pairs all 8 Castle truths without measuring a marked answer",
       len(_accuracy_castle_pairs) == 8 and
       sum(pair["raw_mode"] == "existing" for pair in _accuracy_castle_pairs) == 4 and
       sum(pair["raw_mode"] == "derived-stripped" for pair in _accuracy_castle_pairs) == 4 and
       all(pair["raw_path"] is None or "_stripped" in str(pair["raw_path"])
           for pair in _accuracy_castle_pairs),
       [(pair["marked_path"].name, pair["raw_mode"], str(pair["raw_path"]))
        for pair in _accuracy_castle_pairs])
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")

