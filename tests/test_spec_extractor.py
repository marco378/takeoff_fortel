#!/usr/bin/env python3
"""spec_extractor text parsing + the Brief_Spec schema and provenance.

Sections in this module (printed in this order):
  - spec extractor (construction-detail PDF text parsing)
  - Fortel Brief_Spec schema + field provenance

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import shutil
from pathlib import Path
from reportlab.pdfgen import canvas
from tests import _FixtureNotPresent, _require_fixture, ck

print("spec extractor (construction-detail PDF text parsing)")
from spec_extractor import describe_spec, extract_spec, extract_spec_from_text
_e1 = extract_spec_from_text("175 mm thick with A193 mesh, C32/40 concrete")
ck("depth 175",  _e1.get("depth_mm") == 175)
ck("mesh A193",  _e1.get("mesh") == "A193")
ck("mix C32/40", _e1.get("conc_mix") == "C32/40")
_e2 = extract_spec_from_text("200mm slab with two layers of A393 reinforcement C35/45")
ck("depth 200",  _e2.get("depth_mm") == 200)
ck("2 layers A393", _e2.get("mesh") == "A393" and _e2.get("layers") == 2)
_e3 = extract_spec_from_text("No specification provided")
ck("empty text -> empty spec", not any(k in _e3 for k in ("depth_mm","mesh","conc_mix")))
_e4 = extract_spec_from_text("A393 x2 250 mm C40/50")
ck("x2 notation -> 2 layers", _e4.get("layers") == 2)
_e5 = extract_spec_from_text("A252 mesh")
ck("mesh without a layer count keeps layers unknown", "layers" not in _e5, _e5)
ck("mesh-only human summary says layers not provided",
   describe_spec(_e5) == "A252 mesh (layers not provided)", describe_spec(_e5))

# Inderjit's first live-use review exposed a 150 mm false result while the sheet stated
# 190 mm.  Scale-bar print dimensions and neighbouring build-ups are not slab evidence.
_e6 = extract_spec_from_text(
    "CHECK: scale bar must measure 150 mm when printed.\n"
    "EXTERNAL SERVICE YARD — 190 mm thick reinforced concrete slab with A252 fabric.",
    source_name="Live Yard Joint Layout.pdf", page_number=1,
    context="external service yard",
)
ck("explicit 190mm Yard slab outranks a 150mm scale-bar print dimension",
   _e6.get("depth_mm") == 190, _e6)
ck("extracted thickness carries auditable file/page/text evidence",
   (_e6.get("_evidence") or {}).get("depth_mm", {}).get("file") ==
       "Live Yard Joint Layout.pdf" and
   (_e6.get("_evidence") or {}).get("depth_mm", {}).get("page") == 1 and
   "190 mm thick" in (_e6.get("_evidence") or {}).get("depth_mm", {}).get("text", ""),
   _e6.get("_evidence"))
_e6_detail = extract_spec_from_text(
    "CONCRETE YARD — Concrete Slab: 205mm thickness of PAV2. "
    "Min 300mm thickness of ground improvement. Sub Base 150mm Type 1. "
    "H12 U-bars at 225mm crs.",
    source_name="External Works Details.pdf", page_number=1,
    context="external concrete yard",
)
ck("ground-improvement, sub-base and reinforcement-spacing dimensions are not slab depths",
   _e6_detail.get("depth_mm") == 205 and
   not (_e6_detail.get("_conflicts") or {}).get("depth_mm"), _e6_detail)

_e7 = extract_spec_from_text(
    "PROPOSED EXTERNAL YARD SLAB JOINT LAYOUT. "
    "Allow for joints in external slab at maximum 4.9 by 6.5 metre centres.",
    source_name="Yard Joint Layout.pdf", page_number=2,
    context="external yard",
)
ck("joint-layout spacing is extracted instead of reported as no details",
   _e7.get("bay_sizes") == "4.9 m x 6.5 m centres" and
   (_e7.get("_evidence") or {}).get("bay_sizes", {}).get("page") == 2,
   _e7)

# Portal uploads from every project share one persistent drawings directory.  A details PDF
# from project A must never supply project B's spec merely because it sorts first.
_spec_scope_dir = Path("/tmp/_fortel_spec_scope")
shutil.rmtree(_spec_scope_dir, ignore_errors=True)
_spec_scope_dir.mkdir()
for _spec_name, _spec_text in (
    ("AAA_Other_External_Construction_Details.pdf", "150 mm thick concrete slab A193"),
    ("BBB_Target_Yard.pdf", "190 mm thick external service yard slab A252"),
):
    _spec_canvas = canvas.Canvas(str(_spec_scope_dir / _spec_name), pagesize=(500, 300))
    _spec_canvas.drawString(40, 240, _spec_text)
    _spec_canvas.save()
from takeoff_pipeline import find_engineer_spec as _find_engineer_spec
_scoped_spec = _find_engineer_spec(
    str(_spec_scope_dir / "BBB_Target_Yard.pdf"), project_ref="BBB")
ck("project-scoped lookup cannot import another project's 150mm spec",
   _scoped_spec and _scoped_spec.get("depth_mm") == 190 and
   "AAA_Other" not in str(_scoped_spec), _scoped_spec)
_same_project_detail = _spec_scope_dir / "BBB_Yard_Build-Up.pdf"
_spec_canvas = canvas.Canvas(str(_same_project_detail), pagesize=(500, 300))
_spec_canvas.drawString(40, 240, "CONCRETE YARD — Concrete slab 205mm thick with A393 mesh")
_spec_canvas.save()
_conflicted_spec = _find_engineer_spec(
    str(_spec_scope_dir / "BBB_Target_Yard.pdf"), project_ref="BBB")
ck("competing same-project slab callouts remain a visible conflict, never an auto-confirmed value",
   _conflicted_spec and "depth_mm" not in _conflicted_spec and
   {record["value"] for record in _conflicted_spec["_conflicts"]["depth_mm"]} ==
       {190, 205}, _conflicted_spec)

# Portal project membership, unlike same-directory discovery, is explicit. A later detail
# upload may therefore live in another persisted folder and must still supply cited evidence.
_cross_project_dir = _spec_scope_dir / "project_registry"
_cross_layout_dir = _cross_project_dir / "layout_upload"
_cross_detail_dir = _cross_project_dir / "later_detail_upload"
_cross_layout_dir.mkdir(parents=True)
_cross_detail_dir.mkdir(parents=True)
_cross_target = _cross_layout_dir / "CROSS_Yard_Layout.pdf"
_cross_detail = _cross_detail_dir / "CROSS_External_Works_Details.pdf"
for _cross_path, _cross_text in (
    (_cross_target, "EXTERNAL SERVICE YARD GENERAL ARRANGEMENT"),
    (_cross_detail,
     "EXTERNAL SERVICE YARD — 215 mm thick concrete slab with A393 mesh, C35/45 concrete"),
):
    _spec_canvas = canvas.Canvas(str(_cross_path), pagesize=(500, 300))
    _spec_canvas.drawString(40, 240, _cross_text)
    _spec_canvas.save()
_cross_without_registry = _find_engineer_spec(
    str(_cross_target), project_ref="CROSS")
_cross_with_registry = _find_engineer_spec(
    str(_cross_target), project_ref="CROSS",
    project_files=[str(_cross_target), str(_cross_detail)])
ck("project-wide spec lookup crosses upload folders only through the explicit job registry",
   _cross_without_registry is None and
   _cross_with_registry.get("depth_mm") == 215 and
   _cross_with_registry.get("mesh") == "A393" and
   _cross_with_registry.get("conc_mix") == "C35/45",
   {"without_registry":_cross_without_registry,
    "with_registry":_cross_with_registry})
ck("project-wide extracted fields retain exact drawing and page citations",
   all((_cross_with_registry.get("_evidence") or {}).get(field, {}).get("file") ==
       _cross_detail.name for field in ("depth_mm","mesh","conc_mix")) and
   all((_cross_with_registry.get("_evidence") or {}).get(field, {}).get("page") == 1
       for field in ("depth_mm","mesh","conc_mix")),
   _cross_with_registry.get("_evidence"))

_joint_fixture = Path(
    "drawings/inderjit_markups_31jul/2165 Tanro- Voltage Business Park/"
    "Tanro Voltage Business Park/Bid_Drawings/A)-Tender-Stage/Current/"
    "Engineer---Baynham-Meikle/"
    "13897-114-Proposed-External-Yard-Slab-Joint-Layout-Rev.T1.pdf")
try:
    _require_fixture(_joint_fixture, "Tanro joint-layout fixture not present")
    _joint_fixture_spec = extract_spec(str(_joint_fixture), context="external yard")
    ck("real joint-layout scale-bar 100mm is never confirmed as slab thickness",
       _joint_fixture_spec.get("depth_mm") != 100 and
       bool(_joint_fixture_spec.get("_joint_layout")), _joint_fixture_spec)
except _FixtureNotPresent as _e:
    print(f"  [SKIP] real joint-layout spec guard — {_e} — fixture not present")

_multi_build_fixture = Path(
    "drawings/aryan_31jul/Bidding_Documents___27_0/Tender_drawings/Tender-Stage/"
    "Current/Engineer---PRP/64426-112-External-Works-Details-Rev.T2.pdf")
try:
    _require_fixture(_multi_build_fixture, "PRP multi-build-up detail fixture not present")
    _multi_build_spec = extract_spec(
        str(_multi_build_fixture), context="external service yard concrete yard")
    _depth_conflicts = {
        record.get("value")
        for record in (_multi_build_spec.get("_conflicts") or {}).get("depth_mm", [])
    }
    ck("real multi-build-up detail refuses scope ambiguity without retaining non-slab dimensions",
       "depth_mm" not in _multi_build_spec and "mesh" not in _multi_build_spec and
       {150, 190, 205}.issubset(_depth_conflicts) and
       not ({155, 225, 300} & _depth_conflicts), _multi_build_spec)
except _FixtureNotPresent as _e:
    print(f"  [SKIP] real multi-build-up spec guard — {_e} — fixture not present")

print("Fortel Brief_Spec schema + field provenance")
from slab_spec import (COMMON_FIELDS as _SPEC_COMMON_FIELDS, SLAB_SPEC_SCHEMA as _SPEC_SCHEMA,
                       brief_spec_signature as _brief_signature,
                       build_brief_spec as _build_brief_spec,
                       empty_brief_spec as _empty_brief_spec)
_expected_spec_fields = {
    "external_yard": ("depth_mm", "conc_mix", "mesh", "layers", "bay_sizes", "joint_details"),
    "dock": ("depth_mm", "conc_mix", "mesh", "layers", "bay_sizes", "joint_details"),
    "ground_floor": ("depth_mm", "conc_mix", "mesh", "layers", "joint_details"),
    "upper_floor": ("depth_mm", "conc_mix", "mesh", "layers"),
}
ck("Brief_Spec schema has the exact four slab types and applicable fields",
   set(_SPEC_SCHEMA) == set(_expected_spec_fields) and
   all(tuple(_SPEC_SCHEMA[key]["fields"]) == fields
       for key, fields in _expected_spec_fields.items()))
_empty_yard_spec = _empty_brief_spec("external_yard")
ck("blank Brief_Spec has no invented values and every field is provisional",
   all(field["value"] is None and field["provisional"]
       for field in _empty_yard_spec["fields"].values()), _empty_yard_spec)
_effective_spec = {"depth_mm": 190, "conc_mix": "C32/40", "mesh": "A252", "layers": 1}
_assumed_brief = _build_brief_spec("external_yard", effective_spec=_effective_spec)
_confirmed_brief = _build_brief_spec(
    "external_yard", effective_spec=_effective_spec, confirmed=_effective_spec,
    source="engineer_drawing")
ck("effective costing values remain field-by-field assumed until confirmed",
   all(_assumed_brief["fields"][key]["provisional"] for key in _SPEC_COMMON_FIELDS) and
   all(not _confirmed_brief["fields"][key]["provisional"] for key in _SPEC_COMMON_FIELDS))
ck("assumed and confirmed copies of the same effective spec have different aggregation identity",
   _brief_signature(_assumed_brief) != _brief_signature(_confirmed_brief))
_mesh_only_brief = _build_brief_spec(
    "external_yard", confirmed={"mesh": "A252"}, source="engineer_drawing",
    evidence={"mesh": {"file": "Yard Detail D-112.pdf", "page": 3,
                       "text": "A252 fabric reinforcement"}},
)
ck("a fabric callout confirms mesh but never silently confirms one reinforcement layer",
   not _mesh_only_brief["fields"]["mesh"]["provisional"] and
   _mesh_only_brief["fields"]["layers"]["provisional"] and
   _mesh_only_brief["fields"]["layers"]["value"] is None,
   _mesh_only_brief)

