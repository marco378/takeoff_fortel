#!/usr/bin/env python3
"""deploy survival, job identity, case export, upload size, BOQ misfiling.

Sections in this module (printed in this order):
  - [deploy survival / job identity / case export]
  - [tender-pack upload size]
  - [BOQ section misfiling]

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("\n[deploy survival / job identity / case export]")
import os as _os_ds
from pathlib import Path as _Path_ds
from storage_paths import resolve_storage_paths as _rsp

_vol = "/mnt/fortel-data"
_paths_vol = _rsp({"RAILWAY_VOLUME_MOUNT_PATH": _vol}, app_dir="/app")
ck("volume mounted: jobs file lands on the volume, not the ephemeral app dir",
   str(_paths_vol.jobs_file) == f"{_vol}/approval_jobs.json", _paths_vol.jobs_file)
ck("volume mounted: uploaded drawings survive a deploy (on the volume)",
   str(_paths_vol.drawings_dir) == f"{_vol}/drawings", _paths_vol.drawings_dir)
ck("volume mounted: generated quotations survive a deploy (on the volume)",
   str(_paths_vol.quotations_dir) == f"{_vol}/quotations", _paths_vol.quotations_dir)
ck("volume mounted: archive + backups survive a deploy too",
   str(_paths_vol.jobs_archive_file).startswith(_vol)
   and str(_paths_vol.backup_dir).startswith(_vol),
   (_paths_vol.jobs_archive_file, _paths_vol.backup_dir))

# The real failure mode: a deploy replaces the container (new app dir, same volume). Every
# artifact a half-finished assessment needs must resolve to the SAME location afterwards.
_before = _rsp({"RAILWAY_VOLUME_MOUNT_PATH": _vol}, app_dir="/app")
_after = _rsp({"RAILWAY_VOLUME_MOUNT_PATH": _vol}, app_dir="/app-redeploy-2")
ck("deploy cycle: a new container resolves the SAME jobs/drawings/quotations paths — an "
   "in-flight assessment is still resumable after a redeploy",
   (_before.jobs_file, _before.drawings_dir, _before.quotations_dir)
   == (_after.jobs_file, _after.drawings_dir, _after.quotations_dir),
   (_after.jobs_file, _after.drawings_dir, _after.quotations_dir))
_no_vol = _rsp({}, app_dir="/app")
_no_vol_after = _rsp({}, app_dir="/app-redeploy-2")
ck("no volume: paths follow the app dir and therefore do NOT survive a deploy — the "
   "difference between the two states must stay visible, not silently equal",
   _no_vol.jobs_file != _no_vol_after.jobs_file, (_no_vol.jobs_file, _no_vol_after.jobs_file))
ck("local dev without a volume keeps repo-local paths",
   str(_no_vol.jobs_file) == "/app/approval_jobs.json", _no_vol.jobs_file)
ck("explicit env overrides still win over the volume default",
   str(_rsp({"RAILWAY_VOLUME_MOUNT_PATH": _vol,
             "DRAWINGS_DIR": "/elsewhere/d"}, app_dir="/app").drawings_dir) == "/elsewhere/d")

# Ghost jobs: the portal creates a job at upload. If the approval email creates a SECOND one,
# the assessor's emailed link points at a record the portal never shows, and approving it
# leaves the real case pending forever.  request_approval must attach to the caller's job.
import tempfile as _tf_ds, json as _json_ds
import approval_email as _ae_ds
_ae_orig = (_ae_ds.JOBS_FILE, _ae_ds.render_snapshot, _ae_ds.send_email,
            _ae_ds._send_email_result)
try:
    _tmp_jobs = _Path_ds(_tf_ds.mkdtemp()) / "approval_jobs.json"
    _ae_ds.JOBS_FILE = _tmp_jobs
    _ae_ds.render_snapshot = lambda *a, **k: b"\x89PNG\r\n\x1a\n"
    _ae_ds.send_email = lambda *a, **k: True
    _ae_ds._send_email_result = lambda *a, **k: {
        "sent": True, "status": "sent", "reason": ""}
    _portal_job = _ae_ds.create_job("/x/site.pdf", {"file": "site.pdf", "area_m2": 1.0})
    _returned = _ae_ds.request_approval("/x/site.pdf", {"file": "site.pdf", "area_m2": 2.0},
                                        job_id=_portal_job)
    _jobs_after = _json_ds.loads(_tmp_jobs.read_text())
    ck("approval email attaches to the portal's existing job — no duplicate ghost record",
       len(_jobs_after) == 1, sorted(_jobs_after))
    ck("...and the emailed link carries the portal's OWN job id",
       _returned == _portal_job, (_returned, _portal_job))
    ck("...and the job record is refreshed with the new measurement, not left stale",
       _jobs_after[_portal_job]["result"]["area_m2"] == 2.0, _jobs_after[_portal_job]["result"])
    # A standalone/CLI caller with no job of its own must still get one created.
    _standalone = _ae_ds.request_approval("/x/other.pdf", {"file": "other.pdf", "area_m2": 3.0})
    ck("standalone caller (no job_id) still gets a job created — behaviour unchanged",
       _standalone != _portal_job and len(_json_ds.loads(_tmp_jobs.read_text())) == 2)
    # A stale/unknown id means the caller's real job is gone. Creating one here would be the
    # ghost bug by another route, so it must refuse loudly and leave the store untouched.
    _before_stale = len(_json_ds.loads(_tmp_jobs.read_text()))
    try:
        _ae_ds.request_approval("/x/z.pdf", {"file": "z.pdf", "area_m2": 4.0},
                                job_id="deadbeef")
        _refused, _why = False, "no error raised"
    except ValueError as _e_stale:
        _refused, _why = True, str(_e_stale)
    ck("unknown job_id is refused loudly rather than silently creating a ghost", _refused, _why)
    ck("...and the refusal leaves the job store untouched",
       len(_json_ds.loads(_tmp_jobs.read_text())) == _before_stale)
finally:
    (_ae_ds.JOBS_FILE, _ae_ds.render_snapshot, _ae_ds.send_email,
     _ae_ds._send_email_result) = _ae_orig

# The pipeline must expose the parameter the portal now passes; a silent signature drift here
# would reinstate the ghost-job bug without any test failing.
import inspect as _insp_ds, takeoff_pipeline as _tp_ds
ck("takeoff() accepts approval_job_id so the portal can own the job identity",
   "approval_job_id" in _insp_ds.signature(_tp_ds.takeoff).parameters)

# One case, two documents, perimeters arriving by DIFFERENT routes (a top-level perimeter_lm
# and a zone-carried one). These share a measurement key, so any future divergence in how the
# two rows are seeded breaks the whole case's export — not just one document's.
from quotation import generate_quotation as _gq_ds, quotation_xlsx as _qx_ds
_doc_top = {"file": "top.pdf", "area_m2": 100.0, "perimeter_lm": 40.0,
            "costing": {"area_m2": 100.0, "rate": 50.0,
                        "spec": {"depth_mm": 150, "mesh": "A393"}, "assumed": False}}
_doc_zone = {"file": "zone.pdf", "area_m2": 200.0,
             "costing": {"area_m2": 200.0, "rate": 50.0,
                         "spec": {"depth_mm": 150, "mesh": "A393"}, "assumed": False},
             "zones": [{"category": "external_yard", "perimeter_lm": 80.0, "area_m2": 200.0}]}
for _label, _case in (("top-level only", [_doc_top]), ("zone-carried only", [_doc_zone]),
                      ("mixed", [_doc_top, _doc_zone]),
                      ("mixed, reversed order", [_doc_zone, _doc_top])):
    try:
        _q_ds = _gq_ds(_case, project="P", client="C")
        _xl_ds = _qx_ds(_q_ds)
        _ok_ds, _g_ds = bool(_xl_ds), f"{len(_xl_ds)} bytes"
    except Exception as _e_ds:
        _ok_ds, _g_ds = False, f"{type(_e_ds).__name__}: {_e_ds}"
    ck(f"case export survives perimeters from mixed sources ({_label})", _ok_ds, _g_ds)

_q_mixed = _gq_ds([_doc_top, _doc_zone], project="P", client="C")
_perims = [m for m in _q_mixed["measurements"] if m["description"] == "Slab perimeter"]
ck("both documents' perimeters reach the take-off, aggregated not dropped",
   len(_perims) == 1 and abs(_perims[0]["qty"] - 120.0) < 0.01, _perims)
ck("...and each document keeps its own provenance row",
   len(_perims[0].get("quantity_rows") or []) == 2, _perims[0].get("quantity_rows"))




# ── Tender-pack upload size (Aryan 20 Aug: "the zip upload is still not working") ──────
# Root cause found by EXECUTION, not by reading: a real 118 MB pack returned HTTP 413 from
# Flask's HTML error page, which the portal's `r.json().catch(()=>({}))` turned into an empty
# object and a meaningless toast. Grepping for accept=".pdf,.zip" had "confirmed" zip support
# and proved nothing — the cap, not the wiring, was the blocker.
print("\n[tender-pack upload size]")
import importlib as _il_up
import approval_server as _AS_up
ck("default upload cap fits a real tender pack (packs run 100 MB - 2.4 GB)",
   _AS_up.MAX_UPLOAD_MB >= 1024, f"{_AS_up.MAX_UPLOAD_MB} MB")
ck("cap is env-configurable so a small host lowers it deliberately, not by accident",
   "MAX_UPLOAD_MB" in _AS_up.os.environ or _AS_up.MAX_UPLOAD_MB == 2048, _AS_up.MAX_UPLOAD_MB)
_c_up = _AS_up.app.test_client()
_r_up = _AS_up.app.response_class
with _AS_up.app.test_request_context():
    _resp_up = _AS_up._upload_too_large(None)
_body_up, _code_up = _resp_up[0].get_json(), _resp_up[1]
ck("oversized upload answers 413 with JSON the portal can display, not an HTML page",
   _code_up == 413 and isinstance(_body_up, dict) and "error" in _body_up, _body_up)
ck("...and the message tells the user what to do about it",
   "limit" in _body_up.get("error", "") and _body_up.get("max_upload_mb"), _body_up.get("error"))




# ── Section misfiling: the 5.2 Longwell "second area" / "wrong excel sheet" bug ─────────
# Inderjit, 20 Aug, sharing his screen: "I have approved this area only. Where did this second
# area come from" — he had classified it "dock slab". Aryan, same call: "it didn't pick up the
# correct sheet to mount the values on ... designed to use this as the fallback when the
# current sheet it's unable to pick up." Root cause: _normalise_section answered with the
# External-yard fallback for ANY spelling outside its alias table, so a dock area was priced
# as yard, with the yard rows and none of the dock formulas. Two reported bugs, one defect.
print("\n[BOQ section misfiling]")
from quotation import _normalise_section as _ns, UNCLASSIFIED_SECTION as _UNCLS
from quotation import generate_quotation as _gq_sec

for _probe in ("Dock Slab", "dock_slab", "Dock-Slab", "DOCK", "dock slabs"):
    ck(f"dock spelling {_probe!r} reaches Dock slabs, never the yard fallback",
       _ns(_probe) == "Dock slabs", _ns(_probe))
for _probe, _want in (("Upper floor", "Upper floor slabs"), ("upper_floor", "Upper floor slabs"),
                      ("ground_floor", "Ground floor slabs"), ("footpaths", "Footpath slabs")):
    ck(f"{_probe!r} maps to {_want}", _ns(_probe) == _want, _ns(_probe))
ck("an UNRECOGNISED section is surfaced for classification, not silently priced as yard",
   _ns("totally unknown thing") == _UNCLS, _ns("totally unknown thing"))
ck("a blank section still uses the caller's contextual default (a real default, not a misfile)",
   _ns("") == "External yard slabs" and _ns(None) == "External yard slabs")

# End-to-end: the exact shape of Inderjit's job — one yard area he approved, plus a second
# area he classified as dock. The dock quantity must never appear under External yard slabs.
for _label in ("Dock Slab", "dock_slab", "dock"):
    _doc_sec = {"file": "longwell.pdf", "area_m2": 500.0,
                "costing": {"area_m2": 500.0, "rate": 50.0,
                            "spec": {"depth_mm": 190, "mesh": "A393", "layers": 1},
                            "assumed": False},
                "zones": [{"category": "external_yard", "area_m2": 500.0}],
                "area_elements": [{"element_id": "ae-1", "name": "Dock apron", "category": "dock",
                                   "boq_scope": "main", "area_m2": 120.0, "section": _label}]}
    _q_sec = _gq_sec([_doc_sec], project="5.2 Longwell", client="Fortel")
    _yard_qtys = {li.get("qty") for li in _q_sec["line_items"]
                  if li.get("section") == "External yard slabs" and li.get("unit") == "m²"}
    ck(f"dock area classified {_label!r} does not surface under External yard slabs",
       120.0 not in _yard_qtys, sorted(_yard_qtys))

# ── A measured zone with no BOQ section must not vanish from the quotation ───────────────
# 27 Aug call: an element "created with the wrong tool" stopped contributing to the total area
# and lost its thickness/mesh/finish. _expand_zone_results filtered zones to the four known
# categories, and because expansion REPLACES the parent result, anything left behind left the
# quotation silently. The portal's classification dropdown collapsing to "other region" is
# exactly how an assessor reaches that state. Losing measured area without a word is the same
# contract breach as emitting a silent number, pointed the other way.
_SPEC_ZC = {"depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40", "layers": 1, "conc_rate": 128}


def _zc_doc(category):
    return {"file": "Yard.pdf", "type": "MARKED vector", "confidence": "high",
            "source_discipline": "engineer",
            "costing": {"area_m2": 1000, "rate": 44.89, "total_gbp": 44890.0,
                        "assumed": False, "spec": dict(_SPEC_ZC)},
            "zones": [{"zone_key": "external_yard", "category": "external_yard",
                       "measurement_kind": "area", "area_m2": 1000.0,
                       "spec": dict(_SPEC_ZC), "subjects": ["Yard"]},
                      {"zone_key": "x", "category": category, "measurement_kind": "area",
                       "area_m2": 250.0, "spec": dict(_SPEC_ZC),
                       "subjects": ["Mis-tooled element"]}],
            "flags": []}


for _zc_cat in ("other region", "unclassified", "", "some tool the portal invented"):
    _q_zc = _gq_sec(_zc_doc(_zc_cat), project="P", client="Fortel", ref="TST-ZC")
    _zc_slabs = [li for li in _q_zc["line_items"] if li.get("line_role") == "concrete_slab"]
    _zc_total = sum(li.get("qty") or 0 for li in _zc_slabs)
    ck(f"zone categorised {_zc_cat!r}: its 250 m² is still in the quotation, not dropped",
       _zc_total == 1250.0, f"slab total {_zc_total}")
    _zc_row = [li for li in _zc_slabs if li.get("section") == _UNCLS]
    ck(f"zone categorised {_zc_cat!r}: surfaced as unclassified for the assessor",
       len(_zc_row) == 1 and _zc_row[0].get("qty") == 250.0, [li.get("section") for li in _zc_slabs])
    ck(f"zone categorised {_zc_cat!r}: carries NO rate — none is inherited or invented",
       _zc_row and _zc_row[0].get("rate") is None
       and _zc_row[0].get("assessor_rate_required") is True, _zc_row)
    ck(f"zone categorised {_zc_cat!r}: the assessor is told why it is unpriced",
       any("UNCLASSIFIED ZONE CARRIED" in str(_f) for _f in _q_zc.get("pipeline_flags") or []))

# A recognised category must be completely unaffected by the above.
_q_zc_dock = _gq_sec(_zc_doc("dock"), project="P", client="Fortel", ref="TST-ZC-DOCK")
_zc_dock_secs = {li.get("section") for li in _q_zc_dock["line_items"]
                 if li.get("line_role") == "concrete_slab"}
ck("a recognised 'dock' zone still lands in Dock slabs, unchanged",
   _zc_dock_secs == {"External yard slabs", "Dock slabs"}, sorted(_zc_dock_secs))
ck("...and nothing is marked unclassified when every zone is recognised",
   not any("UNCLASSIFIED ZONE CARRIED" in str(_f)
           for _f in _q_zc_dock.get("pipeline_flags") or []))




# ── Phantom approve block after zone classification ────────────────────────────────────
# Inderjit, 20 Aug: "I just tried on one project today and tried adjusting the AI's markup but
# it is not getting approved... Cannot approve zone classification." He hit it repeatedly; the
# known workaround was Aryan's "you have to re submit the zone classification" — which is the
# diagnosis, not a fix. /zones cleared zone_classification_required but left
# zone_allocation_stale set, so approval still demanded "reclassify/remeasure" — the thing he
# had just done. A gate with no route out is a dead end, and dead ends are the bug.
