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
from robust_takeoff import read_marked, count_manholes_marked
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


# E/O for manhole details — £75.00/Nr, from the real Winvic costing sheet ("E/O for MH
# details, 26 Nr, £75.00, £1,950.00" — see costing.py BOQ). Applies equally whether the
# manhole count is a confirmed marked-drawing figure or an unmarked-path estimate; the
# ESTIMATE case is only ever distinguished by the line description + provisional flag,
# never by a different rate.
MANHOLE_EO_RATE = 75.00


def manhole_eo_line(manhole_count: int = None, manhole_count_estimate: int = None):
    """Build the (desc, qty, unit, rate) E/O BOQ line for manhole details, or (None, False)
    if neither a confirmed count nor an estimate is available. Confirmed counts (from the
    MARKED path's Circle markers) take priority and are NOT marked provisional; an
    estimate-only count (unmarked path) is always labelled ESTIMATE so the quotation
    and costing breakdown never present it as authoritative.

    Returns (line, is_estimate) where line is an (desc, qty, unit, rate) BOQ tuple or None."""
    if manhole_count:
        return ("E/O for MH details", manhole_count, "Nr", MANHOLE_EO_RATE), False
    if manhole_count_estimate:
        return ("E/O for MH details (ESTIMATE — assessor confirm)",
                manhole_count_estimate, "Nr", MANHOLE_EO_RATE), True
    return None, False


def price_with_defaults(area_m2: float, engineer_spec: dict = None,
                        manhole_count: int = None, manhole_count_estimate: int = None) -> dict:
    """
    Price a zone using engineer spec if available, otherwise Fortel defaults.
    Returns a costing dict with area, rate, total, flags, and assumption note.

    manhole_count / manhole_count_estimate (optional): when either is supplied, an
    "E/O for MH details" extra-over BOQ line (£75.00/Nr, from the real Winvic costing
    sheet) is added under costing["extras"] and folded into costing["grand_total_gbp"].
    total_gbp itself stays the SLAB-ONLY total (unchanged) so existing callers that only
    care about the concrete slab price are unaffected; grand_total_gbp is the one to use
    once manholes are in scope. manhole_count (confirmed, from marked-drawing Circle
    markers) takes priority over manhole_count_estimate (unmarked-path ESTIMATE, which is
    always flagged provisional and never silently folded in as if confirmed).
    """
    spec, _ = spec_with_defaults(engineer_spec)
    # Brief_Spec.xlsx makes the four construction fields independent.  Costing may still
    # use the existing fallback spec, unchanged, but a partial engineer record must remain
    # visibly provisional instead of being promoted to fully confirmed by the legacy
    # all-or-nothing provenance bit.
    from slab_spec import COMMON_FIELDS
    supplied = engineer_spec or {}
    assumed = not all(supplied.get(key) is not None for key in COMMON_FIELDS)
    aspec_flags   = flag_assumed(spec, assumed)
    val, rate, perr = price_zone(
        area_m2, spec["depth_mm"], spec["conc_rate"], spec["mesh"],
        spec["layers"], spec["steel_rate_t"], spec["margin"],
        spec["conc_wastage"], spec["steel_wastage"], spec["lap_acc"],
        spec["dpm"], spec["curing"], spec["labour"], spec["trim"])

    extras, extra_flags, grand_total = [], [], val
    line, is_estimate = manhole_eo_line(manhole_count, manhole_count_estimate)
    if line:
        desc, qty, unit, mrate = line
        mvalue = round(qty * mrate, 2)
        extras.append({"description": desc, "qty": qty, "unit": unit, "rate": mrate,
                       "value": mvalue, "estimate": is_estimate})
        if val is not None:
            grand_total = round(val + mvalue, 2)
        if is_estimate:
            extra_flags.append(f"E/O for MH details is an ESTIMATE ({qty} Nr from unmarked-path "
                               "circle detection) — assessor must confirm the count before this "
                               "line is treated as firm; quotation carries it as PROVISIONAL")
        else:
            extra_flags.append(f"E/O for MH details: {qty} Nr confirmed manhole markers x "
                               f"£{mrate:.2f} = £{mvalue:,.2f}")

    return {
        "area_m2":         area_m2,
        "rate":            rate,
        "total_gbp":       val,
        "spec":            spec,
        "assumed":         assumed,
        "note":            assumption_note(spec) if assumed else "",
        "flags":           aspec_flags + perr + extra_flags,
        "extras":          extras,
        "grand_total_gbp": grand_total,
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
        if not _probe.is_pdf:
            # fitz opens bare images/XPS as image documents; downstream native code
            # (render/classify) can segfault on them. The portal converts images to
            # PDF before calling takeoff(); direct callers must convert first.
            raise ValueError("not a native PDF (image/other container) — convert to PDF first "
                             "(the portal does this automatically on upload)")
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
            # Manhole markers (Circle annots Fortel placed) — CONFIRMED count, not an estimate.
            mh_n = count_manholes_marked(meas_pdf)
            if mh_n > 0:
                r["manhole_count"] = mh_n
                r["flags"].append(f"manhole_count={mh_n} (Circle markers on the marked drawing)")

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
                    # A region measured WITHOUT a legend label is a generic grey-hatch guess — its
                    # identity is unconfirmed, so it can never be approvable even if the scale
                    # verifies. Force confidence low in that case so the state machine caps it at
                    # MEASURED_UNVERIFIED (matches the cap inside takeoff_unmarked; the pipeline
                    # re-derives state here with the router's confidence, so the cap must be re-applied).
                    eff_conf = "low" if not tu.get("legend_found", True) else conf
                    state, sflags2 = measurement_state(tu["area_m2"], scale_verified=tu.get("scale_verified", False),
                                                       confidence=eff_conf)
                    r["flags"] = r["flags"] + sflags2
                    r["measurement_state"] = state
                    r["needs_assessor"] = tu.get("needs_assessor", state != MEASURED_VERIFIED)
                    # Manhole count is an ESTIMATE on this path (never authoritative) — the
                    # flag explaining that is already appended by takeoff_unmarked.takeoff().
                    if tu.get("manhole_count_estimate"):
                        r["manhole_count_estimate"] = tu["manhole_count_estimate"]
                    # ...and the AREA-BASED ASSUMPTION (1 per 1,000 m²) when no drainage layout /
                    # no symbols were found. Carried as its own field and DELIBERATELY NOT passed to
                    # price_with_defaults below — it stays a count assumption + flag, never auto-priced.
                    if tu.get("manhole_count_assumed"):
                        r["manhole_count_assumed"] = tu["manhole_count_assumed"]
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
                # Same no-legend cap as the vector path: an unlabelled grey-hatch guess stays
                # MEASURED_UNVERIFIED even if the scale verifies.
                eff_conf = "low" if not tu.get("legend_found", True) else conf
                state, sflags2 = measurement_state(tu["area_m2"],
                                                   scale_verified=tu.get("scale_verified", False),
                                                   confidence=eff_conf)
                r["flags"] = r["flags"] + sflags2
                r["measurement_state"] = state
                r["needs_assessor"] = tu.get("needs_assessor", state != MEASURED_VERIFIED)
                if tu.get("manhole_count_assumed"):
                    r["manhole_count_assumed"] = tu["manhole_count_assumed"]
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

    # Informational formwork quantity only: polygon_pts are PDF points and scale_k is m/pt,
    # so closed polygon length × scale_k gives linear metres.  This never enters pricing.
    if r.get("polygon_pts") and r.get("scale_k"):
        from geometry import polygon_perimeter_lm
        perimeter_lm = polygon_perimeter_lm(r["polygon_pts"], r["scale_k"])
        if perimeter_lm is not None:
            r["perimeter_lm"] = perimeter_lm

    # ── Costing (with defaults where no engineer spec)
    if r.get("area_m2"):
        costing = price_with_defaults(r["area_m2"], engineer_spec,
                                      manhole_count=r.get("manhole_count"),
                                      manhole_count_estimate=r.get("manhole_count_estimate"))
        r["costing"] = costing
        r["flags"] = r["flags"] + costing["flags"]

    # Carry Fortel's client-supplied slab checklist independently from pricing.  Values
    # already used by fallback costing are useful context but remain field-by-field
    # provisional; only fields actually read from an engineer source are confirmed.
    from slab_spec import COMMON_FIELDS, build_brief_spec, normalise_slab_type
    confirmed_spec = {
        key: engineer_spec[key]
        for key in COMMON_FIELDS
        if engineer_spec and engineer_spec.get(key) is not None
    }
    if confirmed_spec:
        # Persist only the client-facing construction fields, not extractor metadata.
        r["engineer_spec"] = dict(confirmed_spec)
    effective_spec = (r.get("costing") or {}).get("spec") or {}
    slab_type = normalise_slab_type(
        r.get("quotation_section"),
        text=" ".join(str(r.get(key) or "") for key in ("file", "project_name", "type")),
    )
    r["brief_spec"] = build_brief_spec(
        slab_type,
        effective_spec=effective_spec,
        confirmed=confirmed_spec,
        source="engineer_drawing",
    )

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
