#!/usr/bin/env python3
"""
REAL takeoff on the marked-up Winvic drawings (v3).

Finding from v1/v2: a single A0 sheet carries multiple viewports at different scales,
so there is NO single sheet scale. The area markup's own Bluebeam measure is calibrated
per-markup and is authoritative. We therefore:
  - report measured area = sum of the polygon markups' stored measures (authoritative),
  - run an INDEPENDENT geometric cross-check using the dimension-line / label scale, and
    flag the sheet as mixed-scale when the geometric value disagrees (needs per-viewport calib).
"""
import fitz, glob, os, re, math
from shapely.geometry import Polygon

PT2M = 0.0254 / 72
GOLD = {"Yard Area": 26080, "Dock Slab": 930}


def pts(v):
    return [(p[0], p[1]) for p in v]


def line_endpoints(doc, a):
    typ, val = doc.xref_get_key(a.xref, "L")
    if typ == "array":
        n = [float(x) for x in re.findall(r"-?\d+\.?\d*", val)]
        if len(n) == 4:
            return (n[0], n[1]), (n[2], n[3])
    return None


def parse(fn):
    doc = fitz.open(fn); p = doc[0]
    lines, polys = [], []
    for a in (p.annots() or []):
        t, c, v = a.type[1], (a.info.get("content", "") or ""), a.vertices
        if t == "Line":
            m = re.search(r"([\d.]+)\s*m", c); ep = line_endpoints(doc, a)
            if m and ep:
                lines.append((float(m.group(1)), math.dist(ep[0], ep[1])))
        elif t == "Polygon" and v:
            m = re.search(r"A\s*=\s*([\d,]+\.?\d*)\s*sq m", c)
            if m:
                polys.append((float(m.group(1).replace(",", "")), Polygon(pts(v)).area))
    labels = [int(s) for s in re.findall(r"1\s*:\s*(\d{2,4})", p.get_text())]
    return lines, polys, labels


print(f"{'drawing':46}{'polys':>6}{'measured m²':>13}{'gold':>8}{'err%':>7}  geometry cross-check")
for fn in sorted(glob.glob("drawings/*.pdf")):
    base = os.path.basename(fn)
    if "synthetic" in base or "Preliminary" in base:
        continue
    lines, polys, labels = parse(fn)
    if not polys:
        print(f"{base[:46]:46}{'0':>6}   no area markups"); continue

    measured = sum(s for s, _ in polys)                 # authoritative
    area_pt = sum(a for _, a in polys)
    gold = next((v for kk, v in GOLD.items() if kk in base), None)
    err = abs(measured - gold) / gold * 100 if gold else None

    # independent geometric check
    if lines:
        ks = sorted(r / pt for r, pt in lines if pt); kmed = ks[len(ks) // 2]; csrc = f"{len(lines)} dim-lines"
    elif labels:
        s = max(set(labels), key=labels.count); kmed = PT2M * s; csrc = f"label 1:{s}"
    else:
        kmed = None; csrc = "n/a"
    geom = area_pt * kmed * kmed if kmed else None
    poly_scale = round(math.sqrt(measured / area_pt) / PT2M)   # scale implied by the markup itself
    if geom and abs(geom - measured) / measured > 0.05:
        check = f"MIXED-SCALE: {csrc}->1:{round(kmed/PT2M)} gives {geom:,.0f}; markup is 1:{poly_scale} (per-viewport calib needed)"
    elif geom:
        check = f"CONFIRMS: {csrc} 1:{round(kmed/PT2M)} -> {geom:,.0f} m² (independent geometry agrees ✅)"
    else:
        check = "no independent reference"
    print(f"{base[:46]:46}{len(polys):>6}{measured:>13,.0f}{str(gold or '-'):>8}"
          f"{('%.2f'%err) if err is not None else '-':>7}  {check}")

print("\n=== yard dimension-line scales (evidence of mixed-scale sheet) ===")
lines, polys, _ = parse("drawings/Yard Area Proposed_Site_Plan.pdf")
for real, pt in lines:
    print(f"  line {real:>7.2f} m  ->  1:{round((real/pt)/PT2M)}")
print(f"  yard polygon markup itself -> 1:{round(math.sqrt(polys[0][0]/polys[0][1])/PT2M)}")
