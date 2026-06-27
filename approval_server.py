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
import os, json, io, datetime, traceback, uuid, re, threading
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
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return {}

def save_jobs(jobs: dict):
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))

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
        from approval_email import render_snapshot
        res  = j.get("result", {})
        pdf  = res.get("pdf_path") or j.get("pdf", "")
        poly = res.get("polygon_pts")
        if not pdf or not Path(pdf).exists():
            return jsonify({"error": "PDF not on disk — snapshot unavailable"}), 404
        png = render_snapshot(pdf, polygon_pts=poly)
        return send_file(io.BytesIO(png), mimetype="image/png")
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── Decision endpoints ────────────────────────────────────────────────────────

@app.route("/approve/<job_id>", methods=["GET", "POST"])
def approve(job_id):
    """
    Approve: accept the AI's measurement as-is, proceed to costing.
    Can be triggered by clicking the email button (GET) or from the portal (POST).
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
    Adjust: assessor provides corrected polygon and/or scale.
    Re-runs geometry measurement with the assessor's inputs, then costs.
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

    with _jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": f"job {job_id!r} not found"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "job is still processing"}), 409
        costing_result = _run_costing(area_m2, job.get("result", {})) if area_m2 else None
        jobs[job_id].update({
            "status":      "adjusted",
            "decision":    "adjusted",
            "decided_at":  now_iso(),
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

def _run_takeoff(job_id: str, pdf_path: str, project_name: str, project_ref: str):
    """Background thread: run takeoff pipeline and update job record when done."""
    try:
        import takeoff_pipeline
        result = takeoff_pipeline.takeoff(pdf_path, project_name=project_name, project_ref=project_ref)
        with _jobs_lock:
            jobs = load_jobs()
            jobs[job_id].update({
                "project_name":     result.get("project_name", project_name),
                "project_ref":      result.get("project_ref",  project_ref),
                "type":             result.get("type"),
                "method":           result.get("method"),
                "confidence":       result.get("confidence"),
                "source_discipline": result.get("source_discipline"),
                "area_m2":          result.get("area_m2"),
                "scale_verified":   result.get("scale_verified"),
                "scale_src":        result.get("scale_src"),
                "scale_sources":    result.get("scale_sources"),
                "costing":          result.get("costing"),
                "flags":            result.get("flags", []),
                "polygon_pts":      result.get("polygon_pts"),
                "result":           result,
                "status":           "pending",
            })
            save_jobs(jobs)
    except Exception as e:
        with _jobs_lock:
            jobs = load_jobs()
            jobs[job_id]["status"] = "error"
            jobs[job_id]["flags"]  = [str(e)]
            save_jobs(jobs)


def _sanitise_filename(name: str) -> str:
    """Strip dangerous characters; keep alphanumeric, dash, underscore, dot."""
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w.\-]", "", name)   # \w = [a-zA-Z0-9_]
    return name[:80]


@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept a new drawing from the assessor portal without CLI intervention.

    multipart/form-data fields:
      pdf          – PDF file (required, .pdf extension only)
      project_name – human-readable project name (required)
      project_ref  – Fortel reference / sequential number (required)

    Returns 201 {"job_id": "...", "status": "ok"} on success.
    """
    # ── Validate required form fields
    project_name = (request.form.get("project_name") or "").strip()
    project_ref  = (request.form.get("project_ref")  or "").strip()
    client_name  = (request.form.get("client_name")  or "").strip()
    pdf_file     = request.files.get("pdf")

    if not project_name:
        return jsonify({"error": "project_name is required"}), 400
    if not project_ref:
        return jsonify({"error": "project_ref is required"}), 400
    if pdf_file is None:
        return jsonify({"error": "pdf file is required"}), 400

    # ── Validate extension
    original_filename = pdf_file.filename or ""
    if not original_filename.lower().endswith(".pdf"):
        return jsonify({"error": "only .pdf files are accepted"}), 400

    # ── Sanitise and build safe save path
    safe_name   = _sanitise_filename(original_filename)
    dest_name   = f"{_sanitise_filename(project_ref)}_{safe_name}"
    drawings_dir = Path(__file__).parent / "drawings"
    drawings_dir.mkdir(exist_ok=True)
    dest_path   = drawings_dir / dest_name

    # Path-traversal guard: resolved path must stay inside drawings/
    try:
        dest_resolved = dest_path.resolve()
        dir_resolved  = drawings_dir.resolve()
        dest_resolved.relative_to(dir_resolved)
    except ValueError:
        return jsonify({"error": "invalid filename (path traversal detected)"}), 400

    # ── Save the PDF
    pdf_file.save(str(dest_path))

    # ── Create a stub job record immediately (status=processing)
    job_id = str(uuid.uuid4())
    job = {
        "id":               job_id,
        "pdf_path":         str(dest_path),
        "project_name":     project_name,
        "project_ref":      project_ref,
        "client_name":      client_name,
        "type":             None,
        "method":           None,
        "confidence":       None,
        "source_discipline": None,
        "area_m2":          None,
        "scale_verified":   None,
        "scale_src":        None,
        "scale_sources":    None,
        "costing":          None,
        "flags":            [],
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
        args=(job_id, str(dest_path), project_name, project_ref),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "processing"}), 202


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("APPROVAL_PORT", 5001))
    host = os.getenv("APPROVAL_HOST", "0.0.0.0")
    print(f"Fortel Approval Server → http://{host}:{port}/portal")
    app.run(host=host, port=port, debug=False)
