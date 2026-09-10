"""Closed CAD boundaries offered as CANDIDATES, because the sheet cannot say which is yours.

Some drawings carry the pavement extents as closed polylines on named layers. On the Skanska
Equinix sheet LDSS2-01-DR-C-151 the layer ``Zz_20_40_60-M_Onsite_Dock_(Rigid)`` holds nine
stroke items that chain into ONE loop closing to 0.00 pt, enclosing 1,113.97 m2 at the
sheet's 1:250 -- against the client's own traced markup of 1,114.55 m2, IoU 0.9924, with the
residual amounting to an 1.8 cm band around a 227 m perimeter. That is the width of the drawn
line. Where this signal exists it is the best geometry in the codebase: an outline the
engineer drew, not one reconstructed from a stipple, so it carries no "AREA IS A MINIMUM".

WHY THIS MODULE OFFERS AND NEVER DECIDES. The shape is exact; the IDENTITY is not available.
Measured on LDSS2:

  * The Key names the surface "P1 Heavy Duty Concrete Build-up", but that row's chip is drawn
    on the catch-all layer ``RWO-DRAINAGE AREA 4`` (10,432 items spanning the whole plan), so
    the chip cannot name its layer the way it does on 2105.
  * The chip-PATTERN trick fails too. Comparing like-for-like on short marks only:
        ground (dock apron)     len med 0.00  p90 2.17  spacing 1.62
        P1 Heavy Duty Concrete  len med 0.00  p90 0.58  spacing 0.47
        P9 Asphalt Paving       len med 0.00  p90 0.00  spacing 2.34
    Mark length rules out the Landscaping and Block Paving chips, but P1 and P9 are BOTH pure
    dots. Spacing cannot break the tie: a chip is drawn dense so it reads inside a small box,
    so its spacing never transfers to the plan (2105: chip 3.08 pt against ground 2.06 pt).

The tempting close is a rule that "Rigid" means concrete and "Flexible" means asphalt. It is
standard civil terminology and it would make this sheet measure today. It is also exactly the
vocabulary leap that substituted a grey band and put 6,510 m2 of car park into a quotation,
and the client asked for the opposite on 10 Sep 2026: "I don't want accuracy improved by
introducing rules that could cause unrelated surfaces such as car parks, roads, or footways
to be included."

He also said what to do instead, and this module does that and nothing more: "If there are
ambiguous areas, show them as separate candidates for the user to review rather than simply
rejecting the whole measurement." So every qualifying loop is returned as a candidate, named
by its own layer, with its exact area -- and NONE is included. A quantity appears only when a
human ticks one. That is why a wrong guess here cannot reach a price.

MEASURED ACROSS THE CORPUS, and the reason "biggest loop wins" is not a shortcut: the signal
exists on about a dozen drawings, but the LARGEST closed loop on a surface-named layer is the
carriageway on LDSS2 itself, a building floor slab on the Tanro and PRP sheets, isolation
joints on another and a kerbline on Indurent. Only about four land on the priced surface.
"""

from __future__ import annotations

import math
import re

# Words a drawing office uses for ground worth pricing. Matching is deliberately broad --
# breadth is safe here BECAUSE nothing is ever included automatically.
SURFACE_WORDS = ("yard", "apron", "dock", "rigid", "flexible", "concrete", "hardstand",
                 "slab", "pavement", "paving", "surfacing", "carriageway", "footway",
                 "carpark", "car park", "permeable", "tactile")
# ...but never an underlay. A tender sheet carries an illustrative masterplan X-Ref, OS
# mapping, a topographic survey; their lines are not this engineer's statement about this
# scheme. On 2105 the masterplan X-Ref supplied 278 of 709 "kerb" lines.
NOT_OUR_DRAWING = ("x-ref", "xref", "illustrative", "masterplan", "topo", "os mapping",
                   "survey", "external reference")
# Joints, kerbs and setting-out lines close into loops too, and they are not the surface.
NOT_A_SURFACE = ("joint", "kerbline", "kerb line", "setting out", "grid", "isolation")

CHAIN_TOL_PT = 1.0        # endpoints this close are the same point
CLOSE_TOL_PT = 1.0        # a loop must return to its start within this
MIN_AREA_M2 = 50.0        # smaller than this is a detail, not a surface to price
MAX_LOOPS_PER_LAYER = 40  # a layer with hundreds of tiny loops is a hatch, not a boundary
BEZIER_SAMPLES = 24       # flatten curves; the CONTROL HULL over-reads a convex corner


def _flatten(op):
    """Points for one path op, with cubics SAMPLED rather than hulled.

    Pushing a cubic's four control points into a polygon uses the control hull, which lies
    OUTSIDE the arc at a convex corner. On LDSS2's two rounded corners that inflated the
    dock loop by 23.9 m2 and turned a 0.05% under-read into a spurious 2.09% OVER -- the
    wrong direction, and the sort of number that costs a client's trust. Always flatten.
    """
    kind = op[0]
    if kind == "l":
        return [(op[1].x, op[1].y), (op[2].x, op[2].y)]
    if kind == "c":
        p0, p1, p2, p3 = op[1], op[2], op[3], op[4]
        pts = [(p0.x, p0.y)]
        for i in range(1, BEZIER_SAMPLES + 1):
            t = i / BEZIER_SAMPLES
            u = 1.0 - t
            pts.append((u * u * u * p0.x + 3 * u * u * t * p1.x
                        + 3 * u * t * t * p2.x + t * t * t * p3.x,
                        u * u * u * p0.y + 3 * u * u * t * p1.y
                        + 3 * u * t * t * p2.y + t * t * t * p3.y))
        return pts
    if kind == "re":
        r = op[1]
        return [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)]
    return []


def _chain(runs, tol=CHAIN_TOL_PT):
    """Join open runs end-to-end into the longest chains they will form."""
    runs = [list(r) for r in runs if len(r) >= 2]
    used = [False] * len(runs)
    out = []
    for i in range(len(runs)):
        if used[i]:
            continue
        used[i] = True
        cur = runs[i][:]
        moved = True
        while moved:
            moved = False
            for j in range(len(runs)):
                if used[j]:
                    continue
                a, b = runs[j][0], runs[j][-1]
                e = cur[-1]
                if (e[0] - a[0]) ** 2 + (e[1] - a[1]) ** 2 <= tol * tol:
                    cur += runs[j]
                    used[j] = True
                    moved = True
                elif (e[0] - b[0]) ** 2 + (e[1] - b[1]) ** 2 <= tol * tol:
                    cur += runs[j][::-1]
                    used[j] = True
                    moved = True
        out.append(cur)
    return out


def _shoelace(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _looks_like_a_surface(name):
    low = str(name).lower()
    if not any(w in low for w in SURFACE_WORDS):
        return False
    if any(w in low for w in NOT_OUR_DRAWING):
        return False
    if any(w in low for w in NOT_A_SURFACE):
        return False
    return True


def find(page, k, rot=None):
    """Closed boundaries on surface-named layers, in RENDERED PDF points.

    Returns a list of dicts sorted by area, each with the layer that carries the loop, its
    exact enclosed area, and its outline. Never decides which one is the priced surface --
    see the module docstring for why that is not available from the sheet.
    """
    if not k or k <= 0:
        return []
    rot = page.rotation_matrix if rot is None else rot
    by_layer = {}
    for d in page.get_drawings():
        name = d.get("layer") or ""
        if not _looks_like_a_surface(name):
            continue
        pts = []
        for op in d.get("items", ()):
            seg = _flatten(op)
            if seg:
                pts.extend(seg)
        if len(pts) >= 2:
            by_layer.setdefault(name, []).append(pts)

    found = []
    for name, runs in by_layer.items():
        if len(runs) > 400:                       # a hatch pattern, not a boundary
            continue
        loops = _chain(runs)
        if len(loops) > MAX_LOOPS_PER_LAYER:
            continue
        for loop in loops:
            if len(loop) < 8:
                continue
            gap = math.hypot(loop[0][0] - loop[-1][0], loop[0][1] - loop[-1][1])
            if gap > CLOSE_TOL_PT:
                continue
            area_m2 = _shoelace(loop) * k * k
            if area_m2 < MIN_AREA_M2:
                continue
            outline = []
            for x, y in loop:
                p = _point(x, y, rot)
                outline.append([round(p[0], 3), round(p[1], 3)])
            found.append({
                "layer": name,
                "short_name": re.split(r"[|]", str(name))[-1][:60],
                "area_m2": round(area_m2, 1),
                "closing_gap_pt": round(gap, 3),
                "vertices": len(loop),
                "polygon_pts": outline,
            })
    found.sort(key=lambda r: -r["area_m2"])
    return found


def _point(x, y, rot):
    import fitz
    p = fitz.Point(x, y) * rot
    return p.x, p.y
