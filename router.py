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


def drawing_priority(name, text=""):
    """Score a drawing's suitability for the concrete takeoff. From the Fortel call: use the
    CONSTRUCTION / external-works / surfacing / kerbing drawing (it carries the concrete-vs-tarmac
    legend) — NOT a 'Proposed Site Plan' / location plan. Pick the highest-scoring sheet in a pack."""
    s = (str(name) + " " + str(text)).lower()
    score = 0
    for w in ("construction", "external works", "external surfacing", "surfacing", "kerb",
              "thickness", "-dr-c-", "-dr-ce-", "pavement", "hardstanding"):
        if w in s:
            score += 2
    for w in ("proposed site plan", "site plan", "location plan", "site layout"):
        if w in s:
            score -= 2
    return score


if __name__ == "__main__":
    for f in sorted(glob.glob("drawings/*.pdf")):
        t, route, conf, meta = classify(f)
        print(f"• {os.path.basename(f)[:52]}")
        print(f"    type={t}  conf={conf}")
        print(f"    route: {route}")
        print(f"    {meta}\n")
