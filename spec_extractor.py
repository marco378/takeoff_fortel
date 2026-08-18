#!/usr/bin/env python3
"""
Engineer construction-detail spec extractor.

Reads slab build-up directly from the text of a construction-detail / construction-thickness PDF
so the pipeline can use exact specs instead of assumed defaults.

What Inderjit said (verbatim, standup):
  "Look for the drawing named: external construction details — gives slab thickness, concrete
   mix, mesh. e.g. '175 mm thick with A193 mesh', or '200 thick with two layers of A393'
   → straight into costing."

Patterns extracted:
  depth_mm   — "175 mm", "175mm thick", "190 mm slab", "200 mm concrete"
  mesh       — "A142", "A193", "A252", "A393", "B785"
  layers     — "2 layers", "two layers", "single layer", "double layer"
  conc_mix   — "C30/37", "C32/40", "C35/45", "C40/50"

Usage:
  from spec_extractor import extract_spec, extract_spec_from_text
  spec = extract_spec("drawings/RBVE_construction_details.pdf")
  # returns: {"depth_mm": 175, "mesh": "A193", "layers": 1, "conc_mix": "C32/40"}
  # or {}  if nothing found

Run standalone to test:
  python3 spec_extractor.py drawings/some_construction_details.pdf
"""
import re, fitz
from collections import Counter, defaultdict
from pathlib import Path

# ── Mesh codes (from MESH_KG in costing.py) ──────────────────────────────────
VALID_MESH = {"A142", "A193", "A252", "A393", "B785"}

# ── Regex patterns ────────────────────────────────────────────────────────────
_DEPTH_RX = re.compile(
    r"\b(\d{2,3})\s*(?:mm|millimeter|millimetre)(?:\s*(?:thick|thk|dp|deep|slab|concrete|reinf))?",
    re.I)

_MESH_RX = re.compile(
    r"\b(A142|A193|A252|A393|B785)\b", re.I)

_MIX_RX = re.compile(
    r"\b(C\s*(?:25|30|32|35|40|45)/(?:30|37|40|45|50))\b", re.I)

_LAYERS_RX = re.compile(
    r"\b(two|2|double|dual)\s+layers?\s+(?:of\s+)?[AB]\d{3}"
    r"|[AB]\d{3}\s+x\s*2"           # "A393 x2" (from the real BOQ)
    r"|\b2\s*layers?\s+(?:of\s+)?[AB]\d{3}",
    re.I)

_SINGLE_LAYER_RX = re.compile(
    r"\b(?:one|1|single)\s+layer", re.I)

_BAY_SIZES_RX = re.compile(
    r"(?:maximum|max\.?\s*)?\s*"
    r"(?P<a>\d{1,2}(?:[.,]\d+)?)\s*(?:m|metres?|meters?)?\s*"
    r"(?:x|by|×)\s*"
    r"(?P<b>\d{1,2}(?:[.,]\d+)?)\s*(?:m|metres?|meters?)"
    r"(?:\s*(?:centres?|centers?|c/c))?",
    re.I,
)

_JOINT_LAYOUT_RX = re.compile(r"\bjoint\s+layout\b", re.I)
_NON_SLAB_DEPTH_RX = re.compile(
    r"(?:sub[- ]?base|base\s+course|insulation|cover|membrane|kerb|upstand|"
    r"ground\s+improvement|aggregate|diameter|spacing|centres?|centers?|\bcrs\b|"
    r"anchor(?:ed|age)?|embed(?:ded|ment)?)",
    re.I,
)

# Slab-context words — helps filter depth readings that are clearly NOT slab thickness
_SLAB_CONTEXT = re.compile(
    r"(?:slab|concrete|pavement|surfac|thick|thk|construction|reinforce|mesh|mix)", re.I)

# Plausible depth range for a service-yard slab (mm)
_MIN_DEPTH, _MAX_DEPTH = 100, 400

def _clean_snippet(text: str, start: int, end: int, radius: int = 150) -> str:
    """Short, human-readable source evidence around one extracted field."""
    return " ".join(text[max(0, start - radius): min(len(text), end + radius)].split())[:360]


def _candidate_clause(text: str, start: int, end: int) -> str:
    """Return the logical sentence containing one candidate, not a layout line.

    Construction details often place a print-check (for example ``150 mm when printed``)
    immediately beside the real slab build-up.  Applying that warning to a broad character
    window suppresses both numbers.  Clause-local gating ties the warning to the dimension it
    actually qualifies while the broader window remains available for positive slab context.
    PDF text extraction inserts newlines at arbitrary drawing-text wraps, including between a
    number and ``ground improvement``; only sentence punctuation is a defensible boundary here.
    """
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start))
    right_candidates = [position for position in (
        text.find(".", end), text.find(";", end)
    ) if position >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1:right]


def _is_print_dimension(text: str, start: int, end: int, value: int) -> bool:
    """True only when the candidate itself belongs to a print/scale check.

    A broad ``scale bar`` window also catches the real build-up printed in the next sentence.
    Conversely, PDF line wrapping means the qualifier may not share a physical text line.  The
    negative lookahead below permits ordinary words/newlines but stops at any intervening mm
    dimension, tying the warning to this candidate rather than a later one.
    """
    lead = " ".join(text[max(0, start - 140):start].split())
    trail = " ".join(text[end:min(len(text), end + 100)].split())
    no_other_mm = r"(?:(?!\b\d{2,3}\s*mm\b).)"
    lead_is_print = re.search(
        rf"(?:scale\s*bar|check.{{0,80}}(?:scale|measure)|\bmeasure)"
        rf"{no_other_mm}{{0,90}}$",
        lead, re.I,
    )
    trail_is_print = re.match(
        rf"^{no_other_mm}{{0,60}}(?:when\s+printed|printed\s+scale|measure\s+correct)",
        trail, re.I,
    )
    if lead_is_print or trail_is_print:
        return True
    # Common printed calibration graphic. Only its terminal 100 mm candidate is in the slab
    # depth range, so a later 190/205 mm build-up in the same sentence is unaffected.
    return value == 100 and bool(re.search(
        r"\b0\s*mm.{0,100}\b100\s*mm", _candidate_clause(text, start, end), re.I | re.S))


def _context_terms(context: str | None) -> set[str]:
    words = re.findall(r"[a-z]{3,}", str(context or "").casefold())
    stop = {"drawing", "layout", "proposed", "plan", "detail", "details", "rev", "pdf"}
    return {word for word in words if word not in stop}


def _candidate(field, value, score, text, start, end, *, source_name, page_number, basis):
    return {
        "field": field,
        "value": value,
        "score": score,
        "file": source_name or "provided text",
        "page": page_number,
        "text": _clean_snippet(text, start, end),
        "basis": basis,
    }


def _extract_candidates(text: str, *, source_name=None, page_number=None, context=None) -> dict:
    """Extract evidence candidates without deciding that a conflicting value is true.

    A tender detail sheet can contain a Yard slab, a bin-store slab, sub-base depths and a
    printed scale bar on one page.  Keeping candidates separate lets the selector refuse a
    tie instead of turning the first/most-common number into an apparently confirmed spec.
    """
    out = defaultdict(list)
    context_words = _context_terms(context)

    for match in _DEPTH_RX.finditer(text):
        value = int(match.group(1))
        if not (_MIN_DEPTH <= value <= _MAX_DEPTH):
            continue
        tight = text[max(0, match.start() - 55): min(len(text), match.end() + 75)]
        tight_normalised = " ".join(tight.split())
        local = text[max(0, match.start() - 220): min(len(text), match.end() + 240)]
        explicit_thickness = bool(re.search(
            rf"(?:\b{value}\s*mm\s*(?:thick(?:ness)?|thk|deep|slab|concrete)|"
            rf"(?:thick(?:ness)?|slab)\s*(?:of\s*)?\b{value}\s*mm)",
            tight_normalised, re.I,
        ))
        slab_near = bool(_SLAB_CONTEXT.search(tight_normalised))
        score = 8 if explicit_thickness else (3 if slab_near else 0)
        score += min(3, len(_SLAB_CONTEXT.findall(local)))
        score += min(4, sum(1 for term in context_words if term in local.casefold()))
        strong_slab_callout = bool(re.search(
            rf"(?:(?:concrete|pavement)\s+slab[^.]{{0,45}}\b{value}\s*mm|"
            rf"\b{value}\s*mm\s*(?:thick(?:ness)?|thk|deep)[^.]{{0,35}}(?:slab|concrete)|"
            rf"(?:minimum|min\.?\s*)\s+thickness\s+of\s+\b{value}\s*mm)",
            tight_normalised, re.I,
        ))
        if _is_print_dimension(text, match.start(), match.end(), value):
            score -= 30
        if _NON_SLAB_DEPTH_RX.search(tight_normalised) and not strong_slab_callout:
            score -= 30
        if score < 5:
            continue
        out["depth_mm"].append(_candidate(
            "depth_mm", value, score, text, match.start(), match.end(),
            source_name=source_name, page_number=page_number,
            basis="explicit slab thickness wording" if explicit_thickness
                  else "depth adjacent to slab/concrete wording",
        ))

    for match in _MESH_RX.finditer(text):
        value = match.group(1).upper()
        if value not in VALID_MESH:
            continue
        local = text[max(0, match.start() - 180): min(len(text), match.end() + 200)]
        score = 4 + min(3, len(_SLAB_CONTEXT.findall(local)))
        score += min(4, sum(1 for term in context_words if term in local.casefold()))
        out["mesh"].append(_candidate(
            "mesh", value, score, text, match.start(), match.end(),
            source_name=source_name, page_number=page_number,
            basis="mesh code printed on drawing",
        ))

    for match in _MIX_RX.finditer(text):
        value = match.group(1).replace(" ", "").upper()
        local = text[max(0, match.start() - 180): min(len(text), match.end() + 200)]
        score = 4 + min(3, len(_SLAB_CONTEXT.findall(local)))
        score += min(4, sum(1 for term in context_words if term in local.casefold()))
        out["conc_mix"].append(_candidate(
            "conc_mix", value, score, text, match.start(), match.end(),
            source_name=source_name, page_number=page_number,
            basis="concrete strength class printed on drawing",
        ))

    layer_matches = []
    layer_matches.extend((match, 2) for match in _LAYERS_RX.finditer(text))
    layer_matches.extend((match, 1) for match in _SINGLE_LAYER_RX.finditer(text))
    for match, value in layer_matches:
        out["layers"].append(_candidate(
            "layers", value, 10, text, match.start(), match.end(),
            source_name=source_name, page_number=page_number,
            basis="explicit reinforcement layer count",
        ))

    for match in _BAY_SIZES_RX.finditer(text):
        a = float(match.group("a").replace(",", "."))
        b = float(match.group("b").replace(",", "."))
        # Bay/joint layouts are in metres.  Reject likely small component dimensions rather
        # than silently calling them bay spacing.
        if not (1 <= a <= 30 and 1 <= b <= 30):
            continue
        local = text[max(0, match.start() - 160): min(len(text), match.end() + 180)]
        if not re.search(r"joint|bay|centres?|centers?|spacing", local, re.I):
            continue
        value = f"{a:g} m x {b:g} m centres"
        out["bay_sizes"].append(_candidate(
            "bay_sizes", value, 10, text, match.start(), match.end(),
            source_name=source_name, page_number=page_number,
            basis="joint/bay spacing printed on drawing",
        ))

    return dict(out)


def _select_candidates(candidates: dict) -> tuple[dict, dict, dict]:
    """Return (values, evidence, conflicts), refusing near-tied conflicting values."""
    values, evidence, conflicts = {}, {}, {}
    for field, records in candidates.items():
        if not records:
            continue
        grouped = defaultdict(list)
        for record in records:
            grouped[record["value"]].append(record)
        ranked = []
        for value, value_records in grouped.items():
            best = max(value_records, key=lambda item: item["score"])
            # Repeated identical callouts are corroboration, capped so repetition cannot
            # overpower a materially stronger, target-specific statement.
            rank = best["score"] + min(2, len(value_records) - 1)
            ranked.append((rank, value, best, len(value_records)))
        ranked.sort(key=lambda item: (-item[0], str(item[1])))
        top = ranked[0]
        if len(ranked) > 1 and ranked[1][0] >= top[0] - 1:
            conflicts[field] = [
                {"value": item[1], "file": item[2].get("file"),
                 "page": item[2].get("page"), "text": item[2].get("text")}
                for item in ranked
            ]
            continue
        values[field] = top[1]
        evidence[field] = {key: value for key, value in top[2].items()
                           if key not in ("field", "value", "score")}
    return values, evidence, conflicts

# ── Supplier inquiry spec patterns (from Amarvir standup 24 Jun 2026) ────────

# Aggregate size — "20mm aggregate", "20 mm aggregate", "20mm crushed", "12mm agg"
_AGG_RX = re.compile(
    r"\b(\d{1,2})\s*mm\s+(?:crushed\s+)?(?:aggregate|agg|gravel)\b", re.I)

# W/C ratio — "0.45 w/c", "w/c ratio 0.45", "water cement ratio 0.45", "w/c=0.45"
_WC_RX = re.compile(
    r"(?:w(?:ater)?[/\-]?c(?:ement)?\s*(?:ratio)?\s*[=:]\s*|water[- ]cement ratio\s+)(0\.\d+)"
    r"|(0\.\d+)\s*w(?:ater)?[/\-]?c(?:ement)?",
    re.I)

# Slump class — "S3", "S4" in a concrete context
_SLUMP_RX = re.compile(r"\b(S[34])\b")

# Air-entrained — "air entrained", "air-entrained", "AE"
_AIR_RX = re.compile(r"\bair[- ]?entrained\b|\bA\.?E\.?\b", re.I)

# Cement type — "CEM I", "CEM II", "SRPC", "OPC", "GGBS"
_CEMENT_RX = re.compile(
    r"\b(CEM\s*I{1,3}(?:[A-Z/\d\-]+)?|SRPC|OPC|GGBS|Portland)\b", re.I)


def extract_spec_from_text(text: str, *, source_name=None, page_number=None,
                           context=None) -> dict:
    """
    Extract slab spec from raw text (string).
    Returns a dict with any subset of: depth_mm, mesh, layers, conc_mix.
    """
    candidates = _extract_candidates(
        text, source_name=source_name, page_number=page_number, context=context)
    spec, evidence, conflicts = _select_candidates(candidates)
    if evidence:
        spec["_evidence"] = evidence
    if conflicts:
        spec["_conflicts"] = conflicts
    if _JOINT_LAYOUT_RX.search(f"{source_name or ''}\n{text}"):
        spec["_joint_layout"] = {
            "detected": True,
            "file": source_name or "provided text",
            "page": page_number,
            "spacing_extracted": "bay_sizes" in spec,
        }

    # ── supplier inquiry fields ───────────────────────────────────────────────

    # aggregate size
    agg_hits = [int(m.group(1)) for m in _AGG_RX.finditer(text)]
    if agg_hits:
        spec["aggregate_mm"] = Counter(agg_hits).most_common(1)[0][0]

    # w/c ratio
    for m in _WC_RX.finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            spec["wc_ratio"] = float(val)
            break

    # slump class
    slump_hits = [m.group(1).upper() for m in _SLUMP_RX.finditer(text)]
    if slump_hits:
        spec["slump_class"] = Counter(slump_hits).most_common(1)[0][0]

    # air-entrained
    if _AIR_RX.search(text):
        spec["air_entrained"] = True

    # cement type
    cement_hits = [m.group(1) for m in _CEMENT_RX.finditer(text)]
    if cement_hits:
        spec["cement_type"] = Counter(cement_hits).most_common(1)[0][0].upper()

    return spec


def extract_spec(pdf_path: str, pages: list = None, *, context=None) -> dict:
    """
    Extract slab spec from a PDF (all pages by default, or the specified page list).
    Tries text extraction first; if sparse (scanned), returns {} with a note.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {"_error": str(e)}

    page_range = pages if pages is not None else range(doc.page_count)
    full_text = ""
    all_candidates = defaultdict(list)
    joint_layout_pages = []
    for i in page_range:
        if i < doc.page_count:
            page_text = doc[i].get_text() or ""
            full_text += page_text + "\n"
            page_candidates = _extract_candidates(
                page_text, source_name=Path(pdf_path).name,
                page_number=i + 1, context=context)
            for field, records in page_candidates.items():
                all_candidates[field].extend(records)
            if _JOINT_LAYOUT_RX.search(f"{Path(pdf_path).name}\n{page_text}"):
                joint_layout_pages.append(i + 1)

    if len(full_text.strip()) < 50 and not any(all_candidates.values()):
        return {"_note": "sparse/scanned PDF — spec must be entered manually"}

    spec, evidence, conflicts = _select_candidates(dict(all_candidates))
    if evidence:
        spec["_evidence"] = evidence
    if conflicts:
        spec["_conflicts"] = conflicts
    if joint_layout_pages:
        spec["_joint_layout"] = {
            "detected": True,
            "file": Path(pdf_path).name,
            "page": joint_layout_pages[0],
            "pages": joint_layout_pages,
            "spacing_extracted": "bay_sizes" in spec,
        }
    spec["_source"] = str(Path(pdf_path).name)
    return spec


def describe_spec(spec: dict) -> str:
    """Human-readable summary of an extracted spec."""
    parts = []
    if "depth_mm" in spec:
        parts.append(f"{spec['depth_mm']} mm")
    if "mesh" in spec:
        if "layers" in spec:
            parts.append(f"{spec['layers']}× {spec['mesh']} mesh")
        else:
            parts.append(f"{spec['mesh']} mesh (layers not provided)")
    if "conc_mix" in spec:
        parts.append(spec["conc_mix"])
    if not parts:
        return "no spec extracted — will use defaults"
    return " / ".join(parts)


# ── Standalone test / demo ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json

    if len(sys.argv) > 1:
        pdf = sys.argv[1]
        print(f"Extracting spec from: {pdf}")
        spec = extract_spec(pdf)
        print(json.dumps(spec, indent=2))
        print("Summary:", describe_spec(spec))
    else:
        print("Spec extractor — text-based tests\n")
        cases = [
            ("175 mm thick with A193 mesh, C32/40 concrete",
             {"depth_mm": 175, "mesh": "A193", "conc_mix": "C32/40"}),
            ("200mm slab with two layers of A393 reinforcement C35/45",
             {"depth_mm": 200, "mesh": "A393", "layers": 2, "conc_mix": "C35/45"}),
            ("250 mm C40/50 concrete slab with A393 x2",
             {"depth_mm": 250, "mesh": "A393", "layers": 2, "conc_mix": "C40/50"}),
            ("190mm thick concrete, A252 mesh, C32/40",
             {"depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40"}),
            ("CONCRETE SERVICE YARD\n150 mm slab B785 single layer mix C30/37",
             {"depth_mm": 150, "mesh": "B785", "layers": 1, "conc_mix": "C30/37"}),
            ("No specification provided",
             {}),
        ]
        passed = 0
        for text, expected in cases:
            got = extract_spec_from_text(text)
            ok = got == expected
            passed += ok
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {text[:55]!r}")
            if not ok:
                for k, v in expected.items():
                    if got.get(k) != v:
                        print(f"         expected {k}={v!r}  got {got.get(k)!r}")
        print(f"\n{passed}/{len(cases)} PASS")
