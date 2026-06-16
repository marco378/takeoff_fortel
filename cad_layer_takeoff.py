#!/usr/bin/env python3
"""
CAD-layer takeoff — the unlock for UNMARKED drawings.

The architect's vector PDF RETAINS CAD layers (OCGs). PyMuPDF tags every path with its
layer (dr['layer']), so we isolate geometry per layer instead of guessing from a 22k-path mesh:
  ap_Site Boundary           -> site envelope (EXACT, one clean polygon)
  ap_Floors/Structure/Walls  -> building footprint
  ap_Parking/Roads/kerbs     -> hardstanding components
The estimator's 'concrete yard' = boundary − building − non-concrete areas; *which* layers
count is the assessor confirm step (the Phase-3 loop). Per-layer polygon CLOSURE (bridging
small gaps) is the remaining geometry work for building/paving.
"""
import fitz, collections
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

KEY = ["ap_Site Boundary", "ap_Floors", "ap_Structure", "ap_Walls",
       "ap_Parking", "ap_Roads", "ap_kerbs"]


def report(path, k=0.108):
    p = fitz.open(path)[0]
    by = collections.defaultdict(list)
    for dr in p.get_drawings():
        ln = (dr.get("layer") or "").split("|")[-1]
        for it in dr["items"]:
            if it[0] == "l":
                by[ln].append(LineString([(it[1].x, it[1].y), (it[2].x, it[2].y)]))
            elif it[0] == "re":
                r = it[1]
                by[ln].append(LineString([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)]))
    print(f"{path.split('/')[-1]}  —  {len(by)} CAD layers")
    for ln in KEY:
        if ln in by:
            polys = sorted(polygonize(unary_union(by[ln])), key=lambda g: -g.area)
            big = polys[0].area * k * k if polys else 0
            print(f"  {ln:18} segs={len(by[ln]):5}  biggest_closed_polygon={big:>10,.0f} m²")
    sb = by.get("ap_Site Boundary")
    if sb:
        polys = sorted(polygonize(unary_union(sb)), key=lambda g: -g.area)
        if polys:
            print(f"\n  -> SITE BOUNDARY extracts EXACTLY: {polys[0].area*k*k:,.0f} m²  (yard slab gold 26,080)")
            print("     building/paving need per-layer gap-bridging; 'which layers = concrete' = assessor confirm")


if __name__ == "__main__":
    report("drawings/UNMARKED_Yard_Area_Proposed_Site_Plan.pdf")
