#!/usr/bin/env python3
"""storage_paths resolver, approved-only pattern memory, learning episodes.

Sections in this module (printed in this order):
  - persistent storage: one volume-aware resolver keeps jobs, drawings and quotations together
  - approved-only pattern memory: prior approval is built from approved jobs only
  - training log cleanup: fixture contamination filter keeps only real assessor history
  - learning episodes: atomic before/after, refusal recovery, agreement and coverage

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck, REPO_ROOT
from tests.test_quotation import _q

print("persistent storage: one volume-aware resolver keeps jobs, drawings and quotations together")
try:
    import tempfile as _tempfile_storage
    import shutil as _shutil_storage
    import json as _json_storage
    import fitz as _fitz_storage
    import approval_server as _AS_storage
    from storage_paths import resolve_storage_paths as _resolve_storage_paths
    from quotation import save_quotation as _save_quote_storage

    _storage_root = Path(_tempfile_storage.mkdtemp(prefix="ci_volume_"))
    _app_root = _storage_root / "ephemeral-app"
    _volume_root = _storage_root / "railway-volume"
    _local_paths = _resolve_storage_paths({}, app_dir=_app_root)
    _volume_paths = _resolve_storage_paths(
        {"RAILWAY_VOLUME_MOUNT_PATH": str(_volume_root)}, app_dir=_app_root)
    ck("storage paths stay repository-local without a Railway volume",
       _local_paths.jobs_file == _app_root / "approval_jobs.json" and
       _local_paths.drawings_dir == _app_root / "drawings" and
       _local_paths.quotations_dir == _app_root / "quotations", _local_paths)
    ck("Railway volume places job store, drawings and quotations under one persistent base",
       _volume_paths.jobs_file == _volume_root / "approval_jobs.json" and
       _volume_paths.drawings_dir == _volume_root / "drawings" and
       _volume_paths.quotations_dir == _volume_root / "quotations" and
       _volume_paths.training_log_file == _volume_root / "training_log.jsonl" and
       _volume_paths.learned_patterns_file == _volume_root / "learned_patterns.json" and
       _volume_paths.jobs_archive_file.parent == _volume_root and
       _volume_paths.backup_dir.parent == _volume_root, _volume_paths)
    _override_paths = _resolve_storage_paths({
        "RAILWAY_VOLUME_MOUNT_PATH": str(_volume_root),
        "JOBS_FILE": str(_storage_root / "custom" / "jobs.qa.json"),
        "DRAWINGS_DIR": str(_storage_root / "custom-drawings"),
        "QUOTATIONS_DIR": str(_storage_root / "custom-quotes"),
    }, app_dir=_app_root)
    ck("explicit jobs/drawings/quotations path overrides still win over the volume default",
       _override_paths.jobs_file == _storage_root / "custom" / "jobs.qa.json" and
       _override_paths.drawings_dir == _storage_root / "custom-drawings" and
       _override_paths.quotations_dir == _storage_root / "custom-quotes" and
       _override_paths.training_log_file == _storage_root / "custom" / "jobs.qa_training_log.jsonl" and
       _override_paths.learned_patterns_file == _storage_root / "custom" / "jobs.qa_learned_patterns.json",
       _override_paths)

    _orig_storage_jobs = _AS_storage.JOBS_FILE
    _orig_storage_backups = _AS_storage.BACKUP_DIR
    try:
        _AS_storage.JOBS_FILE = _volume_paths.jobs_file
        _AS_storage.BACKUP_DIR = _volume_paths.backup_dir
        _volume_paths.drawings_dir.mkdir(parents=True, exist_ok=True)
        _persistent_pdf = _volume_paths.drawings_dir / "PERSIST-001_plan.pdf"
        _persistent_doc = _fitz_storage.open()
        _persistent_doc.new_page(width=100, height=100)
        _persistent_doc.save(_persistent_pdf)
        _persistent_doc.close()
        _persistent_quote_paths = _save_quote_storage(
            _q, out_dir=str(_volume_paths.quotations_dir))
        _AS_storage.save_jobs({"persist-1": {
            "id": "persist-1", "status": "approved",
            "pdf_path": str(_persistent_pdf),
            "quotation_paths": _persistent_quote_paths,
        }})

        # Re-run the pure resolver exactly as a fresh process does after a deploy.  Nothing
        # from the ephemeral application directory is consulted.
        _fresh_paths = _resolve_storage_paths(
            {"RAILWAY_VOLUME_MOUNT_PATH": str(_volume_root)}, app_dir=_app_root)
        _fresh_jobs = _json_storage.loads(_fresh_paths.jobs_file.read_text())
        _fresh_job = _fresh_jobs["persist-1"]
        ck("deploy-cycle re-resolution preserves job record, uploaded PDF and quotation",
           Path(_fresh_job["pdf_path"]).is_file() and
           Path(_fresh_job["quotation_paths"]["xlsx"]).is_file() and
           Path(_fresh_job["quotation_paths"]["json"]).is_file() and
           Path(_fresh_job["pdf_path"]).is_relative_to(_fresh_paths.drawings_dir) and
           Path(_fresh_job["quotation_paths"]["xlsx"]).is_relative_to(
               _fresh_paths.quotations_dir), _fresh_job)
    finally:
        _AS_storage.JOBS_FILE = _orig_storage_jobs
        _AS_storage.BACKUP_DIR = _orig_storage_backups
        _shutil_storage.rmtree(_storage_root, ignore_errors=True)
except (ImportError, OSError, ValueError) as _e:
    ck("persistent storage deploy-cycle test imports and runs", False, _e)

print("approved-only pattern memory: prior approval is built from approved jobs only")
try:
    import tempfile as _tempfile_memory
    import shutil as _shutil_memory
    from training_analytics import build_learned_patterns as _build_learned_patterns
    from training_analytics import prior_approval_for_job as _prior_approval_for_job
    from training_analytics import attach_prior_approval as _attach_prior_approval
    import approval_server as _AS_memory

    _memory_root = Path(_tempfile_memory.mkdtemp(prefix="ci_memory_"))
    _orig_memory_jobs = _AS_memory.JOBS_FILE
    _orig_memory_log = _AS_memory.TRAINING_LOG
    _orig_memory_patterns = _AS_memory.LEARNED_PATTERNS_FILE
    try:
        _AS_memory.JOBS_FILE = _memory_root / "jobs.json"
        _AS_memory.TRAINING_LOG = _memory_root / "training_log.jsonl"
        _AS_memory.LEARNED_PATTERNS_FILE = _memory_root / "learned_patterns.json"
        _approved_job = {
            "id": "approved-1", "status": "approved", "decision": "approved",
            "document_sha256": "a" * 64,
            "project_ref": "MEM-001", "project_name": "Memory Case",
            "result": {"file": "memory.pdf", "area_m2": 100.0, "project_ref": "MEM-001",
                        "project_name": "Memory Case", "scale_src": "bar"},
            "costing": {"assumed": False},
            "flags": ["approved-flag"],
            "decided_at": "2026-08-13T10:00:00",
        }
        _adjusted_job = {
            "id": "adjusted-1", "status": "adjusted", "decision": "adjusted",
            "project_ref": "MEM-001", "project_name": "Memory Case",
            "result": {"file": "memory.pdf", "area_m2": 110.0, "project_ref": "MEM-001",
                        "project_name": "Memory Case"},
            "adjusted": {"area_m2": 110.0},
            "flags": ["adjusted-flag"],
            "decided_at": "2026-08-13T11:00:00",
        }
        _pending_job = {
            "id": "pending-1", "status": "pending", "decision": None,
            "document_sha256": "a" * 64,
            "project_ref": "MEM-001", "project_name": "Memory Case",
            "result": {"file": "memory.pdf", "area_m2": 120.0, "project_ref": "MEM-001",
                        "project_name": "Memory Case"},
        }
        _AS_memory.save_jobs({
            "approved-1": _approved_job,
            "adjusted-1": _adjusted_job,
            "pending-1": _pending_job,
        })
        _patterns_memory = _build_learned_patterns(_AS_memory.load_jobs())
        ck("approved-only builder ignores adjusted jobs",
           "memory.pdf" in _patterns_memory["by_file"] and
           _patterns_memory["by_file"]["memory.pdf"]["latest"]["job_id"] == "approved-1" and
           len(_patterns_memory["by_file"]["memory.pdf"]["history"]) == 1,
           _patterns_memory["by_file"].get("memory.pdf"))
        _prior_memory = _prior_approval_for_job(_pending_job, _patterns_memory)
        ck("matching pending job gets prior approval from approved job only",
           _prior_memory and _prior_memory.get("job_id") == "approved-1" and
           _prior_memory.get("matched_on") == "exact document SHA-256", _prior_memory)
        _attached_memory = _attach_prior_approval("pending-1", _pending_job, _patterns_memory)
        ck("attach_prior_approval adds informational prior_approval payload",
           _attached_memory.get("prior_approval", {}).get("job_id") == "approved-1", _attached_memory)
        ck("prior approval payload contains identity only, never learned quantities or money",
           set(_attached_memory.get("prior_approval", {})) == {
               "job_id", "approved_at", "document_sha256", "matched_on"
           }, _attached_memory.get("prior_approval"))
        _different_drawing = dict(_pending_job, document_sha256="b" * 64)
        ck("same filename/project never decorates a different drawing as previously approved",
           _prior_approval_for_job(_different_drawing, _patterns_memory) is None)
        ck("an approved job is never presented as its own prior approval",
           "prior_approval" not in _attach_prior_approval(
               "approved-1", _approved_job, _patterns_memory))
        _memory_portal = (REPO_ROOT / "assessor_portal.html").read_text()
        ck("prior-approval UI is neutral and explicitly says no measurement/price was reused",
           "This exact drawing file was approved previously" in _memory_portal and
           "no prior measurement or price was reused" in _memory_portal and
           "Previously approved as-is" not in _memory_portal)
        ck("prior_approval does not affect approve-block logic",
           _AS_memory._approve_block_reason({
               "measurement_state": "MEASURED_VERIFIED",
               "scale_confirmed": False,
               "prior_approval": _attached_memory.get("prior_approval"),
           }) is None)
    finally:
        _AS_memory.JOBS_FILE = _orig_memory_jobs
        _AS_memory.TRAINING_LOG = _orig_memory_log
        _AS_memory.LEARNED_PATTERNS_FILE = _orig_memory_patterns
        _shutil_memory.rmtree(_memory_root, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approved-only pattern memory tests — missing dependency: {_e}")

print("training log cleanup: fixture contamination filter keeps only real assessor history")
try:
    import json as _json_cleanup
    from cleanup_training_log import clean_entries as _clean_training_log_entries
    from cleanup_training_log import backup_path_for as _cleanup_backup_path

    _clean_sample = [
        _json_cleanup.dumps({"event": "approve", "job_id": "job-csrf-approve", "file": "csrf_test.pdf"}),
        _json_cleanup.dumps({"event": "reject", "job_id": "real-1", "file": "12345-real.pdf"}),
        _json_cleanup.dumps({"event": "approve", "job_id": "55555555-5555-4555-8555-555555555555",
                             "file": "Mixed perimeter case.pdf"}),
        _json_cleanup.dumps({"event": "adjust", "job_id": "client-real",
                             "file": "Office-GA.pdf", "project_ref": "CASE-REAL-2026"}),
        "malformed-but-potentially-real-evidence",
    ]
    _kept_log, _removed_log = _clean_training_log_entries(_clean_sample)
    ck("cleanup removes explicit fixture lines and preserves real entries",
       len(_kept_log) == 3 and len(_removed_log) == 2 and
       any('real-1' in line for line in _kept_log) and
       any('client-real' in line for line in _kept_log) and
       "malformed-but-potentially-real-evidence" in _kept_log, _removed_log)
    ck("cleanup backup names are collision-resistant even within one second",
       _cleanup_backup_path(Path("training.jsonl")) !=
       _cleanup_backup_path(Path("training.jsonl")))
except ImportError as _e:
    print(f"  [SKIP] training log cleanup tests — missing dependency: {_e}")

print("learning episodes: atomic before/after, refusal recovery, agreement and coverage")
try:
    import copy as _copy_learning
    import json as _json_learning
    import tempfile as _tempfile_learning
    import shutil as _shutil_learning
    import approval_server as _AS_learning
    from learning_capture import ensure_learning_episode as _ensure_episode
    from learning_capture import measurement_snapshot as _learning_snapshot
    from training_analytics import prior_approval_for_job as _prior_exact
    from training_analytics import analytics_report as _learning_analytics

    _learning_root = Path(_tempfile_learning.mkdtemp(prefix="ci_learning_episode_"))
    _old_learning_paths = (
        _AS_learning.JOBS_FILE, _AS_learning.BACKUP_DIR, _AS_learning.TRAINING_LOG,
        _AS_learning.LEARNED_PATTERNS_FILE,
    )
    _old_learning_functions = (_AS_learning._save_quotation, _AS_learning._run_costing)
    try:
        _AS_learning.JOBS_FILE = _learning_root / "jobs.json"
        _AS_learning.BACKUP_DIR = _learning_root / "backups"
        # Directories deliberately make both derivative writes fail. The authoritative job
        # episode must still commit and the HTTP action must still succeed.
        _AS_learning.TRAINING_LOG = _learning_root
        _AS_learning.LEARNED_PATTERNS_FILE = _learning_root
        _AS_learning._save_quotation = lambda *_a, **_k: {"xlsx": "scratch.xlsx"}
        _AS_learning._run_costing = lambda area, _result: {
            "area_m2": area, "rate": 1.0, "total_gbp": area,
        }
        _approved_seed = {
            "id": "learn-agree", "status": "pending", "decision": None,
            "measurement_state": "MEASURED_VERIFIED", "area_m2": 100.0,
            "document_sha256": "c" * 64,
            "prior_approval": {"area_m2": 999999.0, "rate": 999999.0,
                               "total_gbp": 999999.0},
            "result": {"file": "Exact.pdf", "area_m2": 100.0,
                       "measurement_state": "MEASURED_VERIFIED", "flags": []},
            "flags": [],
        }
        _ensure_episode("learn-agree", _approved_seed, source="pipeline")
        _AS_learning.save_jobs({"learn-agree": _approved_seed})
        with _AS_learning.app.test_client() as _client_learning:
            _agree_response = _client_learning.post(
                "/approve/learn-agree", json={"note": "agrees with AI"})
        _agree_saved = _AS_learning.load_jobs()["learn-agree"]
        _agree_episode = _agree_saved.get("learning_episode") or {}
        ck("training/cache write failure cannot split an approved decision from its evidence",
           _agree_response.status_code == 200 and _agree_saved.get("status") == "approved" and
           (_agree_episode.get("terminal") or {}).get("event") == "approved_unchanged",
           {"http": _agree_response.status_code, "episode": _agree_episode})
        ck("learned numeric payload cannot influence fresh approved costing",
           (_agree_response.get_json().get("costing") or {}).get("area_m2") == 100.0 and
           (_agree_response.get_json().get("costing") or {}).get("area_m2") != 999999.0,
           _agree_response.get_json())
        _commercial_probe = _learning_snapshot({
            "zones": [{"area_m2": 10.0, "rate": 99.0, "total_gbp": 990.0}],
            "adjusted": {"area_m2": 10.0, "costing": {"rate": 99.0}},
        })
        ck("atomic learning snapshots exclude rates, totals and nested costing",
           "rate" not in _json_learning.dumps(_commercial_probe) and
           "total_gbp" not in _json_learning.dumps(_commercial_probe) and
           "costing" not in _json_learning.dumps(_commercial_probe), _commercial_probe)

        _refused_seed = {
            "id": "learn-refusal", "status": "error", "decision": None,
            "measurement_state": "UNMEASURED", "area_m2": None,
            "result": {"file": "Office raw.pdf", "area_m2": None,
                       "measurement_state": "UNMEASURED", "flags": ["trace manually"]},
            "flags": ["trace manually"],
        }
        _ensure_episode("learn-refusal", _refused_seed, source="pipeline")
        _jobs_learning = _AS_learning.load_jobs()
        _jobs_learning["learn-refusal"] = _refused_seed
        _AS_learning.save_jobs(_jobs_learning)
        with _AS_learning.app.test_client() as _client_learning:
            _refusal_response = _client_learning.post("/adjust/learn-refusal", json={
                "regions": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                "region_categories": ["ground_floor"],
                "region_scopes": ["ground_floor_core"], "scale_k": 0.1,
            })
        _refusal_saved = _AS_learning.load_jobs()["learn-refusal"]
        _refusal_events = (_refusal_saved.get("learning_episode") or {}).get("events") or []
        _refusal_event = _refusal_events[-1] if _refusal_events else {}
        ck("UNMEASURED manual trace is captured as refusal_recovered with original and final",
           _refusal_response.status_code == 200 and
           _refusal_event.get("event") == "refusal_recovered" and
           _refusal_event.get("before", {}).get("area_m2") is None and
           _refusal_event.get("after", {}).get("area_m2") == 100.0,
           {"http": _refusal_response.status_code, "event": _refusal_event})
        with _AS_learning.app.test_client() as _client_learning:
            _refusal_approve = _client_learning.post("/approve/learn-refusal", json={})
        _analytics_rows = {
            row["job_id"]: row for row in
            _learning_analytics(_AS_learning.load_jobs())["approved_jobs"]
        }
        ck("analytics uses the frozen episode pair and keeps refusal originals honest",
           _refusal_approve.status_code == 200 and
           _analytics_rows["learn-agree"]["learning_outcome"] == "approved_unchanged" and
           _analytics_rows["learn-agree"]["delta_m2"] == 0.0 and
           _analytics_rows["learn-refusal"]["learning_outcome"] ==
           "approved_after_correction" and
           _analytics_rows["learn-refusal"]["ai_area_m2"] is None and
           _analytics_rows["learn-refusal"]["assessed_area_m2"] == 100.0,
           _analytics_rows)

        _malicious = {"by_document_sha256": {"d" * 64: {"latest": {
            "job_id": "old", "approved_at": "now", "document_sha256": "d" * 64,
            "area_m2": 999999, "rate": 999999, "total_gbp": 999999,
        }}}}
        _safe_prior = _prior_exact({"document_sha256": "d" * 64}, _malicious)
        ck("even a malicious learned cache exposes no measurement, rate or total",
           set(_safe_prior or {}) == {
               "job_id", "approved_at", "document_sha256", "matched_on"
           }, _safe_prior)
        from training_analytics import update_learned_patterns as _update_safe_patterns
        _scrubbed_patterns = _update_safe_patterns(_malicious, "new-safe", {
            "status": "approved", "decision": "approved",
            "document_sha256": "e" * 64, "result": {"file": "safe.pdf"},
        })
        _scrubbed_old = (_scrubbed_patterns["by_document_sha256"]["d" * 64]
                         .get("latest") or {})
        ck("refresh scrubs legacy area/rate/total fields from the derivative cache",
           not ({"area_m2", "rate", "total_gbp"} & set(_scrubbed_old)), _scrubbed_old)

        import threading as _threading_learning
        import time as _time_learning
        import training_analytics as _TA_learning
        _AS_learning.LEARNED_PATTERNS_FILE = _learning_root / "concurrent_patterns.json"
        _original_increment = _TA_learning.update_learned_patterns
        def _slow_increment(*args, **kwargs):
            value = _original_increment(*args, **kwargs)
            _time_learning.sleep(0.03)
            return value
        _TA_learning.update_learned_patterns = _slow_increment
        try:
            _threads_learning = [
                _threading_learning.Thread(
                    target=_AS_learning._refresh_learned_patterns,
                    args=(f"parallel-{index}", {
                        "status": "approved", "decision": "approved",
                        "document_sha256": str(index) * 64,
                        "result": {"file": f"parallel-{index}.pdf"},
                    }),
                ) for index in (1, 2)
            ]
            for _thread_learning in _threads_learning:
                _thread_learning.start()
            for _thread_learning in _threads_learning:
                _thread_learning.join()
        finally:
            _TA_learning.update_learned_patterns = _original_increment
        _parallel_patterns = _json_learning.loads(
            _AS_learning.LEARNED_PATTERNS_FILE.read_text())
        ck("concurrent approvals cannot lose one another from the learned-pattern cache",
           set(_parallel_patterns.get("by_document_sha256", {})) == {
               "1" * 64, "2" * 64
           }, _parallel_patterns.get("by_document_sha256"))
        _AS_learning.LEARNED_PATTERNS_FILE = _learning_root

        _coverage_seed = {
            "id": "learn-coverage", "status": "pending", "decision": None,
            "measurement_state": "MEASURED_VERIFIED", "area_m2": 100.0,
            "scale_k": 0.1, "flags": [],
            "zones": [{"zone_key": "unknown-1", "category": "unclassified",
                       "area_m2": 100.0, "measurement_kind": "area",
                       "subjects": ["Unit 1"], "needs_assessor": True}],
            "channel_proposals": [{"proposal_id": "channel-1",
                                   "proposed_length_lm": 10.0,
                                   "polyline_pts": [[0, 0], [100, 0]]}],
            "transition_candidates": [{"candidate_id": "transition-1",
                                       "proposed_length_lm": 5.0,
                                       "polyline_pts": [[0, 0], [50, 0]],
                                       "basis": "assessor review"}],
        }
        _coverage_seed["result"] = {
            "file": "Coverage.pdf", "area_m2": 100.0,
            "measurement_state": "MEASURED_VERIFIED", "flags": [],
            "zones": _copy_learning.deepcopy(_coverage_seed["zones"]),
            "channel_proposals": _copy_learning.deepcopy(_coverage_seed["channel_proposals"]),
            "transition_candidates": _copy_learning.deepcopy(
                _coverage_seed["transition_candidates"]),
            "scale_k": 0.1,
        }
        _ensure_episode("learn-coverage", _coverage_seed, source="pipeline")
        _jobs_learning = _AS_learning.load_jobs()
        _jobs_learning["learn-coverage"] = _coverage_seed
        _AS_learning.save_jobs(_jobs_learning)
        with _AS_learning.app.test_client() as _client_learning:
            _coverage_http = [
                _client_learning.post("/zones/learn-coverage", json={
                    "classifications": [{"zone_key": "unknown-1",
                                         "category": "external_yard"}],
                }).status_code,
                _client_learning.post("/spec-override/learn-coverage", json={
                    "zone_category": "external_yard", "fields": {"depth_mm": 190},
                }).status_code,
                _client_learning.post("/channel-proposals/learn-coverage", json={
                    "decisions": [{"proposal_id": "channel-1", "action": "accept",
                                   "length_lm": 10.0}],
                }).status_code,
                _client_learning.post("/transition-candidates/learn-coverage", json={
                    "decisions": [{"candidate_id": "transition-1", "action": "accept",
                                   "length_lm": 4.5}],
                }).status_code,
            ]
        _coverage_saved = _AS_learning.load_jobs()["learn-coverage"]
        _coverage_events = (_coverage_saved.get("learning_episode") or {}).get("events") or []
        _coverage_types = [event.get("event") for event in _coverage_events]
        ck("zone/spec/channel/transition reviews each preserve atomic before+after evidence",
           _coverage_http == [200, 200, 200, 200] and _coverage_types[-4:] == [
               "zones_classified", "spec_overridden", "channel_proposals_reviewed",
               "transition_candidates_reviewed",
           ] and all(event.get("before") and event.get("after")
                     for event in _coverage_events[-4:]),
           {"http": _coverage_http, "events": _coverage_types})
    finally:
        (_AS_learning.JOBS_FILE, _AS_learning.BACKUP_DIR, _AS_learning.TRAINING_LOG,
         _AS_learning.LEARNED_PATTERNS_FILE) = _old_learning_paths
        (_AS_learning._save_quotation, _AS_learning._run_costing) = _old_learning_functions
        _shutil_learning.rmtree(_learning_root, ignore_errors=True)
except (ImportError, OSError, ValueError) as _e:
    ck("learning episode safeguards import and run", False, _e)

