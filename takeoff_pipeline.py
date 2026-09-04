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
  approval_server.py first persists the canonical portal job, then emails Inderjit a
  snapshot + APPROVE / REJECT / ADJUST controls for that exact job id.  The pipeline itself
  is deliberately unable to create approval jobs: alongside /upload that produced a ghost
  job whose emailed controls did not affect the portal's real case.

  Set SEND_APPROVAL_EMAILS=1 to enable; defaults to off for dev runs.
"""
import math, json, io, contextlib, os, fitz
from pathlib import Path
from router import classify, classify_page
from robust_takeoff import read_marked, read_marked_zones, count_manholes_marked
from geometry import measure_regions
from scale import detect_scale_bar, user_unit
from sanity import plausible, measurement_state, MEASURED_VERIFIED, MEASURED_UNVERIFIED, UNMEASURED, REJECTED
from defaults import spec_with_defaults, assumption_note, flag_assumed
with contextlib.redirect_stdout(io.StringIO()):       # costing self-validates on import; mute its receipt
    from costing import rate_buildup, MESH_KG

SEND_APPROVALS = os.getenv("SEND_APPROVAL_EMAILS", "0") == "1"


_REPORT_CLASS_RX = __import__("re").compile(
    r"\b(?:das(?:[ _-]+report)?|design[ _-]+(?:and|&)[ _-]+access[ _-]+statement|"
    r"planning[ _-]+statement)\b",
    __import__("re").I,
)

_WRITTEN_DOCUMENT_FILENAME_RX = __import__("re").compile(
    r"(?:\bspecification(?:[ _-]+notes?)?\b|\bschedule\b|\bdrawing[ _-]+log\b|"
    r"\bdocument[ _-]+log\b|\btender[ _-]+quer(?:y|ies)\b|\bsubmittal\b|"
    r"\bbill[ _-]+of[ _-]+quantities\b|\btrade[ _-]+bill\b|"
    r"\bemployer(?:'s)?[ _-]+requirements?\b|\bpre[ _-]+construction[ _-]+information\b)",
    __import__("re").I,
)
_WRITTEN_DOCUMENT_TEXT_RX = __import__("re").compile(
    r"(?:\bdevelopment[ _-]+specification\b|\btechnical[ _-]+specification\b|"
    r"\bspecifications?[ _-]+included\b|\bdrawings?[ _-]+included\b|"
    r"\bschedule[ _-]+of[ _-]+works\b|\bbill[ _-]+of[ _-]+quantities\b|"
    r"\bemployer(?:'s)?[ _-]+requirements?\b|\bpre[ _-]+construction[ _-]+information\b)",
    __import__("re").I,
)

# ISO 19650 / BS 1192 document-type field: "...-DR-C-2105..." — DR = DRAWING, followed by the
# discipline letter and the sheet number. Aryan, 4 Sep: a real construction drawing,
# 9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf, was never recognised
# on prod — its title ends in "Specification", so the written-document rule below refused it
# before a single page was rasterised. The sheet's own reference says it is a drawing.
_DRAWING_CODE_RX = __import__("re").compile(
    r"(?:^|[ _-])DR[ _-][A-Z]{1,2}[ _-]?\d",
)

_DEDICATED_BOUNDARY_TREATMENT_RX = __import__("re").compile(
    r"\bboundary[ _-]+treatments?[ _-]+plan\b",
    __import__("re").I,
)
_SLAB_SCOPE_FILENAME_RX = __import__("re").compile(
    r"\b(?:hard[ _-]+landscap(?:e|ing)|external[ _-]+works?|surfacing|slabs?|yards?)\b",
    __import__("re").I,
)


def _fast_report_refusal(pdf_path: str, doc) -> str | None:
    """Identify obvious multi-page planning/report documents without rasterising them.

    This is deliberately a document-class gate, not a measurement heuristic.  It examines
    only the filename and first page text already available from the PDF catalogue. A
    single-sheet file is rejected only when its filename directly identifies a written
    specification/schedule/log/bill class; uncertain documents continue through the normal
    router. The gate exists so a large DAS cannot consume the render watchdog.
    """
    normalised_name = Path(pdf_path).stem.replace("_", " ").replace("-", " ")
    filename_evidence = _REPORT_CLASS_RX.search(normalised_name)
    written_filename_evidence = _WRITTEN_DOCUMENT_FILENAME_RX.search(normalised_name)
    try:
        first_page_text = " ".join((doc[0].get_text() or "").split())[:12_000]
    except Exception:
        first_page_text = ""
    text_evidence = _REPORT_CLASS_RX.search(first_page_text)
    written_text_evidence = _WRITTEN_DOCUMENT_TEXT_RX.search(first_page_text)

    # A dedicated Boundary Treatment Plan is a fencing/site-boundary deliverable, not a slab
    # measurement drawing.  Two real one-sheet examples exhausted the render watchdog despite
    # containing no slab scope.  Refuse only from this precise drawing title and retain combined
    # Hard Landscaping / External Works / Surfacing / Slab / Yard plans: those can contain the
    # very quantities this pipeline is meant to measure.
    boundary_treatment_evidence = _DEDICATED_BOUNDARY_TREATMENT_RX.search(normalised_name)
    slab_scope_evidence = _SLAB_SCOPE_FILENAME_RX.search(normalised_name)
    if boundary_treatment_evidence and not slab_scope_evidence:
        return (
            "FAST REFUSE: dedicated boundary-treatment/fencing plan matched filename "
            f"'{boundary_treatment_evidence.group(0)}'; no page was rasterised or measured"
        )

    # Existing planning-report rule: require a multi-page document so a one-sheet drawing
    # carrying a planning note cannot be discarded on text alone.
    report_evidence = filename_evidence or text_evidence
    if doc.page_count >= 4 and report_evidence:
        return (
            f"FAST REFUSE: multi-page non-drawing report class matched "
            f"'{report_evidence.group(0)}' ({doc.page_count} pages); no page was rasterised "
            "or measured"
        )

    # Specification/schedule/log/bill filenames are direct document-class evidence even for
    # a single-page index. First-page text is inspected as corroboration and is allowed to
    # classify on its own only for a multi-page document. This intentionally refuses the
    # written tender-pack artefacts that consumed live worker slots, while an ordinary drawing
    # with an incidental word such as "schedule" in its notes continues through the router.
    written_evidence = written_filename_evidence or (
        written_text_evidence if doc.page_count >= 4 else None)
    # A drawing-coded reference overrides a written-document TITLE. The override is deliberately
    # narrow: it applies only when the sole evidence is the filename, so a multi-page document
    # whose first page also reads as a specification still refuses, whatever its reference says.
    # It flips no file in the 613-file corpus (51 written-document refusals, none drawing-coded)
    # and exists for the class Aryan hit: an ordinary sheet titled "... Specification".
    drawing_code_evidence = _DRAWING_CODE_RX.search(Path(pdf_path).stem)
    if written_evidence and drawing_code_evidence and not written_text_evidence:
        written_evidence = None
    if written_evidence:
        sources = []
        if written_filename_evidence:
            sources.append(f"filename '{written_filename_evidence.group(0)}'")
        if written_text_evidence:
            sources.append(f"first-page text '{written_text_evidence.group(0)}'")
        return (
            "FAST REFUSE: non-drawing specification/schedule class matched "
            + " + ".join(sources)
            + f" ({doc.page_count} page{'s' if doc.page_count != 1 else ''}); no page was "
              "rasterised or measured"
        )
    return None


# ── Auto-extract engineer spec from the drawing pack ─────────────────────────

def find_engineer_spec(pdf_path: str, project_ref: str | None = None,
                       project_files=None) -> dict | None:
    """
    Look for a construction-detail PDF near the input drawing and extract the slab spec.
    Search order (mirrors Inderjit's method):
      1. The PDF's own text (in case the detail is on a separate page)
      2. Same directory — files whose names match DETAIL_KEYWORDS from router.py
      3. Detail drawings explicitly registered to the same portal project, even when their
         persisted paths are in different upload directories
    Returns spec dict or None (falls through to defaults if nothing found).
    """
    from router import DETAIL_KEYWORDS
    from spec_extractor import extract_spec
    import fitz

    parent = Path(pdf_path).parent
    target = Path(pdf_path)
    safe_project_ref = __import__("re").sub(
        r"[^\w.\-]", "", str(project_ref or "").replace(" ", "_"))[:80]

    # Portal uploads from every client project share one persistent drawings directory.  The
    # filename prefix is therefore a security/correctness boundary, not decoration: scanning an
    # unscoped sibling previously let an alphabetically-earlier project's 150 mm detail become
    # this project's apparently-confirmed spec.  Direct/local corpus runs have no project_ref and
    # retain same-directory discovery.
    def in_project(path: Path) -> bool:
        return not safe_project_ref or path.name.startswith(f"{safe_project_ref}_")

    try:
        with fitz.open(pdf_path) as target_doc:
            target_text = "\n".join(target_doc[i].get_text() or ""
                                     for i in range(min(target_doc.page_count, 3)))
    except Exception:
        target_text = ""
    target_probe = f"{target.name} {target_text[:6000]}".casefold().replace("_", " ")
    if any(term in target_probe for term in ("service yard", "external yard", "concrete yard")):
        target_context = "external service yard concrete yard"
    elif "dock" in target_probe:
        target_context = "dock slab"
    elif any(term in target_probe for term in ("upper floor", "first floor", "metal deck")):
        target_context = "upper floor slab metal deck"
    elif any(term in target_probe for term in ("ground floor", "office core")):
        target_context = "ground floor office core slab"
    else:
        # Filename-only fallback avoids feeding every word in a full tender sheet into the
        # candidate scorer (generic words repeated all over a page are not local evidence).
        target_context = target.name

    spec_sheet_terms = tuple(DETAIL_KEYWORDS) + (
        "external works details", "joint layout", "construction thickness",
        "slab specification", "pavement details",
    )
    normalised_spec_terms = tuple(
        __import__("re").sub(r"[-_]+", " ", term.casefold())
        for term in spec_sheet_terms
    )
    candidates = [target]
    candidate_keys = {str(target.resolve())}

    def add_detail_candidate(path, *, registry_scoped=False):
        """Add one readable detail PDF without weakening cross-project isolation.

        ``registry_scoped`` means approval_server obtained the path from a job record with the
        exact same project_ref. Those paths do not need a filename prefix; local directory
        discovery still requires the prefix because unrelated projects share that directory.
        """
        try:
            path = Path(path)
            key = str(path.resolve())
        except (TypeError, ValueError, OSError):
            return
        if (key in candidate_keys or path.suffix.casefold() != ".pdf" or
                not path.is_file() or (not registry_scoped and not in_project(path))):
            return
        normalised_name = __import__("re").sub(r"[-_]+", " ", path.name.casefold())
        if not any(term in normalised_name for term in normalised_spec_terms):
            return
        candidate_keys.add(key)
        candidates.append(path)

    for sibling in sorted(parent.glob("*.pdf")):
        add_detail_candidate(sibling)
    for registered in sorted(project_files or (), key=lambda value: str(value).casefold()):
        add_detail_candidate(registered, registry_scoped=True)

    extracted = []
    for candidate in candidates:
        try:
            candidate_spec = extract_spec(str(candidate), context=target_context)
        except Exception:
            continue
        if (any(key in candidate_spec for key in
                ("depth_mm", "mesh", "layers", "conc_mix", "bay_sizes", "joint_details"))
                or candidate_spec.get("_joint_layout") or candidate_spec.get("_conflicts")):
            extracted.append(candidate_spec)

    if not extracted:
        return None

    merged, evidence, conflicts, flags = {}, {}, {}, []
    for field in ("depth_mm", "mesh", "layers", "conc_mix", "bay_sizes", "joint_details"):
        field_records = []
        for candidate_spec in extracted:
            if candidate_spec.get(field) is not None:
                field_records.append((candidate_spec[field],
                                      (candidate_spec.get("_evidence") or {}).get(field) or {}))
            if field in (candidate_spec.get("_conflicts") or {}):
                conflicts.setdefault(field, []).extend(candidate_spec["_conflicts"][field])
        if field in conflicts:
            # A clean callout on one sheet does not erase unresolved competing callouts on
            # another sheet in the same case. Include it in the review evidence, but confirm
            # nothing until the assessor identifies the applicable build-up.
            conflicts[field].extend(
                {"value": value, **record_evidence}
                for value, record_evidence in field_records
            )
            continue
        distinct = {str(value) for value, _ in field_records}
        if len(distinct) == 1:
            merged[field] = field_records[0][0]
            evidence[field] = field_records[0][1]
        elif len(distinct) > 1:
            conflicts[field] = [
                {"value": value, **record_evidence}
                for value, record_evidence in field_records
            ]

    joint_layouts = [candidate_spec["_joint_layout"] for candidate_spec in extracted
                     if candidate_spec.get("_joint_layout")]
    if joint_layouts:
        merged["_joint_layout"] = joint_layouts[0]
        if "bay_sizes" not in merged:
            layout = joint_layouts[0]
            flags.append(
                "JOINT LAYOUT DETECTED: "
                f"{layout.get('file')} page {layout.get('page') or '?'}; spacing text could "
                "not be extracted reliably — assessor must read/enter the bay sizes."
            )
            merged.setdefault("_field_notes", {})["bay_sizes"] = {
                "source": "joint_layout_detected_unreadable",
                "note": "Joint layout detected, but spacing text could not be extracted; assessor entry required",
                "evidence": layout,
            }
    if conflicts:
        merged["_conflicts"] = conflicts
        # The checklist an assessor reads must not say "ASSUMED / no details provided" about a
        # field the drawing states twice. Inderjit, 4 Sep: "the spec is given on this drawing,
        # but AI is saying spec is not given on engineering drawing." Reuse the field-note
        # channel the joint-layout case already uses: the value stays blank and unpriced, and
        # the line says what the sheet actually says and that the choice is his.
        for field, records in conflicts.items():
            listed = ", ".join(sorted({str(record.get("value")) for record in records},
                                      key=lambda item: (len(item), item)))
            unit = " mm" if field == "depth_mm" else ""
            merged.setdefault("_field_notes", {})[field] = {
                "source": "drawing_states_more_than_one",
                "note": (f"STATED ON THE DRAWING as {listed}{unit} for different surfaces — "
                         "assessor must confirm which applies here (nothing assumed, nothing priced)"),
            }
        # Quote the drawing. Inderjit, 4 Sep: "the spec is given on this drawing, but AI is
        # saying spec is not given on engineering drawing." It IS given — twice, for two
        # different surfaces — so the honest report is what each value says and where, not a
        # bare list of numbers the assessor then has to go and find on the sheet themselves.
        labels = {"depth_mm": "slab thickness", "mesh": "mesh", "layers": "mesh layers",
                  "conc_mix": "concrete mix", "bay_sizes": "bay sizes",
                  "joint_details": "joint details"}
        for field, records in sorted(conflicts.items()):
            seen, parts = set(), []
            for record in records:
                value = record.get("value")
                if value in seen:
                    continue
                seen.add(value)
                quote = " ".join(str(record.get("text") or "").split())
                where = Path(str(record.get("file") or "")).name
                unit = " mm" if field == "depth_mm" else ""
                detail = f"\u201c{quote[:90]}\u2026\u201d" if quote else ""
                if where:
                    detail = f"{detail} [{where}]" if detail else f"[{where}]"
                parts.append(f"{value}{unit}" + (f" {detail}" if detail else ""))
            flags.append(
                f"SPEC CONFLICT — {labels.get(field, field)}: the drawing states "
                + "; and ".join(parts)
                + ". Both are on the sheet, so nothing is assumed for pricing — the assessor "
                  "must select the one that applies to this surface."
            )
    if evidence:
        merged["_evidence"] = evidence
    if flags:
        merged["_flags"] = flags
    source_files = sorted({str(item.get("file")) for item in evidence.values()
                           if item.get("file")})
    merged["_from_file"] = ", ".join(source_files) if source_files else target.name
    return merged


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


def _zone_reference_flags(pdf: str, zones: list[dict]) -> list[str]:
    """Compare known client-sourced zone gold without changing any measured value.

    Gold is documentary evidence, never a calibration knob.  Unknown drawings simply have no
    comparison.  A known mismatch is surfaced for assessor resolution; the markup is not
    rewritten to make it agree with the BOQ.
    """
    try:
        gold_path = Path(__file__).with_name("gold.json")
        gold = json.loads(gold_path.read_text())
        rel = os.path.relpath(str(pdf), str(Path(__file__).parent))
        entry = gold.get(rel)
        if entry is None:
            basename = Path(pdf).name
            matches = [value for key, value in gold.items()
                       if not key.startswith("_") and Path(key).name == basename]
            entry = matches[0] if len(matches) == 1 else None
        if not entry:
            return []

        actual_area = {}
        actual_length = {}
        for zone in zones or []:
            category = zone.get("category")
            if isinstance(zone.get("area_m2"), (int, float)):
                actual_area[category] = actual_area.get(category, 0.0) + float(zone["area_m2"])
            if isinstance(zone.get("length_lm"), (int, float)):
                actual_length[category] = actual_length.get(category, 0.0) + float(zone["length_lm"])

        flags = []
        for values_key, actual, unit in (
                ("zones_m2", actual_area, "m²"),
                ("boq_reference_lm", actual_length, "Lm")):
            tolerance = float(entry.get("zone_tol_pct", entry.get("tol_pct", 2)))
            for category, expected in (entry.get(values_key) or {}).items():
                measured = actual.get(category)
                mismatch = measured is None or (
                    expected and abs(measured - expected) / expected * 100 > tolerance)
                if mismatch:
                    measured_text = "missing" if measured is None else f"{measured:,.2f} {unit}"
                    flags.append(
                        f"assessor: zone-vs-BOQ mismatch — {category} measured {measured_text}; "
                        f"client BOQ reference {expected:,.2f} {unit} (tolerance {tolerance:g}%)"
                    )
        return flags
    except Exception:
        # Reference comparison is advisory and must never crash an otherwise valid takeoff.
        return []


def _trigger_approval(pdf: str, result: dict, vision: dict = None,
                      project_name: str = None, project_ref: str = None,
                      approval_job_id: str = None, send_requested: bool = None):
    """Log/defer review notification; never create a second approval job.

    ``approval_job_id`` is accepted for API compatibility, but delivery belongs to the
    approval-server worker after its completed result is atomically saved.  A direct pipeline
    caller has no authoritative persisted job context and is therefore refused rather than
    allowed to mint a duplicate.
    """
    enabled = SEND_APPROVALS if send_requested is None else bool(send_requested)
    if not enabled:
        ref_s  = f" [#{project_ref}]" if project_ref else ""
        name_s = f" {project_name}"   if project_name else ""
        print(f"[pipeline] Approval needed{ref_s}{name_s} — {result.get('file')} — "
              f"set SEND_APPROVAL_EMAILS=1 to email.  Portal: http://localhost:5001/portal")
        return
    flag = ("APPROVAL EMAIL DEFERRED: direct pipeline job creation is disabled; "
            "approval_server notifies only after the canonical portal job is saved")
    flags = result.setdefault("flags", [])
    if flag not in flags:
        flags.append(flag)
    print(f"[pipeline] {flag} — {result.get('file')}")


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


def manhole_eo_line(manhole_count: int = None, manhole_count_estimate: int = None,
                    rate: float = MANHOLE_EO_RATE):
    """Build the (desc, qty, unit, rate) E/O BOQ line for manhole details, or (None, False)
    if neither a confirmed count nor an estimate is available. Confirmed counts (from the
    MARKED path's Circle markers) take priority and are NOT marked provisional; an
    estimate-only count (unmarked path) is always labelled ESTIMATE so the quotation
    and costing breakdown never present it as authoritative.

    Returns (line, is_estimate) where line is an (desc, qty, unit, rate) BOQ tuple or None."""
    if manhole_count:
        return ("E/O for MH details", manhole_count, "Nr", rate), False
    if manhole_count_estimate:
        return ("E/O for MH details (ESTIMATE — assessor confirm)",
                manhole_count_estimate, "Nr", rate), True
    return None, False


def price_with_defaults(area_m2: float, engineer_spec: dict = None,
                        manhole_count: int = None, manhole_count_estimate: int = None,
                        client_rates_path=None) -> dict:
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
    # Client-editable values are an overlay on the resolved spec.  The defaults and the
    # existing rate calculation stay untouched; an absent/empty store returns no provenance
    # keys so legacy costing output is byte-for-byte unchanged.
    from client_rates import apply_client_rates
    spec, manhole_rate, rates_provenance = apply_client_rates(
        spec, MANHOLE_EO_RATE, path=client_rates_path,
        manhole_in_scope=bool(manhole_count or manhole_count_estimate))
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
    line, is_estimate = manhole_eo_line(
        manhole_count, manhole_count_estimate, rate=manhole_rate)
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

    result = {
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
    result.update(rates_provenance)
    return result


# ── Main takeoff ──────────────────────────────────────────────────────────────

def takeoff(pdf, vision=None, engineer_spec=None, send_approval=None, auto_extract_spec=True,
            project_name: str = None, project_ref: str = None, client_rates_path=None,
            approval_job_id: str = None, project_files=None):
    """
    vision (optional) = {'regions':[[...]], 'voids':{i:[...]}, 'scale_ref':[[x1,y1],[x2,y2],metres]}
    engineer_spec (optional) = dict from construction-detail drawing (depth_mm, mesh, etc.)
    send_approval (optional) = True/False override; defaults to SEND_APPROVAL_EMAILS env var
    auto_extract_spec       = True: scan the drawing pack for a construction-detail PDF and
                              auto-extract the slab spec before falling back to defaults
    project_name (optional) = human-readable project name e.g. "TSL Agratas Battery Facility"
    project_ref  (optional) = Fortel sequential reference number e.g. "2131"
    client_rates_path       = optional explicit client_rates.json store (server/test isolation)
    approval_job_id         = job record the caller already owns; the approval email attaches
                              to it instead of minting a duplicate ghost job
    project_files           = authoritative paths from other jobs sharing project_ref; detail
                              drawings may therefore supply cited spec across upload folders
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
    fast_report_reason = None
    try:
        # Read five bytes before asking MuPDF to inspect the container.  Large ZIP/DWG/Office
        # files can otherwise spend minutes in format probing (or exhaust the child process)
        # even though direct callers may only supply native PDFs.  This is an intake check,
        # not a filename guess: oddly named files with a real %PDF header still proceed.
        with open(pdf, "rb") as _input:
            if _input.read(5) != b"%PDF-":
                raise ValueError("not a native PDF (%PDF header absent) — use portal intake")
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
        fast_report_reason = _fast_report_refusal(pdf, _probe)
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
    if fast_report_reason:
        return {
            "file": Path(pdf).name, "pdf_path": pdf, "page": 0,
            "type": "NON-DRAWING REPORT", "confidence": "high", "method": "fast-refuse",
            "area_m2": None, "measurement_state": UNMEASURED, "status": UNMEASURED,
            "needs_assessor": False,
            "project_name": project_name or "", "project_ref": project_ref or "",
            "flags": [fast_report_reason, "REFUSED: document is not a measurable slab drawing"],
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
        found = find_engineer_spec(
            pdf, project_ref=project_ref, project_files=project_files)
        if found:
            extracted_pricing_fields = {
                key: found[key] for key in ("depth_mm", "mesh", "layers", "conc_mix")
                if found.get(key) is not None
            }
            if extracted_pricing_fields:
                engineer_spec = found
                r["spec_source"] = found.get("_from_file", "auto")
            if found.get("_evidence"):
                r["spec_evidence"] = found["_evidence"]
            if found.get("_field_notes"):
                r["spec_field_notes"] = found["_field_notes"]
            if found.get("_conflicts"):
                r["spec_conflicts"] = found["_conflicts"]
            r["flags"].extend(found.get("_flags") or [])

    # ── Drawing source discipline (engineer vs architect)
    from router import source_discipline
    discipline = source_discipline(pdf)
    if discipline == "architect" and not engineer_spec and not r.get("spec_conflicts"):
        r["flags"].append(
            "ARCHITECT drawing — build-up ASSUMED; no construction-detail sheet found. "
            "State assumptions in quotation (5% area tolerance applies)."
        )
    elif discipline == "architect" and not engineer_spec:
        # Do not tell an assessor nothing was found when the sheet states several values and
        # the choice flag above is already asking them to pick.
        r["flags"].append(
            "ARCHITECT drawing — build-up NOT assumed silently: the pack states more than one "
            "value for a pricing field (see the spec-choice flag). Pricing waits on that choice."
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
            marked = read_marked_zones(meas_pdf)
            area, n = marked["area_m2"], marked["regions"]
            sflags = plausible(area)
            r.update({
                "area_m2": area,
                "regions": n,
                "markup_annotations": marked.get("markup_annotations", []),
                "zones": marked.get("zones", []),
                "exclusions": marked.get("exclusions", []),
                "exclusion_prompts": marked.get("exclusion_prompts", []),
                "unit_group_review_required": bool(
                    marked.get("unit_group_review_required", False)),
            })
            if marked.get("flags"):
                r["flags"] = r["flags"] + marked["flags"]
            if any(zone.get("category") == "unclassified" for zone in r["zones"]):
                # Zone allocation is orthogonal to the four-state measurement contract: the
                # aggregate Bluebeam quantity remains verified, but approval/quotation must
                # wait for an assessor to classify the unknown subject.
                r["zone_classification_required"] = True
            marked_confidence = (
                "low" if marked.get("unit_group_review_required") else conf
            )
            state, sflags2 = measurement_state(
                area, scale_verified=True, confidence=marked_confidence)
            r["flags"] = r["flags"] + sflags + sflags2
            r["measurement_state"] = state
            # Manhole markers (Circle annots Fortel placed) — CONFIRMED count, not an estimate.
            # None = the check itself failed (file/page unreadable) — say so, never treat as 0.
            mh_n = count_manholes_marked(meas_pdf)
            if mh_n is None:
                r["flags"].append("manhole markers could NOT be checked (file/page unreadable) — "
                                  "not a confirmed zero; assessor confirm manhole count")
            elif mh_n > 0:
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
                r["method"] = tu.get("method") or "colour-segmentation (takeoff_unmarked)"
                if tu.get("area_m2") is not None:
                    r.update({
                        "area_m2":        tu["area_m2"],
                        "scale_k":        tu.get("scale_k"),
                        "scale_src":      tu.get("scale_src"),
                        "scale_verified": tu.get("scale_verified", False),
                        "scale_sources":  tu.get("scale_sources", {}),
                        "polygon_pts":    tu.get("polygon_pts"),
                        "zones":          tu.get("zones", []),
                        "zones_total_area_m2": tu.get("zones_total_area_m2"),
                        "segmentation_components": tu.get("segmentation_components", {}),
                        "yard_regions": tu.get("yard_regions", []),
                        "yard_region_review_required": bool(
                            tu.get("yard_region_review_required", False)),
                        "extent_corroborated": tu.get("extent_corroborated"),
                        "extent_corroboration_reason": tu.get(
                            "extent_corroboration_reason"),
                        # Assumed channel runs are tracing/review aids only. They stay outside
                        # measured zones and every costing/quotation total.
                        "channel_proposals": tu.get("channel_proposals", []),
                        # Yard-entrance boundary prefills are likewise assistance only. They
                        # become Transition Lm only after an assessor traces/confirms them.
                        "transition_candidates": tu.get("transition_candidates", []),
                        "exclusion_prompts": tu.get("exclusion_prompts", []),
                        "exclusion_review_required": bool(
                            tu.get("exclusion_review_required", False)),
                        "boundary_precision_risk": tu.get("boundary_precision_risk"),
                        "measurement_mode": tu.get("measurement_mode"),
                        "light_fill_diagnostics": tu.get("light_fill_diagnostics"),
                        "perimeter_measurement_allowed": tu.get(
                            "perimeter_measurement_allowed", True),
                    })
                    r["flags"] = r["flags"] + tu.get("flags", []) + ["assessor: confirm extent + scale"]
                    # A region measured WITHOUT a legend label is a generic grey-hatch guess — its
                    # identity is unconfirmed, so it can never be approvable even if the scale
                    # verifies. Force confidence low in that case so the state machine caps it at
                    # MEASURED_UNVERIFIED (matches the cap inside takeoff_unmarked; the pipeline
                    # re-derives state here with the router's confidence, so the cap must be re-applied).
                    eff_conf = (
                        "low"
                        if (not tu.get("legend_found", True)
                            or tu.get("region_confidence") == "low")
                        else conf
                    )
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
                    # Office GA sheets are commonly line/hatch drawings with several level plans
                    # on one page.  Ordinary closed-vector faces remain trace candidates only.
                    # A narrower, corroborated metal-deck hatch class may emit an assessor-gated
                    # MEASURED_UNVERIFIED estimate; all other office geometry stays quantity-free.
                    try:
                        assisted = {}
                        if tu.get("terminal_measurement_refusal"):
                            r["flags"].append(
                                "STRUCTURAL LIGHT-FILL REFUSAL IS TERMINAL: automatic office "
                                "reinterpretation disabled; assessor trace remains required")
                        else:
                            from office_candidates import detect_office_candidates
                            office_k, office_verified, office_note, office_sources = TU.scale_for(meas_pdf)
                            assisted = detect_office_candidates(
                                meas_pdf,
                                scale_k=office_k,
                                scale_verified=office_verified,
                            )
                        if assisted.get("candidate_polygons"):
                            r.update({
                                "method": "assisted office vector trace",
                                "candidate_polygons": assisted["candidate_polygons"],
                                "exclusion_prompts": assisted.get("exclusion_prompts", []),
                                "scale_k": round(office_k, 6) if office_k else None,
                                "scale_src": office_note,
                                "scale_verified": office_verified,
                                "scale_sources": office_sources,
                            })
                            r["flags"] = r["flags"] + assisted.get("flags", [])
                            office_auto = assisted.get("auto_measurement")
                            if office_auto:
                                # The colour path refused correctly before the independent
                                # office-structure detector ran.  Do not leave its terminal
                                # "no area emitted" wording beside the later gated estimate.
                                r["flags"] = [
                                    flag for flag in r["flags"]
                                    if "no area emitted" not in str(flag).lower()
                                ]
                                r.update({
                                    "method": office_auto["method"],
                                    "area_m2": office_auto["area_m2"],
                                    "regions": len(office_auto["regions"]),
                                    "polygon_pts": office_auto["polygon_pts"],
                                    "perimeter_lm": office_auto.get("perimeter_lm"),
                                    "zones": office_auto["zones"],
                                    "zones_total_area_m2": round(sum(
                                        zone["area_m2"] for zone in office_auto["zones"]), 2),
                                    # This new estimator is intentionally never VERIFIED even
                                    # if the independent scale machinery later verifies scale.
                                    "measurement_state": MEASURED_UNVERIFIED,
                                    "needs_assessor": True,
                                })
                    except Exception as exc:
                        # Candidate assistance is best-effort.  Failure preserves the existing
                        # safe UNMEASURED/manual-trace outcome instead of failing the job.
                        r["flags"].append(
                            f"OFFICE ASSISTED TRACE unavailable ({type(exc).__name__}: {exc}); "
                            "manual assessor trace remains required"
                        )
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
                r["method"] = tu.get("method") or "colour-segmentation on flattened/raster render"
                r.update({
                    "area_m2":        tu["area_m2"],
                    "scale_k":        tu.get("scale_k"),
                    "scale_src":      tu.get("scale_src"),
                    "scale_verified": tu.get("scale_verified", False),
                    "scale_sources":  tu.get("scale_sources", {}),
                    "polygon_pts":    tu.get("polygon_pts"),
                    "zones":          tu.get("zones", []),
                    "zones_total_area_m2": tu.get("zones_total_area_m2"),
                    "segmentation_components": tu.get("segmentation_components", {}),
                    "yard_regions": tu.get("yard_regions", []),
                    "yard_region_review_required": bool(
                        tu.get("yard_region_review_required", False)),
                    "extent_corroborated": tu.get("extent_corroborated"),
                    "extent_corroboration_reason": tu.get(
                        "extent_corroboration_reason"),
                    "channel_proposals": tu.get("channel_proposals", []),
                    "transition_candidates": tu.get("transition_candidates", []),
                    "exclusion_prompts": tu.get("exclusion_prompts", []),
                    "exclusion_review_required": bool(
                        tu.get("exclusion_review_required", False)),
                    "boundary_precision_risk": tu.get("boundary_precision_risk"),
                    "measurement_mode": tu.get("measurement_mode"),
                    "light_fill_diagnostics": tu.get("light_fill_diagnostics"),
                    "perimeter_measurement_allowed": tu.get(
                        "perimeter_measurement_allowed", True),
                })
                r["flags"] = r["flags"] + tu.get("flags", []) + [
                    "flattened/raster drawing measured from the RENDER (no vector geometry) — "
                    "assessor: confirm extent + scale"]
                # Same no-legend cap as the vector path: an unlabelled grey-hatch guess stays
                # MEASURED_UNVERIFIED even if the scale verifies.
                eff_conf = (
                    "low"
                    if (not tu.get("legend_found", True)
                        or tu.get("region_confidence") == "low")
                    else conf
                )
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

    if r.get("zones"):
        reference_flags = _zone_reference_flags(pdf, r["zones"])
        if reference_flags:
            r["flags"] = r["flags"] + reference_flags
            r["zone_reference_mismatch"] = True

    # The estimator works in metres per PDF point; Fortel assessors work in conventional
    # drawing ratios. Carry the exact same scale as a presentation value (1:N), never as a
    # second measurement source and never as a numeric adjustment.
    if r.get("scale_k"):
        from scale import ratio_from_k
        scale_ratio = ratio_from_k(r["scale_k"])
        if scale_ratio:
            r["scale_ratio"] = scale_ratio

    # Informational formwork quantity only: polygon_pts are PDF points and scale_k is m/pt,
    # so closed polygon length × scale_k gives linear metres.  This never enters pricing.
    if (r.get("perimeter_measurement_allowed", True)
            and r.get("perimeter_lm") is None
            and r.get("polygon_pts") and r.get("scale_k")):
        from geometry import polygon_perimeter_lm
        perimeter_lm = polygon_perimeter_lm(r["polygon_pts"], r["scale_k"])
        if perimeter_lm is not None:
            r["perimeter_lm"] = perimeter_lm

    # ── Costing (with defaults where no engineer spec)
    if r.get("area_m2"):
        costing = price_with_defaults(r["area_m2"], engineer_spec,
                                      manhole_count=r.get("manhole_count"),
                                      manhole_count_estimate=r.get("manhole_count_estimate"),
                                      client_rates_path=client_rates_path)
        r["costing"] = costing
        r["flags"] = r["flags"] + costing["flags"]

    # Carry Fortel's client-supplied slab checklist independently from pricing.  Values
    # already used by fallback costing are useful context but remain field-by-field
    # provisional; only fields actually read from an engineer source are confirmed.
    from slab_spec import (COMMON_FIELDS, FIELD_LABELS, build_brief_spec,
                           normalise_slab_type)
    confirmed_spec = {
        key: engineer_spec[key]
        for key in COMMON_FIELDS
        if engineer_spec and engineer_spec.get(key) is not None
    }
    if confirmed_spec:
        # Persist only the client-facing construction fields, not extractor metadata.
        r["engineer_spec"] = dict(confirmed_spec)
    confirmed_brief_spec = {
        key: engineer_spec[key]
        for key in FIELD_LABELS
        if engineer_spec and engineer_spec.get(key) is not None
    }
    effective_spec = (r.get("costing") or {}).get("spec") or {}
    slab_type = normalise_slab_type(
        r.get("quotation_section"),
        text=" ".join(str(r.get(key) or "") for key in ("file", "project_name", "type")),
    )
    r["brief_spec"] = build_brief_spec(
        slab_type,
        effective_spec=effective_spec,
        confirmed=confirmed_brief_spec,
        source="engineer_drawing",
        evidence=r.get("spec_evidence") or {},
        field_notes=r.get("spec_field_notes") or {},
    )

    # A marked drawing may contain several BOQ slab categories.  Carry one checklist per
    # proven category so Yard/Dock/Ground/Upper can be reviewed independently.  A legacy
    # job-level build-up cannot safely be assigned to two different zones (the client's BOQ
    # proves Yard and Dock use different specifications/rates), so mixed-zone checklists start
    # blank and provisional.  Single-zone drawings retain the existing effective context.
    slab_categories = list(dict.fromkeys(
        zone.get("category") for zone in r.get("zones", [])
        if zone.get("category") in ("external_yard", "dock", "ground_floor", "upper_floor")
        and zone.get("area_m2") is not None
    ))
    if slab_categories:
        mixed_zones = len(slab_categories) > 1
        r["brief_specs"] = {
            category: build_brief_spec(
                category,
                effective_spec={} if mixed_zones else effective_spec,
                confirmed={} if mixed_zones else confirmed_brief_spec,
                source="engineer_drawing",
                evidence={} if mixed_zones else (r.get("spec_evidence") or {}),
                field_notes={} if mixed_zones else (r.get("spec_field_notes") or {}),
            )
            for category in slab_categories
        }

    r.setdefault("measurement_state", UNMEASURED if not r.get("area_m2") else MEASURED_UNVERIFIED)
    r.setdefault("needs_assessor", r["measurement_state"] != MEASURED_VERIFIED)
    r["status"] = r["measurement_state"]   # portal/job-record field name

    # ── Approval trigger
    do_send = send_approval if send_approval is not None else SEND_APPROVALS
    if _needs_approval(r) and (do_send or not os.getenv("SKIP_APPROVAL_LOG")):
        _trigger_approval(pdf, r, vision,
                          project_name=project_name, project_ref=project_ref,
                          approval_job_id=approval_job_id, send_requested=do_send)

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
