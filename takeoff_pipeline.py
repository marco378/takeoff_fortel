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
"""
import math, json, io, contextlib, fitz
from router import classify
from robust_takeoff import read_marked
from geometry import measure_regions
from scale import detect_scale_bar, user_unit
with contextlib.redirect_stdout(io.StringIO()):       # costing self-validates on import; mute its receipt
    from costing import rate_buildup, MESH_KG


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


def takeoff(pdf, vision=None):
    """vision (optional) = {'regions':[[...]], 'voids':{i:[...]}, 'scale_ref':[[x1,y1],[x2,y2],metres]}"""
    typ, route, conf, _ = classify(pdf)
    r = {"file": pdf.split("/")[-1], "type": typ, "confidence": conf, "method": route, "flags": []}
    if typ == "MARKED vector":
        area, n = read_marked(pdf)
        r.update({"area_m2": area, "regions": n})
    elif typ == "UNMARKED vector" and vision:
        uu = user_unit(pdf)
        if vision.get("scale_ref"):
            sr = vision["scale_ref"]; k = sr[2] / math.dist(sr[0], sr[1]) * uu; ksrc = "vision scale_ref"
        else:
            kb, info = detect_scale_bar(pdf); k = (kb * uu) if kb else None; ksrc = f"auto scale-bar: {info}"
        if k is None:
            r["flags"] = ["no scale (no scale_ref, no detectable bar) -> assessor must supply scale"]
        else:
            area, gflags = measure_regions(vision["regions"], k, vision.get("voids"))
            r.update({"area_m2": area, "scale_k": round(k, 4), "scale_src": ksrc,
                      "flags": gflags + ["assessor: confirm extent + scale"]})
    else:
        r["flags"] = ["needs vision {regions, voids, scale_ref}; raster/flattened -> mandatory human"]
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
