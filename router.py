#!/usr/bin/env python3
"""
Input router — classify WHATEVER Fortel receives and pick the takeoff path.
Fortel receives unmarked architect/engineer vector PDFs (confirmed from the tender pack:
25026-HFR-... GA Plan, Site Location Plan, Civil & Structural Spec). It marks them up itself.
"""
import fitz, glob, os, re


def classify(path):
    p = fitz.open(path)[0]
    annots = list(p.annots() or [])
    area_markups = sum(1 for a in annots
                       if a.type[1] == "Polygon" and "sq m" in (a.info.get("content", "") or ""))
    vec = len(p.get_drawings())
    has_red = any(dr.get("color") and dr["color"][0] > 0.5 and dr["color"][1] < 0.45 and dr["color"][2] < 0.45
                  for dr in p.get_drawings())
    scales = sorted(set(re.findall(r"1\s*:\s*\d{2,4}", p.get_text())))

    if vec < 50:
        typ, route, conf = "RASTER / scanned", "CV + OCR + scale-bar detect; MANDATORY human check", "low"
    elif area_markups > 0:
        typ, route, conf = "MARKED vector", "read Bluebeam area annotations (EXACT)", "high"
    else:
        typ, route, conf = "UNMARKED vector", "per-viewport scale + vision-proposed boundary + assessor confirm", "medium"
    return typ, route, conf, dict(vector_paths=vec, area_markups=area_markups,
                                  site_boundary=has_red, scales=scales)


# Designer-folder → discipline. From the standup: "SGP is the architect; if BWB is the engineer they
# name the folder BWB." Engineer (civil & structural) drawings are PREFERRED; architect is the fallback.
ENGINEER_HINTS = ("civil", "structural", "civil and structural", "civil & structural", "bwb",
                  "-dr-c-", "-dr-ce-", "-dr-s-", "engineer")
ARCHITECT_HINTS = ("architect", "sgp", "hard landscaping", "landscaping", "-dr-a-")

# Drawing-name keywords the estimator dictated, in the order he searches them.
AREA_KEYWORDS = ("external surfacing", "external pavements", "external pavement", "external works",
                 "external construction thickness", "construction thickness layout",
                 "construction thickness", "surfacing", "hardstanding", "hard landscaping",
                 "kerb", "pavement", "external construction")
DETAIL_KEYWORDS = ("external construction details", "construction details", "build-up", "buildup",
                   "typical detail", "slab detail")
DEPRIORITISE = ("proposed site plan", "site plan", "location plan", "site layout",
                "roof plan", "elevation", "section", "boundary treatment", "critical areas")


def source_discipline(path_or_name):
    """Classify a drawing/folder as 'engineer' (preferred) or 'architect' (fallback → assume build-up)."""
    s = str(path_or_name).lower()
    if any(h in s for h in ENGINEER_HINTS):
        return "engineer"
    if any(h in s for h in ARCHITECT_HINTS):
        return "architect"
    return "unknown"


def drawing_priority(name, text="", source=None):
    """Score a drawing's suitability for the concrete takeoff. Fortel's dictated order:
    ENGINEER civil/structural first → the external surfacing / pavements / works / construction-thickness
    sheet (it carries the concrete-vs-tarmac legend) → architect hard-landscaping as fallback.
    A 'site plan' / location plan is last resort. `source` ('engineer'/'architect') tilts the score."""
    s = (str(name) + " " + str(text)).lower()
    score = 0
    for w in AREA_KEYWORDS:
        if w in s:
            score += 2
    for w in ("construction", "thickness", "-dr-c-", "-dr-ce-"):
        if w in s:
            score += 1
    for w in DEPRIORITISE:
        if w in s:
            score -= 2
    src = source or source_discipline(name)
    if src == "engineer":
        score += 3          # engineer drawing preferred (exact build-up available)
    elif src == "architect":
        score -= 1          # usable, but ~5% tolerance + assumed build-up
    return score


def buildup_source(name, text="", source=None):
    """Is there a construction-details sheet (engineer) giving thickness/mesh, or must we ASSUME
    (architect)?  Returns ('detail'|'assume', note)."""
    s = (str(name) + " " + str(text)).lower()
    if any(w in s for w in DETAIL_KEYWORDS) and (source or source_discipline(name)) != "architect":
        return "detail", "construction-details sheet present — read thickness/mesh for costing"
    return "assume", ("architect/no construction details — ASSUME build-up (e.g. 190mm/A252) and state "
                      "the assumption in the quote; ~5% area tolerance vs an engineer drawing")


if __name__ == "__main__":
    for f in sorted(glob.glob("drawings/*.pdf")):
        t, route, conf, meta = classify(f)
        print(f"• {os.path.basename(f)[:52]}")
        print(f"    type={t}  conf={conf}")
        print(f"    route: {route}")
        print(f"    {meta}\n")
