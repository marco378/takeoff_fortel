#!/usr/bin/env python3
"""approval_server soft-delete, PORTAL_TOKEN gate, rates API, login form.

Sections in this module (printed in this order):
  - approval_server: soft-delete (archive/unarchive) — Aryan's portal delete-estimation request
  - approval_server: PORTAL_TOKEN auth gate (prod-audit MUST — unauthenticated approve/reject/adjust)
  - approval_server: token-gated rates API + build visibility
  - approval_server: /portal login form (no-token case posts a code instead of a bare 401)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
import shutil
from pathlib import Path
from tests import ck
from tests.test_costing_rates import price_with_defaults

print("approval_server: soft-delete (archive/unarchive) — Aryan's portal delete-estimation request")
try:
    import approval_server as _AS5
    import tempfile as _tempfile5

    _tmpdir5 = Path(_tempfile5.mkdtemp(prefix="ci_archive_"))
    _orig_jobs_file5 = _AS5.JOBS_FILE
    _orig_archive_file5 = _AS5.JOBS_ARCHIVE_FILE
    _AS5.JOBS_FILE = _tmpdir5 / "jobs.json"
    _AS5.JOBS_ARCHIVE_FILE = _tmpdir5 / "jobs_archive.json"
    try:
        _app5 = _AS5.app
        _app5.testing = True
        _client5 = _app5.test_client()

        # Ordinary pending job -> archivable
        _jid5 = "job-pending-1"
        _AS5.save_jobs({_jid5: {"id": _jid5, "status": "pending", "decision": None,
                                 "project_name": "Test Yard", "flags": []}})
        _r5 = _client5.post(f"/archive/{_jid5}", json={"note": "duplicate upload"})
        ck("archive: pending job archives with 200", _r5.status_code == 200, _r5.status_code)
        _jobs_after5 = _AS5.load_jobs()
        ck("archive: job removed from hot jobs file", _jid5 not in _jobs_after5)
        _archive5 = _AS5._load_archive()
        ck("archive: job present in archive file", _jid5 in _archive5)
        ck("archive: archived record carries archived=True + archived_at",
           _archive5.get(_jid5, {}).get("archived") is True and _archive5.get(_jid5, {}).get("archived_at"),
           _archive5.get(_jid5))
        ck("archive: status set to 'deleted' in the archive record",
           _archive5.get(_jid5, {}).get("status") == "deleted", _archive5.get(_jid5, {}).get("status"))
        ck("archive: no data lost — project_name preserved",
           _archive5.get(_jid5, {}).get("project_name") == "Test Yard")

        # /jobs/archived surfaces it, default /jobs does not
        _r_list5 = _client5.get("/jobs/archived")
        ck("GET /jobs/archived includes the archived job", _jid5 in _r_list5.get_json())
        _r_hot5 = _client5.get("/jobs")
        ck("GET /jobs (default) excludes the archived job", _jid5 not in _r_hot5.get_json())

        # Unarchive restores it
        _r_un5 = _client5.post(f"/unarchive/{_jid5}")
        ck("unarchive: restores with 200", _r_un5.status_code == 200, _r_un5.status_code)
        _jobs_restored5 = _AS5.load_jobs()
        ck("unarchive: job back in hot jobs file", _jid5 in _jobs_restored5)
        ck("unarchive: archived flag cleared", _jobs_restored5.get(_jid5, {}).get("archived") is False)
        _archive_after_un5 = _AS5._load_archive()
        ck("unarchive: removed from archive file", _jid5 not in _archive_after_un5)

        # Approved job -> BLOCKED (needs Jas, not a portal button)
        _jid5b = "job-approved-1"
        _AS5.save_jobs({_jid5b: {"id": _jid5b, "status": "approved", "decision": "approved",
                                  "project_name": "Approved Yard", "flags": []}})
        _r5b = _client5.post(f"/archive/{_jid5b}")
        ck("archive: approved job is BLOCKED (409)", _r5b.status_code == 409, _r5b.status_code)
        ck("archive: blocked-job error mentions Jas / manual",
           "jas" in _r5b.get_json().get("error", "").lower(), _r5b.get_json())
        ck("archive: approved job NOT removed from hot jobs file after a blocked attempt",
           _jid5b in _AS5.load_jobs())

        # Processing job -> BLOCKED (409), same pattern as approve/reject/adjust
        _jid5c = "job-processing-1"
        _AS5.save_jobs({_jid5c: {"id": _jid5c, "status": "processing", "decision": None, "flags": []}})
        _r5c = _client5.post(f"/archive/{_jid5c}")
        ck("archive: processing job is BLOCKED (409)", _r5c.status_code == 409, _r5c.status_code)

        # Unknown job -> 404, never a crash
        _r5d = _client5.post("/archive/does-not-exist")
        ck("archive: unknown job -> 404 (not a crash)", _r5d.status_code == 404, _r5d.status_code)
        _r5e = _client5.post("/unarchive/does-not-exist")
        ck("unarchive: unknown archived job -> 404 (not a crash)", _r5e.status_code == 404, _r5e.status_code)
    finally:
        _AS5.JOBS_FILE = _orig_jobs_file5
        _AS5.JOBS_ARCHIVE_FILE = _orig_archive_file5
        shutil.rmtree(_tmpdir5, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server soft-delete tests — missing dependency: {_e}")

print("approval_server: PORTAL_TOKEN auth gate (prod-audit MUST — unauthenticated approve/reject/adjust)")
try:
    import approval_server as _AS6
    import tempfile as _tempfile6

    _tmpdir6 = Path(_tempfile6.mkdtemp(prefix="ci_auth_"))
    _orig_jobs_file6 = _AS6.JOBS_FILE
    _AS6.JOBS_FILE = _tmpdir6 / "jobs.json"
    _orig_token6 = _AS6.APPROVAL_TOKEN
    _AS6.APPROVAL_TOKEN = "test-secret-token-123"
    try:
        _app6 = _AS6.app
        _app6.testing = True
        _client6 = _app6.test_client()

        _jid6 = "job-auth-1"
        _AS6.save_jobs({_jid6: {"id": _jid6, "status": "pending", "decision": None, "flags": []}})

        # No token at all -> 401, never a silent pass-through
        _r6 = _client6.get("/jobs")
        ck("no token -> /jobs is 401 when APPROVAL_TOKEN is set", _r6.status_code == 401, _r6.status_code)

        # Wrong token -> 401
        _r6b = _client6.get("/jobs", headers={"Authorization": "Bearer wrong-token"})
        ck("wrong Bearer token -> 401", _r6b.status_code == 401, _r6b.status_code)

        # Correct Bearer token -> 200
        _r6c = _client6.get("/jobs", headers={"Authorization": "Bearer test-secret-token-123"})
        ck("correct Bearer token -> 200", _r6c.status_code == 200, _r6c.status_code)

        # /status always exempt (health-check must work for deploy monitoring pre-auth)
        _r6d = _client6.get("/status")
        ck("/status is exempt from the token gate", _r6d.status_code == 200, _r6d.status_code)

        # / stays reachable so it can redirect a browser into the portal login flow.
        _r6d0 = _client6.get("/")
        ck("/ is exempt from the token gate so the landing redirect works",
           _r6d0.status_code in (301, 302), _r6d0.status_code)

        # /portal?token=<correct> sets a cookie and redirects
        _r6e = _client6.get(f"/portal?token=test-secret-token-123")
        ck("/portal?token=<correct> redirects (sets cookie)", _r6e.status_code in (301, 302), _r6e.status_code)
        _set_cookie6 = _r6e.headers.get("Set-Cookie", "")
        ck("/portal?token=<correct> Set-Cookie contains the token cookie name",
           "approval_token" in _set_cookie6, _set_cookie6)

        # /portal?token=<wrong> does not authorise — use a FRESH client (no cookie carried
        # over from the earlier correct-token request on _client6, which would mask this).
        _client6fresh = _app6.test_client()
        _r6f = _client6fresh.get("/portal?token=nope")
        ck("/portal?token=<wrong> -> login form, not silently served",
           _r6f.status_code == 200 and b"Access code" in _r6f.data
           and b"Fortel Approval Portal" in _r6f.data, _r6f.status_code)

        # Cookie-based auth works for a mutating route (mirrors what the portal's own fetch()
        # calls will do once the browser holds the cookie from the bootstrap redirect above)
        _client6.set_cookie("approval_token", "test-secret-token-123")
        _r6g = _client6.get(f"/job/{_jid6}")
        ck("cookie auth authorises a normal route", _r6g.status_code == 200, _r6g.status_code)

        # With APPROVAL_TOKEN unset, auth is fully disabled (back-compat / local dev)
        _AS6.APPROVAL_TOKEN = ""
        _client6b = _app6.test_client()
        _r6h = _client6b.get("/jobs")
        ck("no APPROVAL_TOKEN configured -> auth disabled, /jobs open", _r6h.status_code == 200, _r6h.status_code)
    finally:
        _AS6.JOBS_FILE = _orig_jobs_file6
        _AS6.APPROVAL_TOKEN = _orig_token6
        shutil.rmtree(_tmpdir6, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server auth-gate tests — missing dependency: {_e}")

print("approval_server: token-gated rates API + build visibility")
try:
    import approval_server as _AS_rates
    import tempfile as _tempfile_rates_api
    import inspect as _inspect_rates_api
    import os as _os_rates_api
    import client_rates as _client_rates_api
    from unittest import mock as _mock_rates_api

    _rates_api_tmpdir = Path(_tempfile_rates_api.mkdtemp(prefix="ci_rates_api_"))
    _orig_rates_file_api = _AS_rates.CLIENT_RATES_FILE
    _orig_token_rates_api = _AS_rates.APPROVAL_TOKEN
    _AS_rates.CLIENT_RATES_FILE = _rates_api_tmpdir / "client_rates.json"
    _AS_rates.APPROVAL_TOKEN = "test-rates-token"
    try:
        _app_rates = _AS_rates.app
        _app_rates.testing = True
        _client_rates_http = _app_rates.test_client()

        _unauth_rates = _client_rates_http.get("/rates")
        ck("rates endpoint is protected by the existing portal token gate",
           _unauth_rates.status_code == 401, _unauth_rates.status_code)
        _headers_rates = {"Authorization": "Bearer test-rates-token"}
        _get_rates = _client_rates_http.get("/rates", headers=_headers_rates)
        _get_rates_json = _get_rates.get_json() or {}
        ck("GET /rates returns every effective field as DEFAULT at version 0",
           _get_rates.status_code == 200 and _get_rates_json.get("version") == 0 and
           len(_get_rates_json.get("fields") or []) == len(_client_rates_api.RATE_FIELDS) and
           all(field.get("provenance") == "DEFAULT"
               for field in _get_rates_json.get("fields") or []))

        _api_defaults = _AS_rates._client_rate_defaults()
        _new_labour = _api_defaults["labour"] * 1.01
        _post_rates_1 = _client_rates_http.post(
            "/rates", headers=_headers_rates, json={"rates": {"labour": _new_labour}})
        ck("POST /rates saves version 1 with CLIENT-EDITED provenance",
           _post_rates_1.status_code == 200 and
           _post_rates_1.get_json().get("version") == 1 and
           next(field for field in _post_rates_1.get_json()["fields"]
                if field["key"] == "labour")["provenance"] == "CLIENT-EDITED")
        _server_fresh_costing = _AS_rates._run_costing(100, {})
        ck("approval/adjust fresh-costing path applies the same saved rates version",
           _server_fresh_costing.get("client_rates_applied") is True and
           _server_fresh_costing.get("rates_version") == 1 and
           _server_fresh_costing["rate"] != price_with_defaults(
               100, client_rates_path=_rates_api_tmpdir / "absent.json")["rate"])

        _post_rates_2 = _client_rates_http.post(
            "/rates", headers=_headers_rates,
            json={"rates": {"labour": _api_defaults["labour"]}})
        _stored_rates_api = _client_rates_api.load_rate_store(_AS_rates.CLIENT_RATES_FILE)
        ck("each actual save bumps version and restoring a default removes its override",
           _post_rates_2.status_code == 200 and
           _post_rates_2.get_json().get("version") == 2 and
           "labour" not in _stored_rates_api["overrides"] and
           len(_stored_rates_api["audit"]) == 2)
        ck("rates writes use same-filesystem os.replace atomic replacement",
           "os.replace(tmp, path)" in _inspect_rates_api.getsource(
               _client_rates_api.save_client_rates))

        _status_build = _client_rates_http.get("/status")
        _status_build_json = _status_build.get_json() or {}
        ck("/status carries build sha + date and remains health-check accessible",
           _status_build.status_code == 200 and
           set((_status_build_json.get("build") or {})) == {"sha", "date"} and
           bool(_status_build_json["build"]["sha"]) and bool(_status_build_json["build"]["date"]))
        with _mock_rates_api.patch.dict(
                _os_rates_api.environ,
                {"RAILWAY_GIT_COMMIT_SHA": "", "RAILWAY_GIT_COMMIT_DATE": ""}), \
                _mock_rates_api.patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            _no_git_build = _AS_rates._detect_build_info()
        ck("build detection never crashes when Railway env and git metadata are absent",
           _no_git_build == {"sha": "unknown", "date": "unknown"}, _no_git_build)

        _portal_source_rates = Path("assessor_portal.html").read_text()
        ck("portal contains Rates panel, new-pricing warning, and fixed build footer",
           all(marker in _portal_source_rates for marker in (
               'id="ratesModal"', "field.provenance", ".rate-tag.client-edited",
               "new pricing only",
               'id="buildFooter"', "loadBuild()", "fetch(`${BASE}/rates`)")))
        _login_build_html = _AS_rates._portal_login_page()
        ck("login page also shows the build footer without exposing the token",
           'id="buildFooter"' in _login_build_html and "Build " in _login_build_html and
           "test-rates-token" not in _login_build_html)
    finally:
        _AS_rates.CLIENT_RATES_FILE = _orig_rates_file_api
        _AS_rates.APPROVAL_TOKEN = _orig_token_rates_api
        shutil.rmtree(_rates_api_tmpdir, ignore_errors=True)
except (ImportError, OSError, ValueError, StopIteration) as _e:
    ck("rates API and build visibility tests import and run", False, _e)

print("approval_server: /portal login form (no-token case posts a code instead of a bare 401)")
try:
    import approval_server as _AS6b

    _orig_token6b = _AS6b.APPROVAL_TOKEN
    _AS6b.APPROVAL_TOKEN = "test-login-code"
    try:
        _app6b = _AS6b.app
        _app6b.testing = True

        _client6b1 = _app6b.test_client()
        _r6b1 = _client6b1.get("/portal")
        ck("GET /portal with no cookie/token -> login form",
           _r6b1.status_code == 200 and b"Access code" in _r6b1.data
           and b"Review Portal" not in _r6b1.data, _r6b1.status_code)

        _r6b2 = _client6b1.post("/portal", data={"code": "wrong-code"})
        ck("POST /portal wrong code -> re-shown with error, 200",
           _r6b2.status_code == 200 and b"Incorrect code" in _r6b2.data, _r6b2.status_code)

        _r6b3 = _client6b1.post("/portal", data={"code": "test-login-code"})
        ck("POST /portal correct code -> redirect", _r6b3.status_code in (301, 302), _r6b3.status_code)
        ck("POST /portal correct code -> Set-Cookie contains the token cookie name",
           "approval_token" in _r6b3.headers.get("Set-Cookie", ""),
           _r6b3.headers.get("Set-Cookie", ""))

        _r6b4 = _client6b1.get("/portal")
        ck("GET /portal with cookie from login -> real portal, not the login form",
           _r6b4.status_code == 200 and b"Access code" not in _r6b4.data, _r6b4.status_code)
    finally:
        _AS6b.APPROVAL_TOKEN = _orig_token6b
except ImportError as _e:
    print(f"  [SKIP] approval_server /portal login-form tests — missing dependency: {_e}")

