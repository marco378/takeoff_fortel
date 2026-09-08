#!/usr/bin/env python3
"""router build-up vocabulary + takeoff_pipeline fast refusal of report PDFs.

Sections in this module (printed in this order):
  - a sheet that carries the build-up is a detail sheet, whatever it calls itself
  - fast refusal for obvious multi-page reports

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from tests import ck

print("a sheet that carries the build-up is a detail sheet, whatever it calls itself")
# Inderjit, 4 Sep call: "the spec is given on this drawing, but AI is saying spec is not given on
# engineering drawing... I think this is the major problem with this portal, it is not picking the
# correct details from the drawing." His sheet is named "External Construction Specification" and
# the vocabulary only knew "external construction DETAILS", so it was never read for a build-up —
# the same shape as the legend-vocabulary miss on the project-6 sheets.
import re as _re_spec
from router import DETAIL_KEYWORDS as _DETAIL_KW, buildup_source as _buildup_source
_spec_terms = tuple(_re_spec.sub(r"[-_]+", " ", term.casefold()) for term in
                    tuple(_DETAIL_KW) + ("external works details", "joint layout",
                                         "construction thickness", "slab specification",
                                         "pavement details"))
def _is_detail_candidate(name):
    normalised = _re_spec.sub(r"[-_]+", " ", name.casefold())
    return any(term in normalised for term in _spec_terms)
# HELD BACK, with evidence, and pinned so it is not re-added casually: treating a sheet named
# "... External Construction Specification" as a build-up source made the spec WORSE on the real
# pack. That sheet is a TABLE of six build-ups (yard, margins, car park, grasscrete, ...), and
# feeding it in added A142 and a 450 mm CBR figure as competing values against the Pro-Max
# drawing's own clean legend (190 mm / A393 / C32/40). It needs a table-aware extractor that
# reads the row matching the surface being priced.
ck("a specification TABLE is still not treated as a build-up source (held back deliberately)",
   not _is_detail_candidate(
       "9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf"))
for _not_a_detail in ("Please_use_for_information_only_-_Development_Specification_0.0_DRAFT.pdf",
                      "0101-Employers-Requirements-(ERs)_Rev_1.pdf",
                      "Tender_Query_Schedule.pdf",
                      "Document_Log.pdf"):
    ck(f"...while a written tender document is still not a build-up source: {_not_a_detail[:40]}",
       not _is_detail_candidate(_not_a_detail))
ck("...and an underscore-named details sheet is finally seen as one (it never was)",
   _buildup_source("RBVE_construction_details.pdf", source="engineer")[0] == "detail")
ck("...and the existing detail vocabulary still matches what it always did",
   _is_detail_candidate("6_31941-TTE-ZF-762-DR-C-0710-P01-Construction_Thicknessess_Plan.pdf")
   and _is_detail_candidate("RBVE_construction_details.pdf"))

print("fast refusal for obvious multi-page reports")
_das_pdf = "/tmp/_synthetic_heavy_das_report.pdf"
_das_canvas = canvas.Canvas(_das_pdf, pagesize=(1400, 900))
for _das_page in range(12):
    _das_canvas.setFont("Helvetica-Bold", 22)
    _das_canvas.drawString(80, 820, "Design and Access Statement")
    _das_canvas.setFont("Helvetica", 8)
    for _das_row in range(80):
        _das_canvas.drawString(80, 790 - _das_row * 9,
                               f"Planning report narrative row {_das_row} page {_das_page + 1}")
    _das_canvas.showPage()
_das_canvas.save()
from takeoff_pipeline import takeoff as _pipeline_takeoff_fast_report
_das_result = _pipeline_takeoff_fast_report(_das_pdf, auto_extract_spec=False,
                                             send_approval=False)
ck("obvious DAS report refuses before page ranking/raster measurement",
   _das_result.get("measurement_state") == "UNMEASURED" and
   _das_result.get("method") == "fast-refuse" and
   _das_result.get("area_m2") is None and
   any("no page was rasterised" in flag for flag in _das_result.get("flags", [])),
   _das_result)

_spec_pdf = "/tmp/Project_Technical_Specification.pdf"
_spec_canvas = canvas.Canvas(_spec_pdf, pagesize=(900, 700))
for _spec_page in range(6):
    _spec_canvas.drawString(60, 650, "DEVELOPMENT TECHNICAL SPECIFICATION")
    _spec_canvas.drawString(60, 620, f"Written requirements page {_spec_page + 1}")
    _spec_canvas.showPage()
_spec_canvas.save()
_spec_result = _pipeline_takeoff_fast_report(
    _spec_pdf, auto_extract_spec=False, send_approval=False)
ck("obvious specification refuses before page ranking/raster measurement",
   _spec_result.get("measurement_state") == "UNMEASURED" and
   _spec_result.get("method") == "fast-refuse" and
   _spec_result.get("area_m2") is None and
   any("filename 'Specification'" in flag and "first-page text 'TECHNICAL SPECIFICATION'" in flag
       for flag in _spec_result.get("flags", [])),
   _spec_result)

_schedule_pdf = "/tmp/Tender_Query_Schedule.pdf"
_schedule_canvas = canvas.Canvas(_schedule_pdf, pagesize=(900, 700))
_schedule_canvas.drawString(60, 650, "Tender Query Schedule")
_schedule_canvas.save()
_schedule_result = _pipeline_takeoff_fast_report(
    _schedule_pdf, auto_extract_spec=False, send_approval=False)
ck("single-page schedule is refused from strong filename evidence",
   _schedule_result.get("measurement_state") == "UNMEASURED" and
   _schedule_result.get("method") == "fast-refuse" and
   _schedule_result.get("area_m2") is None,
   _schedule_result)

_incidental_pdf = "/tmp/External_Works_Layout.pdf"
_incidental_canvas = canvas.Canvas(_incidental_pdf, pagesize=(900, 700))
_incidental_canvas.drawString(60, 650, "External Works Layout")
_incidental_canvas.drawString(60, 620, "Refer to door schedule for ancillary information")
_incidental_canvas.save()
import fitz as _fitz_fast_refusal
with _fitz_fast_refusal.open(_incidental_pdf) as _incidental_doc:
    _incidental_reason = __import__("takeoff_pipeline")._fast_report_refusal(
        _incidental_pdf, _incidental_doc)
ck("incidental schedule note on a one-sheet drawing is not fast-refused",
   _incidental_reason is None, _incidental_reason)

# A drawing whose TITLE ends in "Specification" is still a drawing. Aryan, 4 Sep: the real sheet
# 9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf was never recognised on
# prod — this rule refused it on the word "Specification" before a page was rendered, so the hatch
# detector and the legend reader never saw it. Its own reference says DR: ISO 19650 for DRAWING.
_drawing_coded_pdf = "/tmp/9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf"
_drawing_coded_canvas = canvas.Canvas(_drawing_coded_pdf, pagesize=(1400, 900))
_drawing_coded_canvas.drawString(60, 850, "EXTERNAL CONSTRUCTION SPECIFICATION")
_drawing_coded_canvas.drawString(60, 820, "CONCRETE SERVICE YARD")
_drawing_coded_canvas.save()
with _fitz_fast_refusal.open(_drawing_coded_pdf) as _drawing_coded_doc:
    _drawing_coded_reason = __import__("takeoff_pipeline")._fast_report_refusal(
        _drawing_coded_pdf, _drawing_coded_doc)
ck("a DRAWING-coded sheet titled '... Specification' is no longer thrown away unread",
   _drawing_coded_reason is None, _drawing_coded_reason)

# The override is narrow on purpose, in three directions.
_spec_coded_pdf = "/tmp/2154-SGP-XX-XX-SP-C-0001_External_Works_Specification.pdf"
_spec_coded_canvas = canvas.Canvas(_spec_coded_pdf, pagesize=(900, 700))
_spec_coded_canvas.drawString(60, 650, "External works specification")
_spec_coded_canvas.save()
with _fitz_fast_refusal.open(_spec_coded_pdf) as _spec_coded_doc:
    _spec_coded_reason = __import__("takeoff_pipeline")._fast_report_refusal(
        _spec_coded_pdf, _spec_coded_doc)
ck("...an SP-coded specification is still refused: SP is not DR",
   _spec_coded_reason is not None and "Specification" in _spec_coded_reason, _spec_coded_reason)

_drawing_coded_text_pdf = "/tmp/25010-RLL-26-XX-DR-C-9999_Specification.pdf"
_dct_canvas = canvas.Canvas(_drawing_coded_text_pdf, pagesize=(900, 700))
for _dct_page in range(5):
    _dct_canvas.drawString(60, 650, "DEVELOPMENT SPECIFICATION")
    _dct_canvas.drawString(60, 620, f"Written requirements page {_dct_page + 1}")
    _dct_canvas.showPage()
_dct_canvas.save()
with _fitz_fast_refusal.open(_drawing_coded_text_pdf) as _dct_doc:
    _dct_reason = __import__("takeoff_pipeline")._fast_report_refusal(
        _drawing_coded_text_pdf, _dct_doc)
ck("...a drawing-coded file whose PAGES read as a specification is still refused",
   _dct_reason is not None, _dct_reason)

with _fitz_fast_refusal.open(_schedule_pdf) as _sched_doc:
    _sched_reason = __import__("takeoff_pipeline")._fast_report_refusal(_schedule_pdf, _sched_doc)
ck("...and an uncoded written document is untouched by the override",
   _sched_reason is not None, _sched_reason)

_boundary_pdf = "/tmp/Standalone_Boundary_Treatment_Plan.pdf"
_boundary_canvas = canvas.Canvas(_boundary_pdf, pagesize=(900, 700))
_boundary_canvas.drawString(60, 650, "BOUNDARY TREATMENT PLAN")
_boundary_canvas.drawString(60, 620, "Paladin fencing and timber acoustic fence")
_boundary_canvas.save()
_boundary_result = _pipeline_takeoff_fast_report(
    _boundary_pdf, auto_extract_spec=False, send_approval=False)
ck("dedicated boundary-treatment drawing refuses before raster measurement",
   _boundary_result.get("measurement_state") == "UNMEASURED" and
   _boundary_result.get("method") == "fast-refuse" and
   _boundary_result.get("area_m2") is None and
   any("boundary-treatment/fencing" in flag and "no page was rasterised" in flag
       for flag in _boundary_result.get("flags", [])),
   _boundary_result)

_combined_boundary_pdf = "/tmp/Hub_Hard_Landscaping_and_Boundary_Treatment_Plan.pdf"
_combined_boundary_canvas = canvas.Canvas(_combined_boundary_pdf, pagesize=(900, 700))
_combined_boundary_canvas.drawString(60, 650, "HARD LANDSCAPING AND BOUNDARY TREATMENT PLAN")
_combined_boundary_canvas.save()
with _fitz_fast_refusal.open(_combined_boundary_pdf) as _combined_boundary_doc:
    _combined_boundary_reason = __import__("takeoff_pipeline")._fast_report_refusal(
        _combined_boundary_pdf, _combined_boundary_doc)
ck("combined hard-landscaping boundary plan is not fast-refused",
   _combined_boundary_reason is None, _combined_boundary_reason)

_fake_large_container = Path("/tmp/_synthetic_large_tender.zip")
_fake_large_container.write_bytes(b"PK\x03\x04" + b"not-a-pdf" * 1024)
_container_result = _pipeline_takeoff_fast_report(
    str(_fake_large_container), auto_extract_spec=False, send_approval=False)
ck("non-PDF container refuses from magic bytes before MuPDF format probing",
   _container_result.get("measurement_state") == "REJECTED" and
   _container_result.get("area_m2") is None and
   any("%PDF header absent" in flag for flag in _container_result.get("flags", [])),
   _container_result)

