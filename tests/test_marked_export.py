#!/usr/bin/env python3
"""marked_pdf export: Bluebeam-ready markup and the recoverable manifest.

Sections in this module (printed in this order):
  - marked-PDF export: permanent Bluebeam-ready markup + recoverable manifest

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck
import copy as _copy

print("marked-PDF export: permanent Bluebeam-ready markup + recoverable manifest")
try:
    import json as _json_marked
    import fitz as _fitz_marked
    from marked_pdf import (MANIFEST_NAME as _MARKED_MANIFEST_NAME,
                            MarkedPdfError as _MarkedPdfError,
                            build_marked_pdf as _build_marked_pdf,
                            marked_pdf_filename as _marked_pdf_filename)

    _marked_source = Path("/tmp/ci_marked_pdf_source.pdf")
    _marked_doc = _fitz_marked.open()
    _marked_page = _marked_doc.new_page(width=300, height=200)
    _marked_page.insert_text((20, 30), "FORTEL MARKED PDF ROTATION QA")
    _source_annot = _marked_page.add_rect_annot(_fitz_marked.Rect(20, 45, 80, 85))
    _source_annot.set_info(content="Existing Bluebeam-style source annotation")
    _source_annot.update()
    _marked_page.set_rotation(90)
    _marked_doc.save(_marked_source)
    _marked_doc.close()

    # Assessor geometry is saved in 2 px/PDF-point snapshot space. Region gross=400m2,
    # contained cut-out=16m2, therefore the exported/priced net region label must be 384m2.
    _marked_job = {
        "id": "marked-pdf-ci-job", "project_ref": "MARKED/QA 01",
        "project_name": "Marked PDF QA", "decision": "adjusted",
        "quotation_revision": 3, "pdf_path": str(_marked_source),
        "result": {"file": "Rotated Drawing.pdf", "pdf_path": str(_marked_source),
                   "page": 0, "area_m2": 384.0, "scale_k": 0.2},
        "adjusted": {
            "regions": [[[40,40],[240,40],[240,240],[40,240]]],
            "region_categories": ["external_yard"], "region_scopes": ["main"],
            "scale_k": 0.1, "snapshot_scale": 2.0, "area_m2": 384.0,
            "cutout_regions": [[[80,80],[120,80],[120,120],[80,120]]],
            "user_channels": [[[40,300],[140,300],[140,380]]],
        },
        "channel_proposals": [{
            "proposal_id": "channel-assumed", "component": "Dock channel",
            "polyline_pts": [[30,230],[130,230]], "basis": "client rule",
        }],
        "channel_proposal_decisions": {"channel-assumed": {
            "decision": "accepted", "length_lm": 10.0,
            "polyline_pts": [[30,230],[130,230]],
        }},
        "transition_candidates": [{
            "candidate_id": "transition-1", "polyline_pts": [[30,200],[130,200]],
            "basis": "yard entrance",
        }],
        "transition_candidate_decisions": {"transition-1": {
            "decision": "accepted", "length_lm": 10.0,
        }},
    }
    _marked_bytes, _marked_manifest = _build_marked_pdf(_marked_job)
    ck("marked-PDF filename carries project, drawing and commercial revision",
       _marked_pdf_filename(_marked_job) ==
       "MARKED_QA_01_Rotated_Drawing_REV_03_MARKED.pdf",
       _marked_pdf_filename(_marked_job))
    _prefixed_marked_job = _copy.deepcopy(_marked_job)
    _prefixed_marked_job["result"]["file"] = "MARKED_QA_01_Rotated_Drawing.pdf"
    ck("marked-PDF client filename does not repeat the storage collision prefix",
       _marked_pdf_filename(_prefixed_marked_job) ==
       "MARKED_QA_01_Rotated_Drawing_REV_03_MARKED.pdf",
       _marked_pdf_filename(_prefixed_marked_job))
    with _fitz_marked.open(stream=_marked_bytes, filetype="pdf") as _marked_roundtrip:
        _marked_annots = sum(1 for _page in _marked_roundtrip
                             for _annot in (_page.annots() or []))
        _marked_embedded = _json_marked.loads(
            _marked_roundtrip.embfile_get(_MARKED_MANIFEST_NAME))
        _marked_text = "\n".join(page.get_text() for page in _marked_roundtrip)
        ck("marked-PDF reopens with original page rotation and all annotations burned in",
           _marked_roundtrip.page_count == 1 and
           _marked_roundtrip[0].rotation == 90 and _marked_annots == 0 and
           _marked_embedded["source_annotations_burned_in"] == 1,
           {"pages":_marked_roundtrip.page_count,"rotation":_marked_roundtrip[0].rotation,
            "annots":_marked_annots})
        ck("marked-PDF embeds versioned job/page geometry for future re-import",
           _marked_roundtrip.embfile_names() == [_MARKED_MANIFEST_NAME] and
           _marked_embedded["schema"] == "fortel.markup.v1" and
           _marked_embedded["job_id"] == "marked-pdf-ci-job" and
           _marked_embedded["source_page_index"] == 0 and
           _marked_embedded["geometry"]["regions"][0]["points"][0] == [20.0,20.0] and
           _marked_embedded["geometry"]["regions"][0]["area_m2"] == 384.0,
           _marked_embedded)
        ck("marked-PDF permanently labels net area, cut-out, channel and transition quantities",
           all(marker in _marked_text for marker in (
               "Service yard - 384.00 m2", "Cut-out 1 - 16.00 m2",
               "Channel 1 - 18.00 Lm", "Dock channel - 10.00 Lm - PROVISIONAL",
               "Transition - 10.00 Lm - PROVISIONAL")), _marked_text[-1000:])
        ck("marked-PDF metadata identifies Fortel schema/job/revision",
           "job_id=marked-pdf-ci-job" in _marked_roundtrip.metadata.get("keywords", "") and
           _marked_roundtrip.metadata.get("subject") ==
               "Fortel assessor marked-up takeoff drawing")

    _marked_blank_source = Path("/tmp/ci_marked_pdf_blank_source.pdf")
    _marked_blank_doc = _fitz_marked.open()
    _marked_blank_doc.new_page(width=300, height=200)
    _marked_blank_doc.save(_marked_blank_source)
    _marked_blank_doc.close()
    try:
        _build_marked_pdf({
            "id":"empty-markup", "decision":"adjusted", "pdf_path":str(_marked_blank_source),
            "result":{"pdf_path":str(_marked_blank_source), "page":0},
        })
        ck("marked-PDF refuses a job with no exportable geometry", False)
    except _MarkedPdfError as _marked_error:
        ck("marked-PDF refuses a job with no exportable geometry",
           "no measured/assessor markup geometry" in str(_marked_error), str(_marked_error))
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] marked-PDF export tests — missing dependency or file: {_e}")

