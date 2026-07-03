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

All state lives in approval_jobs.json (created by approval_email.py).
Training data (decisions + corrections) is appended to training_log.jsonl.

Run:
  pip install flask pillow pymupdf shapely --break-system-packages
  python3 approval_server.py          # default port 5001

Environment:
  APPROVAL_PORT   default 5001
  APPROVAL_HOST   default 0.0.0.0 (set to 127.0.0.1 for local-only)
"""
import os, json, io, datetime, traceback, uuid, re, threading, zipfile, email, shutil
from email import policy
from pathlib import Path
from flask import Flask, request, jsonify, send_file, redirect, Response

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

JOBS_FILE    = Path(__file__).parent / "approval_jobs.json"
TRAINING_LOG = Path(__file__).parent / "training_log.jsonl"
PORTAL_HTML  = Path(__file__).parent / "assessor_portal.html"

_jobs_lock = threading.Lock()


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
            return json.loads(JOBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

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
    tmp = JOBS_FILE.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, JOBS_FILE)

def log_training(entry: dict):
    """Append a decision to the training log for model improvement."""
    TRAINING_LOG.parent.mkdir(exist_ok=True)
    with TRAINING_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()

def get_job(job_id: str) -> dict | None:
    return load_jobs().get(job_id)

def require_job(job_id: str):
    j = get_job(job_id)
    if not j:
        return None, jsonify({"error": f"job {job_id!r} not found"}), 404
    return j, None, None


# ── CORS for the portal ───────────────────────────────────────────────────────
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:p>", methods=["OPTIONS"])
def options(p=""):
    return Response(status=204)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/portal")

@app.route("/portal")
def portal():
    if PORTAL_HTML.exists():
        return PORTAL_HTML.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "Portal HTML not found — make sure assessor_portal.html is in the same folder.", 404

@app.route("/jobs")
def list_jobs():
    return jsonify(load_jobs())

@app.route("/job/<job_id>")
def single_job(job_id):
    j, err, code = require_job(job_id)
    if err: return err, code
    return jsonify(j)


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
        png = render_snapshot(pdf, page=page, polygon_pts=poly)
        resp = send_file(io.BytesIO(png), mimetype="image/png")
        # Expose the ACTUAL render scale (snapshot px per PDF point) so the portal can
        # convert scale_k (m/pt) -> metres per canvas pixel:  mpp = scale_k / snap_scale.
        # Without this the portal assumed 0.5 and mis-scaled area on wide (A1/A0) sheets.
        resp.headers["X-Snapshot-Scale"] = f"{snapshot_scale(pdf, page=page):.6f}"
        resp.headers["Access-Control-Expose-Headers"] = "X-Snapshot-Scale"
        return resp
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Decision endpoints ────────────────────────────────────────────────────────

def _approve_block_reason(job: dict) -> str | None:
    """
    Server-side hard-block mirroring the escalation-guard mechanism (fb5b92b, >£200k
    assumed-spec jobs): MEASURED_UNVERIFIED and UNMEASURED jobs cannot be approved until an
    assessor has confirmed scale+extent (via /adjust, which sets scale_confirmed=True).
    Returns a reason string to block, or None to allow.
    """
    state = job.get("measurement_state") or (job.get("result") or {}).get("measurement_state")
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
    Can be triggered by clicking the email button (GET) or from the portal (POST).

    Hard-blocked (409) for MEASURED_UNVERIFIED / UNMEASURED / REJECTED jobs unless the
    assessor has already confirmed scale+extent (job['scale_confirmed'] set by /adjust) —
    mirrors the >£200k assumed-spec escalation guard (commit fb5b92b).
    """
    data = {}
    if request.method == "POST" and request.is_json:
        data = request.get_json(silent=True) or {}

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
        jobs[job_id].update({
            "status":     "approved",
            "decision":   "approved",
            "decided_at": now_iso(),
            "note":       data.get("note", ""),
        })
        save_jobs(jobs)

    # Trigger costing with the AI's area (use snapshot captured inside lock).
    # If the assessor already used spec-override, preserve their costing rather than
    # recomputing with defaults — recomputing would silently undo the correction.
    res = job.get("result", {})
    if job.get("costing") and job.get("spec_override"):
        costing_result = job["costing"]
    else:
        costing_result = _run_costing(res.get("area_m2"), res)
    # Auto-generate and save quotation
    quotation_paths = _save_quotation(job_id, res, costing_result)
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id]["costing"] = costing_result
        jobs[job_id]["quotation_paths"] = quotation_paths
        save_jobs(jobs)

    log_training({
        "event":      "approve",
        "job_id":     job_id,
        "file":       res.get("file"),
        "area_m2":    res.get("area_m2"),
        "flags":      res.get("flags", []),
        "timestamp":  now_iso(),
    })

    if request.method == "GET":
        return _html_confirmation("approved", job_id, costing_result)
    return jsonify({"status": "approved", "job_id": job_id, "costing": costing_result})


@app.route("/reject/<job_id>", methods=["GET", "POST"])
def reject(job_id):
    """Reject: mark the measurement as wrong; do not proceed to costing."""
    data = {}
    if request.method == "POST" and request.is_json:
        data = request.get_json(silent=True) or {}

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        jobs[job_id].update({
            "status":     "rejected",
            "decision":   "rejected",
            "decided_at": now_iso(),
            "note":       data.get("note", "rejected via portal"),
        })
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":     "reject",
        "job_id":    job_id,
        "file":      res.get("file"),
        "flags":     res.get("flags", []),
        "timestamp": now_iso(),
    })

    if request.method == "GET":
        return _html_confirmation("rejected", job_id, None)
    return jsonify({"status": "rejected", "job_id": job_id})


@app.route("/adjust/<job_id>", methods=["GET", "POST"])
def adjust(job_id):
    """
    Adjust: assessor provides corrected polygon and/or scale (or a bare assessed area for
    UNMEASURED jobs where there's no AI polygon to correct — e.g. raster/scanned drawings).
    Re-runs geometry measurement with the assessor's inputs when a polygon+scale is given,
    then costs. Any assessor-supplied area (polygon-derived OR a direct assessed_area_m2)
    sets scale_confirmed=True, which is what unblocks /approve for MEASURED_UNVERIFIED and
    UNMEASURED jobs (see _approve_block_reason).
    """
    if request.method == "GET":
        # Quick check before redirect (no lock needed — read-only)
        if not load_jobs().get(job_id):
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        return redirect(f"/portal?job={job_id}")

    data = request.get_json(silent=True) or {}
    vertices   = data.get("vertices", [])    # [[x,y], ...]
    scale_k    = data.get("scale_k")         # m/px
    area_m2    = data.get("assessed_area_m2")
    note       = data.get("note", "")

    # If assessor traced a polygon + scale, re-measure (heavy I/O — outside lock)
    if vertices and scale_k and len(vertices) >= 3:
        try:
            from geometry import measure_regions
            area_m2, gflags = measure_regions([vertices], scale_k)
        except Exception as e:
            area_m2, gflags = None, [f"geometry error: {e}"]
    else:
        gflags = []

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
        costing_result = _run_costing(area_m2, job.get("result", {})) if area_m2 else None
        jobs[job_id].update({
            "status":            "adjusted",
            "decision":          "adjusted",
            "decided_at":        now_iso(),
            "scale_confirmed":   confirmed or job.get("scale_confirmed", False),
            "measurement_state": ("MEASURED_VERIFIED" if confirmed
                                  else "MEASURED_UNVERIFIED" if (area_m2 and plaus_flags)
                                  else job.get("measurement_state")),
            "adjusted": {
                "vertices": vertices,
                "scale_k":  scale_k,
                "area_m2":  area_m2,
                "flags":    gflags,
                "note":     note,
            },
            "costing": costing_result,
        })
        save_jobs(jobs)

    res = job.get("result", {})
    log_training({
        "event":          "adjust",
        "job_id":         job_id,
        "file":           res.get("file"),
        "ai_area_m2":     res.get("area_m2"),
        "assessed_area":  area_m2,
        "ai_polygon":     res.get("polygon_pts"),
        "assessed_polygon": vertices,
        "scale_k":        scale_k,
        "flags":          res.get("flags", []),
        "timestamp":      now_iso(),
    })

    return jsonify({
        "status":   "adjusted",
        "job_id":   job_id,
        "area_m2":  area_m2,
        "costing":  costing_result,
        "flags":    gflags,
    })


@app.route("/spec-override/<job_id>", methods=["POST"])
def spec_override(job_id):
    """Assessor overrides the assumed spec (depth, mix, mesh, layers) and re-prices."""
    data = request.get_json(silent=True) or {}
    override = {}
    try:
        if "depth_mm" in data: override["depth_mm"] = int(data["depth_mm"])
        if "conc_mix"  in data: override["conc_mix"]  = str(data["conc_mix"])
        if "mesh"      in data: override["mesh"]       = str(data["mesh"])
        if "layers"    in data: override["layers"]     = int(data["layers"])
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"invalid spec field: {e}"}), 400

    with _jobs_lock:
        jobs = load_jobs()
        job  = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409

        res     = job.get("result") or {}
        adj     = job.get("adjusted") or {}
        area_m2 = adj.get("area_m2") or res.get("area_m2")
        if not area_m2:
            return jsonify({"error": "no area_m2 on job — run takeoff first"}), 409

        try:
            from defaults import spec_with_defaults, flag_assumed
            from costing  import rate_buildup
            spec, _ = spec_with_defaults(override)
            rate, parts = rate_buildup(**{k: spec[k] for k in [
                "depth_mm","conc_rate","conc_wastage","mesh","layers",
                "steel_rate_t","steel_wastage","lap_acc","dpm","curing",
                "labour","trim","margin"]})
            costing = {
                "area_m2":   area_m2,
                "rate":      rate,
                "total_gbp": round(area_m2 * rate, 2),
                "spec":      spec,
                "assumed":   False,
                "note":      "Spec overridden by assessor",
                "flags":     [],
                "breakdown": parts,
            }
        except Exception as e:
            return jsonify({"error": f"costing failed: {e}"}), 500

        jobs[job_id]["costing"]       = costing
        jobs[job_id]["spec_override"] = override
        save_jobs(jobs)

    return jsonify({"status": "ok", "job_id": job_id, "costing": costing})


# ── Costing helper ────────────────────────────────────────────────────────────

def _save_quotation(job_id: str, result: dict, costing: dict | None) -> dict:
    """Generate and save quotation files for this job. Returns paths dict."""
    try:
        from quotation import generate_quotation, save_quotation
        if costing:
            result = dict(result)
            result["costing"] = costing
        # Use the human-entered project name / ref stored at upload time rather
        # than the raw PDF filename.  Falls back gracefully for legacy job records
        # that predate the upload form (project_name not stored).
        job     = load_jobs().get(job_id, {})
        project = job.get("project_name") or result.get("file", "")
        ref     = job.get("project_ref") or None
        client  = job.get("client_name") or ""
        q = generate_quotation(result, project=project, client=client, ref=ref)
        out_dir = Path(__file__).parent / "quotations"
        return save_quotation(q, out_dir=str(out_dir))
    except Exception as e:
        return {"error": str(e)}


def _run_costing(area_m2, result: dict) -> dict | None:
    """Run costing with defaults (or any spec stored in result)."""
    if not area_m2 or area_m2 <= 0:
        return None
    try:
        from defaults import spec_with_defaults, assumption_note, flag_assumed
        from costing  import rate_buildup

        engineer_spec = result.get("engineer_spec")  # None if architect-only
        spec, assumed = spec_with_defaults(engineer_spec)
        rate, parts   = rate_buildup(**{k: spec[k] for k in [
            "depth_mm","conc_rate","conc_wastage","mesh","layers",
            "steel_rate_t","steel_wastage","lap_acc","dpm","curing",
            "labour","trim","margin"]})
        total = round(area_m2 * rate, 2)
        return {
            "area_m2":   area_m2,
            "rate":      rate,
            "total_gbp": total,
            "spec":      spec,
            "assumed":   assumed,
            "note":      assumption_note(spec) if assumed else "",
            "flags":     flag_assumed(spec, assumed),
            "breakdown": parts,
        }
    except Exception as e:
        return {"error": str(e)}


# ── HTML email-click confirmation page ───────────────────────────────────────

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
            {costing.get('area_m2',0):,.0f} m² @ £{costing.get('rate',0)}/m²
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
    """Serve the quotation for an approved/adjusted job in txt, html, or json."""
    j, err, code = require_job(job_id)
    if err: return err, code
    if j.get("decision") not in ("approved", "adjusted"):
        return jsonify({"error": "quotation only available after approval or adjustment"}), 400

    try:
        from quotation import generate_quotation, quotation_text, quotation_html, quotation_json
        result  = j.get("result", {})
        # Use adjusted area if assessor corrected it
        adj = j.get("adjusted", {})
        if adj and adj.get("area_m2"):
            result = dict(result)
            if result.get("costing"):
                result["costing"] = dict(result["costing"])
                result["costing"]["area_m2"]   = adj["area_m2"]
                result["costing"]["total_gbp"] = round(
                    adj["area_m2"] * (result["costing"].get("rate") or 0), 2)
        q = generate_quotation(result, project=result.get("file", ""), client="")
        if fmt == "txt":
            return Response(quotation_text(q),  mimetype="text/plain; charset=utf-8")
        elif fmt == "html":
            return Response(quotation_html(q),  mimetype="text/html; charset=utf-8")
        elif fmt == "json":
            return Response(quotation_json(q),  mimetype="application/json")
        else:
            return jsonify({"error": f"unknown format {fmt!r}"}), 400
    except Exception:
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

    try:
        from approval_email import create_job, render_snapshot, png_to_b64, build_html_email
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

@app.route("/status")
def status():
    """Health-check for deploy tests."""
    jobs = load_jobs()
    return jsonify({"status": "ok", "job_count": len(jobs)})


# ── Upload endpoint ───────────────────────────────────────────────────────────

TAKEOFF_TIMEOUT_S = int(os.getenv("TAKEOFF_TIMEOUT_S", "120"))


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
        jobs[job_id] = job
        save_jobs(jobs)


def _run_takeoff(job_id: str, pdf_path: str, project_name: str, project_ref: str):
    """
    Background thread: run takeoff pipeline and update job record when done.

    Hardened per the "never break, never strand" invariant:
      - ANY exception during takeoff -> job becomes UNMEASURED with a
        "PIPELINE ERROR: ... ; route to assessor" flag, never a bare crash/"error" dead-end.
      - A watchdog timer flips the job to UNMEASURED with a timeout flag if the pipeline
        hasn't finished within TAKEOFF_TIMEOUT_S; the worker thread may keep running (daemon
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
    watchdog = threading.Timer(
        TAKEOFF_TIMEOUT_S, _mark_job_unmeasured, args=(
            job_id,
            f"PIPELINE TIMEOUT: takeoff did not finish within {TAKEOFF_TIMEOUT_S}s; "
            "route to assessor — the worker thread may still be running in the background.",
        ),
        kwargs={"watchdog_fired": True},
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        import takeoff_pipeline
        result = takeoff_pipeline.takeoff(pdf_path, project_name=project_name, project_ref=project_ref)
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
                "flags":            pre_flags + result.get("flags", []),
                "polygon_pts":      result.get("polygon_pts"),
                "result":           result,
                "status":           "pending",
            })
            jobs[job_id] = job
            save_jobs(jobs)
    except Exception as e:
        watchdog.cancel()
        _mark_job_unmeasured(
            job_id,
            f"PIPELINE ERROR: {e}; route to assessor",
            extra={"error": traceback.format_exc()},
        )


def _sanitise_filename(name: str) -> str:
    """Strip dangerous characters; keep alphanumeric, dash, underscore, dot."""
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w.\-]", "", name)   # \w = [a-zA-Z0-9_]
    return name[:80]


MAX_EXTRACT_BYTES  = 200 * 1024 * 1024   # safety cap on total bytes extracted from a zip
MAX_EXTRACT_FILES  = 200                 # safety cap on member count
CAD_EXTENSIONS     = (".dwg", ".dxf")


def _create_rejected_job(project_name, project_ref, client_name, filename, reason) -> str:
    """Create a REJECTED job record — visible in the portal job list with a human-readable
    reason (never a bare HTTP 400 that vanishes)."""
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
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)
    return job_id


def _safe_extract_zip(zip_path: Path, dest_dir: Path, prefix: str = "") -> tuple[list, list]:
    """Extract PDFs from a zip archive, guarding against zip-slip and oversize archives.
    Returns (list of extracted PDF Paths, flags).

    `prefix` (typically the sanitised project_ref) is prepended to every extracted filename so
    two uploads whose archives happen to contain a same-named member (e.g. both ship a
    "Yard_Area_Proposed_Site_Plan.pdf") never collide/overwrite each other on disk — mirrors the
    `{project_ref}_{filename}` convention already used for direct .pdf uploads below."""
    flags = []
    pdfs = []
    pfx = f"{prefix}_" if prefix else ""
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
                target = (dest_dir / f"{pfx}{member_name}").resolve()
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
      pdf          – the uploaded file (required). Accepts:
                       .pdf            -> takeoff runs directly
                       .zip            -> PDFs extracted, best one (by router.drawing_priority
                                          on filename) becomes the job; others listed in flags
                       .eml            -> PDF attachments extracted, same ranking
                       .png/.jpg/.jpeg -> wrapped into a single-page PDF, routed as raster/UNMEASURED
                       .dwg/.dxf/other -> REJECTED job, "CAD/unsupported format — please export PDF"
                     Encrypted/corrupt/zero-byte PDFs (at any stage above) -> REJECTED job with
                     the specific reason instead of a bare HTTP 400.
      project_name – human-readable project name (required)
      project_ref  – Fortel reference / sequential number (required)

    Returns 201 {"job_id": "...", "status": "processing"} for a takeoff-bound job, or
    201 {"job_id": "...", "status": "rejected"} for a REJECTED job — always 2xx with a job
    record the portal can show, never a bare 400 that vanishes.
    """
    # ── Validate required form fields
    project_name = (request.form.get("project_name") or "").strip()
    project_ref  = (request.form.get("project_ref")  or "").strip()
    client_name  = (request.form.get("client_name")  or "").strip()
    up_file      = request.files.get("pdf")

    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    if not project_ref:
        return jsonify({"error": "project_ref is required"}), 400
    if up_file is None:
        return jsonify({"error": "file is required"}), 400

    original_filename = up_file.filename or "upload"
    ext = Path(original_filename).suffix.lower()

    drawings_dir = Path(__file__).parent / "drawings"
    drawings_dir.mkdir(exist_ok=True)

    # ── Save the raw upload to a staging path first (needed for zip/eml parsing + safety checks)
    safe_name  = _sanitise_filename(original_filename) or f"upload_{uuid.uuid4().hex[:8]}{ext}"
    stage_name = f"{_sanitise_filename(project_ref)}_{safe_name}"
    stage_path = drawings_dir / stage_name
    try:
        stage_resolved = stage_path.resolve()
        stage_resolved.relative_to(drawings_dir.resolve())
    except ValueError:
        return jsonify({"error": "invalid filename (path traversal detected)"}), 400
    up_file.save(str(stage_path))

    extra_flags = []
    pdf_path = None

    if ext == ".pdf":
        pdf_path = stage_path

    elif ext == ".zip":
        pdfs, flags = _safe_extract_zip(stage_path, drawings_dir, prefix=_sanitise_filename(project_ref))
        extra_flags += flags
        stage_path.unlink(missing_ok=True)
        if not pdfs:
            job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                          "zip archive contained no extractable PDFs")
            return jsonify({"job_id": job_id, "status": "rejected"}), 201
        ranked = _rank_pdfs_by_priority(pdfs)
        pdf_path = ranked[0]
        if len(ranked) > 1:
            others = ", ".join(p.name for p in ranked[1:6])
            extra_flags.append(f"zip contained {len(ranked)} PDFs; measured '{pdf_path.name}' "
                               f"(highest drawing_priority); others: {others}")

    elif ext == ".eml":
        pdfs, flags = _extract_eml_pdfs(stage_path, drawings_dir, prefix=_sanitise_filename(project_ref))
        extra_flags += flags
        stage_path.unlink(missing_ok=True)
        if not pdfs:
            job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                          "no PDF attachments found in .eml")
            return jsonify({"job_id": job_id, "status": "rejected"}), 201
        ranked = _rank_pdfs_by_priority(pdfs)
        pdf_path = ranked[0]
        if len(ranked) > 1:
            others = ", ".join(p.name for p in ranked[1:6])
            extra_flags.append(f".eml contained {len(ranked)} PDF attachments; measured '{pdf_path.name}' "
                               f"(highest drawing_priority); others: {others}")

    elif ext in (".png", ".jpg", ".jpeg"):
        try:
            import fitz
            img_doc = fitz.open(str(stage_path))
            pdf_doc = fitz.open()
            rect = img_doc[0].rect
            page = pdf_doc.new_page(width=rect.width, height=rect.height)
            page.insert_image(rect, filename=str(stage_path))
            pdf_path = stage_path.with_suffix(".pdf")
            pdf_doc.save(str(pdf_path))
            stage_path.unlink(missing_ok=True)
            extra_flags.append(f"image ({ext}) wrapped into a single-page PDF for takeoff — "
                               "raster source, routes to UNMEASURED/mandatory assessor trace")
        except Exception as e:
            job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                          f"could not wrap image into PDF: {e}")
            return jsonify({"job_id": job_id, "status": "rejected"}), 201

    elif ext in CAD_EXTENSIONS:
        stage_path.unlink(missing_ok=True)
        job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                      "CAD/unsupported format — please export PDF")
        return jsonify({"job_id": job_id, "status": "rejected"}), 201

    else:
        stage_path.unlink(missing_ok=True)
        job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                      f"unsupported file type '{ext or '(none)'}' — please upload PDF, "
                                      "ZIP, EML, PNG or JPG")
        return jsonify({"job_id": job_id, "status": "rejected"}), 201

    # ── Final gate: the chosen PDF must actually open (catches encrypted/corrupt/zero-byte
    # at whatever stage it arrived — direct .pdf upload, zip member, or eml attachment).
    doc, bad_reason = _open_pdf_safely(pdf_path)
    if bad_reason:
        job_id = _create_rejected_job(project_name, project_ref, client_name, original_filename,
                                      bad_reason)
        return jsonify({"job_id": job_id, "status": "rejected"}), 201
    doc.close()

    # ── Create a stub job record immediately (status=processing)
    job_id = str(uuid.uuid4())
    job = {
        "id":               job_id,
        "pdf_path":         str(pdf_path),
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
        "flags":            extra_flags,
        "polygon_pts":      None,
        "status":           "processing",
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "decision":         None,
        "adjusted":         None,
        "result":           {},
    }

    with _jobs_lock:
        jobs = load_jobs()
        jobs[job_id] = job
        save_jobs(jobs)

    # ── Launch takeoff in background; portal polls /jobs every 15 s
    threading.Thread(
        target=_run_takeoff,
        args=(job_id, str(pdf_path), project_name, project_ref),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "processing"}), 202


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("APPROVAL_PORT", 5001))
    host = os.getenv("APPROVAL_HOST", "0.0.0.0")
    print(f"Fortel Approval Server → http://{host}:{port}/portal")
    app.run(host=host, port=port, debug=False)
