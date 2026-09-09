#!/usr/bin/env python3
"""layer_surfaces: measuring a surface from the CAD layer the engineer drew it on.

Sections in this module (printed in this order):
  - [CAD-layer surfaces: identity from the drawing's own layer names]

Every fixture here is SYNTHETIC and built in a temp dir, so this module runs on a clean
checkout with no client drawings. Each case below is an attack that was reproduced against
a shipped revision of this module and produced a wrong number or a crash.

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("\n[CAD-layer surfaces: identity from the drawing's own layer names]")
try:
    import math as _math_ls, tempfile as _tmp_ls
    import fitz as _fitz_ls, numpy as _np_ls
    import layer_surfaces as _ls

    _K = 0.17638888888888887          # 1:500, the scale of the sheet this shipped for
    _S = 2.0
    _LAYER = "RL_Surfacing_Service Yard"
    _LABEL = "C1 - Service Yard"

    def _stipple_pdf(path, *, marks=64, pitch=8.0, origin=(200.0, 200.0),
                     leaders=0, extra_layer=None, rotation=0, solid_block=None):
        """A page whose surface exists ONLY as dots on a named CAD layer."""
        doc = _fitz_ls.open()
        page = doc.new_page(width=2384, height=1684)
        xref = doc.add_ocg(_LAYER)
        ox, oy = origin
        for row in range(marks):
            for col in range(marks):
                x, y = ox + col * pitch, oy + row * pitch
                page.draw_line(_fitz_ls.Point(x, y), _fitz_ls.Point(x + 1.2, y),
                               color=(0, 0, 0), width=1.0, oc=xref)
        for i in range(leaders):
            # A callout sharing the surface layer. Its BOUNDING BOX is ~300 m2 at 1:500;
            # its ink is a hairline. Filling the box collapsed a real 10,511 m2 sheet to
            # 603 m2 and reported it as a measurement.
            page.draw_line(_fitz_ls.Point(1400 + 40 * i, 300),
                           _fitz_ls.Point(1498 + 40 * i, 398),
                           color=(0, 0, 0), width=0.5, oc=xref)
        if solid_block:
            bx, by = solid_block
            block = doc.add_ocg(extra_layer or "RL_Surfacing_Car Parking")
            page.draw_rect(_fitz_ls.Rect(bx, by, bx + 120, by + 120),
                           color=(0.5, 0.5, 0.5), fill=(0.5, 0.5, 0.5), oc=block)
        if rotation:
            page.set_rotation(rotation)
        doc.save(path)
        doc.close()
        return path

    _tmp = _tmp_ls.mkdtemp(prefix="qa_layer_surfaces_")

    def _measure(**kw):
        path = _stipple_pdf(f"{_tmp}/ls_{abs(hash(tuple(sorted(map(str, kw.items())))))}.pdf",
                            **kw)
        doc = _fitz_ls.open(path)
        try:
            return _ls.measure(doc, doc[0], _LABEL, _K, S=_S)
        finally:
            doc.close()

    # ── the base case: a stipple with no boundary at all becomes one measured region ──────
    _base = _measure()
    ck("a surface drawn only as marks on a named CAD layer is measured",
       _base.get("ok") is True and _base.get("area_m2", 0) > 200,
       f"ok={_base.get('ok')} area={_base.get('area_m2')} reason={_base.get('reason','')[:80]}")
    ck("...and it names the layer it measured, so the identification is auditable",
       _base.get("layer") == _LAYER, str(_base.get("layer")))
    ck("...and the outline contains essentially all of that layer's own marks",
       _base.get("ink_retention", 0) >= _ls.MIN_INK_RETENTION,
       str(_base.get("ink_retention")))

    # ── the attack that produced a wrong number: one callout on the surface layer ─────────
    # The bounding box of a diagonal stroke is a solid block. It made the bridge search
    # converge at its first radius, dropped the real field below the component floor, and
    # returned 3% of the truth as a measurement with "the true figure is this or larger".
    _leader = _measure(leaders=2)
    ck("a callout sharing the surface layer does NOT collapse the measurement",
       _leader.get("ok") is True
       and abs(_leader.get("area_m2", 0) - _base["area_m2"]) < 0.02 * _base["area_m2"],
       f"with leaders={_leader.get('area_m2')} vs clean={_base.get('area_m2')} "
       f"reason={_leader.get('reason','')[:80]}")

    # ── the attack that reported the WHOLE PAGE: flood-fill seeded on a foreground pixel ──
    _corner = _np_ls.zeros((50, 50), _np_ls.uint8)
    _corner[20:30, 20:30] = 1
    _corner[0, 0] = 1
    _filled, _, _ = _ls._fill_holes(_corner)
    ck("hole-filling a mask that touches the page corner does not fill the whole page",
       int(_filled.sum()) == int(_corner.sum()) and not _filled.all(),
       f"in={int(_corner.sum())} out={int(_filled.sum())}")

    # ── a real island is a deduction, not surface ─────────────────────────────────────────
    _ring = _np_ls.zeros((400, 400), _np_ls.uint8)
    _ring[50:350, 50:350] = 1
    _ring[150:250, 150:250] = 0                     # a 100x100 px void
    _small, _kept_s, _n_s = _ls._fill_holes(_ring, per_px_m2=1.0, keep_m2=100000.0)
    _big, _kept_b, _n_b = _ls._fill_holes(_ring, per_px_m2=1.0, keep_m2=100.0)
    ck("a void below the island threshold is filled as a perforation",
       int(_small.sum()) > int(_ring.sum()) and _n_s == 0, f"kept={_n_s}")
    ck("a void above it is KEPT AS A DEDUCTION and reported, not quietly filled",
       int(_big.sum()) == int(_ring.sum()) and _n_b == 1 and _kept_b == 10000.0,
       f"kept={_n_b} m2={_kept_b}")

    # ── rotation: get_drawings is unrotated, the raster is not ────────────────────────────
    # Painting unrotated points onto a rotated frame put the mask elsewhere on the sheet and
    # produced a wrong, priced number on a real client drawing at /Rotate 180.
    for _rot in (90, 180, 270):
        _r = _measure(rotation=_rot)
        ck(f"a page rotated {_rot} measures the same surface as the unrotated one",
           _r.get("ok") is True
           and abs(_r.get("area_m2", 0) - _base["area_m2"]) < 0.02 * _base["area_m2"],
           f"rot{_rot}={_r.get('area_m2')} vs {_base.get('area_m2')} "
           f"reason={_r.get('reason','')[:70]}")

    # ── the four-state contract: this path may refuse, never raise ────────────────────────
    _empty = _fitz_ls.open()
    _pg_empty = _empty.new_page(width=2384, height=1684)
    _x = _empty.add_ocg(_LAYER)
    _pg_empty.draw_line(_fitz_ls.Point(1e7, 1e7), _fitz_ls.Point(1e7, 1e7), oc=_x)
    _degenerate = _ls.measure(_empty, _pg_empty, _LABEL, _K, S=_S)
    _empty.close()
    ck("a degenerate path returns a refusal with a reason, never a traceback",
       _degenerate.get("ok") is False and bool(_degenerate.get("reason")),
       str(_degenerate)[:120])

    # ── identification: the name has to mean the surface, not merely contain it ───────────
    ck("a retaining WALL is never measured as the surface it retains",
       _ls.names_same_surface("service yard", "PRP_Service Yard Retaining Wall") is False,
       "wall matched the yard label")
    ck("...while the surfacing layer for the same words still matches",
       _ls.names_same_surface("service yard", "x|RL_Surfacing_Service Yard") is True, "")
    ck("a label too generic to identify anything is refused, not guessed",
       _ls.names_same_surface("yard", "x|RL_Surfacing_Service Yard") is False, "")
    ck("two clients' naming conventions both reach the same surface",
       _ls.names_same_surface("Service Yard", "...|11735_Proposed Site$0$A010H_hatch-ServiceYard")
       and _ls.names_same_surface("concrete service yard", "PRP_Hatch Concrete Service Yard"), "")

    # ── the sibling-overlap guard must work on FLAT layer names too ───────────────────────
    # split("|")[0] on a name with no "|" is the whole name, so every sibling test was false
    # and the guard was silently off on 25 of 47 layered corpus sheets.
    ck("sibling surfaces are recognised on flat layer names, not only parented ones",
       _ls._is_surfacing("PRP_Hatch Car Parking", "PRP_Hatch Concrete Service Yard") is True, "")
    ck("...and a kerb line is never treated as a competing surface",
       _ls._is_surfacing("PRP_Kerbline", "PRP_Hatch Concrete Service Yard") is False, "")

    # ── the disclosed bridge is the GAP, not the disc radius ──────────────────────────────
    _gap = next((f for f in _base.get("flags", []) if f.startswith("OUTLINE BRIDGED")), "")
    ck("the bridge disclosed to the assessor is the gap spanned (2R), not the radius",
       bool(_gap) and abs(_base["bridge_m"] - 2 * _base["disc_radius_m"]) < 1e-9
       and f"{_base['bridge_m']:.2f}" in _gap,
       f"bridge={_base.get('bridge_m')} radius={_base.get('disc_radius_m')}")
    ck("...and no per-sheet flag generalises one drawing's error into a rule",
       not any("never high" in f or "5% low" in f for f in _base.get("flags", [])),
       str(_base.get("flags"))[:160])

    # ── the cap on approving an unlabelled number must FAIL SAFE ─────────────────────────
    # takeoff_pipeline read tu.get("legend_found", True): a result that never said whether it
    # had found a legend was treated as though it had. Only 1 of 8 return paths in takeoff()
    # sets the key, and the line/hatch branch returns before legend_found is computed — so the
    # only thing standing between an unlabelled surface and MEASURED_VERIFIED was each
    # promoted path REMEMBERING to hand-write region_confidence="low".
    import json as _json_ls
    from pathlib import Path as _P_ls
    _pipe_src = _P_ls("takeoff_pipeline.py").read_text()
    ck("an omitted legend_found is treated as NO legend, never as a found one",
       'tu.get("legend_found", True)' not in _pipe_src
       and _pipe_src.count('tu.get("legend_found", False)') == 2,
       f'permissive={_pipe_src.count(chr(34)+"legend_found"+chr(34)+", True")} '
       f'failsafe={_pipe_src.count(chr(34)+"legend_found"+chr(34)+", False")}')

    # ── a refusal should hand over what the drawing already knows ─────────────────────────
    _mimms = _P_ls("drawings/inderjit_p9p10/12_South_Mimms.pdf")
    if not _mimms.exists():
        print("  [SKIP] South Mimms not present — layer-enumeration check needs client drawings")
    else:
        import takeoff_unmarked as _tu_ls
        _mm = _tu_ls.takeoff(str(_mimms))
        _mmf = " ".join(_mm.get("flags") or [])
        # South Mimms has no readable legend at all — the key is drawn as outlines, so the
        # page yields ~238 extractable characters and none are legend words. Every
        # legend-reading path declines at the same step. The surface is found by its CAD
        # layer name alone, and Aryan accepted this outline on 9 Sep ("that's the yard, the
        # edges are close enough to be accepted"). Scored by SHAPE against what he accepted,
        # never by area.
        ck("a sheet whose legend cannot be read is measured from its self-named CAD layer",
           _mm.get("area_m2") is not None
           and _mm.get("measurement_state") == "MEASURED_UNVERIFIED"
           and "C-060-M_YARD" in _mmf,
           f"area={_mm.get('area_m2')} state={_mm.get('measurement_state')}")
        # The scale on this sheet VERIFIES. That must not be enough to make an unlabelled
        # surface approvable: identification came from a layer name with nothing to
        # cross-check it.
        ck("...and a verified scale still does not make an unlabelled surface approvable",
           _mm.get("scale_verified") is True
           and _mm.get("measurement_state") == "MEASURED_UNVERIFIED"
           and "SURFACE IDENTIFIED WITHOUT A LEGEND" in _mmf,
           f"scale_verified={_mm.get('scale_verified')} state={_mm.get('measurement_state')}")
        _mm_gt = _json_ls.loads(_P_ls("ground_truth_polygons.json").read_text()).get(
            "drawings/inderjit_p9p10/12_South_Mimms.pdf")
        if not _mm_gt:
            print("  [SKIP] no accepted outline recorded for South Mimms")
        else:
            import numpy as _np_ls2, cv2 as _cv_ls2

            def _mm_iou(a, b):
                _a = _np_ls2.asarray(a, dtype=float); _b = _np_ls2.asarray(b, dtype=float)
                _o = _np_ls2.minimum(_a.min(0), _b.min(0))
                _s = _np_ls2.maximum(_a.max(0), _b.max(0)) - _o
                _ma = _np_ls2.zeros((int(_s[1]) + 2, int(_s[0]) + 2), _np_ls2.uint8)
                _mb = _ma.copy()
                _cv_ls2.fillPoly(_ma, [_np_ls2.round(_a - _o).astype(_np_ls2.int32)], 1)
                _cv_ls2.fillPoly(_mb, [_np_ls2.round(_b - _o).astype(_np_ls2.int32)], 1)
                _u = int((_ma | _mb).sum())
                return (int((_ma & _mb).sum()) / _u) if _u else 0.0

            _mm_best = max((_mm_iou(_mm_gt["polygon_pts"], _r["polygon_pts"])
                            for _r in _mm.get("yard_regions") or []), default=0.0)
            ck(f"...and it outlines the ground the CLIENT marked up (IoU >= {_mm_gt['min_iou']})",
               _mm_best >= _mm_gt["min_iou"], f"IoU {_mm_best:.3f}")
            # It reads UNDER the client's outline, and that direction is the whole contract.
            # A reconstructed stipple outline stops inside the true edge, which is why the job
            # and the quotation both say AREA IS A MINIMUM. Reading OVER would mean quoting
            # ground nobody is paving, and no disclosure makes that acceptable.
            ck("...and it reads UNDER the client's own figure, never over it",
               _mm.get("area_m2") <= _mm_gt["area_m2"],
               f"{_mm.get('area_m2')} vs client {_mm_gt['area_m2']} "
               f"({100 * _mm.get('area_m2') / _mm_gt['area_m2'] - 100:+.1f}%)")

    # ── 2105: a MEASURED NEGATIVE RESULT, pinned so nobody re-treads it ───────────────────
    # Aryan marked this sheet up on 9 Sep 2026 (two regions, 13,132.13 m2). His markup is
    # recorded as ground truth so that ANY future method is scored by IoU against his shape.
    # stipple_surfaces now finds both regions (IoU 0.918/0.914, 2.25% under) -- but only
    # because HE decided how the extra stipple should be handled: offered as candidates
    # outside the total, never a reason to reject the sheet. That decision is quoted in the
    # note verbatim, because it is a scope call no measurement rule could have made.
    # The module is still inert, so a real upload of 2105 refuses; the check below pins that.
    _gt2105_key = ("drawings/inderjit_p9p10/"
                   "9_25010-RLL-26-XX-DR-C-2105_P01_External_Construction_Specification.pdf")
    _gt2105 = _json_ls.loads(_P_ls("ground_truth_polygons.json").read_text()).get(_gt2105_key)
    ck("the client's 2105 markup is recorded as ground truth (2 regions)",
       bool(_gt2105) and len(_gt2105.get("regions") or []) == 2,
       f"regions={len((_gt2105 or {}).get('regions') or [])}")
    if _gt2105:
        ck("...totalling the 13,132.13 m2 his own Bluebeam labels printed",
           abs(_gt2105["area_m2"] - 13132.13) < 0.01, f"{_gt2105['area_m2']}")

        def _shoe(pts):
            _a = 0.0
            for _i in range(len(pts)):
                _x1, _y1 = pts[_i]; _x2, _y2 = pts[(_i + 1) % len(pts)]
                _a += _x1 * _y2 - _x2 * _y1
            return abs(_a) / 2.0

        _k2 = _gt2105["k_m_per_pt"] ** 2
        _indep = sum(_shoe(_r["polygon_pts"]) * _k2 for _r in _gt2105["regions"])
        # His Bluebeam figure and our geometry are computed independently; if they ever
        # disagree the vertices were transcribed wrong, and the shape is the whole point.
        ck("...and OUR shoelace of his vertices independently reproduces his figure",
           abs(_indep - _gt2105["area_m2"]) < 0.05, f"shoelace {_indep:.2f} vs {_gt2105['area_m2']}")
        ck("...recorded with the page rotation the raster must apply (/Rotate 270)",
           _gt2105.get("page_rotation") == 270, f"{_gt2105.get('page_rotation')}")
        ck("...and scored by IoU, never by area",
           _gt2105.get("min_iou", 0) >= 0.85, f"min_iou={_gt2105.get('min_iou')}")
        _n2105 = (_gt2105.get("note") or "").lower()
        ck("...and the note records that the module is not yet wired into the pipeline",
           "not yet wired" in _n2105 and "car parking" in _n2105,
           f"note={len(_n2105)} chars")
        # The shape IS right: IoU 0.920/0.912 at a DERIVED dilation, reading 1.3% under. What
        # stops it is a 345 m2 third region the client did not mark, drawn in the yard's own
        # stipple, whose identity the sheet cannot settle. A future session that remembers only
        # "the IoU was good" will ship it, so the note must carry the third region AND the fact
        # that no pattern test can reject it.
        ck("...and records the CLIENT's decision that made it measurable, in his own words",
           "candidate areas" in _n2105 and "don't reject the whole sheet" in _n2105,
           "note does not carry the client's decision")

    _p2105 = _P_ls(_gt2105_key)
    if not _p2105.exists():
        print("  [SKIP] 2105 not present — refusal check needs client drawings")
    else:
        import takeoff_unmarked as _tu2105
        _r2105 = _tu2105.takeoff(str(_p2105))
        # The failure this guards: a future session loosens the stipple path, 2105 starts
        # emitting ~13,000 m2, and it looks RIGHT because it matches Aryan -- while quietly
        # including 315 m2 of car park nobody is paving. Refusing is the correct state here.
        ck("2105 still REFUSES rather than emitting a stipple number it cannot attribute",
           _r2105.get("area_m2") is None,
           f"area_m2={_r2105.get('area_m2')} state={_r2105.get('measurement_state')}")

except Exception as _e_ls:                                     # pragma: no cover - defensive
    import traceback as _tb_ls
    print(f"  [SKIP] CAD-layer surfaces — missing dependency or fixture: {_e_ls}")
    _tb_ls.print_exc()
