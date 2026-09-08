#!/usr/bin/env python3
"""approval quotation errors, backups, startup sweep, webhook guard, email.

Sections in this module (printed in this order):
  - critical approval safeguards: quotation errors visible; email uses canonical saved job
  - approval_server: jobs-file backup rotation + corrupt-file preservation (prod-audit MUST)
  - approval_server: startup sweep clears stranded 'processing' jobs on restart (prod-audit MUST)
  - approval_server: /webhook/n8n pdf_path containment guard (prod-audit MUST — arbitrary file read)
  - approval_server: GET on /approve /reject does NOT mutate; POST does (top-level-navigation CSRF fix — SameSite=Lax cookies + a mutating GET meant an email client's link-preview prefetch, or any page merely linking here, could silently approve/reject a job)
  - approval_email: emailed approve/reject/adjust links carry ?token= when the token gate is enabled (token mode previously 401'd every emailed action link)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import shutil
from pathlib import Path
from tests import ck

print("critical approval safeguards: quotation errors visible; email uses canonical saved job")
try:
    import tempfile as _tempfile_critical
    import shutil as _shutil_critical
    import os as _os_critical
    from unittest import mock as _mock_critical
    import approval_server as _AS_critical
    import approval_email as _AE_critical
    import takeoff_pipeline as _TP_critical

    _critical_root = Path(_tempfile_critical.mkdtemp(prefix="ci_critical_"))
    _critical_jobs = _critical_root / "jobs.json"
    _orig_critical_jobs = _AS_critical.JOBS_FILE
    _orig_critical_backups = _AS_critical.BACKUP_DIR
    _orig_critical_quotes = _AS_critical.QUOTATIONS_DIR
    _orig_critical_token = _AS_critical.APPROVAL_TOKEN
    _orig_smtp_pass = _AE_critical.SMTP_PASS
    _AS_critical.JOBS_FILE = _critical_jobs
    _AS_critical.BACKUP_DIR = _critical_root / "backups"
    _AS_critical.QUOTATIONS_DIR = _critical_root / "quotations"
    _AS_critical.APPROVAL_TOKEN = ""
    try:
        _app_critical = _AS_critical.app
        _app_critical.testing = True
        _client_critical = _app_critical.test_client()

        _quotation_failure_id = "quotation-failure-visible"
        _AS_critical.save_jobs({_quotation_failure_id: {
            "id": _quotation_failure_id, "status": "pending", "decision": None,
            "measurement_state": "MEASURED_VERIFIED", "scale_confirmed": False,
            "flags": [], "result": {
                "file": "Mixed perimeter case.pdf", "area_m2": 100.0,
                "measurement_state": "MEASURED_VERIFIED", "flags": [],
            },
        }})
        with _mock_critical.patch.object(
                _AS_critical, "_run_costing", return_value={"area_m2":100.0}), \
                _mock_critical.patch.object(
                    _AS_critical, "_save_quotation",
                    return_value={"error":"KeyError: quantity_rows"}):
            _quotation_failure_response = _client_critical.post(
                f"/approve/{_quotation_failure_id}", json={"note":"reviewed"})
        _quotation_failure_job = _AS_critical.load_jobs()[_quotation_failure_id]
        ck("quotation generation failure returns HTTP 500 and is persisted on the approved job",
           _quotation_failure_response.status_code == 500 and
           _quotation_failure_response.get_json()["status"] == "quotation_error" and
           _quotation_failure_job["decision"] == "approved" and
           _quotation_failure_job["quotation_status"] == "error" and
           _quotation_failure_job["quotation_error"] == "KeyError: quantity_rows" and
           any(flag.startswith("QUOTATION GENERATION ERROR:")
               for flag in _quotation_failure_job["flags"]),
           _quotation_failure_response.get_json())
        ck("portal source renders a specific quotation-error banner and hides broken links",
           "job.quotation_error" in Path("assessor_portal.html").read_text() and
           "Quotation generation failed" in Path("assessor_portal.html").read_text())

        # SEND_APPROVAL_EMAILS can no longer make the pipeline create a second job.  Direct
        # pipeline use has no authoritative persisted context, so it only emits a loud defer
        # flag; the approval-server worker owns delivery after its atomic save.
        _deferred_result = {"file":"direct.pdf", "flags":[]}
        with _mock_critical.patch.object(_AE_critical, "create_job") as _create_job_mock:
            _TP_critical._trigger_approval(
                "direct.pdf", _deferred_result, approval_job_id="real-job",
                send_requested=True)
        ck("direct pipeline approval trigger never creates a duplicate job",
           _create_job_mock.call_count == 0 and
           any(flag.startswith("APPROVAL EMAIL DEFERRED:")
               for flag in _deferred_result["flags"]), _deferred_result)

        _real_job_id = "portal-real-job-id"
        _AS_critical.save_jobs({_real_job_id: {
            "id":_real_job_id, "status":"processing", "decision":None,
            "project_name":"Email case", "project_ref":"EMAIL-001", "flags":[],
            "result":{"file":"EMAIL-001_plan.pdf"},
        }})
        _email_targets = []
        _pipeline_context = []

        def _fake_takeoff_critical(pdf, project_name=None, project_ref=None,
                                   client_rates_path=None, approval_job_id=None):
            _pipeline_context.append(approval_job_id)
            return {
                "file":"EMAIL-001_plan.pdf", "area_m2":100.0,
                "measurement_state":"MEASURED_UNVERIFIED", "needs_assessor":True,
                "project_name":project_name, "project_ref":project_ref,
                "flags":[], "zones":[],
            }

        def _fake_email_critical(job_id, pdf_path, result,
                                 project_name=None, project_ref=None, to=None):
            # This assertion happens inside the notifier: delivery must not start until the
            # completed result is visible in the canonical store.
            saved = _AS_critical.load_jobs()[job_id]
            _email_targets.append((job_id, saved["status"], saved["result"]["file"]))
            return {"job_id":job_id, "sent":True, "status":"sent", "reason":""}

        with _mock_critical.patch.object(
                _TP_critical, "takeoff", new=_fake_takeoff_critical), \
                _mock_critical.patch.object(
                    _AE_critical, "send_job_approval_email",
                    side_effect=_fake_email_critical), \
                _mock_critical.patch.dict(
                    _os_critical.environ, {"SEND_APPROVAL_EMAILS":"1"}):
            _AS_critical._run_takeoff(
                _real_job_id, str(_critical_root / "EMAIL-001_plan.pdf"),
                "Email case", "EMAIL-001")
        _email_jobs = _AS_critical.load_jobs()
        ck("worker email targets the portal's real saved job_id and creates no duplicate",
           list(_email_jobs) == [_real_job_id] and
           _pipeline_context == [_real_job_id] and
           _email_targets == [(_real_job_id, "pending", "EMAIL-001_plan.pdf")] and
           _email_jobs[_real_job_id]["approval_email_status"] == "sent",
           {"jobs":list(_email_jobs), "targets":_email_targets,
            "pipeline_context":_pipeline_context})

        # Missing credentials are checked before snapshot rendering and stored on the job;
        # there is no automatic approval_emails/*.html fallback on this production path.
        _AE_critical.SMTP_PASS = ""
        with _mock_critical.patch.dict(
                _os_critical.environ, {"SEND_APPROVAL_EMAILS":"1"}):
            _missing_smtp = _AS_critical._notify_saved_review_job(
                _real_job_id, str(_critical_root / "missing.pdf"),
                _email_jobs[_real_job_id]["result"], "Email case", "EMAIL-001")
        _missing_smtp_job = _AS_critical.load_jobs()[_real_job_id]
        ck("missing SMTP configuration is recorded visibly and never treated as sent",
           _missing_smtp["status"] == "not_configured" and
           _missing_smtp["sent"] is False and
           _missing_smtp_job["approval_email_status"] == "not_configured" and
           "SMTP_PASS" in _missing_smtp_job["approval_email_error"] and
           any(flag.startswith("APPROVAL EMAIL NOT SENT:")
               for flag in _missing_smtp_job["flags"]) and
           not (_critical_root / "approval_emails" / f"{_real_job_id}.html").exists(),
           _missing_smtp)
    finally:
        _AS_critical.JOBS_FILE = _orig_critical_jobs
        _AS_critical.BACKUP_DIR = _orig_critical_backups
        _AS_critical.QUOTATIONS_DIR = _orig_critical_quotes
        _AS_critical.APPROVAL_TOKEN = _orig_critical_token
        _AE_critical.SMTP_PASS = _orig_smtp_pass
        _shutil_critical.rmtree(_critical_root, ignore_errors=True)
except (ImportError, OSError, ValueError, KeyError) as _e:
    ck("critical approval safeguard tests import and run", False, _e)

print("approval_server: jobs-file backup rotation + corrupt-file preservation (prod-audit MUST)")
try:
    import approval_server as _AS7
    import tempfile as _tempfile7

    _tmpdir7 = Path(_tempfile7.mkdtemp(prefix="ci_backup_"))
    _orig_jobs_file7 = _AS7.JOBS_FILE
    _orig_backup_dir7 = _AS7.BACKUP_DIR
    _AS7.JOBS_FILE = _tmpdir7 / "jobs.json"
    _AS7.BACKUP_DIR = _tmpdir7 / "backups"
    try:
        # First-ever save: JOBS_FILE doesn't exist yet, so there's nothing to snapshot —
        # _rotate_backup is a correct no-op here (never backs up a file that isn't there yet).
        _AS7.save_jobs({"a": {"id": "a"}})
        # Backup filenames are keyed off JOBS_FILE.stem ("jobs" here, not "approval_jobs") —
        # see approval_server._rotate_backup's stem-based naming (item 5, QA-instance isolation).
        _backups7 = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("no backup created on the very first save (nothing existed yet to snapshot)",
           len(_backups7) == 0, [str(p) for p in _backups7])

        # Second save the same day: JOBS_FILE now exists from the first save, so THIS save's
        # rotation check snapshots it before overwriting -> exactly one dated backup appears.
        _AS7.save_jobs({"a": {"id": "a"}, "b": {"id": "b"}})
        _backups7b = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("save_jobs creates a same-day backup once a prior file exists to snapshot",
           len(_backups7b) == 1, [str(p) for p in _backups7b])

        # A third save the same day must NOT create a second backup file for today
        _AS7.save_jobs({"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}})
        _backups7b2 = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("no duplicate backup for a third save on the same day",
           len(_backups7b2) == 1, [str(p) for p in _backups7b2])

        # Pruning: force more than BACKUP_KEEP dated backup files to exist, then trigger a
        # rotation check that should prune down to the newest BACKUP_KEEP.
        import datetime as _dt7
        for _i in range(20):
            _fake_date = (_dt7.date(2020, 1, 1) + _dt7.timedelta(days=_i)).isoformat()
            (_AS7.BACKUP_DIR / f"jobs.{_fake_date}.json").write_text("{}")
        _AS7._rotate_backup()  # today's backup already exists, so this call only prunes
        _backups7c = sorted(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck(f"backup pruning keeps at most BACKUP_KEEP={_AS7.BACKUP_KEEP} files",
           len(_backups7c) <= _AS7.BACKUP_KEEP, len(_backups7c))

        # Corrupt (non-empty, unparseable) jobs file -> preserved as .corrupt-*, load returns {}
        _AS7.JOBS_FILE.write_text("{not valid json!!")
        _loaded7 = _AS7.load_jobs()
        ck("corrupt jobs file -> load_jobs returns {} (never raises)", _loaded7 == {}, _loaded7)
        _corrupt_copies7 = list(_tmpdir7.glob("jobs.json.corrupt-*"))
        ck("corrupt jobs file -> a .corrupt-* copy is preserved for recovery",
           len(_corrupt_copies7) == 1, [str(p) for p in _corrupt_copies7])
    finally:
        _AS7.JOBS_FILE = _orig_jobs_file7
        _AS7.BACKUP_DIR = _orig_backup_dir7
        shutil.rmtree(_tmpdir7, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server backup-rotation tests — missing dependency: {_e}")

print("approval_server: startup sweep clears stranded 'processing' jobs on restart (prod-audit MUST)")
try:
    import approval_server as _AS8
    import tempfile as _tempfile8

    _tmpdir8 = Path(_tempfile8.mkdtemp(prefix="ci_sweep_"))
    _orig_jobs_file8 = _AS8.JOBS_FILE
    _AS8.JOBS_FILE = _tmpdir8 / "jobs.json"
    try:
        _jid8 = "job-stranded-1"
        _AS8.save_jobs({_jid8: {"id": _jid8, "status": "processing", "decision": None, "flags": []}})
        _AS8._sweep_stranded_processing_jobs()
        _swept8 = _AS8.load_jobs()[_jid8]
        ck("stranded 'processing' job flipped to UNMEASURED by the startup sweep",
           _swept8.get("measurement_state") == "UNMEASURED", _swept8.get("measurement_state"))
        ck("startup sweep flag mentions PIPELINE INTERRUPTED",
           any("PIPELINE INTERRUPTED" in f for f in _swept8.get("flags", [])), _swept8.get("flags"))
        ck("startup sweep sets needs_assessor=True", _swept8.get("needs_assessor") is True)

        # A job that is NOT processing must be left untouched
        _jid8b = "job-approved-untouched"
        _AS8.save_jobs({_jid8b: {"id": _jid8b, "status": "approved", "decision": "approved", "flags": ["ok"]}})
        _AS8._sweep_stranded_processing_jobs()
        _unswept8 = _AS8.load_jobs()[_jid8b]
        ck("non-processing job untouched by the startup sweep",
           _unswept8.get("status") == "approved" and _unswept8.get("flags") == ["ok"], _unswept8)
    finally:
        _AS8.JOBS_FILE = _orig_jobs_file8
        shutil.rmtree(_tmpdir8, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server startup-sweep tests — missing dependency: {_e}")

print("approval_server: /webhook/n8n pdf_path containment guard (prod-audit MUST — arbitrary file read)")
try:
    import approval_server as _AS9
    import approval_email as _AE9
    import tempfile as _tempfile9
    import json as _json9

    # /webhook/n8n -> approval_email.create_job() writes straight to approval_email.JOBS_FILE.
    # Left pointed at the real approval_jobs.json, every POST in this test (three per run)
    # permanently wrote a junk "x.pdf" / area=100 pending job into the LIVE jobs file — this is
    # exactly how the ~17 junk jobs that were polluting approval_jobs.json got there. Point
    # approval_email.JOBS_FILE at a tempfile.mkdtemp() scratch path for the duration of this
    # test (never a hardcoded /tmp path) and restore it in finally, so CI is byte-stable
    # against the live jobs file no matter how many times it runs.
    _tmpdir9 = Path(_tempfile9.mkdtemp(prefix="ci_webhook_n8n_"))
    _orig_ae_jobs_file9 = _AE9.JOBS_FILE
    _AE9.JOBS_FILE = _tmpdir9 / "jobs.json"
    try:
        _app9 = _AS9.app
        _app9.testing = True
        _client9 = _app9.test_client()

        _r9 = _client9.post("/webhook/n8n", json={
            "pdf_path": "/etc/passwd",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("pdf_path outside drawings/ is rejected with 400, not read",
           _r9.status_code == 400, _r9.status_code)

        _r9b = _client9.post("/webhook/n8n", json={
            "pdf_path": "../../etc/passwd",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("path-traversal pdf_path is rejected with 400",
           _r9b.status_code == 400, _r9b.status_code)

        # Empty pdf_path (legit use case — result created without a snapshot) still works
        _r9c = _client9.post("/webhook/n8n", json={
            "pdf_path": "",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("empty pdf_path (no snapshot) is not blocked by the containment guard",
           _r9c.status_code == 200, _r9c.status_code)

        # The job this test creates must land in the scratch JOBS_FILE, never the live one.
        ck("webhook test job landed in the scratch jobs file, not the live approval_jobs.json",
           _AE9.JOBS_FILE.exists() and len(_json9.loads(_AE9.JOBS_FILE.read_text())) >= 1,
           str(_AE9.JOBS_FILE))
    finally:
        _AE9.JOBS_FILE = _orig_ae_jobs_file9
        shutil.rmtree(_tmpdir9, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server webhook containment tests — missing dependency: {_e}")

print("approval_server: GET on /approve /reject does NOT mutate; POST does "
      "(top-level-navigation CSRF fix — SameSite=Lax cookies + a mutating GET meant an "
      "email client's link-preview prefetch, or any page merely linking here, could "
      "silently approve/reject a job)")
try:
    import approval_server as _AS10
    import tempfile as _tempfile10

    _tmpdir10 = Path(_tempfile10.mkdtemp(prefix="ci_csrf_"))
    _orig_jobs_file10 = _AS10.JOBS_FILE
    _AS10.JOBS_FILE = _tmpdir10 / "jobs.json"
    try:
        _app10 = _AS10.app
        _app10.testing = True
        _client10 = _app10.test_client()

        # --- /approve: GET must not mutate ---
        _jid10a = "job-csrf-approve"
        _AS10.save_jobs({_jid10a: {
            "id": _jid10a, "status": "pending", "decision": None,
            "measurement_state": "MEASURED_VERIFIED", "scale_confirmed": True,
            "result": {"area_m2": 1000, "file": "csrf_test.pdf"}, "flags": [],
        }})
        _rg10a = _client10.get(f"/approve/{_jid10a}")
        ck("GET /approve/<id> returns 200 (confirm page, not a mutation)",
           _rg10a.status_code == 200, _rg10a.status_code)
        ck("GET /approve/<id> renders an HTML confirm page (not JSON)",
           "text/html" in _rg10a.content_type, _rg10a.content_type)
        _job_after_get10a = _AS10.load_jobs()[_jid10a]
        ck("GET /approve/<id> did NOT change job status (still 'pending')",
           _job_after_get10a["status"] == "pending", _job_after_get10a["status"])
        ck("GET /approve/<id> did NOT set a decision",
           _job_after_get10a.get("decision") is None, _job_after_get10a.get("decision"))
        # The confirm page must contain a POST form targeting the real action, not a link
        # that itself mutates (otherwise it's just moved the vulnerability one click later).
        _body10a = _rg10a.get_data(as_text=True)
        ck("confirm page's form method is POST", 'method="POST"' in _body10a, _body10a[:200])
        ck(f"confirm page's form posts to /approve/{_jid10a}",
           f"/approve/{_jid10a}" in _body10a)

        # Now POST actually mutates
        _rp10a = _client10.post(f"/approve/{_jid10a}", json={})
        ck("POST /approve/<id> returns 200", _rp10a.status_code == 200, _rp10a.status_code)
        _job_after_post10a = _AS10.load_jobs()[_jid10a]
        ck("POST /approve/<id> DID change job status to 'approved'",
           _job_after_post10a["status"] == "approved", _job_after_post10a["status"])

        # --- /reject: GET must not mutate ---
        _jid10b = "job-csrf-reject"
        _AS10.save_jobs({_jid10b: {
            "id": _jid10b, "status": "pending", "decision": None,
            "result": {"file": "csrf_test2.pdf"}, "flags": [],
        }})
        _rg10b = _client10.get(f"/reject/{_jid10b}")
        ck("GET /reject/<id> returns 200 (confirm page, not a mutation)",
           _rg10b.status_code == 200, _rg10b.status_code)
        _job_after_get10b = _AS10.load_jobs()[_jid10b]
        ck("GET /reject/<id> did NOT change job status (still 'pending')",
           _job_after_get10b["status"] == "pending", _job_after_get10b["status"])

        _rp10b = _client10.post(f"/reject/{_jid10b}", json={})
        ck("POST /reject/<id> returns 200", _rp10b.status_code == 200, _rp10b.status_code)
        _job_after_post10b = _AS10.load_jobs()[_jid10b]
        ck("POST /reject/<id> DID change job status to 'rejected'",
           _job_after_post10b["status"] == "rejected", _job_after_post10b["status"])

        # --- /adjust: GET already only redirects (never mutated) — confirm that holds ---
        _jid10c = "job-csrf-adjust"
        _AS10.save_jobs({_jid10c: {
            "id": _jid10c, "status": "pending", "decision": None,
            "result": {"file": "csrf_test3.pdf"}, "flags": [],
        }})
        _rg10c = _client10.get(f"/adjust/{_jid10c}", follow_redirects=False)
        ck("GET /adjust/<id> redirects into the portal (302/301), never mutates",
           _rg10c.status_code in (301, 302), _rg10c.status_code)
        _job_after_get10c = _AS10.load_jobs()[_jid10c]
        ck("GET /adjust/<id> did NOT change job status", _job_after_get10c["status"] == "pending")

        # --- Unknown job on GET -> 404, not a 200 confirm page for a job that doesn't exist ---
        _r404_10 = _client10.get("/approve/does-not-exist-10")
        ck("GET /approve/<unknown> -> 404, not a confirm page for a nonexistent job",
           _r404_10.status_code == 404, _r404_10.status_code)
    finally:
        _AS10.JOBS_FILE = _orig_jobs_file10
        shutil.rmtree(_tmpdir10, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server GET-no-mutation tests — missing dependency: {_e}")

print("approval_email: emailed approve/reject/adjust links carry ?token= when the token "
      "gate is enabled (token mode previously 401'd every emailed action link)")
try:
    import approval_email as _AE11
    import importlib as _importlib11

    _orig_token11 = _AE11.APPROVAL_TOKEN
    try:
        # --- Token configured: every action link + the portal link carries ?token= ---
        _AE11.APPROVAL_TOKEN = "test-email-token-456"
        _html11 = _AE11.build_html_email(
            "job-email-1",
            {"area_m2": 500, "file": "email_test.pdf", "flags": []},
            png_b64="",
        )
        ck("approve link carries ?token= when APPROVAL_TOKEN is set",
           "/approve/job-email-1?token=test-email-token-456" in _html11)
        ck("reject link carries ?token= when APPROVAL_TOKEN is set",
           "/reject/job-email-1?token=test-email-token-456" in _html11)
        ck("adjust link carries ?token= when APPROVAL_TOKEN is set",
           "/adjust/job-email-1?token=test-email-token-456" in _html11)
        ck("portal review link carries ?token= when APPROVAL_TOKEN is set",
           "/review/job-email-1?token=test-email-token-456" in _html11)

        # --- No token configured: links are unchanged (no bare '?token=' with an empty value) ---
        _AE11.APPROVAL_TOKEN = ""
        _html11b = _AE11.build_html_email(
            "job-email-2",
            {"area_m2": 500, "file": "email_test.pdf", "flags": []},
            png_b64="",
        )
        ck("no token configured -> approve link has no ?token= param at all",
           "token=" not in _html11b.split('href="')[1].split('"')[0]
           if 'href="' in _html11b else True)
        ck("no token configured -> approve link is the plain job URL",
           "/approve/job-email-2" in _html11b)
    finally:
        _AE11.APPROVAL_TOKEN = _orig_token11
except ImportError as _e:
    print(f"  [SKIP] approval_email token-link tests — missing dependency: {_e}")

