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

# Slab-context words — helps filter depth readings that are clearly NOT slab thickness
_SLAB_CONTEXT = re.compile(
    r"(?:slab|concrete|pavement|surfac|thick|thk|construction|reinforce|mesh|mix)", re.I)

# Plausible depth range for a service-yard slab (mm)
_MIN_DEPTH, _MAX_DEPTH = 100, 400

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


def extract_spec_from_text(text: str) -> dict:
    """
    Extract slab spec from raw text (string).
    Returns a dict with any subset of: depth_mm, mesh, layers, conc_mix.
    """
    spec = {}

    # ── depth_mm ─────────────────────────────────────────────────────────────
    depth_candidates = []
    for m in _DEPTH_RX.finditer(text):
        d = int(m.group(1))
        if _MIN_DEPTH <= d <= _MAX_DEPTH:
            # Give higher score if slab-context words are nearby (±200 chars)
            ctx = text[max(0, m.start()-200): m.end()+200]
            score = len(_SLAB_CONTEXT.findall(ctx))
            depth_candidates.append((score, d))
    if depth_candidates:
        # Pick the candidate with the most slab-context signals
        depth_candidates.sort(reverse=True)
        spec["depth_mm"] = depth_candidates[0][1]

    # ── mesh ─────────────────────────────────────────────────────────────────
    meshes_found = [m.group(1).upper() for m in _MESH_RX.finditer(text)
                    if m.group(1).upper() in VALID_MESH]
    if meshes_found:
        # Most common mesh code wins (handles repeated mentions)
        from collections import Counter
        spec["mesh"] = Counter(meshes_found).most_common(1)[0][0]

    # ── layers ───────────────────────────────────────────────────────────────
    if _LAYERS_RX.search(text):
        spec["layers"] = 2
    elif _SINGLE_LAYER_RX.search(text):
        spec["layers"] = 1
    elif "mesh" in spec:
        spec["layers"] = 1   # default if mesh is mentioned but layers aren't

    # ── concrete mix ─────────────────────────────────────────────────────────
    mixes = [m.group(1).replace(" ", "").upper() for m in _MIX_RX.finditer(text)]
    if mixes:
        from collections import Counter
        spec["conc_mix"] = Counter(mixes).most_common(1)[0][0]

    # ── supplier inquiry fields ───────────────────────────────────────────────

    # aggregate size
    agg_hits = [int(m.group(1)) for m in _AGG_RX.finditer(text)]
    if agg_hits:
        from collections import Counter
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
        from collections import Counter
        spec["slump_class"] = Counter(slump_hits).most_common(1)[0][0]

    # air-entrained
    if _AIR_RX.search(text):
        spec["air_entrained"] = True

    # cement type
    cement_hits = [m.group(1) for m in _CEMENT_RX.finditer(text)]
    if cement_hits:
        from collections import Counter
        spec["cement_type"] = Counter(cement_hits).most_common(1)[0][0].upper()

    return spec


def extract_spec(pdf_path: str, pages: list = None) -> dict:
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
    for i in page_range:
        if i < doc.page_count:
            full_text += doc[i].get_text() + "\n"

    if len(full_text.strip()) < 50:
        return {"_note": "sparse/scanned PDF — spec must be entered manually"}

    spec = extract_spec_from_text(full_text)
    spec["_source"] = str(Path(pdf_path).name)
    return spec


def describe_spec(spec: dict) -> str:
    """Human-readable summary of an extracted spec."""
    parts = []
    if "depth_mm" in spec:
        parts.append(f"{spec['depth_mm']} mm")
    if "mesh" in spec:
        layers = spec.get("layers", 1)
        parts.append(f"{layers}× {spec['mesh']} mesh")
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
             {"depth_mm": 175, "mesh": "A193", "layers": 1, "conc_mix": "C32/40"}),
            ("200mm slab with two layers of A393 reinforcement C35/45",
             {"depth_mm": 200, "mesh": "A393", "layers": 2, "conc_mix": "C35/45"}),
            ("250 mm C40/50 concrete slab with A393 x2",
             {"depth_mm": 250, "mesh": "A393", "layers": 2, "conc_mix": "C40/50"}),
            ("190mm thick concrete, A252 mesh, C32/40",
             {"depth_mm": 190, "mesh": "A252", "layers": 1, "conc_mix": "C32/40"}),
            ("CONCRETE SERVICE YARD\n150 mm slab B785 single layer mix C30/37",
             {"depth_mm": 150, "mesh": "B785", "layers": 1, "conc_mix": "C30/37"}),
            ("No specification provided",
             {}),
        ]
        passed = 0
        for text, expected in cases:
            got = extract_spec_from_text(text)
            ok = all(got.get(k) == v for k, v in expected.items())
            passed += ok
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {text[:55]!r}")
            if not ok:
                for k, v in expected.items():
                    if got.get(k) != v:
                        print(f"         expected {k}={v!r}  got {got.get(k)!r}")
        print(f"\n{passed}/{len(cases)} PASS")
