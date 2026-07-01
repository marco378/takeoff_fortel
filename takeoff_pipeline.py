#!/usr/bin/env python3
"""
Fortel AI Takeoff — FINAL consolidated pipeline.

  ingest(pdf) -> classify (router) -> measure -> price -> structured result + flags + confidence

  MARKED vector    : read Bluebeam area markups (exact, multi-region) — no scale needed
  UNMARKED vector  : render -> Claude vision returns {regions, voids, scale_ref}
                     -> geometry.measure_regions (voids/self-intersection/overlap hardened)
                     -> assessor confirms extent + scale
  RASTER/flattened : vision + MANDATORY human review

Measured area -> price_zone (deterministic, validated) -> GBP.

MANUAL APPROVAL FLOW:
  When the result needs human sign-off (scale unverified, architect drawing, raster, etc.)
  the pipeline calls request_approval() which:
    1. Creates a job record in approval_jobs.json
    2. Emails Inderjit a snapshot + YES / NO / ADJUST buttons
    3. Starts the approval_server.py portal if not already running

  Set SEND_APPROVAL_EMAILS=1 to enable; defaults to off for dev runs.
"""
import math, json, io, contextlib, os, fitz
from pathlib import Path
from router import classify, classify_page
from robust_takeoff import read_marked
from geometry import measure_regions
from scale import detect_scale_bar, user_unit
from sanity import plausible, measurement_state, MEASURED_VERIFIED, MEASURED_UNVERIFIED, UNMEASURED, REJECTED
from defaults import spec_with_defaults, assumption_note, flag_assumed
with contextlib.redirect_stdout(io.StringIO()):       # costing self-validates on import; mute its receipt
    from costing import rate_buildup, MESH_KG

SEND_APPROVALS = os.getenv("SEND_APPROVAL_EMAILS", "0") == "1"


# ── Auto-extract engineer spec from the drawing pack ─────────────────────────

def find_engineer_spec(pdf_path: str) -> dict | None:
    """
    Look for a construction-detail PDF near the input drawing and extract the slab spec.
    Search order (mirrors Inderjit's method):
      1. Same directory — files whose names match DETAIL_KEYWORDS from router.py
      2. The PDF's own text (in case the detail is on a separate page)
    Returns spec dict or None (falls through to defaults if nothing found).
    """
    from router import DETAIL_KEYWORDS
    from spec_extractor import extract_spec, extract_spec_from_text
    import fitz

    parent = Path(pdf_path).parent

    # ── Search sibling files for construction-detail drawings ────────────────
    for p in sorted(parent.glob("*.pdf")):
        name_lower = p.name.lower()
        if any(kw in name_lower for kw in DETAIL_KEYWORDS):
            spec = extract_spec(str(p))
            spec.pop("_source", None)
            if any(k in spec for k in ("depth_mm", "mesh", "conc_mix")):
                spec["_from_file"] = p.name
                return spec

    # ── Fallback: scan all pages of the input PDF itself ────────────────────
    try:
        doc = fitz.open(pdf_path)
        full_text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        spec = extract_spec_from_text(full_text)
        if any(k in spec for k in ("depth_mm", "mesh", "conc_mix")):
            spec["_from_file"] = Path(pdf_path).name + " (self)"
            return spec
    except Exception:
        pass

    return None


# ── Approval flags — any of these triggers a manual review email ─────────────
_APPROVAL_TRIGGERS = (
    "assessor: confirm",
    "UNVERIFIED",
    "IMPOSSIBLE",
    "ASSUMED",
    "mandatory human",
    "MIXED-SCALE",
)

def _needs_approval(result: dict) -> bool:
    flags = result.get("flags", [])
    return any(any(t in f for t in _APPROVAL_TRIGGERS) for f in flags) or \
           result.get("type") in ("RASTER / scanned",) or \
           result.get("confidence") == "low"


def _trigger_approval(pdf: str, result: dict, vision: dict = None,
                      project_name: str = None, project_ref: str = None):
    """Fire-and-forget: email Inderjit, create job record.  Never blocks the pipeline."""
    if not SEND_APPROVALS:
        ref_s  = f" [#{project_ref}]" if project_ref else ""
        name_s = f" {project_name}"   if project_name else ""
        print(f"[pipeline] Approval needed{ref_s}{name_s} — {result.get('file')} — "
              f"set SEND_APPROVAL_EMAILS=1 to email.  Portal: http://localhost:5001/portal")
        return
    try:
        from approval_email import request_approval
        poly = None
        if vision and vision.get("regions") and vision["regions"]:
            poly = vision["regions"][0]   # first region as the proposed polygon
        jid = request_approval(pdf, result, polygon_pts=poly,
                               project_name=project_name, project_ref=project_ref)
        print(f"[pipeline] Approval email sent. Job: {jid}")
    except Exception as e:
        print(f"[pipeline] Approval email failed (non-fatal): {e}")


# ── Costing with defaults ─────────────────────────────────────────────────────

def price_zone(area_m2, depth_mm, conc_rate, mesh, layers, steel_rate_t, margin,
               conc_wastage=0.03, steel_wastage=0.10, lap_acc=0.18,
               dpm=0.46, curing=0.23, labour=10.0, trim=0.40):
    """Deterministic per-zone price with input validation (no silent crashes / garbage)."""
    if mesh not in MESH_KG:
        return None, None, [f"unknown mesh '{mesh}' — not in rate table; assessor to add"]
    if not area_m2 or area_m2 <= 0:
        return None, None, ["non-positive area — cannot price"]
    if depth_mm <= 0 or conc_rate <= 0:
        return None, None, ["non-positive thickness/rate — invalid"]
    rate, _ = rate_buildup(depth_mm, conc_rate, conc_wastage, mesh, layers,
                           steel_rate_t, steel_wastage, lap_acc, dpm, curing, labour, trim, margin)
    return round(area_m2 * rate, 2), rate, []


def price_with_defaults(area_m2: float, engineer_spec: dict = None) -> dict:
    """
    Price a zone using engineer spec if available, otherwise Fortel defaults.
    Returns a costing dict with area, rate, total, flags, and assumption note.
    """
    spec, assumed = spec_with_defaults(engineer_spec)
    aspec_flags   = flag_assumed(spec, assumed)
    val, rate, perr = price_zone(
        area_m2, spec["depth_mm"], spec["conc_rate"], spec["mesh"],
        spec["layers"], spec["steel_rate_t"], spec["margin"],
        spec["conc_wastage"], spec["steel_wastage"], spec["lap_acc"],
        spec["dpm"], spec["curing"], spec["labour"], spec["trim"])
    return {
        "area_m2":    area_m2,
        "rate":       rate,
        "total_gbp":  val,
        "spec":       spec,
        "assumed":    assumed,
        "note":       assumption_note(spec) if assumed else "",
        "flags":      aspec_flags + perr,
    }


# ── Main takeoff ──────────────────────────────────────────────────────────────

def takeoff(pdf, vision=None, engineer_spec=None, send_approval=None, auto_extract_spec=True,
            project_name: str = None, project_ref: str = None):
    """
    vision (optional) = {'regions':[[...]], 'voids':{i:[...]}, 'scale_ref':[[x1,y1],[x2,y2],metres]}
    engineer_spec (optional) = dict from construction-detail drawing (depth_mm, mesh, etc.)
    send_approval (optional) = True/False override; defaults to SEND_APPROVAL_EMAILS env var
    auto_extract_spec       = True: scan the drawing pack for a construction-detail PDF and
                              auto-extract the slab spec before falling back to defaults
    project_name (optional) = human-readable project name e.g. "TSL Agratas Battery Facility"
    project_ref  (optional) = Fortel sequential reference number e.g. "2131"
    """
    # ── Multi-page tender pack: never assume page 0. Classify every page, rank candidates
    # by router.drawing_priority (external-works/hard-landscaping/construction-thickness
    # sheets first, "site plan" down-ranked), and measure the best-ranked page. Single-page
    # PDFs skip this (page 0 is the only choice) so behaviour/perf on the common case is
    # unchanged.
    # ── Reject unreadable input up front: corrupt/truncated/zero-byte/non-PDF bytes must
    # yield a REJECTED result, never an exception out of takeoff().
    page = 0
    page_flags = []
    try:
        _probe = fitz.open(pdf)
        page_count = _probe.page_count
        if page_count == 0:
            raise ValueError("document has 0 pages")
        _probe.close()
    except Exception as e:
        return {
            "file": pdf.split("/")[-1], "pdf_path": pdf, "page": 0,
            "type": "UNREADABLE", "confidence": "n/a", "method": "none",
            "area_m2": None, "measurement_state": REJECTED, "status": REJECTED,
            "needs_assessor": False,
            "project_name": project_name or "", "project_ref": project_ref or "",
            "flags": [f"REJECTED: file could not be opened as a PDF ({type(e).__name__}: {e}). "
                      "If this is a ZIP/EML/image, upload it via the portal which extracts/converts; "
                      "if CAD, export a PDF."],
        }
    if page_count > 1:
        from router import rank_pages
        ranked = rank_pages(pdf)
        if ranked:
            page = ranked[0]["page"]
            others = ", ".join(f"p{c['page']} '{c['title']}'" for c in ranked[1:4])
            page_flags.append(
                f"MULTI-PAGE: measured page {page} of {page_count} ('{ranked[0]['title']}')"
                + (f"; other candidates: {others}" if others else "")
            )

    typ, route, conf, _ = classify_page(pdf, page) if page_count > 1 else classify(pdf)
    r = {"file": pdf.split("/")[-1], "pdf_path": pdf, "page": page,
         "type": typ, "confidence": conf, "method": route, "flags": list(page_flags),
         "project_name": project_name or "", "project_ref": project_ref or ""}

    # ── Auto-extract engineer spec from the pack (if not already provided)
    if engineer_spec is None and auto_extract_spec:
        found = find_engineer_spec(pdf)
        if found:
            engineer_spec = found
            r["spec_source"] = found.get("_from_file", "auto")

    # ── Drawing source discipline (engineer vs architect)
    from router import source_discipline
    discipline = source_discipline(pdf)
    if discipline == "architect" and not engineer_spec:
        r["flags"].append(
            "ARCHITECT drawing — build-up ASSUMED; no construction-detail sheet found. "
            "State assumptions in quotation (5% area tolerance applies)."
        )
        r["source_discipline"] = "architect"
    else:
        r["source_discipline"] = discipline

    # For a non-zero chosen page, extract it to a temp single-page PDF so the (page-0-only)
    # measurement helpers below (read_marked, takeoff_unmarked.takeoff, detect_scale_bar)
    # measure the RIGHT page without touching their internal math/constants.
    meas_pdf = pdf
    _tmp_page_pdf = None
    if page != 0:
        try:
            src = fitz.open(pdf)
            single = fitz.open()
            single.insert_pdf(src, from_page=page, to_page=page)
            _tmp_page_pdf = str(Path(pdf).with_suffix("")) + f".__page{page}.tmp.pdf"
            single.save(_tmp_page_pdf)
            meas_pdf = _tmp_page_pdf
        except Exception as e:
            r["flags"].append(f"could not isolate page {page} for measurement ({e}); falling back to page 0")
            meas_pdf = pdf

    try:
        # ── Measurement
        if typ == "MARKED vector":
            area, n = read_marked(meas_pdf)
            sflags = plausible(area)
            r.update({"area_m2": area, "regions": n})
            state, sflags2 = measurement_state(area, scale_verified=True, confidence=conf)
            r["flags"] = r["flags"] + sflags + sflags2
            r["measurement_state"] = state

        elif typ == "UNMARKED vector":
            if vision:
                # ── LLM vision path (caller supplied region polygons + scale) ────────
                uu = user_unit(meas_pdf)
                if vision.get("scale_ref"):
                    sr = vision["scale_ref"]; k = sr[2] / math.dist(sr[0], sr[1]) * uu; ksrc = "vision scale_ref"
                else:
                    kb, info = detect_scale_bar(meas_pdf); k = (kb * uu) if kb else None; ksrc = f"auto scale-bar: {info}"
                if k is None:
                    r["flags"].append("no scale (no scale_ref, no detectable bar) -> assessor must supply scale")
                    r["measurement_state"] = UNMEASURED
                else:
                    area, gflags = measure_regions(vision["regions"], k, vision.get("voids"))
                    sflags = plausible(area, site_m2=vision.get("site_m2"))
                    r.update({"area_m2": area, "scale_k": round(k, 4), "scale_src": ksrc,
                              "flags": r["flags"] + gflags + sflags + ["assessor: confirm extent + scale"],
                              "polygon_pts": vision["regions"][0] if vision.get("regions") else None})
                    # vision path scale is caller-supplied, never auto-verified against a 2nd source
                    state, sflags2 = measurement_state(area, scale_verified=False, confidence=conf)
                    r["flags"] = r["flags"] + sflags2
                    r["measurement_state"] = state
            else:
                # ── Deterministic colour-segmentation path (takeoff_unmarked) ────────
                import takeoff_unmarked as TU
                tu = TU.takeoff(meas_pdf, source=discipline)
                r["method"] = "colour-segmentation (takeoff_unmarked)"
                if tu.get("area_m2") is not None:
                    r.update({
                        "area_m2":        tu["area_m2"],
                        "scale_k":        tu.get("scale_k"),
                        "scale_src":      tu.get("scale_src"),
                        "scale_verified": tu.get("scale_verified", False),
                        "scale_sources":  tu.get("scale_sources", {}),
                        "polygon_pts":    tu.get("polygon_pts"),
                    })
                    r["flags"] = r["flags"] + tu.get("flags", []) + ["assessor: confirm extent + scale"]
                    state, sflags2 = measurement_state(tu["area_m2"], scale_verified=tu.get("scale_verified", False),
                                                       confidence=conf)
                    r["flags"] = r["flags"] + sflags2
                    r["measurement_state"] = state
                    r["needs_assessor"] = tu.get("needs_assessor", state != MEASURED_VERIFIED)
                else:
                    r["flags"] = r["flags"] + tu.get("flags", []) + [
                        "takeoff_unmarked: no area emitted — assessor must trace manually"
                    ]
                    r["measurement_state"] = UNMEASURED
                    r["needs_assessor"] = True
        else:
            # RASTER / scanned or flattened drawing. The colour-segmentation path measures
            # rendered PIXELS, not vector paths, so a flattened-but-colour-coded sheet (e.g.
            # D77 exports with vec<50) is still measurable — attempt it before giving up.
            # A genuine scan without vector text yields no scale there and falls through.
            tu = None
            try:
                import takeoff_unmarked as TU
                tu = TU.takeoff(meas_pdf, source=discipline)
            except Exception as e:
                r["flags"].append(f"raster fallback (colour-segmentation) unavailable: {e}")
            if tu and tu.get("area_m2") is not None:
                r["method"] = "colour-segmentation on flattened/raster render"
                r.update({
                    "area_m2":        tu["area_m2"],
                    "scale_k":        tu.get("scale_k"),
                    "scale_src":      tu.get("scale_src"),
                    "scale_verified": tu.get("scale_verified", False),
                    "scale_sources":  tu.get("scale_sources", {}),
                    "polygon_pts":    tu.get("polygon_pts"),
                })
                r["flags"] = r["flags"] + tu.get("flags", []) + [
                    "flattened/raster drawing measured from the RENDER (no vector geometry) — "
                    "assessor: confirm extent + scale"]
                state, sflags2 = measurement_state(tu["area_m2"],
                                                   scale_verified=tu.get("scale_verified", False),
                                                   confidence=conf)
                r["flags"] = r["flags"] + sflags2
                r["measurement_state"] = state
                r["needs_assessor"] = tu.get("needs_assessor", state != MEASURED_VERIFIED)
            else:
                # area_m2 stays None; the PDF snapshot must still render (portal renders straight
                # from pdf_path/page, not from anything computed here) so the assessor can trace.
                # Approve stays blocked until the assessor supplies an area via /adjust.
                r["flags"].append(
                    "RASTER/scanned or flattened drawing — no reliable vector geometry to measure. "
                    "UNMEASURED: mandatory assessor trace via the portal (snapshot renders for tracing); "
                    "supply {regions, voids, scale_ref} vision data or trace manually via /adjust."
                )
                r["area_m2"] = None
                r["measurement_state"] = UNMEASURED
                r["needs_assessor"] = True
    finally:
        if _tmp_page_pdf:
            try:
                os.remove(_tmp_page_pdf)
            except OSError:
                pass

    # ── Costing (with defaults where no engineer spec)
    if r.get("area_m2"):
        costing = price_with_defaults(r["area_m2"], engineer_spec)
        r["costing"] = costing
        r["flags"] = r["flags"] + costing["flags"]

    r.setdefault("measurement_state", UNMEASURED if not r.get("area_m2") else MEASURED_UNVERIFIED)
    r.setdefault("needs_assessor", r["measurement_state"] != MEASURED_VERIFIED)
    r["status"] = r["measurement_state"]   # portal/job-record field name

    # ── Approval trigger
    do_send = send_approval if send_approval is not None else SEND_APPROVALS
    if _needs_approval(r) and (do_send or not os.getenv("SKIP_APPROVAL_LOG")):
        _trigger_approval(pdf, r, vision,
                          project_name=project_name, project_ref=project_ref)

    return r


def takeoff_pack(pdf):
    """Multi-page tender pack: classify EVERY page (never assume page 0)."""
    d = fitz.open(pdf); out = []
    for i in range(d.page_count):
        p = d[i]
        vec = len(p.get_drawings())
        nmark = sum(1 for a in (p.annots() or []) if a.type[1] == "Polygon")
        kind = "raster" if vec < 50 else ("marked" if nmark else "unmarked/context")
        out.append({"page": i, "kind": kind, "vector_paths": vec, "area_markups": nmark})
    return out


if __name__ == "__main__":
    for c in ["Yard Area Proposed_Site_Plan.pdf", "Dock Slab Area Proposed_Site_Plan.pdf",
              "Area Office Floors Proposed_GA_Office_Plan_ref_S2_P01.pdf",
              "Area Hub Office Proposed_Transport_Office_ref_S2_P01.pdf"]:
        print(json.dumps(takeoff("drawings/" + c)))
    val, rate, _ = price_zone(26080, 190, 128, "A252", 1, 850, 0.11)
    print(f"\nyard end-to-end: 26,080 m2 @ GBP{rate}/m2 = GBP{val:,.2f}  (actual quote GBP1,170,731.20)")
    print("costing edge cases (validated, no crash):")
    print("  unknown mesh ->", price_zone(100, 150, 128, "A999", 1, 850, 0.11)[2])
    print("  zero area    ->", price_zone(0, 150, 128, "A142", 1, 850, 0.11)[2])
