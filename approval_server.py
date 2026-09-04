#!/usr/bin/env python3
"""
Fortel AI Takeoff — Approval Server

Flask app that handles all manual-review interactions:

  GET  /                     → redirect to portal
  GET  /portal               → serve assessor_portal.html
  GET  /jobs                 → list all jobs (JSON)
  GET  /job/<id>             → single job detail (JSON)
  GET  /snapshot/<id>        → drawing PNG with AI polygon overlaid
  POST /approve/<id>         → mark approved; trigger costing with original area
  POST /reject/<id>          → mark rejected; log reason
  POST /adjust/<id>          → accept assessor's corrected polygon/scale; re-cost

All state lives in approval_jobs.json (created by approval_email.py). Assessor learning episodes
are stored atomically inside that job record; a volume-aware JSONL log remains a derivative.

Run:
  pip install flask pillow pymupdf shapely --break-system-packages
  python3 approval_server.py          # default port 5001

Environment:
  APPROVAL_PORT    default 5001
  APPROVAL_HOST    default 127.0.0.1 (single-team deployment; set 0.0.0.0 only with
                    APPROVAL_TOKEN also set, e.g. on the shared office Mac)
  PORTAL_TOKEN     shared secret gating every mutating/data-bearing route (APPROVAL_TOKEN is
                    accepted as an older alias). If unset, the server runs with NO auth (fine
                    for 127.0.0.1-only local use) but refuses to bind 0.0.0.0 without one (see
                    main guard below).
  JOBS_FILE        override path to the jobs datastore (default approval_jobs.json next to
                    this file) — lets QA/test instances point at a scratch file instead of
                    colliding with the live jobs file (CLAUDE.md: "QA jobs out of
                    approval_jobs.json").
  CLIENT_RATES_FILE optional override path; otherwise client_rates.json is stored beside
                    JOBS_FILE (including on the same Railway volume).
"""
import os, json, io, datetime, traceback, uuid, re, threading, queue, zipfile, email, shutil, secrets, hashlib, math, html, copy
from email import policy
from pathlib import Path
from flask import Flask, request, jsonify, send_file, redirect, Response

app = Flask(__name__)
# Tender packs are routinely 100 MB-2 GB (Inderjit's CADIC pack was 2.4 GB). A 50 MB cap
# silently 413'd every real zip, which the portal surfaced as a generic failure and the team
# reasonably read as "zip upload does not work". Configurable so a constrained host can lower
# it deliberately rather than by accident.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Railway's container filesystem is EPHEMERAL.  Resolve every resumable assessment artifact
# from one volume-aware source so a deploy cannot preserve a job record while orphaning its
# uploaded PDF or generated quotation.  Explicit per-path env overrides still win.
from storage_paths import resolve_storage_paths
_STORAGE_PATHS = resolve_storage_paths()
JOBS_FILE = _STORAGE_PATHS.jobs_file
DRAWINGS_DIR = _STORAGE_PATHS.drawings_dir
QUOTATIONS_DIR = _STORAGE_PATHS.quotations_dir
from client_rates import rates_path_for_jobs
CLIENT_RATES_FILE = Path(os.getenv("CLIENT_RATES_FILE") or rates_path_for_jobs(JOBS_FILE))
# JOBS_ARCHIVE_FILE / BACKUP_DIR used to derive from JOBS_FILE.parent only — so a QA instance
# started with JOBS_FILE=approval_jobs.qa.json still shared approval_jobs_archive.json and
# backups/ with the live instance (both live in the same directory). Archive/backups now
# derive from the JOBS_FILE STEM instead, so approval_jobs.qa.json gets its own
# approval_jobs.qa_archive.json and backups_approval_jobs.qa/ — never colliding with the live
# instance's approval_jobs_archive.json / backups/. Dedicated env overrides win if set
# (e.g. a QA setup that wants archive/backups somewhere else entirely).
JOBS_ARCHIVE_FILE = _STORAGE_PATHS.jobs_archive_file
TRAINING_LOG = _STORAGE_PATHS.training_log_file
LEARNED_PATTERNS_FILE = _STORAGE_PATHS.learned_patterns_file
PORTAL_HTML  = Path(__file__).parent / "assessor_portal.html"
BACKUP_DIR   = _STORAGE_PATHS.backup_dir
BACKUP_KEEP  = 14   # keep the newest N daily backups

_jobs_lock = threading.Lock()
_training_log_lock = threading.Lock()
_learned_patterns_lock = threading.Lock()

# ── Auth (pragmatic shared-secret — right-sized for a single small team) ─────
# PORTAL_TOKEN (or its older alias APPROVAL_TOKEN — both accepted, PORTAL_TOKEN wins if both
# are set), if set, gates every route except /status (health-check) and the static portal
# shell itself (the portal's own fetch() calls still need the token/cookie to get any data
# back, so an unauthenticated visitor sees an empty, non-functional page — not a 404, since a
# 404 here would be more confusing than useful).
APPROVAL_TOKEN = os.getenv("PORTAL_TOKEN") or os.getenv("APPROVAL_TOKEN", "")
_TOKEN_COOKIE  = "approval_token"


def _detect_build_info() -> dict:
    """Resolve deploy SHA/date once at startup; health checks must never depend on git."""
    sha = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    date = (os.getenv("RAILWAY_GIT_COMMIT_DATE") or "").strip()
    repo_dir = Path(__file__).parent
    try:
        import subprocess
        if not sha:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
                text=True, timeout=2, check=True,
            ).stdout.strip()
        if sha and not date:
            date = subprocess.run(
                ["git", "show", "-s", "--format=%cI", sha], cwd=repo_dir,
                capture_output=True, text=True, timeout=2, check=True,
            ).stdout.strip()
    except Exception:
        # Railway images may intentionally omit .git. The env SHA still remains useful.
        pass
    if sha and not date:
        # Observed on the live Railway deploy: RAILWAY_GIT_COMMIT_SHA is set but
        # RAILWAY_GIT_COMMIT_DATE is not, and the image has no .git — so the footer read
        # "Build 435bbdd · unknown". Fall back to this process's start time, labelled as a
        # deploy time (honest: it is when this build started serving, not when it was
        # committed) so the footer always answers "how fresh is what I'm looking at".
        date = f"deployed {datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
    return {"sha": sha or "unknown", "date": date or "unknown"}


BUILD_INFO = _detect_build_info()


def _build_label() -> str:
    sha = BUILD_INFO.get("sha") or "unknown"
    short_sha = sha[:7] if sha != "unknown" else sha
    return f"Build {short_sha} · {BUILD_INFO.get('date') or 'unknown'}"


def _token_ok(supplied: str) -> bool:
    if not APPROVAL_TOKEN or not supplied:
        return False
    # constant-time compare — this is a shared secret, not a public value
    return secrets.compare_digest(supplied, APPROVAL_TOKEN)


def _portal_login_page(error: bool = False) -> str:
    """Render a small shared-code login form without exposing the configured secret."""
    import html as _html
    error_html = ('<p style="color:#c0392b;font-size:13px;margin:10px 0 0 0">Incorrect code</p>'
                  if error else "")
    build_label = _html.escape(_build_label())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — Sign in</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:360px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <h2 style="color:#13294b;margin:0 0 8px 0">Fortel Approval Portal</h2>
      <p style="color:#666;font-size:14px">Enter the access code to continue.</p>
      <form method="post" action="/portal">
        <input type="password" name="code" placeholder="Access code" autofocus required
               style="width:100%;box-sizing:border-box;padding:10px;font-size:14px;
                      border:1px solid #ccc;border-radius:6px;margin-top:14px">
        <button type="submit" style="display:block;width:100%;box-sizing:border-box;
               padding:12px;margin-top:10px;background:#13294b;color:#fff;border:none;
               border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;">Enter</button>
      </form>
      {error_html}
    </div>
    <div id="buildFooter" style="position:fixed;right:12px;bottom:8px;color:#777;
         font-size:11px">{build_label}</div>
    </body></html>"""


@app.before_request
def _require_token():
    if not APPROVAL_TOKEN:
        return None  # no token configured -> auth disabled (local/dev use)
    if request.method == "OPTIONS":
        return None
    if request.path in ("/status", "/"):
        return None
    # One-time bootstrap: /portal?token=XXX sets the cookie, then redirects to the clean URL
    # so the token never lingers in browser history/bookmarks past the first visit.
    if request.path == "/portal":
        qtoken = request.args.get("token", "")
        if _token_ok(qtoken):
            resp = redirect("/portal")
            resp.set_cookie(_TOKEN_COOKIE, APPROVAL_TOKEN, httponly=True, samesite="Lax",
                             max_age=60 * 60 * 24 * 30)
            return resp
        if _token_ok(request.cookies.get(_TOKEN_COOKIE, "")):
            return None
        if request.method == "POST":
            if _token_ok(request.form.get("code", "")):
                resp = redirect("/portal")
                resp.set_cookie(_TOKEN_COOKIE, APPROVAL_TOKEN, httponly=True, samesite="Lax",
                                 max_age=60 * 60 * 24 * 30)
                return resp
            return Response(_portal_login_page(error=True), 200,
                             {"Content-Type": "text/html; charset=utf-8"})
        return Response(_portal_login_page(), 200,
                        {"Content-Type": "text/html; charset=utf-8"})
    # Every other route: accept Bearer header, cookie, or (for emailed action links) ?token=
    supplied = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        supplied = auth_header[len("Bearer "):]
    if not supplied:
        supplied = request.cookies.get(_TOKEN_COOKIE, "")
    if not supplied:
        supplied = request.args.get("token", "")
    if not _token_ok(supplied):
        return jsonify({"error": "unauthorized — missing or invalid token"}), 401
    return None


# ── helpers ──────────────────────────────────────────────────────────────────

def load_jobs() -> dict:
    """Read approval_jobs.json.

    save_jobs() below writes atomically (tmp file + os.replace), so a well-formed writer
    can never leave a torn/partial file on disk. But this file predates that fix, and any
    external writer (a script, a stray editor) could still leave a transiently-partial file
    mid-write; guard the parse so a concurrent /jobs poll never 500s on a race, it just sees
    a momentarily-empty job list.
    """
    if JOBS_FILE.exists():
        try:
            text = JOBS_FILE.read_text()
            return json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            # A non-empty file that fails to parse is a real corruption event, not the benign
            # torn-read race the try/except above was originally written for (this file
            # predates atomic writes for some external writers). Preserve the evidence instead
            # of silently returning {} and then having the next save_jobs() overwrite it —
            # rename the bad file aside so it can be inspected/recovered, and log loudly so a
            # blank job list in the portal is never a silent mystery.
            try:
                if JOBS_FILE.exists() and JOBS_FILE.stat().st_size > 0:
                    corrupt_path = JOBS_FILE.with_suffix(
                        f".json.corrupt-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}")
                    shutil.copy2(JOBS_FILE, corrupt_path)
                    print(f"[load_jobs] CRITICAL: {JOBS_FILE} failed to parse ({e}); "
                          f"preserved a copy at {corrupt_path}. Returning empty job list.")
            except OSError:
                pass
            return {}
    return {}

def _rotate_backup():
    """Once per calendar day, copy the current jobs file into backups/ before the first save
    of the day, then prune to the newest BACKUP_KEEP. Cheap insurance against a corrupting
    write/edit destroying all decision history — no database needed at this scale.

    Backup filenames are keyed off JOBS_FILE.stem (not hardcoded "approval_jobs") so that if
    BACKUP_DIR is ever shared between two differently-named jobs files (e.g. an explicit
    BACKUP_DIR override), their dated backups don't collide or get pruned against each other.
    """
    try:
        stem = JOBS_FILE.stem
        if JOBS_FILE.exists():
            BACKUP_DIR.mkdir(exist_ok=True)
            today = datetime.date.today().isoformat()
            dated = BACKUP_DIR / f"{stem}.{today}.json"
            if not dated.exists():
                shutil.copy2(JOBS_FILE, dated)
        # Prune unconditionally (not just when a new backup was just made) — otherwise a
        # backlog of old backups (e.g. BACKUP_KEEP lowered, or files added by another process)
        # never gets cleaned up once today's backup already exists.
        if BACKUP_DIR.exists():
            backups = sorted(BACKUP_DIR.glob(f"{stem}.*.json"))
            for stale in backups[:-BACKUP_KEEP]:
                stale.unlink(missing_ok=True)
    except OSError as e:
        print(f"[_rotate_backup] WARNING: could not rotate jobs backup: {e}")

def save_jobs(jobs: dict):
    """Write approval_jobs.json atomically.

    Plain write_text() is NOT atomic — it truncates the file then streams bytes in, so any
    concurrent reader (the portal polls GET /jobs every 15s; multiple upload/approve/reject/
    adjust/watchdog writers can all fire close together) can observe a half-written file and
    hit a JSONDecodeError. That surfaced in the field as "the server is unstable". Write to a
    temp file in the same directory and os.replace() it into place — POSIX guarantees rename
    is atomic, so readers always see either the old or the new complete file, never a partial
    one.
    """
    _rotate_backup()
    tmp = JOBS_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, JOBS_FILE)

def log_training(entry: dict):
    """Append a best-effort derivative event; the atomic job episode is authoritative."""
    payload = dict(entry)
    payload.setdefault("environment", os.getenv("LEARNING_ENVIRONMENT") or
                       ("production" if os.getenv("RAILWAY_ENVIRONMENT") else "local"))
    payload.setdefault("build", BUILD_INFO)
    try:
        TRAINING_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _training_log_lock, TRAINING_LOG.open("a") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as exc:
        print(f"[learning] derivative training-log append failed: {type(exc).__name__}: {exc}")
        return False


def _refresh_learned_patterns(job_id: str, job: dict) -> dict:
    """Update the approved-only derivative incrementally; never fail an approval."""
    from training_analytics import load_json, update_learned_patterns, save_json_atomic
    try:
        with _learned_patterns_lock:
            patterns = update_learned_patterns(load_json(LEARNED_PATTERNS_FILE), job_id, job)
            save_json_atomic(LEARNED_PATTERNS_FILE, patterns)
        return patterns
    except (OSError, ValueError, TypeError) as exc:
        print(f"[learning] learned-pattern refresh failed: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}


def _load_learned_patterns() -> dict:
    from training_analytics import build_learned_patterns, load_json, save_json_atomic
    with _learned_patterns_lock:
        loaded = load_json(LEARNED_PATTERNS_FILE)
        if loaded is not None:
            return loaded
        patterns = build_learned_patterns(load_jobs())
        try:
            save_json_atomic(LEARNED_PATTERNS_FILE, patterns)
        except OSError as exc:
            print(f"[learning] learned-pattern cache unavailable: {type(exc).__name__}: {exc}")
    return patterns


def _ensure_learning_episode(job_id: str, job: dict, *, source="pipeline",
                             original_available=True, pdf_path=None):
    from learning_capture import ensure_learning_episode
    return ensure_learning_episode(
        job_id, job, build=BUILD_INFO, source=source,
        original_available=original_available, pdf_path=pdf_path,
    )


def _record_learning_event(job_id: str, job: dict, before: dict, event: str,
                           *, details=None, terminal=False):
    from learning_capture import append_learning_event
    return append_learning_event(
        job_id, job, event_type=event, before_job=before,
        details=details, terminal=terminal, build=BUILD_INFO,
    )


def _decorate_job(job_id: str, job: dict, patterns: dict | None = None) -> dict:
    from training_analytics import attach_prior_approval
    if patterns is None:
        patterns = _load_learned_patterns()
    return attach_prior_approval(job_id, job, patterns)

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()

def get_job(job_id: str) -> dict | None:
    """Look up a job in the hot store, falling back to the archive.

    /archive moves a job's record into approval_jobs_archive.json and deletes it from the hot
    JOBS_FILE (see archive_job) — but /snapshot/<id>, /job/<id> and /quotation/<id>.<fmt> all
    route through get_job()/require_job(), so an archived job used to 404 on every one of
    them. That's a real regression for an assessor who archives a job then later wants to look
    at (or re-download the quotation for) what they archived — soft-delete is supposed to mean
    "hidden from the default list", not "unreachable". Fall back to the archive so archived
    jobs keep working everywhere except the default /jobs listing.
    """
    job = load_jobs().get(job_id)
    if job is not None:
        return job
    return _load_archive().get(job_id)

def require_job(job_id: str):
    j = get_job(job_id)
    if not j:
        return None, jsonify({"error": f"job {job_id!r} not found"}), 404
    return j, None, None


# ── CORS ──────────────────────────────────────────────────────────────────────
# The portal is served same-origin from /portal and needs no CORS at all. A wildcard
# Access-Control-Allow-Origin combined with (formerly) no auth meant ANY webpage open in
# ANY browser on the LAN could drive /approve, /reject etc. cross-origin — closed per the
# prod audit. If a legitimate cross-origin caller is ever needed (e.g. an n8n instance on a
# different host calling /webhook/n8n from browser JS — most n8n setups call server-side and
# don't need this at all), set APPROVAL_CORS_ORIGIN to that single origin. Never '*'.
_CORS_ORIGIN = os.getenv("APPROVAL_CORS_ORIGIN", "")


@app.after_request
def add_cors(resp):
    if _CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"]  = _CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:p>", methods=["OPTIONS"])
def options(p=""):
    return Response(status=204)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/portal")

@app.route("/portal", methods=["GET", "POST"])
def portal():
    if PORTAL_HTML.exists():
        return PORTAL_HTML.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "Portal HTML not found — make sure assessor_portal.html is in the same folder.", 404

@app.route("/jobs")
def list_jobs():
    jobs = load_jobs()
    patterns = _load_learned_patterns()
    return jsonify({job_id: _decorate_job(job_id, job, patterns) for job_id, job in jobs.items()})

@app.route("/job/<job_id>")
def single_job(job_id):
    j, err, code = require_job(job_id)
    if err: return err, code
    return jsonify(_decorate_job(job_id, j))


def _client_rate_defaults() -> dict:
    """Read current defaults without duplicating or modifying any value."""
    from defaults import DEFAULT_SPEC
    from takeoff_pipeline import MANHOLE_EO_RATE
    from client_rates import RATE_FIELDS
    return {
        key: (MANHOLE_EO_RATE if key == "manhole_eo_rate" else DEFAULT_SPEC[key])
        for key in RATE_FIELDS
    }


def _apply_current_client_rates(spec: dict, *, manhole_in_scope: bool = False):
    """Apply the persisted layer to a resolved pricing spec; calculation stays elsewhere."""
    from client_rates import apply_client_rates
    from takeoff_pipeline import MANHOLE_EO_RATE
    return apply_client_rates(
        spec, MANHOLE_EO_RATE, path=CLIENT_RATES_FILE,
        manhole_in_scope=manhole_in_scope)


@app.route("/rates", methods=["GET", "POST"])
def client_rates_endpoint():
    """Show/save client rate overrides. The global token gate protects both methods."""
    from client_rates import (ClientRatesError, effective_rate_payload,
                              save_client_rates)
    defaults = _client_rate_defaults()
    try:
        if request.method == "GET":
            return jsonify(effective_rate_payload(defaults, path=CLIENT_RATES_FILE))
        data = request.get_json(silent=True) or {}
        who = "assessor-token-authenticated" if APPROVAL_TOKEN else "assessor-local"
        saved, changes = save_client_rates(
            data.get("rates"), defaults, path=CLIENT_RATES_FILE, who=who)
        if not changes:
            return jsonify({"error": "no rate values changed; no version was saved"}), 409
        payload = effective_rate_payload(defaults, path=CLIENT_RATES_FILE)
        payload.update({"status": "saved", "changes": changes,
                        "version": saved["version"]})
        return jsonify(payload)
    except ClientRatesError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/snapshot/<job_id>")
def snapshot(job_id):
    """Render the drawing page with the AI polygon overlaid, return PNG."""
    j, err, code = require_job(job_id)
    if err: return err, code
    try:
        from approval_email import render_snapshot, snapshot_scale
        res  = j.get("result", {})
        # "pdf_path" is used by new upload-form jobs; legacy jobs used "pdf"
        pdf  = res.get("pdf_path") or j.get("pdf_path") or j.get("pdf", "")
        # Resolve relative paths (legacy records) against the server directory
        if pdf and not Path(pdf).is_absolute():
            pdf = str(Path(__file__).parent / pdf)
        poly = res.get("polygon_pts")
        # Multi-page tender packs: takeoff_pipeline.takeoff() ranks every page and measures
        # the best one (result["page"]), NOT necessarily page 0 — see router.rank_pages. The
        # AI's polygon_pts are in that measured page's PDF-point coordinate space. Rendering
        # page 0 unconditionally (the old behaviour) showed the WRONG page for any multi-page
        # pack whose best page wasn't 0, so the "AI polygon" either looked misplaced/garbled
        # or simply didn't correspond to anything visible on screen — this was the field
        # report "need to show the actual highlighted area in AI polygon". Always render the
        # SAME page the measurement came from.
        page = res.get("page") or 0
        if not pdf or not Path(pdf).exists():
            return jsonify({"error": "PDF not on disk — snapshot unavailable"}), 404
        # Guard against a stale/out-of-range page index (e.g. a page count mismatch after
        # the source file was replaced) — fall back to page 0 rather than 500ing.
        try:
            import fitz as _fitz
            with _fitz.open(pdf) as _doc:
                if not (0 <= page < _doc.page_count):
                    page = 0
        except Exception:
            page = 0
        png = render_snapshot(pdf, page=page)
        resp = send_file(io.BytesIO(png), mimetype="image/png")
        # Expose the ACTUAL render scale (snapshot px per PDF point) so the portal can
        # convert scale_k (m/pt) -> metres per canvas pixel:  mpp = scale_k / snap_scale.
        # Without this the portal assumed 0.5 and mis-scaled area on wide (A1/A0) sheets.
        resp.headers["X-Snapshot-Scale"] = f"{snapshot_scale(pdf, page=page):.6f}"
        resp.headers["Access-Control-Expose-Headers"] = "X-Snapshot-Scale"
        return resp
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


@app.route("/snapshot-vector/<job_id>.svg")
def snapshot_vector(job_id):
    """Serve the measured page as a scalable vector viewport for close corner picking.

    The ordinary PNG remains the coordinate authority: its exact X-Snapshot-Scale continues
    to define every assessor point and m/px conversion.  This SVG is a visual layer in rotated
    PDF-point space, CSS-scaled to that unchanged canvas, so zoom quality improves without a
    second measurement coordinate system. Flattened raster sheets remain raster (honestly);
    native line/text drawings stay sharp at arbitrary browser zoom.
    """
    job, err, code = require_job(job_id)
    if err:
        return err, code
    try:
        import fitz as _fitz
        import gzip as _gzip
        result = job.get("result") or {}
        pdf_path = result.get("pdf_path") or job.get("pdf_path") or job.get("pdf", "")
        if pdf_path and not Path(pdf_path).is_absolute():
            pdf_path = str(Path(__file__).parent / pdf_path)
        if not pdf_path or not Path(pdf_path).exists():
            return jsonify({"error": "PDF not on disk — vector viewport unavailable"}), 404
        page_index = int(result.get("page") or 0)
        with _fitz.open(pdf_path) as document:
            if not 0 <= page_index < document.page_count:
                page_index = 0
            # Bluebeam markups are annotations, not page drawing commands. Bake only this
            # in-memory copy so the scalable viewport does not make client markups disappear;
            # the persisted uploaded PDF remains byte-for-byte untouched.
            if any(page.first_annot for page in document):
                document.bake(annots=True, widgets=True)
            page = document[page_index]
            svg_bytes = page.get_svg_image(text_as_path=False).encode("utf-8")
            width, height = page.rect.width, page.rect.height
        max_bytes = int(os.getenv("VECTOR_SNAPSHOT_MAX_BYTES", str(64 * 1024 * 1024)))
        if len(svg_bytes) > max_bytes:
            return jsonify({
                "error": "vector viewport exceeds the safe response limit; raster view retained"
            }), 413
        response_bytes = svg_bytes
        response = Response(response_bytes, mimetype="image/svg+xml")
        if "gzip" in (request.headers.get("Accept-Encoding") or "").lower():
            response_bytes = _gzip.compress(svg_bytes, compresslevel=6)
            response = Response(response_bytes, mimetype="image/svg+xml")
            response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Vector-Coordinate-Space"] = "rotated_pdf_points"
        response.headers["X-Vector-Page-Width"] = f"{width:g}"
        response.headers["X-Vector-Page-Height"] = f"{height:g}"
        response.headers["Access-Control-Expose-Headers"] = (
            "X-Vector-Coordinate-Space, X-Vector-Page-Width, X-Vector-Page-Height"
        )
        return response
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Decision endpoints ────────────────────────────────────────────────────────

def _zone_block_reason(job: dict) -> str | None:
    """Return the assessor action needed before zone-aware approval/quotation is safe."""
    result = job.get("result") or {}
    zones = job.get("zones")
    if not isinstance(zones, list):
        zones = result.get("zones") if isinstance(result.get("zones"), list) else []
    required = bool(
        job.get("zone_classification_required")
        or result.get("zone_classification_required")
        or job.get("zone_allocation_stale")
        or result.get("zone_allocation_stale")
        or job.get("zone_reference_mismatch")
        or result.get("zone_reference_mismatch")
        or job.get("zone_geometry_overlap")
        or result.get("zone_geometry_overlap")
        or any((zone.get("category") or "").strip().lower() == "unclassified"
               for zone in zones if isinstance(zone, dict))
    )
    if not required:
        return None
    if job.get("zone_allocation_stale") or result.get("zone_allocation_stale"):
        return ("zone allocation is stale after an aggregate adjustment; assessor must "
                "reclassify/remeasure the drawing zones")
    if job.get("zone_reference_mismatch") or result.get("zone_reference_mismatch"):
        return ("measured zone quantities do not match the client reference beyond tolerance; "
                "assessor must review the mismatch before approval")
    if job.get("zone_geometry_overlap") or result.get("zone_geometry_overlap"):
        return ("assessor trace regions overlap, so their per-zone quantities do not reconcile "
                "to the measured union; edit and resubmit the region outlines")
    return ("one or more measured markup zones are unclassified; assessor must classify "
            "every zone before approval")


def _channel_proposal_block_reason(job: dict) -> str | None:
    """Pending channel assumptions require an explicit accept/edit/remove decision.

    The decisions do not turn proposals into measured zones and do not alter costing.  This
    gate merely prevents an assessor from approving the drawing while an assumption panel is
    still awaiting review.
    """
    result = job.get("result") or {}
    proposals = job.get("channel_proposals")
    if not isinstance(proposals, list):
        proposals = (result.get("channel_proposals")
                     if isinstance(result.get("channel_proposals"), list) else [])
    proposals = [proposal for proposal in proposals
                 if isinstance(proposal, dict) and proposal.get("proposal_id")]
    if not proposals:
        return None
    decisions = job.get("channel_proposal_decisions")
    if not isinstance(decisions, dict):
        decisions = (result.get("channel_proposal_decisions")
                     if isinstance(result.get("channel_proposal_decisions"), dict) else {})
    pending = [proposal["proposal_id"] for proposal in proposals
               if decisions.get(proposal["proposal_id"], {}).get("decision")
               not in {"accepted", "removed"}]
    if pending:
        return (f"{len(pending)} assumed channel proposal(s) require assessor "
                "accept/edit/remove review before approval")
    return None


def _transition_candidate_block_reason(job: dict) -> str | None:
    """Pending Transition assumptions require explicit accept/edit/remove review."""
    result = job.get("result") or {}
    candidates = job.get("transition_candidates")
    if not isinstance(candidates, list):
        candidates = (result.get("transition_candidates")
                      if isinstance(result.get("transition_candidates"), list) else [])
    candidates = [candidate for candidate in candidates
                  if isinstance(candidate, dict) and candidate.get("candidate_id")]
    if not candidates:
        return None
    decisions = job.get("transition_candidate_decisions")
    if not isinstance(decisions, dict):
        decisions = (result.get("transition_candidate_decisions")
                     if isinstance(result.get("transition_candidate_decisions"), dict) else {})
    pending = [candidate["candidate_id"] for candidate in candidates
               if decisions.get(candidate["candidate_id"], {}).get("decision")
               not in {"accepted", "removed"}]
    if pending:
        return (f"{len(pending)} assumed Transition candidate(s) require assessor "
                "accept/edit/remove review before approval")
    return None


def _yard_region_block_reason(job: dict) -> str | None:
    """A multi-component tint result is a candidate total until every extent is reviewed."""
    result = job.get("result") or {}
    if job.get("yard_region_review_required") or result.get("yard_region_review_required"):
        regions = job.get("yard_regions")
        if not isinstance(regions, list):
            regions = result.get("yard_regions") if isinstance(result.get("yard_regions"), list) else []
        return (f"{len(regions)} retained same-tint Yard regions require assessor "
                "keep/exclude review before approval")
    return None


def _area_element_block_reason(job: dict) -> str | None:
    """Independent assessor areas need an explicit BOQ section before approval.

    A free-text name is client-facing, not reliable classification evidence.  Keeping an
    unclassified named element out of approval is safer than silently pricing a footpath,
    dock or duct slab under the drawing filename's section.
    """
    adjusted = job.get("adjusted") or {}
    elements = job.get("area_elements")
    if not isinstance(elements, list):
        elements = adjusted.get("area_elements") if isinstance(
            adjusted.get("area_elements"), list) else []
    unresolved = [
        str(element.get("name") or "Unnamed area")
        for element in elements if isinstance(element, dict)
        and str(element.get("category") or "").strip().lower()
        not in {"external_yard", "dock", "ground_floor", "upper_floor"}
    ]
    if unresolved:
        return (f"{len(unresolved)} separately named area element(s) require an explicit "
                "BOQ section: " + ", ".join(unresolved[:3]))
    return None


def _approve_block_reason(job: dict) -> str | None:
    """
    Server-side hard-block mirroring the escalation-guard mechanism (fb5b92b, >£200k
    assumed-spec jobs): MEASURED_UNVERIFIED and UNMEASURED jobs cannot be approved until an
    assessor has confirmed scale+extent (via /adjust, which sets scale_confirmed=True).
    Returns a reason string to block, or None to allow.
    """
    result = job.get("result") or {}
    if job.get("exclusion_review_required") or result.get("exclusion_review_required"):
        return ("drawing-labelled slab exclusion(s) have no resolved outline; assessor must "
                "confirm the measured extent/exclusions before approval")
    if job.get("unit_group_review_required") or result.get("unit_group_review_required"):
        return ("Unit-4 subunit set is incomplete; assessor must confirm the combined 4A-4D "
                "slab extent before approval")
    zone_reason = _zone_block_reason(job)
    if zone_reason:
        return zone_reason
    yard_reason = _yard_region_block_reason(job)
    if yard_reason:
        return yard_reason
    area_element_reason = _area_element_block_reason(job)
    if area_element_reason:
        return area_element_reason
    channel_reason = _channel_proposal_block_reason(job)
    if channel_reason:
        return channel_reason
    transition_reason = _transition_candidate_block_reason(job)
    if transition_reason:
        return transition_reason
    if job.get("spec_pricing_warning"):
        return "slab specification is saved but requires human pricing review before approval"
    state = job.get("measurement_state") or result.get("measurement_state")
    if job.get("scale_confirmed"):
        return None
    if state == "REJECTED":
        return "job is REJECTED — cannot be approved"
    if state == "UNMEASURED":
        return ("UNMEASURED — no reliable area was measured; assessor must supply area+scale "
                "via Adjust before this job can be approved")
    if state == "MEASURED_UNVERIFIED":
        return ("MEASURED_UNVERIFIED — scale unverified, low confidence, or implausible area; "
                "assessor must confirm scale+extent via Adjust before this job can be approved")
    return None


@app.route("/approve/<job_id>", methods=["GET", "POST"])
def approve(job_id):
    """
    Approve: accept the AI's measurement as-is, proceed to costing.

    GET performs NO mutation — it only renders a confirm page with a POST button. Mutating
    routes accepting GET with SameSite=Lax cookies is a top-level-navigation CSRF hole: any
    page (or a pre-fetching email client / link scanner) that merely links to or navigates to
    this URL would have approved/rejected a job just by being opened, no user click required.
    The emailed action buttons and the portal's own JS both POST already (see
    approval_email.build_html_email and assessor_portal.html's submitDecision) — GET now only
    exists so an emailed link lands on a safe, human-readable "confirm this?" page.
    """
    if request.method == "GET":
        j = get_job(job_id)
        if not j:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return _html_confirm_page("approve", job_id)

    data = request.get_json(silent=True) or {} if request.is_json else {}

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        block_reason = _approve_block_reason(job)
        if block_reason:
            return jsonify({"error": f"approve blocked: {block_reason}"}), 409
        before_job = copy.deepcopy(job)
        jobs[job_id].update({
            "status":     "approved",
            "decision":   "approved",
            "decided_at": now_iso(),
            "note":       data.get("note", ""),
        })
        previous_events = ((job.get("learning_episode") or {}).get("events") or [])
        corrected_before_approval = any(
            event.get("event") not in {"confirmed_existing_measurement"}
            for event in previous_events if isinstance(event, dict)
        ) or any(job.get(key) for key in (
            "adjusted", "spec_override", "zone_classification_required",
            "yard_region_decisions", "channel_proposal_decisions",
            "transition_candidate_decisions",
        ))
        approval_event = ("approved_after_correction" if corrected_before_approval
                          else "approved_unchanged")
        _record_learning_event(
            job_id, jobs[job_id], before_job, approval_event,
            details={"note": data.get("note", "")}, terminal=True,
        )
        save_jobs(jobs)

    # Price the effective assessor-approved result, not the immutable pipeline snapshot.
    # /adjust deliberately retains the AI result as audit evidence under job["result"], while
    # the corrected geometry/area/scale live under job["adjusted"] and the replacement zones
    # at job level. Quotation and approval must consume the same merged view or approval can
    # silently re-price the original AI area after the portal displayed the correction.
    res = _quotation_result_for_job(job)
    # If the assessor already used spec-override, preserve their costing rather than
    # recomputing with defaults — recomputing would silently undo the correction.
    if job.get("costing") and job.get("spec_override"):
        costing_result = job["costing"]
    else:
        costing_result = _run_costing(res.get("area_m2"), res)
    # Auto-generate and save quotation
    quotation_paths = _save_quotation(job_id, res, costing_result)
    quotation_error = quotation_paths.get("error") if isinstance(quotation_paths, dict) else None
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id]["costing"] = costing_result
        jobs[job_id]["quotation_paths"] = quotation_paths
        if quotation_error:
            flag = f"QUOTATION GENERATION ERROR: {quotation_error}"
            flags = [f for f in (jobs[job_id].get("flags") or [])
                     if not str(f).startswith("QUOTATION GENERATION ERROR:")]
            flags.append(flag)
            jobs[job_id].update({
                "quotation_status": "error",
                "quotation_error": quotation_error,
                "flags": flags,
            })
        else:
            revision = int(jobs[job_id].get("quotation_revision") or 1)
            history = list(jobs[job_id].get("quotation_history") or [])
            if not any(int(entry.get("revision") or 0) == revision for entry in history):
                history.append({
                    "revision": revision,
                    "label": f"REV_{revision:02d}",
                    "issued_at": now_iso(),
                    "reason": "initial approval" if revision == 1 else "approved revision",
                    "paths": dict(quotation_paths),
                })
            jobs[job_id].update({
                "quotation_status": "ready",
                "quotation_revision": revision,
                "quotation_history": history,
            })
            jobs[job_id].pop("quotation_error", None)
        saved_approved_job = copy.deepcopy(jobs[job_id])
        save_jobs(jobs)

    log_training({
        "event":      "approve",
        "job_id":     job_id,
        "file":       res.get("file"),
        "project_ref": job.get("project_ref") or res.get("project_ref"),
        "project_name": job.get("project_name") or res.get("project_name"),
        "area_m2":    res.get("area_m2"),
        "flags":      res.get("flags", []),
        "decision_source": "json" if request.is_json else "form",
        "timestamp":  now_iso(),
    })
    _refresh_learned_patterns(job_id, saved_approved_job)

    if quotation_error:
        message = ("approval was recorded, but quotation generation failed; the job has been "
                   f"flagged for review: {quotation_error}")
        if not request.is_json:
            return Response(
                f"<!doctype html><meta charset='utf-8'><title>Quotation failed</title>"
                f"<h2>Quotation generation failed</h2><p>{html.escape(message)}</p>"
                f"<p><a href='/portal?job={job_id}'>Open the flagged job in the portal</a></p>",
                status=500, mimetype="text/html")
        return jsonify({"status": "quotation_error", "job_id": job_id,
                        "error": message, "costing": costing_result}), 500

    # The confirm page's <form> does a plain (non-JSON) POST and wants the human-readable
    # result page back; the portal's own JS POSTs JSON and wants JSON back (unchanged).
    if not request.is_json:
        return _html_confirmation("approved", job_id, costing_result)
    return jsonify({"status": "approved", "job_id": job_id, "costing": costing_result})


@app.route("/reject/<job_id>", methods=["GET", "POST"])
def reject(job_id):
    """Reject: mark the measurement as wrong; do not proceed to costing.

    GET performs NO mutation (see approve() docstring for the CSRF rationale) — it renders a
    confirm page whose button issues the actual POST.
    """
    if request.method == "GET":
        j = get_job(job_id)
        if not j:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return _html_confirm_page("reject", job_id)

    data = request.get_json(silent=True) or {} if request.is_json else {}

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        before_job = copy.deepcopy(job)
        jobs[job_id].update({
            "status":     "rejected",
            "decision":   "rejected",
            "decided_at": now_iso(),
            "note":       data.get("note", "rejected via portal"),
        })
        _record_learning_event(
            job_id, jobs[job_id], before_job, "rejected",
            details={"note": data.get("note", "rejected via portal")}, terminal=True,
        )
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":     "reject",
        "job_id":    job_id,
        "file":      res.get("file"),
        "flags":     res.get("flags", []),
        "timestamp": now_iso(),
    })

    if not request.is_json:
        return _html_confirmation("rejected", job_id, None)
    return jsonify({"status": "rejected", "job_id": job_id})


def _without_zone_stale_flags(flags) -> list:
    """Keep diagnostic evidence while removing only the superseded stale-allocation gate."""
    return [flag for flag in (flags or [])
            if not str(flag).startswith("ZONE ALLOCATION STALE:")]


@app.route("/confirm-measurement/<job_id>", methods=["POST"])
def confirm_measurement(job_id):
    """Assessor confirms the existing scale and extent without replacing its geometry.

    This is deliberately separate from ``/adjust``.  An aggregate adjustment invalidates a
    zone split because its new outline has no per-zone allocation; confirming an already
    measured drawing must instead preserve those zones.  The explicit boolean prevents an
    accidental empty POST from mutating a job.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm_scale_extent") is not True:
        return jsonify({"error": "confirm_scale_extent=true is required"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job.get("status") == "processing":
            return jsonify({"error": "job is still processing"}), 409
        before_job = copy.deepcopy(job)

        result = dict(job.get("result") or {})
        area_m2 = result.get("area_m2")
        scale_k = job.get("scale_k") or result.get("scale_k")
        state = job.get("measurement_state") or result.get("measurement_state")
        zones = job.get("zones") if isinstance(job.get("zones"), list) else result.get("zones")
        zones = list(zones or [])
        if state != "MEASURED_UNVERIFIED":
            return jsonify({
                "error": "only an existing MEASURED_UNVERIFIED result can be confirmed"
            }), 409
        if (not isinstance(area_m2, (int, float)) or isinstance(area_m2, bool)
                or not math.isfinite(area_m2) or area_m2 <= 0):
            return jsonify({"error": "existing measured area is required; use Adjust"}), 409
        if (not isinstance(scale_k, (int, float)) or isinstance(scale_k, bool)
                or not math.isfinite(scale_k) or scale_k <= 0):
            return jsonify({"error": "existing scale is required; use Calibrate/Adjust"}), 409
        # A prior aggregate replacement has destroyed the old allocation.  Confirmation
        # cannot reconstruct it and must not clear the gate unless real zones still exist.
        stale = bool(job.get("zone_allocation_stale") or result.get("zone_allocation_stale"))
        if stale and not zones:
            return jsonify({
                "error": "zone allocation is stale and empty; remeasure/classify zones"
            }), 409

        confirmed_at = now_iso()
        result.update({
            "measurement_state": "MEASURED_VERIFIED",
            "scale_confirmed": True,
            "extent_confirmed": True,
            "zone_allocation_stale": False,
            "flags": _without_zone_stale_flags(result.get("flags")),
            "exclusion_review_required": False,
            "unit_group_review_required": False,
        })
        job.update({
            "measurement_state": "MEASURED_VERIFIED",
            "scale_confirmed": True,
            "extent_confirmed": True,
            "zone_allocation_stale": False,
            "measurement_confirmed_at": confirmed_at,
            "measurement_confirmation_note": str(data.get("note") or ""),
            "flags": _without_zone_stale_flags(job.get("flags")),
            "exclusion_review_required": False,
            "unit_group_review_required": False,
            "exclusion_prompts": [
                {**prompt, "status": "assessor_confirmed"}
                for prompt in (job.get("exclusion_prompts")
                               or result.get("exclusion_prompts") or [])
            ],
            "result": result,
        })
        result["exclusion_prompts"] = list(job["exclusion_prompts"])
        jobs[job_id] = job
        _record_learning_event(
            job_id, job, before_job, "confirmed_existing_measurement",
            details={"note": str(data.get("note") or "")},
        )
        save_jobs(jobs)

    log_training({
        "event": "confirm_existing_measurement",
        "job_id": job_id,
        "file": result.get("file"),
        "area_m2": area_m2,
        "zone_count": len(zones),
        "timestamp": confirmed_at,
    })
    return jsonify({
        "status": "measurement_confirmed", "job_id": job_id,
        "area_m2": area_m2, "zone_count": len(zones),
        "measurement_state": "MEASURED_VERIFIED",
    })


@app.route("/adjust/<job_id>", methods=["GET", "POST"])
def adjust(job_id):
    """
    Adjust: assessor provides corrected polygon region(s) and/or scale (or a bare assessed area for
    UNMEASURED jobs where there's no AI polygon to correct — e.g. raster/scanned drawings).
    Re-runs geometry measurement with the assessor's inputs when a polygon+scale is given,
    then costs. Any assessor-supplied area (polygon-derived OR a direct assessed_area_m2)
    sets scale_confirmed=True, which is what unblocks /approve for MEASURED_UNVERIFIED and
    UNMEASURED jobs (see _approve_block_reason).

    GET already performed NO mutation before this CSRF pass (it only redirects into the
    portal for manual polygon adjustment there, which itself POSTs) — adjust doesn't need
    the confirm-page treatment approve/reject got since there's nothing to confirm without
    the assessor's actual polygon/scale input.
    """
    if request.method == "GET":
        # Quick check before redirect (no lock needed — read-only, no mutation)
        if not load_jobs().get(job_id):
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return redirect(f"/portal?job={job_id}")

    data = request.get_json(silent=True) or {}
    vertices   = data.get("vertices", [])    # legacy single [[x,y], ...]
    regions_in = data.get("regions")         # new multi-region [[[x,y], ...], ...]
    candidate_ids = data.get("candidate_ids") or []
    region_categories_in = data.get("region_categories")
    region_scopes_in = data.get("region_scopes")
    scale_k    = data.get("scale_k")         # m/px
    snapshot_scale_in = data.get("snapshot_scale")  # canvas px per rotated PDF point
    area_m2    = data.get("assessed_area_m2")
    note       = data.get("note", "")
    cutout_regions_in = data.get("cutout_regions") or []  # [[[x,y], ...], ...]
    user_channels_in = data.get("user_channels") or []    # open polylines [[[x,y], ...], ...]
    area_elements_present = "area_elements" in data
    area_elements_in = data.get("area_elements") if area_elements_present else None
    manhole_count_in = data.get("manhole_count")  # assessor-confirmed manhole count (int or None to clear)

    def _valid_region(region):
        return (
            isinstance(region, list)
            and 3 <= len(region) <= 500
            and all(isinstance(point, (list, tuple)) and len(point) == 2
                    and all(isinstance(value, (int, float)) and math.isfinite(value)
                            and abs(value) <= 10_000_000 for value in point)
                    for point in region)
        )

    def _region_shape_error(items, what, *, minimum=1):
        """Name the limit that was actually hit.

        One message covered three different failures — too many polygons, too few vertices,
        too many vertices — and blamed the count in all three. Inderjit hit it on 27 Aug with
        a shape problem and read back "it needs to be fifty polygon, something like that";
        he could not act on it because it did not describe what he had done."""
        if not isinstance(items, list):
            return f"{what} must be a list of polygons"
        if len(items) < minimum:
            return f"at least {minimum} {what} polygon is required"
        if len(items) > 50:
            return f"{len(items)} {what} polygons submitted; the limit is 50"
        for index, region in enumerate(items, 1):
            if not isinstance(region, list) or not all(
                    isinstance(point, (list, tuple)) and len(point) == 2
                    and all(isinstance(value, (int, float)) and math.isfinite(value)
                            and abs(value) <= 10_000_000 for value in point)
                    for point in region):
                return f"{what} polygon {index} contains a point that is not a finite [x, y] pair"
            if len(region) < 3:
                return f"{what} polygon {index} has {len(region)} point(s); an outline needs at least 3"
            if len(region) > 500:
                return f"{what} polygon {index} has {len(region)} points; the limit is 500"
        return None

    if regions_in is not None:
        shape_error = _region_shape_error(regions_in, "region", minimum=1)
        if shape_error:
            return jsonify({"error": shape_error}), 400
        regions = regions_in
    elif vertices:
        if not _valid_region(vertices):
            return jsonify({"error": "vertices must contain a valid polygon"}), 400
        regions = [vertices]
    else:
        regions = []

    candidate_ids_valid = (
        isinstance(candidate_ids, list)
        and len(candidate_ids) <= len(regions)
        and all(isinstance(candidate_id, str) and candidate_id for candidate_id in candidate_ids)
    )
    if (not candidate_ids_valid
            or len(candidate_ids) != len(set(candidate_ids))):
        return jsonify({"error": "candidate_ids must be unique known candidate identifiers"}), 400

    area_categories = {
        "external_yard", "dock", "ground_floor", "upper_floor", "other", "unclassified"
    }
    if region_categories_in is not None:
        if (not isinstance(region_categories_in, list)
                or len(region_categories_in) != len(regions)
                or any(not isinstance(category, str)
                       or category.strip().lower() not in area_categories
                       for category in region_categories_in)):
            return jsonify({
                "error": "region_categories must classify every region with a valid area category"
            }), 400
        region_categories = [category.strip().lower() for category in region_categories_in]
    else:
        region_categories = []
    allowed_region_scopes = {"main", "ground_floor_core", "main_upper_floor",
                             "plant_deck", "pod_first_floor"}
    if region_scopes_in is not None:
        if (not isinstance(region_scopes_in, list)
                or len(region_scopes_in) != len(regions)
                or any(not isinstance(scope, str)
                       or scope.strip().lower() not in allowed_region_scopes
                       for scope in region_scopes_in)):
            return jsonify({
                "error": "region_scopes must assign every region a valid BOQ scope"
            }), 400
        region_scopes = [scope.strip().lower() for scope in region_scopes_in]
    else:
        region_scopes = []

    # ``+ Area`` polygons are independent priced elements, not additions to the main slab
    # outline.  Their names are assessor-entered client text; retain it verbatim (bounded and
    # control-character free) and escape only at presentation boundaries.
    submitted_area_elements = []
    if area_elements_present:
        if not isinstance(area_elements_in, list):
            return jsonify({"error": "area_elements must be a list"}), 400
        if len(area_elements_in) > 50:
            return jsonify({
                "error": f"{len(area_elements_in)} separate area elements submitted; the limit is 50"
            }), 400
        for index, element in enumerate(area_elements_in, 1):
            if not isinstance(element, dict):
                return jsonify({"error": "each area element must be an object"}), 400
            name = " ".join(str(element.get("name") or "").split())
            points = element.get("polygon_pts") or element.get("points")
            category = str(element.get("category") or "unclassified").strip().lower()
            scope = str(element.get("boq_scope") or "main").strip().lower()
            element_id = str(element.get("element_id") or f"area-element-{index}").strip()
            if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
                return jsonify({
                    "error": "each area element needs a 1-120 character printable name"
                }), 400
            if not element_id or len(element_id) > 120:
                return jsonify({"error": "area element_id must be 1-120 characters"}), 400
            element_shape_error = _region_shape_error([points], "separate area", minimum=1)
            if element_shape_error:
                return jsonify({
                    "error": element_shape_error.replace("separate area polygon 1",
                                                         f"separate area {index} ({name})")
                }), 400
            if category not in area_categories:
                return jsonify({"error": "area element category is invalid"}), 400
            if scope not in allowed_region_scopes:
                return jsonify({"error": "area element BOQ scope is invalid"}), 400
            submitted_area_elements.append({
                "element_id": element_id,
                "name": name,
                "category": category,
                "boq_scope": scope,
                "polygon_pts": points,
            })

    # Validate cut-out regions (polygons to subtract from the measured area)
    cutout_regions = []
    if cutout_regions_in:
        cutout_error = _region_shape_error(cutout_regions_in, "cut-out", minimum=0)
        if cutout_error:
            return jsonify({"error": cutout_error}), 400
        cutout_regions = cutout_regions_in

    if snapshot_scale_in is not None:
        if (not isinstance(snapshot_scale_in, (int, float))
                or isinstance(snapshot_scale_in, bool)
                or not math.isfinite(snapshot_scale_in)
                or not 0 < snapshot_scale_in <= 100):
            return jsonify({"error": "snapshot_scale must be a positive finite number"}), 400
        snapshot_scale_in = float(snapshot_scale_in)

    # Validate assessor-drawn channel polylines. AI proposals remain separately constrained to
    # straight/non-diagonal two-point assumptions in /channel-proposals; a real drainage run may
    # bend and the assessor must be able to trace that actual geometry.
    user_channels = []
    if user_channels_in:
        if not isinstance(user_channels_in, list) or len(user_channels_in) > 20:
            return jsonify({"error": "user_channels must contain 0-20 channel lines"}), 400
        for ch in user_channels_in:
            if (not isinstance(ch, list) or not 2 <= len(ch) <= 100
                    or any(not isinstance(point, (list, tuple)) or len(point) != 2
                           or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                                  for value in point)
                           for point in ch)):
                return jsonify({
                    "error": "each user_channel must contain 2-100 finite [x,y] points"
                }), 400
            length_px = sum(math.dist(ch[index - 1], ch[index])
                            for index in range(1, len(ch)))
            if length_px <= 1e-6:
                return jsonify({"error": "user_channels must have positive length"}), 400
        user_channels = user_channels_in

    # Validate assessor-confirmed manhole count.  None means "clear / use AI detection".
    # A non-negative integer overrides whatever the pipeline detected.
    manhole_count = None
    if manhole_count_in is not None:
        if manhole_count_in is None:
            manhole_count = None  # explicit clear
        elif isinstance(manhole_count_in, bool) or not isinstance(manhole_count_in, int):
            return jsonify({"error": "manhole_count must be a non-negative integer or null"}), 400
        elif manhole_count_in < 0:
            return jsonify({"error": "manhole_count must be non-negative"}), 400
        else:
            manhole_count = manhole_count_in

    # If assessor traced one or more polygons + scale, re-measure (heavy I/O — outside lock).
    # Legacy `vertices` remains exactly one region; Office GA candidates can now be combined.
    if regions and scale_k:
        try:
            from geometry import measure_regions, measure_regions_with_cutouts, polygon_perimeter_lm
            if cutout_regions:
                # Use proper geometric subtraction: union of regions minus union of cutouts.
                # This ensures:
                # - Cutouts only remove geometry they actually intersect
                # - Overlapping cutouts are only subtracted once
                # - Cutouts outside the measured region remove nothing
                gross_area_m2, cutout_removed_m2, area_m2, gflags = measure_regions_with_cutouts(
                    regions, cutout_regions, scale_k)
                # Compute per-region areas with cutouts applied
                region_areas = []
                for region in regions:
                    _, _, region_net, _ = measure_regions_with_cutouts(
                        [region], cutout_regions, scale_k)
                    region_areas.append(region_net)
                if cutout_removed_m2 > 0:
                    gflags.append(f"cut-out: {cutout_removed_m2:,.1f} m² removed")
            else:
                gross_area_m2, gross_flags = measure_regions(regions, scale_k)
                area_m2, gflags = gross_area_m2, gross_flags
                region_areas = [measure_regions([region], scale_k)[0]
                                for region in regions]
            perimeters = [polygon_perimeter_lm(region, scale_k) for region in regions]
            perimeter_lm = round(sum(value for value in perimeters if value is not None), 2)
        except Exception as e:
            area_m2, perimeter_lm, region_areas, perimeters, gflags = (
                None, None, [], [], [f"geometry error: {e}"])
    else:
        region_areas = []
        perimeters = []
        perimeter_lm = None
        gflags = []

    computed_area_elements = []
    if submitted_area_elements:
        if (not isinstance(scale_k, (int, float)) or isinstance(scale_k, bool)
                or not math.isfinite(scale_k) or scale_k <= 0):
            return jsonify({
                "error": "a positive scale_k is required to measure named area elements"
            }), 400
        try:
            from geometry import measure_regions, measure_regions_with_cutouts, polygon_perimeter_lm
            for element in submitted_area_elements:
                # Measure area element, subtracting any overlapping cutouts
                if cutout_regions:
                    _, _, element_area, element_flags = measure_regions_with_cutouts(
                        [element["polygon_pts"]], cutout_regions, scale_k)
                else:
                    element_area, element_flags = measure_regions(
                        [element["polygon_pts"]], scale_k)
                if not element_area or element_area <= 0:
                    return jsonify({
                        "error": f"named area element {element['name']!r} has no measurable area"
                    }), 400
                computed_area_elements.append({
                    **element,
                    "area_m2": round(float(element_area), 1),
                    "perimeter_lm": polygon_perimeter_lm(
                        element["polygon_pts"], scale_k),
                    "measurement_source": "assessor-traced-independent-area",
                    "assessor_supplied": True,
                    "flags": list(element_flags or []),
                })
        except Exception as exc:
            return jsonify({"error": f"named area geometry error: {exc}"}), 400

    # Adding/renaming an independent area must not replace, confirm or invalidate the main
    # measurement.  This is the key semantic difference from ``+ Region``.
    main_measurement_changed = bool(regions or cutout_regions or area_m2 is not None)
    preserve_main_measurement = bool(area_elements_present and not main_measurement_changed)

    # A valid assessor-supplied area (however it arrived) is a human confirmation of
    # scale+extent — this is what unblocks approve for MEASURED_UNVERIFIED/UNMEASURED jobs.
    # Still run the plausibility guard (sanity.plausible): an assessor can fat-finger a trace
    # too, so an implausible area does NOT silently confirm — it stays blocked for a second look.
    from sanity import plausible as _plausible
    plaus_flags = _plausible(area_m2) if area_m2 else []
    gflags = gflags + plaus_flags
    confirmed = bool(area_m2 and area_m2 > 0 and not plaus_flags)

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        before_job = copy.deepcopy(job)
        stored_candidate_records = {
            candidate.get("candidate_id"): candidate
            for candidate in (job.get("candidate_polygons")
                              or (job.get("result") or {}).get("candidate_polygons") or [])
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        }
        if any(candidate_id not in stored_candidate_records for candidate_id in candidate_ids):
            return jsonify({"error": "one or more candidate_ids are stale or unknown"}), 409
        stored_result = dict(job.get("result") or {})
        existing_adjusted = dict(job.get("adjusted") or {})
        if not area_elements_present:
            existing_elements = job.get("area_elements")
            if not isinstance(existing_elements, list):
                existing_elements = existing_adjusted.get("area_elements")
            computed_area_elements = copy.deepcopy(existing_elements or [])
        if preserve_main_measurement:
            area_m2 = (existing_adjusted.get("area_m2") or job.get("area_m2")
                       or stored_result.get("area_m2"))
            perimeter_lm = (existing_adjusted.get("perimeter_lm")
                            if "perimeter_lm" in existing_adjusted
                            else job.get("perimeter_lm", stored_result.get("perimeter_lm")))
            confirmed = bool(job.get("scale_confirmed"))
            gflags = list(existing_adjusted.get("flags") or [])
        # Cutout-only: subtract from existing measured area when no new trace regions.
        # We need the original measured geometry to perform proper geometric subtraction.
        # The scalar path is unsafe because it subtracts the full cutout polygon area,
        # not just the intersection with the measured region.
        if cutout_regions and scale_k and not regions:
            # Retrieve the original measured regions from the job
            existing_regions = (
                (job.get("adjusted") or {}).get("regions") or 
                [job.get("adjusted", {}).get("vertices")] if(job.get("adjusted") or {}).get("vertices") else []
            )
            # Filter to valid regions
            existing_regions = [r for r in existing_regions if isinstance(r, list) and len(r) >= 3]
            
            if existing_regions:
                try:
                    from geometry import measure_regions_with_cutouts
                    gross_area_m2, cutout_removed_m2, net_area_m2, cutout_flags = (
                        measure_regions_with_cutouts(existing_regions, cutout_regions, scale_k))
                    if cutout_removed_m2 > 0:
                        area_m2 = net_area_m2
                        gflags = [f"cut-out only: {cutout_removed_m2:,.1f} m² removed from {gross_area_m2:,.1f} m²"] + cutout_flags
                    else:
                        area_m2 = gross_area_m2
                        gflags = []
                except Exception as e:
                    area_m2, gflags = None, [f"geometry error: {e}"]
            else:
                # Fallback: if we can't retrieve original regions, use scalar subtraction
                # but warn that this is less accurate
                existing_area = (job.get("adjusted") or {}).get("assessed_area_m2") or stored_result.get("area_m2")
                if existing_area and existing_area > 0:
                    try:
                        from geometry import measure_regions
                        cutout_area, cutout_flags = measure_regions(cutout_regions, scale_k)
                        if cutout_area > 0:
                            area_m2 = round(max(0, existing_area - cutout_area), 1)
                            gflags = [f"cut-out only (scalar fallback): {cutout_area:,.1f} m² subtracted from {existing_area:,.1f} m²"] + cutout_flags
                        else:
                            area_m2 = existing_area
                            gflags = []
                    except Exception as e:
                        area_m2, gflags = None, [f"geometry error: {e}"]
        # Older clients supplied one candidate id per region but no category list.  Preserve
        # that flow by taking the detector's explicit category.  New clients send a category
        # for every region, including ``unclassified`` for a manual outline.
        effective_region_categories = list(region_categories)
        effective_region_scopes = list(region_scopes)
        if (not effective_region_categories and regions
                and len(candidate_ids) == len(regions)):
            effective_region_categories = [
                str(stored_candidate_records[candidate_id].get("category")
                    or "unclassified").strip().lower()
                for candidate_id in candidate_ids
            ]
            if any(category not in area_categories for category in effective_region_categories):
                effective_region_categories = []
        if (not effective_region_scopes and regions
                and len(candidate_ids) == len(regions)):
            effective_region_scopes = [
                str(stored_candidate_records[candidate_id].get("boq_scope")
                    or "main").strip().lower()
                for candidate_id in candidate_ids
            ]
            if any(scope not in allowed_region_scopes for scope in effective_region_scopes):
                effective_region_scopes = []
        if not effective_region_scopes and regions:
            effective_region_scopes = [
                ("ground_floor_core" if category == "ground_floor"
                 else "main_upper_floor" if category == "upper_floor" else "main")
                for category in effective_region_categories
            ]
        # Whether the assessor supplied a category for every region is independent of whether
        # the resulting area is plausible (`confirmed`) — conflating them used to wipe a fully
        # categorized submission's zones to [] whenever the area tripped sanity.plausible()
        # (e.g. a legitimately large yard over the 60,000 m^2 single-zone guard), leaving
        # zone_allocation_stale=True with nothing left to classify: a dead end, since the
        # assessor HAD just done the reclassification the gate demanded. An implausible area
        # still blocks approval on its own via measurement_state/scale_confirmed below; it must
        # not also destroy the categorization the assessor just gave it.
        categorized_remeasure = bool(
            regions and len(effective_region_categories) == len(regions)
            and len(effective_region_scopes) == len(regions)
            and len(region_areas) == len(regions)
        )
        had_zone_allocation = main_measurement_changed and bool(area_m2 and area_m2 > 0) and bool(
            (isinstance(job.get("zones"), list) and job.get("zones"))
            or (isinstance(stored_result.get("zones"), list) and stored_result.get("zones"))
        )
        zone_stale_flag = None
        if had_zone_allocation and not categorized_remeasure:
            # /adjust supplies one replacement aggregate trace/area, not a per-zone edit.
            # Keeping the old marked-PDF split would make the four quotation sections add up
            # to the superseded measurement. Preserve raw annotation evidence, but clear the
            # derived zones and hard-block approval/quotation until an assessor reclassifies
            # or remeasures them.
            zone_stale_flag = (
                "ZONE ALLOCATION STALE: aggregate adjustment replaced the measured area; "
                "assessor must reclassify/remeasure zones"
            )
            gflags = list(gflags) + [zone_stale_flag]
            result_flags = list(stored_result.get("flags") or [])
            if zone_stale_flag not in result_flags:
                result_flags.append(zone_stale_flag)
            stored_result.update({
                "zones": [],
                "zone_classification_required": True,
                "zone_allocation_stale": True,
                "flags": result_flags,
                "needs_assessor": True,
            })

        replacement_zones = []
        zone_geometry_overlap = False
        zone_classification_required = False
        brief_specs = dict(job.get("brief_specs") or stored_result.get("brief_specs") or {})
        if categorized_remeasure:
            grouped = {}
            for index, (category, scope, region_area, region_perimeter) in enumerate(zip(
                    effective_region_categories, effective_region_scopes,
                    region_areas, perimeters), 1):
                group_key = (category, scope)
                scope_labels = {
                    "plant_deck": "Plant deck",
                    "pod_first_floor": "POD first floor",
                    "main_upper_floor": "Main upper floor",
                    "ground_floor_core": "Ground floor core",
                }
                zone = grouped.setdefault(group_key, {
                    "zone_key": f"assessor-trace:{category}:{scope}",
                    "category": category,
                    "boq_scope": scope,
                    "scope_label": scope_labels.get(scope),
                    "subjects": [],
                    "measurement_kind": "area",
                    "area_m2": 0.0,
                    "perimeter_lm": 0.0,
                    "annotation_count": 0,
                    "region_indices": [],
                    "classification_source": (
                        "assessor-assisted-trace" if category != "unclassified"
                        else "assessor-classification-required"
                    ),
                    "needs_assessor": category == "unclassified",
                })
                candidate_id = candidate_ids[index - 1] if len(candidate_ids) == len(regions) else None
                candidate = stored_candidate_records.get(candidate_id, {})
                subject = (candidate.get("level_label") or candidate.get("source_label")
                           or f"Assessor region {index}")
                if subject not in zone["subjects"]:
                    zone["subjects"].append(subject)
                zone["area_m2"] += float(region_area)
                zone["perimeter_lm"] += float(region_perimeter or 0)
                zone["annotation_count"] += 1
                zone["region_indices"].append(index - 1)
            replacement_zones = list(grouped.values())
            for zone in replacement_zones:
                zone["area_m2"] = round(zone["area_m2"], 1)
                zone["perimeter_lm"] = round(zone["perimeter_lm"], 2)
            zone_geometry_overlap = any(str(flag).startswith("regions overlap") for flag in gflags)
            zone_classification_required = any(
                zone["category"] == "unclassified" for zone in replacement_zones
            )
            if zone_classification_required:
                gflags = list(gflags) + [
                    "assessor: classify zone 'Assessor-traced unclassified region'"
                ]
            if zone_geometry_overlap:
                gflags = list(gflags) + [
                    "assessor: edit overlapping trace regions before zone approval"
                ]
            from slab_spec import empty_brief_spec
            for category in effective_region_categories:
                if category in {"external_yard", "dock", "ground_floor", "upper_floor"}:
                    brief_specs.setdefault(category, empty_brief_spec(category))
            stored_result.update({
                "zones": replacement_zones,
                "brief_specs": brief_specs,
                "zone_classification_required": zone_classification_required,
                "zone_allocation_stale": False,
                "zone_geometry_overlap": zone_geometry_overlap,
                "flags": _without_zone_stale_flags(stored_result.get("flags")) + list(gflags),
                "needs_assessor": bool(zone_classification_required or zone_geometry_overlap),
            })
        # Carry assessor-confirmed manhole count into the result so costing uses it.
        if manhole_count_in is not None:
            if manhole_count is not None:
                stored_result["manhole_count"] = manhole_count
                stored_result.pop("manhole_count_estimate", None)
                stored_result.pop("manhole_count_assumed", None)
            else:
                # Assessor cleared the count — fall back to AI detection
                stored_result.pop("manhole_count", None)
        costing_result = ((job.get("costing") or stored_result.get("costing"))
                          if preserve_main_measurement
                          else _run_costing(area_m2, stored_result) if area_m2 else None)
        if preserve_main_measurement:
            adjusted_payload = existing_adjusted
            adjusted_payload.update({
                "area_elements": computed_area_elements,
                "note": note or existing_adjusted.get("note", ""),
            })
            if snapshot_scale_in is not None:
                adjusted_payload["snapshot_scale"] = snapshot_scale_in
            if manhole_count_in is not None:
                adjusted_payload["manhole_count"] = manhole_count
        else:
            adjusted_payload = {
                "vertices": regions[0] if len(regions) == 1 else [],
                "regions": regions,
                "candidate_ids": candidate_ids,
                "region_categories": effective_region_categories,
                "region_scopes": effective_region_scopes,
                "scale_k": scale_k,
                # Assessor points are stored in canvas/snapshot pixels. Persist the exact
                # px-per-PDF-point mapping with those points so marked-PDF export and a future
                # importer never have to infer it from a later render configuration.
                "snapshot_scale": snapshot_scale_in,
                "area_m2": area_m2,
                "perimeter_lm": perimeter_lm,
                "flags": gflags,
                "note": note,
                "cutout_regions": cutout_regions,
                "user_channels": user_channels,
                "area_elements": computed_area_elements,
                "manhole_count": manhole_count,
            }
        persisted_cutout_regions = (
            copy.deepcopy(existing_adjusted.get("cutout_regions") or [])
            if preserve_main_measurement else cutout_regions
        )
        persisted_user_channels = (
            copy.deepcopy(existing_adjusted.get("user_channels") or [])
            if preserve_main_measurement else user_channels
        )
        persisted_region_scopes = (
            list(existing_adjusted.get("region_scopes") or job.get("region_scopes") or [])
            if preserve_main_measurement else effective_region_scopes
        )
        canonical_update = {
            "status":            "adjusted",
            "decision":          "adjusted",
            "decided_at":        now_iso(),
            "area_m2":           area_m2,
            "scale_confirmed":   confirmed or job.get("scale_confirmed", False),
            "measurement_state": (job.get("measurement_state") if preserve_main_measurement
                                  else "MEASURED_VERIFIED" if confirmed
                                  else "MEASURED_UNVERIFIED" if (area_m2 and plaus_flags)
                                  else job.get("measurement_state")),
            "adjusted": adjusted_payload,
            "region_scopes": persisted_region_scopes,
            "costing": costing_result,
            "cutout_regions": persisted_cutout_regions,
            "user_channels": persisted_user_channels,
            "area_elements": computed_area_elements,
            "area_element_classification_required": any(
                str(element.get("category") or "").strip().lower() not in {
                    "external_yard", "dock", "ground_floor", "upper_floor"
                } for element in computed_area_elements
            ),
        }
        # Scale belongs to the assessor geometry that it measures. Keep an earlier scale only
        # for direct area-only adjustments where the assessor supplied no replacement scale.
        if scale_k and not preserve_main_measurement:
            canonical_update["scale_k"] = scale_k
        if perimeter_lm is not None:
            canonical_update["perimeter_lm"] = perimeter_lm
        jobs[job_id].update(canonical_update)
        if had_zone_allocation:
            if categorized_remeasure:
                jobs[job_id].update({
                    "zones": replacement_zones,
                    "brief_specs": brief_specs,
                    "zone_classification_required": zone_classification_required,
                    "zone_allocation_stale": False,
                    "zone_geometry_overlap": zone_geometry_overlap,
                    "needs_assessor": bool(zone_classification_required or zone_geometry_overlap),
                    "result": stored_result,
                    "flags": _without_zone_stale_flags(jobs[job_id].get("flags")) + list(gflags),
                })
            else:
                jobs[job_id].update({
                    "zones": [],
                    "zone_classification_required": True,
                    "zone_allocation_stale": True,
                    "needs_assessor": True,
                    "result": stored_result,
                })
                top_flags = list(jobs[job_id].get("flags") or [])
                if zone_stale_flag not in top_flags:
                    top_flags.append(zone_stale_flag)
                jobs[job_id]["flags"] = top_flags
        elif categorized_remeasure:
            jobs[job_id].update({
                "zones": replacement_zones,
                "brief_specs": brief_specs,
                "zone_classification_required": zone_classification_required,
                "zone_allocation_stale": False,
                "zone_geometry_overlap": zone_geometry_overlap,
                "needs_assessor": bool(zone_classification_required or zone_geometry_overlap),
                "result": stored_result,
                "flags": _without_zone_stale_flags(jobs[job_id].get("flags")) + list(gflags),
                "region_scopes": effective_region_scopes,
            })
        before_state = (before_job.get("measurement_state")
                        or (before_job.get("result") or {}).get("measurement_state"))
        event_type = "refusal_recovered" if before_state == "UNMEASURED" else "measurement_adjusted"
        _record_learning_event(
            job_id, jobs[job_id], before_job, event_type,
            details={
                "note": note,
                "candidate_ids": list(candidate_ids),
                "region_categories": list(effective_region_categories),
                "region_scopes": list(effective_region_scopes),
            },
        )
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":          "adjust",
        "job_id":         job_id,
        "file":           res.get("file"),
        "ai_area_m2":     res.get("area_m2"),
        "assessed_area":  area_m2,
        "ai_polygon":     res.get("polygon_pts"),
        "assessed_polygon": regions[0] if len(regions) == 1 else None,
        "assessed_regions": regions,
        "candidate_ids":    candidate_ids,
        "region_categories": effective_region_categories,
        "region_scopes": effective_region_scopes,
        "scale_k":        scale_k,
        "flags":          res.get("flags", []),
        "timestamp":      now_iso(),
    })

    return jsonify({
        "status":   "adjusted",
        "job_id":   job_id,
        "area_m2":  area_m2,
        "perimeter_lm": perimeter_lm,
        "region_count": len(regions),
        "zone_count": len(replacement_zones),
        "main_area_m2": area_m2,
        "area_element_count": len(computed_area_elements),
        "area_elements_total_m2": round(sum(
            float(element.get("area_m2") or 0) for element in computed_area_elements), 1),
        "costing":  costing_result,
        "flags":    gflags,
    })


@app.route("/zones/<job_id>", methods=["POST"])
def classify_zones(job_id):
    """Persist explicit assessor classifications for previously unknown markup subjects."""
    data = request.get_json(silent=True) or {}
    classifications = data.get("classifications") or []
    acknowledge_mismatch = data.get("acknowledge_reference_mismatch") is True
    if not isinstance(classifications, list) or (not classifications and not acknowledge_mismatch):
        return jsonify({"error": "classifications or mismatch acknowledgement required"}), 400
    requested = {
        str(item.get("zone_key") or ""): str(item.get("category") or "").strip().lower()
        for item in classifications if isinstance(item, dict)
    }
    allowed = {"external_yard", "dock", "ground_floor", "upper_floor",
               "channel", "transition", "construction_joint", "other"}
    if any(not key or category not in allowed for key, category in requested.items()):
        return jsonify({"error": "invalid zone_key/category"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        before_job = copy.deepcopy(job)
        result = dict(job.get("result") or {})
        source_zones = job.get("zones") if isinstance(job.get("zones"), list) else result.get("zones")
        zones = [dict(zone) for zone in (source_zones or []) if isinstance(zone, dict)]
        seen = set()
        for zone in zones:
            zone_key = str(zone.get("zone_key") or "")
            if zone_key not in requested:
                continue
            category = requested[zone_key]
            is_area = isinstance(zone.get("area_m2"), (int, float))
            is_length = isinstance(zone.get("length_lm"), (int, float))
            if is_area and category not in {"external_yard", "dock", "ground_floor",
                                            "upper_floor", "other"}:
                return jsonify({"error": f"area zone {zone_key!r} cannot be {category}"}), 400
            if is_length and category not in {
                    "channel", "transition", "construction_joint", "other"}:
                return jsonify({"error": f"length zone {zone_key!r} cannot be {category}"}), 400
            if not is_area and not is_length and category != "other":
                return jsonify({"error": f"unparsed zone {zone_key!r} can only be other"}), 400
            zone.update({
                "category": category,
                "classification_source": "assessor",
                "needs_assessor": False,
            })
            seen.add(zone_key)
        missing = set(requested) - seen
        if missing:
            return jsonify({"error": f"unknown zone_key(s): {', '.join(sorted(missing))}"}), 400

        still_unclassified = any(zone.get("category") == "unclassified" for zone in zones)
        result_flags = [flag for flag in (result.get("flags") or [])
                        if not str(flag).startswith("assessor: classify zone")]
        top_flags = [flag for flag in (job.get("flags") or [])
                     if not str(flag).startswith("assessor: classify zone")]
        if acknowledge_mismatch:
            acknowledgement = "assessor acknowledged zone-vs-BOQ mismatch after review"
            if acknowledgement not in result_flags:
                result_flags.append(acknowledgement)
            if acknowledgement not in top_flags:
                top_flags.append(acknowledgement)
            result["zone_reference_mismatch"] = False
            result["zone_reference_reviewed_at"] = now_iso()
        if still_unclassified:
            for zone in zones:
                if zone.get("category") == "unclassified":
                    subject = ", ".join(zone.get("subjects") or []) or zone.get("zone_key")
                    flag = f"assessor: classify zone '{subject}'"
                    result_flags.append(flag)
                    top_flags.append(flag)

        brief_specs = dict(job.get("brief_specs") or result.get("brief_specs") or {})
        from slab_spec import empty_brief_spec
        for zone in zones:
            category = zone.get("category")
            if category in {"external_yard", "dock", "ground_floor", "upper_floor"}:
                brief_specs.setdefault(category, empty_brief_spec(category))

        # Classifying every zone IS the remeasure/reclassify the staleness gate asks for, so it
        # must clear its own block. Previously /zones cleared zone_classification_required but
        # left zone_allocation_stale set, so approval still failed with "assessor must
        # reclassify/remeasure" — telling Inderjit to do the thing he had just done. He hit this
        # repeatedly (20 Aug: "it is not getting approved... Cannot approve zone classification")
        # and the known workaround was to resubmit the adjustment, which is what actually cleared
        # the flag. A phantom block is a dead end, and dead ends are the bug.
        allocation_resolved = not still_unclassified
        stale_after = bool(
            (job.get("zone_allocation_stale") or result.get("zone_allocation_stale"))
        ) and not allocation_resolved

        result.update({
            "zones": zones,
            "brief_specs": brief_specs,
            "zone_allocation_stale": stale_after,
            "zone_classification_required": still_unclassified,
            "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                result.get("zone_reference_mismatch", False)),
            "flags": result_flags,
        })
        job.update({
            "zones": zones,
            "brief_specs": brief_specs,
            "zone_allocation_stale": stale_after,
            "zone_classification_required": still_unclassified,
            "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                job.get("zone_reference_mismatch", result.get("zone_reference_mismatch", False))),
            "flags": top_flags,
            "result": result,
        })
        jobs[job_id] = job
        _record_learning_event(
            job_id, job, before_job, "zones_classified",
            details={"classifications": copy.deepcopy(classifications),
                     "acknowledge_reference_mismatch": acknowledge_mismatch},
        )
        save_jobs(jobs)
    log_training({
        "event": "zones_classified", "job_id": job_id,
        "classifications": classifications,
        "acknowledge_reference_mismatch": acknowledge_mismatch,
        "timestamp": now_iso(),
    })
    return jsonify({"status": "zones_updated", "zones": zones,
                    "zone_classification_required": still_unclassified,
                    "zone_reference_mismatch": False if acknowledge_mismatch else bool(
                        result.get("zone_reference_mismatch", False))})


@app.route("/channel-proposals/<job_id>", methods=["POST"])
def review_channel_proposals(job_id):
    """Record assessor accept/edit/remove decisions without creating measured quantities.

    Backward-compatible request items may contain only ``length_lm``. New portal clients can
    additionally send ``polyline_pts`` as two PDF-point coordinates; edited geometry must remain
    a straight horizontal/vertical segment and its accepted length is derived from the stored
    job scale rather than trusting a contradictory number from the browser.
    """
    data = request.get_json(silent=True) or {}
    submitted = data.get("decisions")
    if not isinstance(submitted, list) or not submitted:
        return jsonify({"error": "decisions must be a non-empty list"}), 400

    normalised = []
    for item in submitted:
        if not isinstance(item, dict):
            return jsonify({"error": "each decision must be an object"}), 400
        proposal_id = str(item.get("proposal_id") or "").strip()
        action = str(item.get("action") or "").strip().lower()
        if not proposal_id or action not in {"accept", "remove"}:
            return jsonify({"error": "proposal_id and action accept/remove are required"}), 400
        length_lm = item.get("length_lm")
        polyline_pts = item.get("polyline_pts")
        if action == "accept":
            if (not isinstance(length_lm, (int, float)) or isinstance(length_lm, bool)
                    or not math.isfinite(length_lm) or not 0 < length_lm <= 100_000):
                return jsonify({"error": "accepted length_lm must be a positive number"}), 400
            length_lm = round(float(length_lm), 2)
            if polyline_pts is not None:
                if (not isinstance(polyline_pts, list) or len(polyline_pts) != 2
                        or any(not isinstance(point, list) or len(point) != 2
                               for point in polyline_pts)):
                    return jsonify({"error": "polyline_pts must contain exactly two [x,y] points"}), 400
                try:
                    polyline_pts = [
                        [float(point[0]), float(point[1])] for point in polyline_pts
                    ]
                except (TypeError, ValueError):
                    return jsonify({"error": "polyline_pts coordinates must be numeric"}), 400
                if any(not math.isfinite(value) or abs(value) > 10_000_000
                       for point in polyline_pts for value in point):
                    return jsonify({"error": "polyline_pts coordinates must be finite"}), 400
                dx = abs(polyline_pts[1][0] - polyline_pts[0][0])
                dy = abs(polyline_pts[1][1] - polyline_pts[0][1])
                if dx <= 1e-6 and dy <= 1e-6:
                    return jsonify({"error": "polyline_pts must have positive length"}), 400
                if dx > 1e-6 and dy > 1e-6:
                    return jsonify({
                        "error": "channel proposal geometry must be straight and non-diagonal"
                    }), 400
        else:
            length_lm = None
            polyline_pts = None
        normalised.append((proposal_id, action, length_lm, polyline_pts))
    if len({proposal_id for proposal_id, _, _, _ in normalised}) != len(normalised):
        return jsonify({"error": "proposal_id decisions must be unique"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        before_job = copy.deepcopy(job)
        result = dict(job.get("result") or {})
        proposals = job.get("channel_proposals")
        if not isinstance(proposals, list):
            proposals = (result.get("channel_proposals")
                         if isinstance(result.get("channel_proposals"), list) else [])
        known = {
            proposal.get("proposal_id"): proposal
            for proposal in proposals
            if isinstance(proposal, dict) and proposal.get("proposal_id")
        }
        unknown = [proposal_id for proposal_id, _, _, _ in normalised
                   if proposal_id not in known]
        if unknown:
            return jsonify({"error": "unknown channel proposal_id(s): "
                            + ", ".join(sorted(unknown))}), 409

        decisions = dict(job.get("channel_proposal_decisions")
                         or result.get("channel_proposal_decisions") or {})
        decided_at = now_iso()
        scale_k = job.get("scale_k") or result.get("scale_k")
        scale_k = float(scale_k) if isinstance(scale_k, (int, float)) and scale_k > 0 else None
        for proposal_id, action, length_lm, polyline_pts in normalised:
            proposal = known[proposal_id]
            accepted_polyline = polyline_pts or proposal.get("polyline_pts", [])
            if action == "accept" and polyline_pts is not None:
                if scale_k is None:
                    return jsonify({
                        "error": "job scale_k is required to accept edited channel geometry"
                    }), 409
                length_lm = round(math.dist(polyline_pts[0], polyline_pts[1]) * scale_k, 2)
            decisions[proposal_id] = {
                "proposal_id": proposal_id,
                "decision": "accepted" if action == "accept" else "removed",
                "length_lm": length_lm,
                "original_proposed_length_lm": proposal.get("proposed_length_lm"),
                "edited": bool(
                    action == "accept"
                    and (length_lm != proposal.get("proposed_length_lm")
                         or accepted_polyline != proposal.get("polyline_pts", []))
                ),
                "geometry_edited": bool(
                    action == "accept"
                    and accepted_polyline != proposal.get("polyline_pts", [])
                ),
                "decided_at": decided_at,
                # Both remain assumptions: editing the geometry does not turn it into a measured
                # zone or costing quantity. Original geometry remains on the proposal record.
                "polyline_pts": accepted_polyline,
            }
        reviewed = bool(known) and all(
            decisions.get(proposal_id, {}).get("decision") in {"accepted", "removed"}
            for proposal_id in known
        )
        result.update({
            "channel_proposal_decisions": decisions,
            "channel_proposals_reviewed": reviewed,
        })
        job.update({
            "channel_proposal_decisions": decisions,
            "channel_proposals_reviewed": reviewed,
            "result": result,
        })
        jobs[job_id] = job
        _record_learning_event(
            job_id, job, before_job, "channel_proposals_reviewed",
            details={"decisions": copy.deepcopy(decisions), "review_complete": reviewed},
        )
        save_jobs(jobs)

    log_training({
        "event": "channel_proposals_reviewed",
        "job_id": job_id,
        "proposal_ids": [proposal_id for proposal_id, _, _, _ in normalised],
        "decisions": decisions,
        "review_complete": reviewed,
        "timestamp": decided_at,
    })
    return jsonify({
        "status": "channel_proposals_updated",
        "decisions": decisions,
        "review_complete": reviewed,
    })


@app.route("/transition-candidates/<job_id>", methods=["POST"])
def review_transition_candidates(job_id):
    """Persist assessor accept/edit/remove decisions for assumed Transition lengths.

    Accepted candidates become explicit provisional quantities on the job, outside measured
    zones and costing. Quotation generation consumes only those accepted quantities and leaves
    their rate blank; pending candidates remain declarations only.
    """
    data = request.get_json(silent=True) or {}
    submitted = data.get("decisions")
    if not isinstance(submitted, list) or not submitted:
        return jsonify({"error": "decisions must be a non-empty list"}), 400

    normalised = []
    for item in submitted:
        if not isinstance(item, dict):
            return jsonify({"error": "each decision must be an object"}), 400
        candidate_id = str(item.get("candidate_id") or "").strip()
        action = str(item.get("action") or "").strip().lower()
        if not candidate_id or action not in {"accept", "remove"}:
            return jsonify({
                "error": "candidate_id and action accept/remove are required"
            }), 400
        length_lm = item.get("length_lm")
        if action == "accept":
            if (not isinstance(length_lm, (int, float)) or isinstance(length_lm, bool)
                    or not math.isfinite(length_lm) or not 0 < length_lm <= 100_000):
                return jsonify({"error": "accepted length_lm must be a positive number"}), 400
            length_lm = round(float(length_lm), 2)
        else:
            length_lm = None
        normalised.append((candidate_id, action, length_lm))
    if len({candidate_id for candidate_id, _, _ in normalised}) != len(normalised):
        return jsonify({"error": "candidate_id decisions must be unique"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        before_job = copy.deepcopy(job)
        result = dict(job.get("result") or {})
        candidates = job.get("transition_candidates")
        if not isinstance(candidates, list):
            candidates = (result.get("transition_candidates")
                          if isinstance(result.get("transition_candidates"), list) else [])
        known = {
            candidate.get("candidate_id"): candidate
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        }
        unknown = [candidate_id for candidate_id, _, _ in normalised
                   if candidate_id not in known]
        if unknown:
            return jsonify({"error": "unknown transition candidate_id(s): "
                            + ", ".join(sorted(unknown))}), 409

        decisions = dict(job.get("transition_candidate_decisions")
                         or result.get("transition_candidate_decisions") or {})
        decided_at = now_iso()
        for candidate_id, action, length_lm in normalised:
            candidate = known[candidate_id]
            decisions[candidate_id] = {
                "candidate_id": candidate_id,
                "decision": "accepted" if action == "accept" else "removed",
                "length_lm": length_lm,
                "original_proposed_length_lm": candidate.get("proposed_length_lm"),
                "edited": bool(
                    action == "accept"
                    and length_lm != candidate.get("proposed_length_lm")
                ),
                "decided_at": decided_at,
            }
        reviewed = bool(known) and all(
            decisions.get(candidate_id, {}).get("decision") in {"accepted", "removed"}
            for candidate_id in known
        )
        accepted_quantities = []
        for candidate_id, candidate in known.items():
            decision = decisions.get(candidate_id) or {}
            if decision.get("decision") != "accepted":
                continue
            accepted_quantities.append({
                "candidate_id": candidate_id,
                "region_id": candidate.get("region_id"),
                "category": "transition",
                "measurement_kind": "length",
                "length_lm": decision.get("length_lm"),
                "unit": "Lm",
                "assumed": True,
                "provisional": True,
                "basis": candidate.get("basis"),
                "source": candidate.get("source"),
                "assessor_edited": bool(decision.get("edited")),
            })
        result.update({
            "transition_candidate_decisions": decisions,
            "transition_candidates_reviewed": reviewed,
            "accepted_transition_quantities": accepted_quantities,
        })
        job.update({
            "transition_candidate_decisions": decisions,
            "transition_candidates_reviewed": reviewed,
            "accepted_transition_quantities": accepted_quantities,
            "result": result,
        })
        jobs[job_id] = job
        _record_learning_event(
            job_id, job, before_job, "transition_candidates_reviewed",
            details={"decisions": copy.deepcopy(decisions),
                     "accepted_quantities": copy.deepcopy(accepted_quantities),
                     "review_complete": reviewed},
        )
        save_jobs(jobs)

    log_training({
        "event": "transition_candidates_reviewed",
        "job_id": job_id,
        "candidate_ids": [candidate_id for candidate_id, _, _ in normalised],
        "decisions": decisions,
        "accepted_quantities": accepted_quantities,
        "review_complete": reviewed,
        "timestamp": decided_at,
    })
    return jsonify({
        "status": "transition_candidates_updated",
        "decisions": decisions,
        "accepted_quantities": accepted_quantities,
        "review_complete": reviewed,
    })


@app.route("/yard-regions/<job_id>", methods=["POST"])
def review_yard_regions(job_id):
    """Keep/exclude every retained same-tint component and recalculate its Yard quantity.

    These are measured raster components, not assumed quantities.  A multi-region result is
    deliberately approval-blocked until this endpoint receives one explicit decision per
    region; that is what lets the same generic mechanism include a second unit Yard while an
    assessor excludes a neighbouring building carrying the same surface tint.
    """
    data = request.get_json(silent=True) or {}
    submitted = data.get("decisions")
    if not isinstance(submitted, list) or not submitted:
        return jsonify({"error": "decisions must be a non-empty list"}), 400
    normalised = []
    for item in submitted:
        if not isinstance(item, dict):
            return jsonify({"error": "each decision must be an object"}), 400
        region_id = str(item.get("region_id") or "").strip()
        action = str(item.get("action") or "").strip().lower()
        if not region_id or action not in {"keep", "exclude"}:
            return jsonify({"error": "region_id and action keep/exclude are required"}), 400
        normalised.append((region_id, action))
    if len({region_id for region_id, _ in normalised}) != len(normalised):
        return jsonify({"error": "region_id decisions must be unique"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job.get("status") == "processing":
            return jsonify({"error": "job is still processing"}), 409
        before_job = copy.deepcopy(job)
        result = dict(job.get("result") or {})
        regions = job.get("yard_regions")
        if not isinstance(regions, list):
            regions = result.get("yard_regions") if isinstance(result.get("yard_regions"), list) else []
        known = {str(region.get("region_id")): dict(region)
                 for region in regions if isinstance(region, dict) and region.get("region_id")}
        submitted_ids = {region_id for region_id, _ in normalised}
        if submitted_ids != set(known):
            return jsonify({
                "error": "decisions must cover every current yard region exactly once"
            }), 409
        decisions = {}
        decided_at = now_iso()
        for region_id, action in normalised:
            known[region_id]["included"] = action == "keep"
            decisions[region_id] = {
                "region_id": region_id,
                "decision": "kept" if action == "keep" else "excluded",
                "decided_at": decided_at,
            }
        ordered_regions = [known[str(region["region_id"])] for region in regions]
        kept = [region for region in ordered_regions if region.get("included")]
        if not kept:
            return jsonify({"error": "at least one Yard region must be kept; use Adjust to replace it"}), 400
        area_m2 = round(sum(float(region.get("area_m2") or 0) for region in kept), 1)

        zones = job.get("zones") if isinstance(job.get("zones"), list) else result.get("zones")
        zones = [dict(zone) for zone in (zones or [])]
        for zone in zones:
            if str(zone.get("category") or "").strip().lower() == "external_yard":
                zone["area_m2"] = area_m2
                zone["needs_assessor"] = False
                zone["region_count"] = len(kept)
        zones_total = round(sum(float(zone.get("area_m2") or 0) for zone in zones), 1)
        old_flags = [flag for flag in (result.get("flags") or [])
                     if not str(flag).startswith("YARD REGION REVIEW REQUIRED:")]
        summary = (f"assessor Yard-region review: kept {len(kept)} of {len(ordered_regions)} "
                   f"same-tint regions; final Yard {area_m2:,.1f} m²")
        old_flags.append(summary)
        result.update({
            "area_m2": area_m2,
            "zones": zones,
            "zones_total_area_m2": zones_total,
            "yard_regions": ordered_regions,
            "yard_region_decisions": decisions,
            "yard_region_review_required": False,
            "flags": old_flags,
            "polygon_pts": kept[0].get("polygon_pts"),
        })
        costing_result = _run_costing(area_m2, result)
        if costing_result:
            result["costing"] = costing_result
        job_flags = [flag for flag in (job.get("flags") or [])
                     if not str(flag).startswith("YARD REGION REVIEW REQUIRED:")]
        job_flags.append(summary)
        job.update({
            "area_m2": area_m2,
            "zones": zones,
            "yard_regions": ordered_regions,
            "yard_region_decisions": decisions,
            "yard_region_review_required": False,
            "yard_regions_reviewed_at": decided_at,
            "polygon_pts": kept[0].get("polygon_pts"),
            "costing": costing_result,
            "flags": job_flags,
            "result": result,
        })
        jobs[job_id] = job
        _record_learning_event(
            job_id, job, before_job, "yard_regions_reviewed",
            details={"decisions": copy.deepcopy(decisions), "final_area_m2": area_m2},
        )
        save_jobs(jobs)

    log_training({
        "event": "yard_regions_reviewed", "job_id": job_id,
        "kept_region_ids": [region["region_id"] for region in kept],
        "excluded_region_ids": [region["region_id"] for region in ordered_regions
                                if not region.get("included")],
        "area_m2": area_m2, "timestamp": decided_at,
    })
    return jsonify({
        "status": "yard_regions_reviewed", "job_id": job_id,
        "area_m2": area_m2, "kept": len(kept),
        "excluded": len(ordered_regions) - len(kept),
        "review_complete": True,
    })


def _brief_spec_changes(before: dict, after: dict) -> list[dict]:
    """Auditable field changes, excluding source metadata that does not change the spec."""
    from slab_spec import FIELD_LABELS
    before_fields = (before or {}).get("fields") or {}
    after_fields = (after or {}).get("fields") or {}
    changes = []
    for key in FIELD_LABELS:
        old = before_fields.get(key) if isinstance(before_fields.get(key), dict) else {}
        new = after_fields.get(key) if isinstance(after_fields.get(key), dict) else {}
        old_state = (old.get("value"), bool(old.get("provisional", True)))
        new_state = (new.get("value"), bool(new.get("provisional", True)))
        if old_state == new_state:
            continue
        changes.append({
            "field": key,
            "label": FIELD_LABELS[key],
            "old": old.get("value"),
            "new": new.get("value"),
            "old_provisional": bool(old.get("provisional", True)),
            "new_provisional": bool(new.get("provisional", True)),
        })
    return changes


def _mark_post_approval_spec_correction(job: dict, before: dict, after: dict,
                                        *, zone_category: str | None = None) -> dict | None:
    """Turn an approved-job edit into a visible revised decision, never an in-place rewrite."""
    if job.get("decision") != "approved":
        return None
    changes = _brief_spec_changes(before, after)
    if not changes:
        return None
    corrected_at = now_iso()
    history = list(job.get("quotation_history") or [])
    issued_revisions = [int(entry.get("revision") or 0) for entry in history
                        if isinstance(entry, dict) and entry.get("paths")]
    prior_revision = max(issued_revisions, default=(
        int(job.get("quotation_revision") or 1)
        if job.get("quotation_status") == "ready" and job.get("quotation_paths") else 1
    ))
    if not any(int(entry.get("revision") or 0) == prior_revision for entry in history):
        history.append({
            "revision": prior_revision,
            "label": f"REV_{prior_revision:02d}",
            "issued_at": job.get("decided_at"),
            "reason": "approval before specification correction",
            "paths": dict(job.get("quotation_paths") or {}),
        })
    correction = {
        "corrected_at": corrected_at,
        "prior_decision": "approved",
        "prior_revision": prior_revision,
        "revision": prior_revision + 1,
        "zone_category": zone_category,
        "changes": changes,
    }
    corrections = list(job.get("spec_correction_history") or [])
    corrections.append(correction)
    job.update({
        # The assessor is correcting their own approved specification. Keep that authority
        # explicit: this is still approved, but it is a later commercial revision. Demoting it
        # to the generic "adjusted" state hid the fact that an issued quote had been corrected
        # and made subsequent corrections stop creating revisions.
        "status": "approved",
        "decision": "approved",
        "spec_corrected_after_approval": True,
        "spec_corrected_at": corrected_at,
        "spec_correction_history": corrections,
        "quotation_revision": prior_revision + 1,
        "quotation_history": history,
        "quotation_status": "revision_pending",
    })
    job.pop("quotation_error", None)
    return correction


def _issue_post_approval_spec_revision(jobs: dict, job_id: str,
                                       correction: dict | None) -> dict:
    """Save REV_N beside (never over) the earlier issued files and update its audit history."""
    if not correction:
        return {}
    job = jobs[job_id]
    revision = int(correction["revision"])
    safe_ref = _sanitise_filename(str(job.get("project_ref") or job_id)) or job_id
    result = _quotation_result_for_job(job)
    paths = _save_quotation(
        job_id, result, job.get("costing"),
        file_stem=f"{safe_ref}-REV_{revision:02d}",
    )
    error = paths.get("error") if isinstance(paths, dict) else None
    if error:
        job.update({
            "quotation_status": "error",
            "quotation_error": error,
        })
    else:
        history = list(job.get("quotation_history") or [])
        history.append({
            "revision": revision,
            "label": f"REV_{revision:02d}",
            "issued_at": now_iso(),
            "reason": "specification correction after approval",
            "paths": dict(paths),
            "changes": list(correction.get("changes") or []),
        })
        job.update({
            "quotation_paths": paths,
            "quotation_history": history,
            "quotation_status": "ready",
        })
        job.pop("quotation_error", None)
    jobs[job_id] = job
    save_jobs(jobs)
    return paths


def _costing_for_brief_spec(area_m2: float, brief_spec: dict) -> tuple[dict | None, dict, str]:
    """Use the existing calculation for one assessor-scoped slab specification.

    This is an adapter above the denylisted pricing modules: it supplies the assessor's
    confirmed fields, applies the existing defaults/client-rate layer, and returns an explicit
    per-zone costing. Unsupported open text remains saved but produces a pricing-review warning,
    never a guessed rate.
    """
    from slab_spec import COMMON_FIELDS, build_brief_spec, confirmed_values
    from defaults import spec_with_defaults, assumption_note, flag_assumed
    from costing import rate_buildup

    confirmed = confirmed_values(brief_spec)
    pricing_override = {key: confirmed[key] for key in COMMON_FIELDS if key in confirmed}
    try:
        spec, _ = spec_with_defaults(pricing_override)
        spec, _manhole_rate, rates_provenance = _apply_current_client_rates(spec)
        assumed = not all(key in pricing_override for key in COMMON_FIELDS)
        rate, parts = rate_buildup(**{key: spec[key] for key in [
            "depth_mm", "conc_rate", "conc_wastage", "mesh", "layers",
            "steel_rate_t", "steel_wastage", "lap_acc", "dpm", "curing",
            "labour", "trim", "margin"]})
        costing = {
            "area_m2": area_m2,
            "rate": rate,
            "total_gbp": round(area_m2 * rate, 2),
            "spec": spec,
            "assumed": assumed,
            "note": assumption_note(spec) if assumed else "Spec overridden by assessor",
            "flags": flag_assumed(spec, assumed),
            "breakdown": parts,
        }
        costing.update(rates_provenance)
        rebuilt = build_brief_spec(
            brief_spec.get("slab_type"),
            effective_spec=spec,
            confirmed=confirmed,
            source="assessor",
            replace=True,
        )
        return costing, rebuilt, ""
    except Exception:
        return None, brief_spec, (
            "Specification saved, but the current rate build-up does not support one or more "
            "supplied pricing fields; human pricing review required."
        )


@app.route("/spec-override/<job_id>", methods=["POST"])
def spec_override(job_id):
    """Capture Fortel's slab checklist and re-price only its supplied pricing fields.

    The extra Brief_Spec fields are presentation/provenance only.  The existing rate
    calculation remains unchanged; partial common specs continue to use the existing
    costing fallbacks but remain visibly provisional instead of being marked confirmed.
    """
    data = request.get_json(silent=True) or {}
    nested_fields = data.get("fields")
    if nested_fields is not None and not isinstance(nested_fields, dict):
        return jsonify({"error": "fields must be an object"}), 400
    from slab_spec import (COMMON_FIELDS, FIELD_LABELS, build_brief_spec,
                           confirmed_values, normalise_slab_type, schema_definition)
    supplied_slab_type = data.get("slab_type")
    zone_category = data.get("zone_category")
    if supplied_slab_type and (
            not isinstance(supplied_slab_type, str)
            or supplied_slab_type not in schema_definition()):
        return jsonify({"error": "unknown slab_type"}), 400
    if zone_category and (
            not isinstance(zone_category, str)
            or zone_category not in schema_definition()):
        return jsonify({"error": "unknown zone_category"}), 400
    supplied_fields = dict(nested_fields) if nested_fields is not None else {
        key: data[key] for key in FIELD_LABELS if key in data
    }

    with _jobs_lock:
        jobs = load_jobs()
        job  = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        before_job = copy.deepcopy(job)

        res     = dict(job.get("result") or {})
        adj     = job.get("adjusted") or {}
        area_m2 = adj.get("area_m2") or res.get("area_m2")
        if zone_category:
            # A mixed marked sheet has no safe job-level rate to inherit (the real BOQ uses
            # different Yard/Dock build-ups). Capture the client checklist per zone, but leave
            # its rate for explicit assessor entry in the editable quotation.
            zones = job.get("zones") if isinstance(job.get("zones"), list) else res.get("zones") or []
            if not any(zone.get("category") == zone_category for zone in zones
                       if isinstance(zone, dict)):
                return jsonify({"error": "zone_category is not present on this job"}), 400
            brief_specs = dict(job.get("brief_specs") or res.get("brief_specs") or {})
            existing_zone_brief = brief_specs.get(zone_category) or {}
            try:
                brief_spec = build_brief_spec(
                    zone_category,
                    confirmed=supplied_fields,
                    source="assessor",
                    existing=existing_zone_brief,
                    replace=nested_fields is not None,
                )
            except (TypeError, ValueError) as e:
                return jsonify({"error": f"invalid spec field: {e}"}), 400
            existing_zone_confirmed = confirmed_values(existing_zone_brief)
            pricing_fields_submitted = any(
                key in supplied_fields and supplied_fields.get(key) not in (None, "")
                for key in COMMON_FIELDS
            ) or any(
                key in existing_zone_confirmed and key in supplied_fields
                and supplied_fields.get(key) in (None, "")
                for key in COMMON_FIELDS
            )
            zone_area_m2 = round(sum(
                float(zone.get("area_m2") or 0)
                for zone in zones if isinstance(zone, dict)
                and zone.get("category") == zone_category
            ), 2)
            zone_costing = None
            repriced = False
            pricing_warning = job.get("spec_pricing_warning") or ""
            if pricing_fields_submitted and zone_area_m2 > 0:
                zone_costing, brief_spec, pricing_warning = _costing_for_brief_spec(
                    zone_area_m2, brief_spec)
                repriced = zone_costing is not None
            elif pricing_fields_submitted:
                pricing_warning = ""
            brief_specs[zone_category] = brief_spec
            res["brief_specs"] = brief_specs
            job["brief_specs"] = brief_specs
            if zone_costing:
                zone_costings = dict(job.get("zone_costings") or res.get("zone_costings") or {})
                zone_costings[zone_category] = zone_costing
                res["zone_costings"] = zone_costings
                job["zone_costings"] = zone_costings
                measured_categories = {
                    zone.get("category") for zone in zones if isinstance(zone, dict)
                    and zone.get("area_m2") is not None
                }
                if measured_categories == {zone_category}:
                    # A one-zone result has no mixed-spec ambiguity: keep the portal's costing
                    # card and the later approve path on the same assessor-corrected build-up.
                    res["costing"] = zone_costing
                    job["costing"] = zone_costing
                    job["spec_override"] = confirmed_values(brief_spec)
            if pricing_warning:
                job["spec_pricing_warning"] = pricing_warning
            else:
                job.pop("spec_pricing_warning", None)
            job["result"] = res
            correction = _mark_post_approval_spec_correction(
                job, existing_zone_brief, brief_spec, zone_category=zone_category)
            jobs[job_id] = job
            _record_learning_event(
                job_id, job, before_job, "spec_overridden",
                details={"zone_category": zone_category,
                         "fields": copy.deepcopy(supplied_fields), "repriced": False},
            )
            save_jobs(jobs)
            revision_paths = _issue_post_approval_spec_revision(jobs, job_id, correction)
            log_training({
                "event": "spec_override", "job_id": job_id,
                "zone_category": zone_category, "fields": supplied_fields,
                "repriced": False, "timestamp": now_iso(),
            })
            if correction:
                log_training({
                    "event": "spec_correction_after_approval", "job_id": job_id,
                    "revision": correction["revision"], "zone_category": zone_category,
                    "changes": correction["changes"], "timestamp": correction["corrected_at"],
                })
            return jsonify({
                "status": "ok", "job_id": job_id, "costing": job.get("costing"),
                "zone_costing": zone_costing,
                "brief_spec": brief_spec, "brief_specs": brief_specs,
                "spec_schema": schema_definition(), "repriced": repriced,
                "pricing_warning": pricing_warning,
                "post_approval_correction": bool(correction),
                "quotation_revision": correction.get("revision") if correction else None,
                "quotation_paths": revision_paths,
            })
        try:
            existing_brief = job.get("brief_spec") or res.get("brief_spec") or {}
            slab_type = normalise_slab_type(
                data.get("slab_type") or existing_brief.get("slab_type"),
                text=" ".join(str(res.get(key) or "") for key in
                              ("quotation_section", "file", "project_name", "type")),
            )
            costing = dict(job.get("costing") or res.get("costing") or {})
            effective_spec = costing.get("spec") or {}
            brief_spec = build_brief_spec(
                slab_type,
                effective_spec=effective_spec,
                confirmed=supplied_fields,
                source="assessor",
                existing=existing_brief,
                replace=nested_fields is not None,
            )
            confirmed = confirmed_values(brief_spec)
            pricing_override = {key: confirmed[key] for key in COMMON_FIELDS if key in confirmed}

            # Optional checklist metadata never touches a rate.  When a common pricing field
            # is submitted, preserve the legacy override behaviour and use the same existing
            # defaults + rate_buildup calculation, while keeping missing fields provisional.
            existing_confirmed = confirmed_values(existing_brief)
            pricing_fields_submitted = any(
                key in supplied_fields and supplied_fields.get(key) not in (None, "")
                for key in COMMON_FIELDS
            ) or any(
                key in existing_confirmed and key in supplied_fields
                and supplied_fields.get(key) in (None, "")
                for key in COMMON_FIELDS
            )
            repriced = False
            rates_provenance = {}
            pricing_warning = job.get("spec_pricing_warning") or ""
            if pricing_fields_submitted and not area_m2:
                from costing import MESH_KG
                if pricing_override.get("mesh") and pricing_override["mesh"] not in MESH_KG:
                    pricing_warning = (
                        "Specification saved, but the current rate build-up does not support "
                        "one or more supplied pricing fields; human pricing review required."
                    )
                else:
                    pricing_warning = ""
            if pricing_fields_submitted and area_m2:
                from defaults import spec_with_defaults, assumption_note, flag_assumed
                from costing  import rate_buildup
                try:
                    spec, _ = spec_with_defaults(pricing_override)
                    spec, _manhole_rate, rates_provenance = _apply_current_client_rates(spec)
                    assumed = not all(key in pricing_override for key in COMMON_FIELDS)
                    rate, parts = rate_buildup(**{key: spec[key] for key in [
                        "depth_mm", "conc_rate", "conc_wastage", "mesh", "layers",
                        "steel_rate_t", "steel_wastage", "lap_acc", "dpm", "curing",
                        "labour", "trim", "margin"]})
                    costing = {
                        "area_m2": area_m2,
                        "rate": rate,
                        "total_gbp": round(area_m2 * rate, 2),
                        "spec": spec,
                        "assumed": assumed,
                        "note": assumption_note(spec) if assumed else "Spec overridden by assessor",
                        "flags": flag_assumed(spec, assumed),
                        "breakdown": parts,
                    }
                    costing.update(rates_provenance)
                    # Rebuild so fallback values used in the unchanged calculation are visible,
                    # field-by-field, as assumed rather than as blank confirmed client data.
                    brief_spec = build_brief_spec(
                        slab_type,
                        effective_spec=spec,
                        confirmed=pricing_override | {
                            key: value for key, value in confirmed.items() if key not in COMMON_FIELDS
                        },
                        source="assessor",
                        replace=True,
                    )
                    repriced = True
                    pricing_warning = ""
                except Exception:
                    # The client checklist deliberately accepts open text. Preserve an
                    # unsupported-but-valid client specification without inventing a rate or
                    # losing their entry; approval stays blocked for human pricing review.
                    pricing_warning = (
                        "Specification saved, but the current rate build-up does not support "
                        "one or more supplied pricing fields; human pricing review required."
                    )
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid spec field: {e}"}), 400
        except Exception as e:
            return jsonify({"error": f"costing failed: {e}"}), 500

        res["brief_spec"] = brief_spec
        if pricing_override:
            res["engineer_spec"] = dict(pricing_override)
        else:
            res.pop("engineer_spec", None)
        jobs[job_id]["result"] = res
        jobs[job_id]["brief_spec"] = brief_spec
        if costing:
            jobs[job_id]["costing"] = costing
        jobs[job_id]["spec_override"] = confirmed_values(brief_spec)
        if pricing_warning:
            jobs[job_id]["spec_pricing_warning"] = pricing_warning
        else:
            jobs[job_id].pop("spec_pricing_warning", None)
        correction = _mark_post_approval_spec_correction(
            jobs[job_id], existing_brief, brief_spec)
        _record_learning_event(
            job_id, jobs[job_id], before_job, "spec_overridden",
            details={"zone_category": None, "fields": copy.deepcopy(supplied_fields),
                     "repriced": repriced},
        )
        save_jobs(jobs)
        revision_paths = _issue_post_approval_spec_revision(jobs, job_id, correction)
        if correction:
            log_training({
                "event": "spec_correction_after_approval", "job_id": job_id,
                "revision": correction["revision"],
                "changes": correction["changes"], "timestamp": correction["corrected_at"],
            })

    log_training({
        "event": "spec_override", "job_id": job_id,
        "zone_category": None, "fields": supplied_fields,
        "repriced": repriced, "timestamp": now_iso(),
    })
    return jsonify({
        "status": "ok", "job_id": job_id, "costing": costing,
        "brief_spec": brief_spec, "spec_schema": schema_definition(),
        "repriced": repriced, "pricing_warning": pricing_warning,
        "post_approval_correction": bool(correction),
        "quotation_revision": correction.get("revision") if correction else None,
        "quotation_paths": revision_paths,
    })


# ── Soft delete (archive) ─────────────────────────────────────────────────────
#
# Aryan asked for a portal "delete estimation" button. A hard delete would destroy client
# decision history and training-log context, and an unauthenticated/mis-clicked hard delete
# of an approved six-figure job is unrecoverable — approval_jobs.json is the only system of
# record. So: SOFT delete only. /archive sets status='deleted' + an archived_at timestamp and
# moves a COPY of the record into approval_jobs_archive.json (never destroys it); the job is
# then removed from the hot jobs file (kept small) and hidden from the default /jobs list.
# /unarchive restores it. There is deliberately no hard-delete endpoint — see CLAUDE.md
# ("never commit client data") and the prod-audit finding this implements.

def _load_archive() -> dict:
    if JOBS_ARCHIVE_FILE.exists():
        try:
            return json.loads(JOBS_ARCHIVE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_archive(archive: dict):
    tmp = JOBS_ARCHIVE_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(archive, indent=2))
    os.replace(tmp, JOBS_ARCHIVE_FILE)


@app.route("/archive/<job_id>", methods=["POST"])
def archive_job(job_id):
    """Soft-delete: move the job to approval_jobs_archive.json, mark status='deleted' there,
    and drop it from the hot jobs file. Blocked for already-approved jobs — those represent a
    committed client quotation and need Jas (a human, out-of-band) to unwind, not a button."""
    data = request.get_json(silent=True) or {}
    note = data.get("note", "")

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job.get("status") == "processing":
            return jsonify({"error": "job is still processing — wait for it to finish"}), 409
        if job.get("decision") == "approved":
            return jsonify({"error": "job is already approved — this represents a committed "
                                      "client quotation; ask Jas to archive/unwind it manually, "
                                      "it cannot be deleted from the portal"}), 409

        job = dict(job)
        job["archived"]    = True
        job["archived_at"] = now_iso()
        job["archive_note"] = note
        job["status"]      = "deleted"

        archive = _load_archive()
        archive[job_id] = job
        _save_archive(archive)

        del jobs[job_id]
        save_jobs(jobs)

    log_training({"event": "archive", "job_id": job_id, "note": note, "timestamp": now_iso()})
    return jsonify({"status": "archived", "job_id": job_id})


@app.route("/unarchive/<job_id>", methods=["POST"])
def unarchive_job(job_id):
    """Reverse an /archive — mistakes must be recoverable. Restores the job into the hot
    jobs file and removes it from the archive."""
    with _jobs_lock:
        archive = _load_archive()
        job = archive.get(job_id)
        if not job:
            return jsonify({"error": f"archived job {job_id!r} not found"}), 404

        job = dict(job)
        job["archived"] = False
        job.pop("archived_at", None)
        job.pop("archive_note", None)
        # Restore a sane status — 'pending' unless the job carries its own decision already.
        job["status"] = job.get("decision") or "pending"

        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)

        del archive[job_id]
        _save_archive(archive)

    log_training({"event": "unarchive", "job_id": job_id, "timestamp": now_iso()})
    return jsonify({"status": "unarchived", "job_id": job_id})


@app.route("/jobs/archived")
def list_archived_jobs():
    """Archived jobs, kept out of the default /jobs listing (which the portal polls every
    15s) so the hot list stays focused on live work."""
    return jsonify(_load_archive())


# ── Costing / quotation helpers ───────────────────────────────────────────────

class QuotationPricingBlocked(RuntimeError):
    """A saved specification or zone-review state makes quotation output unsafe."""


def _quotation_result_for_job(job: dict, result_override=None, costing_override=None) -> dict:
    """Effective approved result, preferring assessor-adjusted measurements when present."""
    result = dict(result_override if result_override is not None else (job.get("result") or {}))
    costing = costing_override if costing_override is not None else job.get("costing")
    if costing:
        result["costing"] = dict(costing)
    if job.get("brief_spec"):
        result["brief_spec"] = dict(job["brief_spec"])
    if isinstance(job.get("brief_specs"), dict):
        result["brief_specs"] = dict(job["brief_specs"])
    if isinstance(job.get("zone_costings"), dict):
        result["zone_costings"] = dict(job["zone_costings"])
    if isinstance(job.get("zones"), list):
        result["zones"] = list(job["zones"])
    if isinstance(job.get("channel_proposals"), list):
        result["channel_proposals"] = list(job["channel_proposals"])
    if isinstance(job.get("transition_candidates"), list):
        result["transition_candidates"] = list(job["transition_candidates"])
    if isinstance(job.get("transition_candidate_decisions"), dict):
        result["transition_candidate_decisions"] = dict(
            job["transition_candidate_decisions"])
    if isinstance(job.get("accepted_transition_quantities"), list):
        result["accepted_transition_quantities"] = list(
            job["accepted_transition_quantities"])
    if isinstance(job.get("channel_proposal_decisions"), dict):
        result["channel_proposal_decisions"] = dict(job["channel_proposal_decisions"])
    if isinstance(job.get("yard_regions"), list):
        result["yard_regions"] = list(job["yard_regions"])
    if isinstance(job.get("yard_region_decisions"), dict):
        result["yard_region_decisions"] = dict(job["yard_region_decisions"])
    if isinstance(job.get("user_channels"), list):
        result["user_channels"] = list(job["user_channels"])
    if isinstance(job.get("area_elements"), list):
        result["area_elements"] = copy.deepcopy(job["area_elements"])
    if "yard_region_review_required" in job:
        result["yard_region_review_required"] = bool(job["yard_region_review_required"])
    if "zone_classification_required" in job:
        result["zone_classification_required"] = bool(job["zone_classification_required"])
    if job.get("zone_reference_mismatch"):
        result["zone_reference_mismatch"] = True
    if job.get("zone_allocation_stale"):
        result["zone_allocation_stale"] = True
    adjusted = job.get("adjusted") or {}
    if adjusted.get("area_m2"):
        result["area_m2"] = adjusted["area_m2"]
        if result.get("costing"):
            result["costing"] = dict(result["costing"])
            result["costing"]["area_m2"] = adjusted["area_m2"]
            # Preserve the route's existing adjusted-area total behaviour unchanged.
            result["costing"]["total_gbp"] = round(
                adjusted["area_m2"] * (result["costing"].get("rate") or 0), 2)
    if isinstance(adjusted.get("scale_k"), (int, float)) and adjusted["scale_k"] > 0:
        # Assessor-drawn areas and lengths share this coordinate space. Leaving the pipeline
        # k here made a 100 px channel submitted at 0.1 m/px quote as 17.64 Lm using 0.17639.
        result["scale_k"] = adjusted["scale_k"]
    if adjusted and "perimeter_lm" in adjusted:
        if adjusted.get("perimeter_lm") is not None:
            result["perimeter_lm"] = adjusted["perimeter_lm"]
        elif adjusted.get("area_m2"):
            # A direct area-only adjustment has no matching final geometry. Do not present
            # the superseded AI outline's perimeter as if it described the adjusted area.
            result.pop("perimeter_lm", None)
            result.pop("polygon_pts", None)
    if isinstance(adjusted.get("user_channels"), list):
        result["user_channels"] = list(adjusted["user_channels"])
    if isinstance(adjusted.get("cutout_regions"), list):
        result["cutout_regions"] = list(adjusted["cutout_regions"])
    if (not isinstance(job.get("area_elements"), list)
            and isinstance(adjusted.get("area_elements"), list)):
        result["area_elements"] = copy.deepcopy(adjusted["area_elements"])
    # Carry assessor-confirmed manhole count into the quotation result.
    if "manhole_count" in adjusted:
        if adjusted["manhole_count"] is not None:
            result["manhole_count"] = adjusted["manhole_count"]
            result.pop("manhole_count_estimate", None)
            result.pop("manhole_count_assumed", None)
        else:
            result.pop("manhole_count", None)
    return result


def _quotation_for_job(job_id: str, result_override=None, costing_override=None):
    """Build one project quotation from every approved/adjusted sibling job."""
    from quotation import generate_quotation

    hot_jobs = load_jobs()
    # Anchor lookup reaches into the archive (the job may have just been archived
    # while the assessor was still reviewing it), but sibling discovery must stay
    # within hot_jobs only — otherwise a new project that reuses the same
    # project_ref would inherit measurements from archived drawings.
    all_jobs = dict(_load_archive())
    all_jobs.update(hot_jobs)
    anchor = all_jobs.get(job_id, {})
    project_ref = anchor.get("project_ref")
    unmeasured = []
    if project_ref:
        # ONE quotation per case (Aryan, 17 Jul: "it needs to be one that contains information
        # of all the documents in one case"). Every sibling document participates:
        #   - approved/adjusted -> firm quantities (as before)
        #   - pending BUT measured -> included, marked PROVISIONAL (pending assessor approval)
        #   - unmeasured/refused (e.g. line/hatch office GA plans awaiting a manual trace)
        #     -> listed explicitly in the quotation as awaiting assessor measurement, so a
        #        document can never silently vanish from the case output.
        siblings, seen = [], set()
        for sibling_id, sibling in hot_jobs.items():
            if sibling.get("project_ref") != project_ref or sibling_id in seen:
                continue
            seen.add(sibling_id)
            res = sibling.get("result") or {}
            area = (sibling.get("adjusted") or {}).get("area_m2") or res.get("area_m2")
            if sibling.get("decision") in ("approved", "adjusted") or area:
                # Filter out low-priority drawings (site plans, elevations, etc.) that
                # shouldn't contribute measured area to the quote. Only include if the
                # drawing has a reasonable priority score or is explicitly approved.
                from router import drawing_priority
                fname = res.get("file") or ""
                priority = drawing_priority(fname)
                if priority < -1 and sibling.get("decision") not in ("approved", "adjusted"):
                    # Low-priority drawing with no assessor approval — list as unmeasured
                    # instead of including its possibly-wrong measured area.
                    state = res.get("measurement_state") or sibling.get("status") or "UNMEASURED"
                    unmeasured.append({"file": fname, "state": state,
                                       "reason": f"drawing priority too low ({priority}); "
                                       "assessor must explicitly approve to include"})
                    continue
                siblings.append((sibling_id, sibling))
            else:
                fname = res.get("file") or Path(str(sibling.get("pdf") or "")).name or sibling_id
                state = res.get("measurement_state") or sibling.get("status") or "UNMEASURED"
                unmeasured.append({"file": fname, "state": state})
        siblings.sort(key=lambda pair: (pair[1].get("created_at") or "", pair[0]))
        # A case where NOTHING is measured yet (e.g. only office GA plans awaiting trace)
        # must still produce the case workbook — every document listed as awaiting
        # measurement, totals empty. Only fall back to the anchor when the case is
        # genuinely empty (no unmeasured docs either).
        if not siblings and not unmeasured:
            siblings = [(job_id, anchor)]
    else:
        siblings = [(job_id, anchor)]

    # A project quotation is deliberately identical whichever sibling drawing the assessor
    # opens. Revision metadata must follow the latest corrected sibling too; otherwise opening
    # Unit B could produce corrected Unit A money under the old REV_01 label with no caveat.
    revision_anchor = max(
        (sibling for _, sibling in siblings),
        key=lambda sibling: (
            int(sibling.get("quotation_revision") or 1),
            str(sibling.get("spec_corrected_at") or sibling.get("decided_at") or ""),
        ),
        default=anchor,
    )

    zone_blocks = []
    for sibling_id, sibling in siblings:
        # Zone gates only apply to documents CONTRIBUTING quantities — an unmeasured
        # line/hatch sheet awaiting a trace must not block the whole case quotation.
        res = sibling.get("result") or {}
        if not ((sibling.get("adjusted") or {}).get("area_m2") or res.get("area_m2")):
            continue
        reason = _zone_block_reason(sibling)
        if reason:
            zone_blocks.append((sibling_id, reason))
    # A FIRM quotation (anchor approved/adjusted) keeps the hard zone gate. A DRAFT case
    # (nothing approved yet — Aryan's fresh-upload flow) degrades the gate to a loud caveat
    # inside the workbook instead of a 409, so the team can see the whole case early.
    draft = anchor.get("decision") not in ("approved", "adjusted")
    caveats = []
    correction_history = revision_anchor.get("spec_correction_history") or []
    if correction_history:
        latest_correction = correction_history[-1]
        changed_labels = ", ".join(
            str(change.get("label") or change.get("field"))
            for change in latest_correction.get("changes", [])
        ) or "slab specification"
        caveats.append(
            "CORRECTION AFTER APPROVAL — "
            f"revision REV_{int(revision_anchor.get('quotation_revision') or 2):02d}; "
            f"corrected fields: {changed_labels}. The earlier issued quotation remains in "
            "the job's quotation history and has not been overwritten."
        )
    if zone_blocks:
        if not draft:
            sibling_id, reason = zone_blocks[0]
            raise QuotationPricingBlocked(
                f"quotation blocked: project drawing {sibling_id} {reason}"
            )
        for sibling_id, reason in zone_blocks:
            fname = ((all_jobs.get(sibling_id) or {}).get("result") or {}).get("file") or sibling_id
            caveats.append(f"CLASSIFY BEFORE APPROVAL — {fname}: {reason}. Quantities from "
                           "this drawing are provisional until the assessor classifies its zones.")

    if any(sibling.get("spec_pricing_warning") for _, sibling in siblings):
        raise QuotationPricingBlocked(
            "quotation blocked: a project drawing has a saved specification that requires "
            "human pricing review"
        )

    results = []
    for sibling_id, sibling in siblings:
        unit = _quotation_result_for_job(
            sibling,
            result_override if sibling_id == job_id else None,
            costing_override if sibling_id == job_id else None,
        )
        if sibling.get("decision") not in ("approved", "adjusted"):
            unit = dict(unit)
            unit["pending_approval"] = True
        results.append(unit)
    project = anchor.get("project_name") or (results[0].get("file", "") if results else "")
    client = anchor.get("client_name") or ""
    commercial = dict(anchor.get("commercial") or {})
    if revision_anchor.get("quotation_revision"):
        commercial["revision"] = f"REV_{int(revision_anchor['quotation_revision']):02d}"
    return generate_quotation(
        results, project=project, client=client, ref=project_ref or None,
        commercial=commercial or None,
        unmeasured=unmeasured or None, caveats=caveats or None,
    )

def _save_quotation(job_id: str, result: dict, costing: dict | None,
                    *, file_stem: str | None = None) -> dict:
    """Generate and save quotation files for this job. Returns paths dict."""
    try:
        from quotation import save_quotation
        q = _quotation_for_job(job_id, result_override=result, costing_override=costing)
        return save_quotation(q, out_dir=str(QUOTATIONS_DIR), file_stem=file_stem)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _run_costing(area_m2, result: dict) -> dict | None:
    """Run costing with defaults (or any spec stored in result)."""
    if not area_m2 or area_m2 <= 0:
        return None
    try:
        from defaults import spec_with_defaults, assumption_note, flag_assumed
        from costing  import rate_buildup

        engineer_spec = result.get("engineer_spec")  # None if architect-only
        spec, _ = spec_with_defaults(engineer_spec)
        manhole_in_scope = bool(result.get("manhole_count") or
                                 result.get("manhole_count_estimate"))
        spec, manhole_rate, rates_provenance = _apply_current_client_rates(
            spec, manhole_in_scope=manhole_in_scope)
        from slab_spec import COMMON_FIELDS
        supplied = engineer_spec or {}
        assumed = not all(supplied.get(key) is not None for key in COMMON_FIELDS)
        rate, parts   = rate_buildup(**{k: spec[k] for k in [
            "depth_mm","conc_rate","conc_wastage","mesh","layers",
            "steel_rate_t","steel_wastage","lap_acc","dpm","curing",
            "labour","trim","margin"]})
        total = round(area_m2 * rate, 2)
        costing = {
            "area_m2":   area_m2,
            "rate":      rate,
            "total_gbp": total,
            "spec":      spec,
            "assumed":   assumed,
            "note":      assumption_note(spec) if assumed else "",
            "flags":     flag_assumed(spec, assumed),
            "breakdown": parts,
        }
        if manhole_in_scope:
            from takeoff_pipeline import manhole_eo_line
            line, is_estimate = manhole_eo_line(
                result.get("manhole_count"), result.get("manhole_count_estimate"),
                rate=manhole_rate)
            if line:
                description, qty, unit, extra_rate = line
                extra_value = round(qty * extra_rate, 2)
                costing["extras"] = [{
                    "description": description, "qty": qty, "unit": unit,
                    "rate": extra_rate, "value": extra_value, "estimate": is_estimate,
                }]
                costing["grand_total_gbp"] = round(total + extra_value, 2)
        costing.update(rates_provenance)
        return costing
    except Exception as e:
        return {"error": str(e)}


# ── HTML email-click confirm page (GET, no mutation) ─────────────────────────

def _html_confirm_page(action: str, job_id: str) -> str:
    """Rendered for GET /approve|reject/<job_id> — the link an assessor clicks straight out
    of the email. Performs NO mutation itself: it's a plain HTML page whose <form> issues the
    real POST when (and only when) the human clicks the button. This is what keeps a mutating
    action from firing on mere top-level navigation (an email client link-preview/scanner
    prefetching the URL, or an attacker page that just links here, would otherwise have
    silently approved/rejected a job under the SameSite=Lax cookie — see approve()'s docstring).
    """
    labels  = {"approve": ("✅ Approve", "#27ae60"), "reject": ("✗ Reject", "#c0392b")}
    label, col = labels.get(action, (action.title(), "#13294b"))
    # Preserve ?token=... (or a cookie already covers it) so the POST from this page's form
    # is itself authorised when the token gate is on.
    token = request.args.get("token", "")
    action_url = f"/{action}/{job_id}" + (f"?token={token}" if token else "")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — Confirm {action.title()}</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:460px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <h2 style="color:#13294b;margin:0 0 8px 0">Confirm action</h2>
      <p style="color:#666;font-size:14px">
        Job <b>{job_id}</b> — clicking below will <b>{action}</b> this job.
      </p>
      <form method="POST" action="{action_url}">
        <button type="submit" style="
           display:inline-block;padding:12px 28px;margin:14px 4px 4px 4px;
           background:{col};color:#fff;border:none;border-radius:8px;
           font-size:15px;font-weight:700;cursor:pointer;">{label}</button>
      </form>
      <a href="/portal?job={job_id}"
         style="display:inline-block;margin-top:10px;color:#888;font-size:12px;
                text-decoration:none;">Open in portal instead →</a>
    </div></body></html>"""


def _html_confirmation(action: str, job_id: str, costing) -> str:
    colours = {"approved": "#27ae60", "rejected": "#c0392b", "adjusted": "#2980b9"}
    icons   = {"approved": "✅", "rejected": "✗", "adjusted": "✏️"}
    col = colours.get(action, "#13294b")
    icon = icons.get(action, "")

    cost_block = ""
    if costing and "total_gbp" in costing:
        cost_block = f"""
        <div style="margin:20px 0;padding:16px;background:#f7f8fa;border-radius:8px;text-align:center">
          <div style="font-size:13px;color:#666">Estimated value</div>
          <div style="font-size:32px;font-weight:800;color:#13294b">
            £{costing['total_gbp']:,.2f}
          </div>
          <div style="font-size:13px;color:#888">
            {costing.get('area_m2',0):,.0f} m² @ £{costing.get('rate',0):.2f}/m²
          </div>
          {f'<div style="font-size:12px;color:#e67e22;margin-top:6px">{costing.get("note","")}</div>'
           if costing.get("note") else ""}
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Fortel AI Takeoff — {action.title()}</title></head>
    <body style="font-family:Arial,sans-serif;background:#f0f2f5;display:flex;
                 align-items:center;justify-content:center;min-height:100vh;margin:0">
    <div style="background:#fff;border-radius:12px;padding:40px;max-width:460px;
                box-shadow:0 2px 20px rgba(0,0,0,.1);text-align:center">
      <div style="font-size:48px">{icon}</div>
      <h2 style="color:{col};margin:12px 0 4px 0">{action.title()}</h2>
      <p style="color:#666;font-size:14px">Job <b>{job_id}</b> has been {action}.</p>
      {cost_block}
      <a href="/portal?job={job_id}"
         style="display:inline-block;padding:10px 24px;background:#13294b;color:#fff;
                text-decoration:none;border-radius:6px;font-size:14px;margin-top:8px">
        Open in Portal →
      </a>
    </div></body></html>"""


# ── Quotation endpoints ───────────────────────────────────────────────────────

@app.route("/quotation/<job_id>.<fmt>")
def quotation_download(job_id, fmt):
    """Serve the ONE case quotation (all sibling documents) anchored at this job.

    Previously gated on this job being approved/adjusted, which made a fresh case
    undownloadable and (combined with approved-only sibling aggregation) produced a
    separate workbook per document — Aryan's 17 Jul report. Now: a DRAFT case quotation
    is available as soon as ANY document in the case has a measured area; every
    not-yet-approved quantity is marked provisional inside the quotation itself, and
    unmeasured documents are listed as awaiting assessor trace. Nothing firm is implied
    before approval — the provisional markings carry that state."""
    j, err, code = require_job(job_id)
    if err: return err, code
    # No "nothing measured yet" gate: an office-only case (all documents awaiting assessor
    # trace) still gets its case workbook — every document listed, totals empty. A refused
    # download here is exactly what read as "skipping the office drawings" in the field.

    try:
        from quotation import quotation_text, quotation_html, quotation_json, quotation_xlsx
        q = _quotation_for_job(job_id)
        if fmt == "txt":
            return Response(quotation_text(q),  mimetype="text/plain; charset=utf-8")
        elif fmt == "html":
            return Response(quotation_html(q),  mimetype="text/html; charset=utf-8")
        elif fmt == "json":
            return Response(quotation_json(q),  mimetype="application/json")
        elif fmt == "xlsx":
            filename = f"{_sanitise_filename(str(j.get('project_ref') or job_id))}.xlsx"
            return send_file(
                io.BytesIO(quotation_xlsx(q)),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=filename,
            )
        else:
            return jsonify({"error": f"unknown format {fmt!r}"}), 400
    except QuotationPricingBlocked as e:
        return jsonify({"error": str(e)}), 409
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


@app.route("/marked-pdf/<job_id>.pdf")
def marked_pdf_download(job_id):
    """Export the assessor-approved drawing with permanent, recoverable markup.

    The page vectors and labels are burned into a copy of the original PDF. A versioned JSON
    attachment retains job/page/geometry metadata for a future re-import feature; this endpoint
    deliberately performs no import or job mutation.
    """
    job, err, code = require_job(job_id)
    if err:
        return err, code
    if job.get("decision") not in {"approved", "adjusted"}:
        return jsonify({
            "error": "marked PDF is available after assessor approval or adjustment"
        }), 409
    result = job.get("result") or {}
    pdf_path = result.get("pdf_path") or job.get("pdf_path") or job.get("pdf")
    if pdf_path and not Path(pdf_path).is_absolute():
        pdf_path = str(Path(__file__).parent / pdf_path)
    try:
        from approval_email import snapshot_scale
        from marked_pdf import build_marked_pdf, marked_pdf_filename
        page_index = int(result.get("page") or 0)
        fallback_snapshot_scale = snapshot_scale(pdf_path, page=page_index)
        pdf_bytes, manifest = build_marked_pdf(
            job, pdf_path, snapshot_scale_value=fallback_snapshot_scale)
        response = send_file(
            io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
            download_name=marked_pdf_filename(job),
        )
        response.headers["X-Fortel-Markup-Schema"] = manifest["schema"]
        response.headers["Access-Control-Expose-Headers"] = "X-Fortel-Markup-Schema"
        return response
    except Exception as exc:
        from marked_pdf import MarkedPdfError
        if isinstance(exc, MarkedPdfError):
            return jsonify({"error": str(exc)}), 409
        return jsonify({"error": traceback.format_exc()}), 500


# ── n8n webhook endpoint ──────────────────────────────────────────────────────

@app.route("/webhook/n8n", methods=["POST"])
def n8n_webhook():
    """
    Inbound webhook from n8n — receives a completed takeoff result and creates
    an approval job, then returns the email HTML payload for n8n to send.

    Expected body:
      {"pdf_path": "...", "result": {...}, "polygon_pts": [[x,y],...], "to": "..."}

    n8n flow:
      HTTP Request node (POST /webhook/n8n) → Email node (body = response.html)
    """
    data = request.get_json(silent=True) or {}
    pdf_path    = data.get("pdf_path", "")
    result      = data.get("result", {})
    polygon_pts = data.get("polygon_pts")
    to          = data.get("to", os.getenv("APPROVAL_TO", "inderjit@fortel.co.uk"))

    if not result:
        return jsonify({"error": "result required"}), 400

    # Containment guard: pdf_path comes straight from the request body. Without this, any
    # caller could point it at an arbitrary file readable by the server user and have it
    # rendered/exfiltrated back as a base64 PNG. Only allow paths that resolve inside this
    # server's own drawings/ directory (same pattern already used at /upload).
    if pdf_path:
        drawings_dir = DRAWINGS_DIR.resolve()
        try:
            resolved = Path(pdf_path).resolve()
            resolved.relative_to(drawings_dir)
        except ValueError:
            return jsonify({"error": "pdf_path must resolve inside the server's drawings/ "
                                      "directory"}), 400

    try:
        from approval_email import create_job, render_snapshot, png_to_b64, build_html_email
        if polygon_pts is not None and not result.get("polygon_pts"):
            result = dict(result)
            result["polygon_pts"] = polygon_pts
        job_id = create_job(pdf_path, result)
        # Snapshot (best-effort — PDF may not be on this server's disk)
        b64 = ""
        if pdf_path and Path(pdf_path).exists():
            png = render_snapshot(pdf_path, polygon_pts=polygon_pts)
            b64 = png_to_b64(png)
        html = build_html_email(job_id, result, b64)
        return jsonify({
            "job_id":   job_id,
            "to":       to,
            "subject":  f"Fortel AI Takeoff — Review: {result.get('file', job_id)}",
            "html":     html,
            "portal":   f"{os.getenv('APPROVAL_BASE_URL','http://localhost:5001')}/review/{job_id}",
            "area_m2":  result.get("area_m2"),
            "total_gbp": result.get("costing", {}).get("total_gbp"),
            "flags":    result.get("flags", []),
        })
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Health-check ─────────────────────────────────────────────────────────────

@app.errorhandler(413)
def _upload_too_large(_error):
    """Answer JSON, not Flask's HTML page.

    The portal parses JSON and shows `error`; an HTML 413 left it with an empty object and a
    meaningless toast, so an oversized tender pack looked like "zip upload is broken".
    """
    return jsonify({
        "error": f"upload exceeds the {MAX_UPLOAD_MB} MB limit for this server; "
                 "split the pack or raise MAX_UPLOAD_MB",
        "max_upload_mb": MAX_UPLOAD_MB,
    }), 413


@app.route("/status")
def status():
    """Health-check for deploy tests."""
    jobs = load_jobs()
    return jsonify({"status": "ok", "job_count": len(jobs), "build": BUILD_INFO})


# ── Admin file download ──────────────────────────────────────────────────────

_ALLOWED_DOWNLOADS = {
    "training_log.jsonl": TRAINING_LOG,
    "learned_patterns.json": LEARNED_PATTERNS_FILE,
    "approval_jobs.json": JOBS_FILE,
    "approval_jobs_archive.json": JOBS_ARCHIVE_FILE,
}


@app.route("/admin/download/<filename>")
def admin_download(filename):
    """Download a server-side data file (auth handled by before_request hook)."""
    path = _ALLOWED_DOWNLOADS.get(filename)
    if path is None or not path.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


# ── Upload endpoint ───────────────────────────────────────────────────────────

TAKEOFF_TIMEOUT_S = int(os.getenv("TAKEOFF_TIMEOUT_S", "120"))


def _takeoff_worker_count(raw_value=None, cpu_count=None) -> int:
    """Resolve a deliberately small measurement pool without making startup fragile."""
    if raw_value is None:
        raw_value = os.getenv("TAKEOFF_WORKERS")
    if raw_value not in (None, ""):
        try:
            configured = int(raw_value)
            if configured > 0:
                return configured
        except (TypeError, ValueError):
            pass
        print(f"[takeoff-queue] ignoring invalid TAKEOFF_WORKERS={raw_value!r}; using CPU default")
    detected = cpu_count if cpu_count is not None else os.cpu_count()
    try:
        detected = max(1, int(detected or 1))
    except (TypeError, ValueError):
        detected = 1
    # Railway's takeoff is CPU- and memory-heavy. Two concurrent drawings are enough to use
    # a multi-core service without recreating the 26-way contention that caused the incident.
    return min(2, detected)


TAKEOFF_WORKERS = _takeoff_worker_count()


class _TakeoffDispatcher:
    """Fixed daemon-worker queue for CPU-heavy takeoffs.

    Uploading N drawings creates N queue entries, not N measurement threads.  `_run_takeoff`
    (and therefore its watchdog) is called only after a worker removes an entry from the queue,
    so time spent waiting for CPU can never consume a drawing's measurement budget.
    """

    def __init__(self, max_workers: int, runner=None):
        self.max_workers = max(1, int(max_workers))
        self._runner = runner
        self._queue = queue.Queue()
        self._start_lock = threading.Lock()
        self._started = False

    def _ensure_started(self):
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            for index in range(self.max_workers):
                threading.Thread(
                    target=self._worker,
                    name=f"takeoff-worker-{index + 1}",
                    daemon=True,
                ).start()
            self._started = True

    def _worker(self):
        while True:
            args = self._queue.get()
            try:
                runner = self._runner or _run_takeoff
                runner(*args)
            except Exception as exc:
                # `_run_takeoff` already converts ordinary failures to UNMEASURED. This is a
                # final dispatcher boundary so even a regression before that handler cannot
                # strand a queued job forever.
                _mark_job_unmeasured(
                    args[0],
                    f"PIPELINE DISPATCH ERROR: {type(exc).__name__}: {exc}; route to assessor",
                    extra={"takeoff_phase": "failed", "takeoff_finished_at": now_iso()},
                )
            finally:
                self._queue.task_done()

    def submit(self, job_id: str, pdf_path: str, project_name: str, project_ref: str):
        self._ensure_started()
        self._queue.put((job_id, pdf_path, project_name, project_ref))

    def wait_for_idle(self):
        """Test/maintenance hook; production requests never block on the queue."""
        self._queue.join()


_TAKEOFF_DISPATCHER = _TakeoffDispatcher(TAKEOFF_WORKERS)


def _mark_job_unmeasured(job_id: str, flag: str, extra: dict = None, watchdog_fired: bool = False):
    """Flip a job to UNMEASURED with a flag — used by both the error handler and the
    watchdog so a job NEVER gets stranded on 'processing' forever.

    status stays "error" (the legacy field the portal already renders specially: it still
    fetches the snapshot and lets the assessor trace manually — see assessor_portal.html's
    job.status === 'error' branch) while measurement_state carries the new four-state value
    so the state machine is explicit and machine-checkable.

    watchdog_fired=True marks the job with a "_watchdog_fired" sentinel so that IF the real
    takeoff thread later completes successfully, _run_takeoff can detect the conflict, strip
    the now-stale "PIPELINE TIMEOUT" flag instead of baking it permanently into a job that
    actually succeeded, and log the race instead of silently overwriting.
    """
    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return
        flags = list(job.get("flags") or [])
        flags.append(flag)
        job.update({
            "status":            "error",
            "measurement_state": "UNMEASURED",
            "needs_assessor":    True,
            "area_m2":           job.get("area_m2"),
            "flags":             flags,
        })
        if watchdog_fired:
            job["_watchdog_fired"] = True
        if extra:
            job.update(extra)
        # keep 'result' consistent with the top-level fields so the portal (which reads
        # job.result.flags in some views) sees the same picture
        res = dict(job.get("result") or {})
        res["flags"] = flags
        res.setdefault("measurement_state", "UNMEASURED")
        res.setdefault("area_m2", job.get("area_m2"))
        job["result"] = res
        _ensure_learning_episode(
            job_id, job,
            source="watchdog" if watchdog_fired else "pipeline_error",
            original_available=True,
        )
        jobs[job_id] = job
        save_jobs(jobs)


def _notify_saved_review_job(job_id: str, pdf_path: str, result: dict,
                             project_name: str, project_ref: str) -> dict:
    """Notify against the canonical, already-saved portal job and persist the outcome."""
    if os.getenv("SEND_APPROVAL_EMAILS", "0") != "1":
        return {"job_id": job_id, "sent": False, "status": "disabled", "reason": ""}
    try:
        from approval_email import send_job_approval_email
        outcome = send_job_approval_email(
            job_id, pdf_path, result,
            project_name=project_name, project_ref=project_ref)
    except Exception as exc:
        outcome = {
            "job_id": job_id, "sent": False, "status": "internal_error",
            "reason": f"approval email notifier failed: {type(exc).__name__}: {exc}",
        }

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return outcome
        status = str(outcome.get("status") or "send_failed")
        reason = str(outcome.get("reason") or "")
        job.update({
            "approval_email_status": status,
            "approval_email_attempted_at": now_iso(),
        })
        if outcome.get("sent"):
            job["approval_email_sent_at"] = now_iso()
            job.pop("approval_email_error", None)
        else:
            job["approval_email_error"] = reason or "approval email was not sent"
            flag = f"APPROVAL EMAIL NOT SENT: {job['approval_email_error']}"
            flags = [f for f in (job.get("flags") or [])
                     if not str(f).startswith("APPROVAL EMAIL NOT SENT:")]
            flags.append(flag)
            job["flags"] = flags
            stored_result = dict(job.get("result") or {})
            result_flags = [f for f in (stored_result.get("flags") or [])
                            if not str(f).startswith("APPROVAL EMAIL NOT SENT:")]
            result_flags.append(flag)
            stored_result["flags"] = result_flags
            job["result"] = stored_result
        jobs[job_id] = job
        save_jobs(jobs)
    return outcome


def _mark_takeoff_started(job_id: str) -> bool:
    """Move one queued record into active measurement immediately before its watchdog."""
    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return False
        job.update({
            "status": "processing",
            "takeoff_phase": "measuring",
            "takeoff_started_at": now_iso(),
        })
        jobs[job_id] = job
        save_jobs(jobs)
    return True


def _project_pdf_paths(project_ref: str, current_pdf: str | None = None) -> list[str]:
    """Return readable PDFs belonging to the exact persisted portal project.

    ``find_engineer_spec`` must not infer project membership from a shared upload directory.
    The job store is the authority: only records with the same non-empty project_ref are
    exposed, and the current file remains available for direct/local-style extraction.
    """
    paths = {}

    def include(raw_path):
        if not raw_path:
            return
        try:
            path = Path(raw_path)
            if path.suffix.casefold() != ".pdf" or not path.is_file():
                return
            paths[str(path.resolve())] = str(path)
        except (TypeError, ValueError, OSError):
            return

    include(current_pdf)
    ref = str(project_ref or "").strip()
    if not ref:
        return sorted(paths.values(), key=str.casefold)
    with _jobs_lock:
        project_jobs = list(load_jobs().values())
    for job in project_jobs:
        if str(job.get("project_ref") or "").strip() != ref:
            continue
        include(job.get("pdf_path") or (job.get("result") or {}).get("pdf_path"))
    return sorted(paths.values(), key=str.casefold)


def _run_takeoff(job_id: str, pdf_path: str, project_name: str, project_ref: str):
    """
    Background thread: run takeoff pipeline and update job record when done.

    Hardened per the "never break, never strand" invariant:
      - ANY exception during takeoff -> job becomes UNMEASURED with a
        "PIPELINE ERROR: ... ; route to assessor" flag, never a bare crash/"error" dead-end.
      - A watchdog timer flips the job to UNMEASURED with a timeout flag if actual measurement
        hasn't finished within TAKEOFF_TIMEOUT_S. Queue wait is excluded because this function
        is entered only after a fixed worker takes the job; the worker may keep running (daemon
        thread, no forced kill) but the job record is never left stuck on "processing".
      - watchdog-vs-completion race: threading.Timer.cancel() is a no-op once the timer has
        already fired, so if takeoff() finishes just after the 120s mark the watchdog may have
        already flipped the job to UNMEASURED before this thread gets the lock back. That's
        fine — the completed result below always overwrites it (a late success should win over
        a timeout placeholder) — but the watchdog's "PIPELINE TIMEOUT" flag must not survive
        into the completed job's flags (it would be a permanently-confusing lie on a job that
        in fact succeeded). Detect the "_watchdog_fired" sentinel, strip that one flag, and log
        the race so it's visible in the server log rather than silently swallowed.
    """
    if not _mark_takeoff_started(job_id):
        return
    watchdog = threading.Timer(
        TAKEOFF_TIMEOUT_S, _mark_job_unmeasured, args=(
            job_id,
            f"PIPELINE TIMEOUT: measurement exceeded {TAKEOFF_TIMEOUT_S}s after a worker "
            "started it (queue wait excluded); route to assessor — the worker may still be "
            "running in the background.",
        ),
        kwargs={"watchdog_fired": True, "extra": {
            "takeoff_phase": "timed_out", "takeoff_finished_at": now_iso()}},
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        import takeoff_pipeline
        takeoff_kwargs = {"project_name": project_name, "project_ref": project_ref}
        # A few integrations/tests provide a narrow takeoff-compatible callable. Preserve
        # that interface while the real pipeline receives the explicit isolated rates path.
        import inspect
        _takeoff_params = inspect.signature(takeoff_pipeline.takeoff).parameters
        if "client_rates_path" in _takeoff_params:
            takeoff_kwargs["client_rates_path"] = CLIENT_RATES_FILE
        # The portal already owns this job record. If approval emails are ever enabled
        # (SEND_APPROVAL_EMAILS=1), the pipeline must attach to THIS id — otherwise it creates
        # a duplicate job and emails the assessor a link to the ghost, leaving the real case
        # pending forever.
        if "approval_job_id" in _takeoff_params:
            takeoff_kwargs["approval_job_id"] = job_id
        if "project_files" in _takeoff_params:
            takeoff_kwargs["project_files"] = _project_pdf_paths(project_ref, pdf_path)
        result = takeoff_pipeline.takeoff(pdf_path, **takeoff_kwargs)
        watchdog.cancel()
        with _jobs_lock:
            jobs = load_jobs()
            job = jobs.get(job_id)
            if job is None:
                return  # job vanished (shouldn't happen) — nothing to update
            # Preserve any pre-takeoff flags already on the job (e.g. zip/eml disambiguation
            # notes recorded at upload time) rather than letting the pipeline result overwrite them.
            pre_flags = list(job.get("flags") or [])
            if job.get("_watchdog_fired"):
                # The watchdog already fired and flipped this job to UNMEASURED before we got
                # here. We're overwriting that with a real completed result (the right call —
                # late success beats a timeout placeholder) but strip the now-stale
                # "PIPELINE TIMEOUT" flag it appended so it doesn't linger on a job that in
                # fact succeeded, and log the race so it shows up in the server log.
                pre_flags = [f for f in pre_flags if not f.startswith("PIPELINE TIMEOUT")]
                print(f"[watchdog-race] job {job_id}: pipeline finished AFTER the "
                      f"{TAKEOFF_TIMEOUT_S}s watchdog already marked it UNMEASURED; "
                      f"overwriting with the completed result (state={result.get('measurement_state')}).")
            job.pop("_watchdog_fired", None)
            result_flags = [f for f in (result.get("flags") or [])
                            if not str(f).startswith("APPROVAL EMAIL DEFERRED:")]
            result = dict(result)
            result["flags"] = result_flags
            job.update({
                "project_name":     result.get("project_name", project_name),
                "project_ref":      result.get("project_ref",  project_ref),
                "type":             result.get("type"),
                "method":           result.get("method"),
                "confidence":       result.get("confidence"),
                "source_discipline": result.get("source_discipline"),
                "area_m2":          result.get("area_m2"),
                "measurement_state": result.get("measurement_state"),
                "needs_assessor":   result.get("needs_assessor", True),
                "scale_verified":   result.get("scale_verified"),
                "scale_confirmed":  False,
                "scale_src":        result.get("scale_src"),
                "scale_sources":    result.get("scale_sources"),
                "costing":          result.get("costing"),
                "flags":            pre_flags + result_flags,
                "polygon_pts":      result.get("polygon_pts"),
                "candidate_polygons": result.get("candidate_polygons", []),
                "channel_proposals": result.get("channel_proposals", []),
                "transition_candidates": result.get("transition_candidates", []),
                "exclusions":        result.get("exclusions", []),
                "exclusion_prompts": result.get("exclusion_prompts", []),
                "exclusion_review_required": bool(
                    result.get("exclusion_review_required", False)),
                "unit_group_review_required": bool(
                    result.get("unit_group_review_required", False)),
                "boundary_precision_risk": result.get("boundary_precision_risk"),
                "channel_proposal_decisions": {},
                "transition_candidate_decisions": {},
                "accepted_transition_quantities": [],
                "yard_regions":      result.get("yard_regions", []),
                "yard_region_decisions": {},
                "yard_region_review_required": bool(
                    result.get("yard_region_review_required", False)),
                "extent_corroborated": result.get("extent_corroborated"),
                "extent_corroboration_reason": result.get(
                    "extent_corroboration_reason"),
                "perimeter_lm":     result.get("perimeter_lm"),
                # Mirror zone-aware marked-PDF evidence at job level for the portal while
                # retaining the canonical nested pipeline result for backward compatibility.
                "zones":            result.get("zones", []),
                "segmentation_components": result.get("segmentation_components", {}),
                "markup_annotations": result.get("markup_annotations", []),
                "brief_specs":      result.get("brief_specs", {}),
                "zone_classification_required": bool(
                    result.get("zone_classification_required", False)),
                "zone_reference_mismatch": bool(
                    result.get("zone_reference_mismatch", False)),
                "zone_allocation_stale": bool(result.get("zone_allocation_stale", False)),
                "result":           result,
                "status":           "pending",
                "takeoff_phase":    "completed",
                "takeoff_finished_at": now_iso(),
            })
            _ensure_learning_episode(
                job_id, job, source="pipeline", original_available=True, pdf_path=pdf_path,
            )
            jobs[job_id] = job
            save_jobs(jobs)
        if result.get("measurement_state") != "REJECTED":
            _notify_saved_review_job(
                job_id, pdf_path, result,
                result.get("project_name", project_name),
                result.get("project_ref", project_ref),
            )
    except Exception as e:
        watchdog.cancel()
        _mark_job_unmeasured(
            job_id,
            f"PIPELINE ERROR: {e}; route to assessor",
            extra={"error": traceback.format_exc(), "takeoff_phase": "failed",
                   "takeoff_finished_at": now_iso()},
        )


def _sanitise_filename(name: str) -> str:
    """Strip dangerous characters; keep alphanumeric, dash, underscore, dot."""
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w.\-]", "", name)   # \w = [a-zA-Z0-9_]
    return name[:80]


MAX_EXTRACT_BYTES  = 200 * 1024 * 1024   # safety cap on total bytes extracted from a zip
MAX_EXTRACT_FILES  = 200                 # safety cap on member count
CAD_EXTENSIONS     = (".dwg", ".dxf")


def _rejected_job_record(project_name, project_ref, client_name, filename, reason) -> tuple[str, dict]:
    job_id = str(uuid.uuid4())
    job = {
        "id":               job_id,
        "pdf_path":         None,
        "project_name":     project_name,
        "project_ref":      project_ref,
        "client_name":      client_name,
        "type":             None,
        "method":           None,
        "confidence":       None,
        "source_discipline": None,
        "area_m2":          None,
        "measurement_state": "REJECTED",
        "needs_assessor":   False,
        "scale_verified":   None,
        "scale_src":        None,
        "scale_sources":    None,
        "costing":          None,
        "flags":            [f"REJECTED: {reason}"],
        "polygon_pts":      None,
        "status":           "rejected",
        "decision":         "rejected",
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "decided_at":       now_iso(),
        "note":             reason,
        "adjusted":         None,
        "result":           {"file": filename, "measurement_state": "REJECTED",
                             "flags": [f"REJECTED: {reason}"]},
    }
    _ensure_learning_episode(
        job_id, job, source="pipeline_rejection", original_available=True,
    )
    job["learning_episode"]["terminal"] = {
        "event": "pipeline_rejected", "at": now_iso(),
        "snapshot": copy.deepcopy(job["learning_episode"]["initial"]["snapshot"]),
    }
    return job_id, job


def _create_rejected_job(project_name, project_ref, client_name, filename, reason) -> str:
    """Create a REJECTED job record — visible in the portal job list with a human-readable
    reason (never a bare HTTP 400 that vanishes)."""
    job_id, job = _rejected_job_record(
        project_name, project_ref, client_name, filename, reason)
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)
    return job_id


def _processing_job_record(project_name, project_ref, client_name, pdf_path, flags) -> tuple[str, dict]:
    from learning_capture import document_sha256
    job_id = str(uuid.uuid4())
    return job_id, {
        "id":               job_id,
        "pdf_path":         str(pdf_path),
        "document_sha256":  document_sha256(pdf_path),
        "project_name":     project_name,
        "project_ref":      project_ref,
        "client_name":      client_name,
        "type":             None,
        "method":           None,
        "confidence":       None,
        "source_discipline": None,
        "area_m2":          None,
        "measurement_state": None,
        "needs_assessor":   None,
        "scale_verified":   None,
        "scale_confirmed":  False,
        "scale_src":        None,
        "scale_sources":    None,
        "costing":          None,
        "flags":            list(flags),
        "polygon_pts":      None,
        "status":           "processing",
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "takeoff_phase":    "queued",
        "takeoff_queued_at": now_iso(),
        "decision":         None,
        "adjusted":         None,
        "result":           {"file": Path(pdf_path).name},
    }


def _unique_prefixed_path(dest_dir: Path, prefix: str, filename: str) -> Path:
    safe_name = _sanitise_filename(filename) or f"upload_{uuid.uuid4().hex[:8]}"
    safe_prefix = _sanitise_filename(prefix) or "project"
    target = dest_dir / f"{safe_prefix}_{safe_name}"
    if target.exists():
        target = dest_dir / f"{safe_prefix}_{Path(safe_name).stem}_{uuid.uuid4().hex[:8]}{Path(safe_name).suffix}"
    return target


def _safe_extract_zip(zip_path: Path, dest_dir: Path, prefix: str = "") -> tuple[list, list]:
    """Extract PDFs from a zip archive, guarding against zip-slip and oversize archives.
    Returns (list of extracted PDF Paths, flags).

    `prefix` (typically the sanitised project_ref) is prepended to every extracted filename so
    two uploads whose archives happen to contain a same-named member (e.g. both ship a
    "Yard_Area_Proposed_Site_Plan.pdf") never collide/overwrite each other on disk — mirrors the
    `{project_ref}_{filename}` convention already used for direct .pdf uploads below."""
    flags = []
    pdfs = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_EXTRACT_FILES:
                flags.append(f"zip has {len(infos)} entries — only first {MAX_EXTRACT_FILES} considered")
                infos = infos[:MAX_EXTRACT_FILES]
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if not info.filename.lower().endswith(".pdf"):
                    continue
                total += info.file_size
                if total > MAX_EXTRACT_BYTES:
                    flags.append("zip extraction stopped — size cap exceeded")
                    break
                # zip-slip guard: resolved member path must stay inside dest_dir
                member_name = _sanitise_filename(Path(info.filename).name)
                if not member_name:
                    continue
                target = _unique_prefixed_path(dest_dir, prefix, member_name).resolve()
                if not str(target).startswith(str(dest_dir.resolve())):
                    flags.append(f"skipped unsafe zip entry: {info.filename!r}")
                    continue
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                pdfs.append(target)
    except zipfile.BadZipFile as e:
        flags.append(f"corrupt zip archive: {e}")
    return pdfs, flags


def _extract_eml_pdfs(eml_path: Path, dest_dir: Path, prefix: str = "") -> tuple[list, list]:
    """Parse a .eml with the stdlib email lib, save any PDF attachments. Returns (paths, flags).

    `prefix` is prepended to every extracted attachment filename for the same collision-avoidance
    reason as _safe_extract_zip above (two enquiry emails can easily carry an attachment named
    "Proposed_Site_Plan.pdf")."""
    flags = []
    pdfs = []
    pfx = f"{prefix}_" if prefix else ""
    try:
        with open(eml_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        for part in msg.iter_attachments():
            fname = part.get_filename() or ""
            if fname.lower().endswith(".pdf"):
                safe = _sanitise_filename(fname) or f"attachment_{uuid.uuid4().hex[:8]}.pdf"
                target = dest_dir / f"{pfx}{safe}"
                payload = part.get_payload(decode=True)
                if payload:
                    target.write_bytes(payload)
                    pdfs.append(target)
        if not pdfs:
            flags.append("no PDF attachments found in .eml")
    except Exception as e:
        flags.append(f"failed to parse .eml: {e}")
    return pdfs, flags


def _rank_pdfs_by_priority(pdf_paths: list) -> list:
    """Rank candidate PDFs by router.drawing_priority on filename (best first)."""
    from router import drawing_priority
    return sorted(pdf_paths, key=lambda p: drawing_priority(Path(p).name), reverse=True)


def _open_pdf_safely(path: Path):
    """Try to open a PDF and confirm it's readable. Returns (fitz.Document|None, reason|None)."""
    try:
        import fitz
        if path.stat().st_size == 0:
            return None, "zero-byte file"
        doc = fitz.open(str(path))
        if doc.needs_pass:
            return None, "encrypted/password-protected PDF"
        if doc.page_count < 1:
            return None, "PDF has no pages"
        _ = doc[0].get_text()   # force a real read — catches some corrupt-stream cases
        return doc, None
    except Exception as e:
        return None, f"corrupt or unreadable PDF ({e})"


@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept a new drawing/enquiry from the assessor portal without CLI intervention.

    multipart/form-data fields:
      pdf          – one or more uploaded files (required; repeat the multipart field). Accepts:
                       .pdf            -> takeoff runs directly
                       .zip            -> every contained PDF gets its own project job, ordered
                                          by router.drawing_priority for deterministic display
                       .eml            -> PDF attachments extracted, same ranking
                       .png/.jpg/.jpeg -> wrapped into a single-page PDF, routed as raster/UNMEASURED
                       .dwg/.dxf/other -> REJECTED job, "CAD/unsupported format — please export PDF"
                     Encrypted/corrupt/zero-byte PDFs (at any stage above) -> REJECTED job with
                     the specific reason instead of a bare HTTP 400.
      project_name – human-readable project name (required)
      project_ref  – Fortel reference / sequential number (required)
      existing_project_job_id – optional anchor job; server reuses its project identity so
                                 later drawings cannot drift into a look-alike project

    Returns 202 {"job_id": "...", "status": "processing"} for a takeoff-bound job, or
    201 {"job_id": "...", "status": "rejected"} for a REJECTED job — always 2xx with a job
    record the portal can show, never a bare 400 that vanishes. Multi-job responses also include
    job_ids and jobs while retaining the legacy job_id/status fields.
    """
    # ── Validate required form fields
    project_name = (request.form.get("project_name") or "").strip()
    project_ref  = (request.form.get("project_ref")  or "").strip()
    client_name  = (request.form.get("client_name")  or "").strip()
    existing_project_job_id = (request.form.get("existing_project_job_id") or "").strip()
    if existing_project_job_id:
        existing_project = get_job(existing_project_job_id)
        if not existing_project:
            return jsonify({
                "error": f"existing project anchor {existing_project_job_id!r} not found"
            }), 404
        existing_ref = str(existing_project.get("project_ref") or "").strip()
        if not existing_ref:
            return jsonify({
                "error": "existing project anchor has no project_ref; add one before upload"
            }), 409
        # The persisted anchor is authoritative. Do not trust editable form text to recreate
        # the same-looking project under a different ref/name/client.
        project_ref = existing_ref
        project_name = str(existing_project.get("project_name") or project_name).strip()
        client_name = str(existing_project.get("client_name") or client_name).strip()
    # Optional enquiry identification from the n8n workflow (Aryan, 16 Jul: "the request now
    # include the subject and body information for better identification") — stored on every
    # job in the batch so a failure/review is attributable to the right enquiry email.
    email_subject = (request.form.get("email_subject") or request.form.get("subject") or "").strip()[:300]
    email_body    = (request.form.get("email_body") or request.form.get("body") or "").strip()[:2000]
    up_files     = [f for f in request.files.getlist("pdf") if f and f.filename]

    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    if not project_ref:
        return jsonify({"error": "project_ref is required"}), 400
    if not up_files:
        return jsonify({"error": "file is required"}), 400

    drawings_dir = DRAWINGS_DIR
    drawings_dir.mkdir(parents=True, exist_ok=True)
    upload_items = []

    for up_file in up_files:
        original_filename = up_file.filename or "upload"
        ext = Path(original_filename).suffix.lower()
        stage_path = _unique_prefixed_path(drawings_dir, project_ref, original_filename)
        try:
            stage_path.resolve().relative_to(drawings_dir.resolve())
        except ValueError:
            return jsonify({"error": "invalid filename (path traversal detected)"}), 400
        up_file.save(str(stage_path))

        if ext == ".pdf":
            upload_items.append({"path": stage_path, "filename": original_filename, "flags": []})
        elif ext == ".zip":
            pdfs, flags = _safe_extract_zip(stage_path, drawings_dir, prefix=project_ref)
            stage_path.unlink(missing_ok=True)
            ranked = _rank_pdfs_by_priority(pdfs)
            if not ranked:
                upload_items.append({"filename": original_filename,
                                     "reason": "zip archive contained no extractable PDFs"})
            else:
                zip_flags = list(flags)
                zip_flags.append(f"zip contained {len(ranked)} PDFs; every PDF queued as a "
                                 "separate drawing under this project")
                for pdf_path in ranked:
                    upload_items.append({"path": pdf_path, "filename": pdf_path.name,
                                         "flags": zip_flags})
        elif ext == ".eml":
            pdfs, flags = _extract_eml_pdfs(stage_path, drawings_dir, prefix=project_ref)
            stage_path.unlink(missing_ok=True)
            ranked = _rank_pdfs_by_priority(pdfs)
            if not ranked:
                upload_items.append({"filename": original_filename,
                                     "reason": "no PDF attachments found in .eml"})
            else:
                pdf_path = ranked[0]
                eml_flags = list(flags)
                if len(ranked) > 1:
                    others = ", ".join(p.name for p in ranked[1:6])
                    eml_flags.append(f".eml contained {len(ranked)} PDF attachments; measured "
                                     f"'{pdf_path.name}' (highest drawing_priority); others: {others}")
                upload_items.append({"path": pdf_path, "filename": pdf_path.name,
                                     "flags": eml_flags})
        elif ext in (".png", ".jpg", ".jpeg"):
            try:
                import fitz
                img_doc = fitz.open(str(stage_path))
                pdf_doc = fitz.open()
                rect = img_doc[0].rect
                page = pdf_doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(rect, filename=str(stage_path))
                pdf_path = _unique_prefixed_path(
                    drawings_dir, project_ref, f"{Path(original_filename).stem}.pdf")
                pdf_doc.save(str(pdf_path))
                pdf_doc.close()
                img_doc.close()
                stage_path.unlink(missing_ok=True)
                upload_items.append({
                    "path": pdf_path,
                    "filename": original_filename,
                    "flags": [f"image ({ext}) wrapped into a single-page PDF for takeoff — "
                              "raster source, routes to UNMEASURED/mandatory assessor trace"],
                })
            except Exception as e:
                stage_path.unlink(missing_ok=True)
                upload_items.append({"filename": original_filename,
                                     "reason": f"could not wrap image into PDF: {e}"})
        elif ext in CAD_EXTENSIONS:
            stage_path.unlink(missing_ok=True)
            upload_items.append({"filename": original_filename,
                                 "reason": "CAD/unsupported format — please export PDF"})
        else:
            stage_path.unlink(missing_ok=True)
            upload_items.append({
                "filename": original_filename,
                "reason": f"unsupported file type '{ext or '(none)'}' — please upload PDF, "
                          "ZIP, EML, PNG or JPG",
            })

    records = []
    workers = []
    response_jobs = []
    for item in upload_items:
        pdf_path = item.get("path")
        reason = item.get("reason")
        if pdf_path and not reason:
            doc, reason = _open_pdf_safely(pdf_path)
            if doc is not None:
                doc.close()
        if reason:
            job_id, job = _rejected_job_record(
                project_name, project_ref, client_name, item["filename"], reason)
            status = "rejected"
        else:
            job_id, job = _processing_job_record(
                project_name, project_ref, client_name, pdf_path, item.get("flags", []))
            status = "processing"
            workers.append((job_id, str(pdf_path)))
        if email_subject:
            job["email_subject"] = email_subject
        if email_body:
            job["email_body"] = email_body
        records.append((job_id, job))
        response_jobs.append({"job_id": job_id, "status": status, "filename": item["filename"]})

    with _jobs_lock:
        jobs = load_jobs()
        for job_id, job in records:
            jobs[job_id] = job
        save_jobs(jobs)

    for job_id, pdf_path in workers:
        _TAKEOFF_DISPATCHER.submit(job_id, pdf_path, project_name, project_ref)

    primary = next((j for j in response_jobs if j["status"] == "processing"), response_jobs[0])
    payload = {"job_id": primary["job_id"], "status": primary["status"]}
    if len(response_jobs) > 1:
        payload.update({
            "project_ref": project_ref,
            "job_ids": [j["job_id"] for j in response_jobs],
            "jobs": response_jobs,
        })
    if existing_project_job_id:
        payload.update({
            "project_ref": project_ref,
            "added_to_project": True,
            "existing_project_job_id": existing_project_job_id,
        })
    return jsonify(payload), (202 if workers else 201)


# ── Startup sweep ─────────────────────────────────────────────────────────────

def _sweep_stranded_processing_jobs():
    """Route queued/measuring jobs orphaned by a prior process to assessor review.

    Both fixed workers and their in-memory queue are daemon/process-local, so neither can
    survive a restart. No processing record can legitimately still be active at boot.
    """
    with _jobs_lock:
        jobs = load_jobs()
        changed = False
        for job_id, job in jobs.items():
            if job.get("status") == "processing":
                flags = list(job.get("flags") or [])
                old_phase = job.get("takeoff_phase") or "measuring"
                flags.append(
                    f"PIPELINE INTERRUPTED: server restarted while takeoff was {old_phase}; "
                    "route to assessor"
                )
                job.update({
                    "status":            "error",
                    "measurement_state": "UNMEASURED",
                    "needs_assessor":    True,
                    "flags":             flags,
                    "takeoff_phase":     "interrupted",
                    "takeoff_finished_at": now_iso(),
                })
                res = dict(job.get("result") or {})
                res["flags"] = flags
                res.setdefault("measurement_state", "UNMEASURED")
                job["result"] = res
                changed = True
                print(f"[startup-sweep] job {job_id} was stranded on 'processing' at a prior "
                      "restart — marked UNMEASURED, routed to assessor.")
        if changed:
            save_jobs(jobs)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Railway injects PORT and requires binding to all interfaces. Bare-metal/local runs keep
    # the safer loopback default and still refuse a wide bind without portal authentication.
    is_railway = bool(os.getenv("RAILWAY_PROJECT_ID"))
    _raw_port = (os.getenv("PORT") or "").strip()
    if _raw_port:
        port = int(_raw_port)
        host = "0.0.0.0"
    else:
        port = int((os.getenv("APPROVAL_PORT") or "5001").strip())
        host = os.getenv("APPROVAL_HOST", "127.0.0.1")
        if not is_railway and host not in ("127.0.0.1", "localhost") and not APPROVAL_TOKEN:
            print(f"REFUSING to bind {host} without APPROVAL_TOKEN set — anyone on the network "
                  "could approve/reject/adjust jobs. Set APPROVAL_TOKEN, or leave APPROVAL_HOST "
                  "at the 127.0.0.1 default for local-only use. Falling back to 127.0.0.1.")
            host = "127.0.0.1"

    _sweep_stranded_processing_jobs()

    # Startup banner goes to stdout, which run.sh redirects straight into logs/portal.log
    # (and launchd's own copy). Printing the raw token there means it sits in a plaintext
    # log file indefinitely — mask everything but the first 4 chars so the log is still
    # useful for confirming *which* token is loaded (e.g. after a rotation) without being a
    # second place the live secret is stored. Get the real value from `.env`/env, not the log.
    def _mask_token(t: str) -> str:
        if not t:
            return ""
        return f"{t[:4]}…" if len(t) > 4 else "…"

    print("Fortel Approval Server — config:")
    print(f"  host:port     = {host}:{os.getenv('APPROVAL_PORT', 5001)}")
    print(f"  jobs file     = {JOBS_FILE}")
    print(f"  client rates  = {CLIENT_RATES_FILE}")
    print(f"  jobs archive  = {JOBS_ARCHIVE_FILE}")
    print(f"  backups dir   = {BACKUP_DIR}")
    print(f"  drawings dir  = {DRAWINGS_DIR}")
    print(f"  quotations dir= {QUOTATIONS_DIR}")
    print(f"  base url      = {os.getenv('APPROVAL_BASE_URL', 'http://localhost:5001')}")
    print(f"  token set     = {'yes (' + _mask_token(APPROVAL_TOKEN) + ')' if APPROVAL_TOKEN else 'no'}")
    print(f"  cors origin   = {_CORS_ORIGIN or '(none — same-origin only)'}")
    print(f"  portal        = http://{host}:{port}/portal"
          + (f"?token={_mask_token(APPROVAL_TOKEN)} (masked — see .env for the real value)"
             if APPROVAL_TOKEN else ""))

    app.run(host=host, port=port, debug=False, threaded=True)
