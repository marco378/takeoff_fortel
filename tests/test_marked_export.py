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
    # ── the markup is how you CHECK a measurement, so it cannot require approving it ──────
    # Aryan, 9 Sep: "give us a visual reference for every measurement it produces, so we can
    # verify that the AI is measuring the correct area and not just confirm that the
    # calculation ran successfully." The export existed but was gated on
    # decision in {approved, adjusted} — you could only obtain the document that proves the
    # number by first accepting the number. That is how a car park stayed priced as a service
    # yard for six days.
    import fitz as _fitz_ua
    from marked_pdf import build_marked_pdf as _bmp_ua, marked_pdf_filename as _mpf_ua
    _ua_doc = _fitz_ua.open()
    _ua_page = _ua_doc.new_page(width=800, height=600)
    _ua_src = str(Path(_TMP_MARKED) / "unapproved_source.pdf") if "_TMP_MARKED" in dir() \
        else "/tmp/_qa_unapproved_source.pdf"
    _ua_doc.save(_ua_src)
    _ua_doc.close()
    _ua_job = {
        "id": "job-unapproved", "project_ref": "QA-UA", "pdf_path": _ua_src,
        "result": {"file": "sheet.pdf", "page": 0, "area_m2": 1234.5,
                   "measurement_state": "MEASURED_UNVERIFIED",
                   "flags": ["AREA IS A MINIMUM: the marks stop short of the edge."],
                   "yard_regions": [{"region_id": "yard-region-1", "area_m2": 1234.5,
                                     "polygon_pts": [[100, 100], [400, 100],
                                                     [400, 380], [100, 380]],
                                     "included": True}]},
    }
    _ua_bytes, _ua_manifest = _bmp_ua(_ua_job, _ua_src)
    ck("a measured job that has NOT been approved still exports a marked drawing",
       isinstance(_ua_bytes, bytes) and len(_ua_bytes) > 500
       and len(_ua_manifest["geometry"]["regions"]) == 1,
       f"bytes={len(_ua_bytes)} regions={len(_ua_manifest['geometry']['regions'])}")
    # Normalise whitespace: the stamp wraps, and what matters is the sentence surviving, not
    # where the line breaks fall.
    _ua_text = " ".join(
        _fitz_ua.open(stream=_ua_bytes, filetype="pdf")[0].get_text().split())
    ck("...stamped on the drawing as NOT APPROVED, so it cannot be mistaken for an issue copy",
       "NOT APPROVED. FOR CHECKING ONLY." in _ua_text, _ua_text[:140])
    ck("...and the stamp renders no corrupt glyphs (base-14 fonts have no em-dash or bullet)",
       "?" not in _ua_text.split("computed from")[0], _ua_text[:160])
    # A narrow page once wrapped the headline and DROPPED the word "NOT", rendering
    # "AI MEASUREMENT - APPROVED. FOR CHECKING ONLY." on an unapproved measurement. The
    # headline may never wrap; where it cannot fit, the export must refuse outright rather
    # than hand over a drawing that says the opposite of the truth.
    _narrow = _fitz_ua.open()
    _narrow.new_page(width=90, height=70)
    _narrow_src = "/tmp/_ci_marked_narrow.pdf"
    _narrow.save(_narrow_src); _narrow.close()
    _narrow_job = _copy.deepcopy(_ua_job)
    _narrow_job["pdf_path"] = _narrow_src
    _narrow_job["result"]["yard_regions"][0]["polygon_pts"] = [[10, 10], [40, 10],
                                                              [40, 40], [10, 40]]
    try:
        _narrow_bytes, _ = _bmp_ua(_narrow_job, _narrow_src)
        _narrow_text = " ".join(
            _fitz_ua.open(stream=_narrow_bytes, filetype="pdf")[0].get_text().split())
        _narrow_ok = "NOT APPROVED. FOR CHECKING ONLY." in _narrow_text
        _narrow_why = _narrow_text[:120]
    except Exception as _narrow_exc:
        _narrow_ok = "too small to carry the UNAPPROVED stamp" in str(_narrow_exc)
        _narrow_why = str(_narrow_exc)[:120]
    ck("a page too narrow for the warning refuses, rather than printing 'APPROVED'",
       _narrow_ok, _narrow_why)
    ck("...and its filename says so too, so it never files next to an approved markup",
       _mpf_ua(_ua_job).endswith("_AI_CHECK_UNAPPROVED.pdf"), _mpf_ua(_ua_job))
    _ua_job_ok = _copy.deepcopy(_ua_job); _ua_job_ok["decision"] = "approved"
    ck("...while an APPROVED job keeps its revisioned issue name and carries no stamp",
       _mpf_ua(_ua_job_ok).endswith("_REV_01_MARKED.pdf")
       and "NOT APPROVED" not in _fitz_ua.open(
           stream=_bmp_ua(_ua_job_ok, _ua_src)[0], filetype="pdf")[0].get_text(),
       _mpf_ua(_ua_job_ok))

except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] marked-PDF export tests — missing dependency or file: {_e}")

