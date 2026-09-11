#!/usr/bin/env python3
"""D77 gold, manhole counting, refusals, hatch surfaces, dock aprons.

Sections in this module (printed in this order):
  - D77 accuracy invariant (measurement math unchanged)
  - D77 border/legend exclusion (Aryan field report: real SGP sheet over-measures by border strips + legend swatch that share the yard's grey)
  - manhole counting — MARKED path (robust_takeoff.count_manholes_marked)
  - manhole counting — UNMARKED path (takeoff_unmarked.detect_manholes, conservative ESTIMATE)
  - refuse-instead-of-guess guard — non-slab sheets must REFUSE, not emit a garbage area
  - hatch-drawn surfaces (MJM 9000 class: slanted legend hatching, not a solid fill)
  - dock apron slabs on a joint-layout sheet (0720 class: slab named by build-up, no yard label)
  - manhole E/O costing line (costing.py Winvic rate: £75.00/Nr)

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import _FixtureNotPresent, _require_fixture, ck
from sanity import MEASURED_UNVERIFIED, MEASURED_VERIFIED

print("D77 accuracy invariant (measurement math unchanged)")
try:
    _require_fixture("drawings/_int_d77.pdf", "D77 accuracy test")
    from takeoff_unmarked import takeoff as _tu_takeoff
    _d77 = _tu_takeoff("drawings/_int_d77.pdf")
    ck("D77 area unchanged at 3,159 m² (Smita gold 3,156)", _d77.get("area_m2") == 3159.0)
    ck("D77 scale verified True (bar agrees with title via scale_consensus)",
       _d77.get("scale_verified") is True)
    # 11 Sep: both were VERIFIED / needs_assessor False. This sheet's legend chip cannot be
    # read, so its surface IDENTITY comes from the SGP grey constant -- a guess about which
    # colour is the priced surface. Extent corroboration below still passes, but that proves
    # the region is a real drawn shape, never that it is the RIGHT shape. A guessed identity
    # is now gated like its sibling branches instead of being certified. The AREA is the
    # invariant this block exists to protect and it has not moved.
    ck("D77 measurement_state MEASURED_UNVERIFIED — identity came from the grey constant",
       _d77.get("measurement_state") == MEASURED_UNVERIFIED, _d77.get("measurement_state"))
    ck("D77 needs_assessor True — a guessed surface colour must reach a human",
       _d77.get("needs_assessor") is True, _d77.get("needs_assessor"))
    ck("D77 has independent native-boundary extent corroboration (shape real, identity still guessed)",
       _d77.get("extent_corroborated") is True and
       all(region.get("perimeter_confidence") == "high"
           for region in _d77.get("yard_regions", []) if region.get("included")),
       {"extent_corroborated": _d77.get("extent_corroborated"),
        "regions": _d77.get("yard_regions")})

    # Corpus-backed confidence regression for the live 575 vs 1,212 failure class. Keep the
    # real D77 pixels, legend and verified scale, but remove only the independent native-boundary
    # corroboration. The candidate area must remain unchanged while VERIFIED is withheld.
    import takeoff_unmarked as _tu_extent_guard
    _real_native_drawings_extent = _tu_extent_guard._native_boundary_drawings
    try:
        _tu_extent_guard._native_boundary_drawings = lambda page: (
            [], "regression fixture: native extent unavailable")
        _d77_uncorroborated = _tu_extent_guard.takeoff("drawings/_int_d77.pdf")
        from takeoff_pipeline import takeoff as _pipeline_extent_guard
        _d77_uncorroborated_pipeline = _pipeline_extent_guard(
            "drawings/_int_d77.pdf", send_approval=False, auto_extract_spec=False)
    finally:
        _tu_extent_guard._native_boundary_drawings = _real_native_drawings_extent
    ck("uncorroborated raw extent preserves the measured candidate number",
       _d77_uncorroborated.get("area_m2") == _d77.get("area_m2") == 3159.0,
       {"corroborated": _d77.get("area_m2"),
        "uncorroborated": _d77_uncorroborated.get("area_m2")})
    ck("verified scale + legend alone cannot promote a partial raw extent to VERIFIED",
       _d77_uncorroborated.get("scale_verified") is True and
       _d77_uncorroborated.get("extent_corroborated") is False and
       _d77_uncorroborated.get("measurement_state") == MEASURED_UNVERIFIED and
       _d77_uncorroborated.get("needs_assessor") is True and
       any("YARD EXTENT UNCORROBORATED" in flag
           for flag in _d77_uncorroborated.get("flags", [])),
       {"state": _d77_uncorroborated.get("measurement_state"),
        "flags": _d77_uncorroborated.get("flags")})
    ck("pipeline preserves the raw extent confidence cap",
       _d77_uncorroborated_pipeline.get("extent_corroborated") is False and
       _d77_uncorroborated_pipeline.get("measurement_state") == MEASURED_UNVERIFIED and
       _d77_uncorroborated_pipeline.get("needs_assessor") is True,
       {"state": _d77_uncorroborated_pipeline.get("measurement_state"),
        "extent": _d77_uncorroborated_pipeline.get("extent_corroborated")})
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 accuracy test — missing dependency or file: {_e}")

print("D77 border/legend exclusion (Aryan field report: real SGP sheet over-measures by "
      "border strips + legend swatch that share the yard's grey)")
try:
    _require_fixture("drawings", "D77 border/legend exclusion test")
    import fitz as _fitz_b
    from takeoff_unmarked import takeoff as _tu_takeoff2, segment_hatch as _seg_b

    def _gen_d77_borders(out_path, with_borders):
        """Rebuild _int_d77.pdf's exact yard rect + scale bar (same geometry, so the
        measured area is directly comparable), optionally adding:
          - a grey sheet-frame border strip running around the full page edge
            (same fill colour as the yard hatch — this is what a real SGP sheet's
            outer frame line looks like when rendered to raster and colour-segmented)
          - a small grey legend swatch rectangle near the title block (isolated,
            far from the yard, same grey) — mimics a legend colour chip.
        WITHOUT the fix these must inflate the measured area; WITH the fix
        (segment_hatch exclude_border=True, default) the result must match
        plain _int_d77.pdf (3,159 m²) within 0.5%.
        """
        d = _fitz_b.open()
        W, H = 1067.7659912109375, 824.853515625
        pg = d.new_page(width=W, height=H)
        GREY = (0.84, 0.84, 0.84)
        # Same yard rect + scale bar + title text as drawings/_int_d77.pdf
        pg.draw_rect(_fitz_b.Rect(130.0, 120.0, 937.765625, 624.853515625),
                     color=(0, 0, 0), fill=GREY, width=1.0)
        pg.draw_line(_fitz_b.Point(130.0, 714.853515625), _fitz_b.Point(696.9290771484375, 714.853515625),
                     color=(0, 0, 0), width=2.0)
        pg.insert_text((130.0, 80), "PROPOSED HARD LANDSCAPING - CONCRETE SERVICE YARD    Scale 1:250",
                       fontsize=13)
        pg.insert_text((126.0, 731), "0", fontsize=11)
        pg.insert_text((678.9, 731), "50 m", fontsize=11)
        if with_borders:
            # Sheet-frame border strip: four thin grey rects running along the outer page
            # edge (inside the outer ~1% margin), same grey as the yard hatch — drawn as
            # separate strips (not a filled rect + white hole) so they don't cover other
            # content or perturb the solid-fill drawing-style heuristic.
            bw = 6  # strip thickness in pt
            m = 4   # inset from the physical page edge
            for r in (
                _fitz_b.Rect(m, m, W - m, m + bw),                 # top
                _fitz_b.Rect(m, H - m - bw, W - m, H - m),          # bottom
                _fitz_b.Rect(m, m, m + bw, H - m),                  # left
                _fitz_b.Rect(W - m - bw, m, W - m, H - m),          # right
            ):
                pg.draw_rect(r, color=None, fill=GREY, width=0)
            # Thin grey bridging tail connecting the left border strip to the yard rect's
            # own left edge — this reproduces the real failure mode Aryan found: a border
            # line that runs close enough to the yard boundary that binary_closing (kernel
            # size 9) fuses it into the SAME connected component as the yard hatch, directly
            # inflating the measured area rather than appearing as an isolated, easily-
            # skipped satellite blob. A frame that stays fully isolated out in the margin is
            # already handled by the pre-existing best-plausible-component selection, so it
            # alone would not exercise this fix.
            pg.draw_rect(_fitz_b.Rect(m + bw, 300, 130, 306), color=None, fill=GREY, width=0)
            # Legend colour swatch: small isolated grey chip near the title block, far
            # from the yard polygon (same grey, small — a real legend colour key patch).
            pg.draw_rect(_fitz_b.Rect(950, 760, 966, 776), color=(0, 0, 0), fill=GREY, width=0.5)
        d.save(out_path)
        d.close()

    _p_plain = "/tmp/_ci_d77_borders_plain.pdf"
    _p_bord = "drawings/_int_d77_borders.pdf"
    _gen_d77_borders(_p_plain, with_borders=False)
    _gen_d77_borders(_p_bord, with_borders=True)

    # Sanity: the regenerated plain fixture reproduces the real _int_d77.pdf's area.
    _r_plain = _tu_takeoff2(_p_plain)
    ck("regenerated D77 fixture matches real _int_d77.pdf area (3,159 m²)",
       _r_plain.get("area_m2") == 3159.0, f"got {_r_plain.get('area_m2')}")

    # WITHOUT the exclusion: border pixels (frame touches the mask + legend swatch)
    # must inflate the measured area if segmented with exclude_border=False.
    import numpy as _np_b
    from PIL import Image as _Image_b
    _pgb = _fitz_b.open(_p_bord)[0]
    _pixb = _pgb.get_pixmap(matrix=_fitz_b.Matrix(2.0, 2.0))
    _imb = _np_b.frombuffer(_pixb.samples, _np_b.uint8).reshape(_pixb.height, _pixb.width, _pixb.n)[..., :3]
    _GREY_RGB = (214, 214, 214)
    _k77 = 0.08819   # same k as D77 (1:250, verified)
    _comp_noex = _seg_b(_imb, _GREY_RGB, k=_k77, S=2.0, exclude_border=False)
    _area_noex = round(int(_comp_noex.sum()) * (1 / 2.0) ** 2 * _k77 * _k77, 0)
    ck("WITHOUT exclusion: borders+legend over-measure vs plain 3,159 m²",
       _area_noex > 3159.0 + 15, f"got {_area_noex}")

    # WITH the exclusion (default path, via full takeoff()): must land back on 3,159 ± 0.5%.
    _r_bord = _tu_takeoff2(_p_bord)
    _area_bord = _r_bord.get("area_m2")
    ck("WITH exclusion: _int_d77_borders.pdf area back to 3,159 m² (±0.5%)",
       _area_bord is not None and abs(_area_bord - 3159.0) / 3159.0 <= 0.005,
       f"got {_area_bord}")
    ck("WITH exclusion: flag lists excluded border/legend components",
       any("excluded" in f and "border/legend" in f for f in _r_bord.get("flags", [])),
       _r_bord.get("flags"))
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 border/legend exclusion test — missing dependency or file: {_e}")

print("manhole counting — MARKED path (robust_takeoff.count_manholes_marked)")
try:
    _require_fixture("drawings", "manhole counting (marked path) test")
    import fitz as _fitz_mh
    from robust_takeoff import read_marked as _read_marked_mh, count_manholes_marked

    def _gen_synthetic_yard(out_path, n_manholes=26):
        """drawings/synthetic_yard.pdf: the gold.json 'synthetic_yard.pdf' fixture —
        a yard boundary labelled with its NET area (25,920 sq m — gross 26,080 minus a
        160 m² void, mirroring how gold.json tracks gross_m2/void_m2/net_m2 for this
        fixture and how a real Bluebeam net-area markup states the final net figure
        directly on the polygon, not gross+void as two separate summed entries) plus
        n_manholes Circle annots scattered inside (Fortel's manhole-marker convention
        on the MARKED path). read_marked() sums Polygon-labelled areas, so a single
        polygon labelled with the net figure reproduces net_m2 exactly.
        """
        d = _fitz_mh.open()
        pg = d.new_page(width=1800, height=1800)
        ox, oy = 50, 50
        W, H = 1630, 1600
        # router.classify() gates MARKED-vs-RASTER on vector path count (vec >= 50); a plain
        # annot-only PDF has 0 page-content vector paths and would misclassify as RASTER.
        # Draw the actual yard boundary + a filler grid as real vector lines (matching how
        # ci_tests.py's own multi-page/marked fixtures push vec >= 50) so this fixture
        # classifies as MARKED vector like a real Bluebeam-marked drawing does.
        pg.draw_rect(_fitz_mh.Rect(ox, oy, ox + W, oy + H), color=(0, 0, 0), width=1.5)
        for i in range(60):
            pg.draw_line(_fitz_mh.Point(ox + 10 + i, oy + H + 40), _fitz_mh.Point(ox + 10 + i, oy + H + 140))
        # Yard boundary polygon, labelled with the NET area (gross 26,080 - void 160).
        outer = [(ox, oy), (ox + W, oy), (ox + W, oy + H), (ox, oy + H)]
        a = pg.add_polygon_annot(outer)
        a.set_info(content="L = 6,460.0 m\rA = 25,920.0 sq m")
        a.update()
        # A drawn (non-annotated) void rectangle purely for visual/context completeness —
        # NOT a separate Polygon annot, so read_marked (which sums every Polygon annot's
        # labelled area) doesn't double count it against the net figure above.
        pg.draw_rect(_fitz_mh.Rect(ox + 700, oy + 700, ox + 800, oy + 860), color=(0.5, 0.5, 0.5), width=1)
        # 26 manhole markers: small Circle annots scattered on a grid inside the yard,
        # avoiding the void rectangle.
        placed = 0
        gx, gy = 0, 0
        cols = 6
        while placed < n_manholes:
            cx = ox + 120 + (gx % cols) * 260
            cy = oy + 120 + gy * 260
            if not (ox + 680 <= cx <= ox + 820 and oy + 680 <= cy <= oy + 880):
                c = pg.add_circle_annot(_fitz_mh.Rect(cx - 6, cy - 6, cx + 6, cy + 6))
                c.set_info(content="MH")
                c.update()
                placed += 1
            gx += 1
            if gx % cols == 0:
                gy += 1
        d.save(out_path)
        d.close()

    _p_synth = "drawings/synthetic_yard.pdf"
    _gen_synthetic_yard(_p_synth, n_manholes=26)

    _area_synth, _n_regions = _read_marked_mh(_p_synth)
    ck("synthetic_yard net area == gold net_m2 (25,920 = 26,080 gross - 160 void)",
       _area_synth == 25920.0, f"got {_area_synth}")
    _mh_count = count_manholes_marked(_p_synth)
    ck("synthetic_yard manhole_count (Circle annots) == 26 (gold marker_count/manhole_count)",
       _mh_count == 26, f"got {_mh_count}")

    # A drawing with no Circle annots at all -> 0, not a crash.
    _d_nomh = _fitz_mh.open(); _p_nomh = _d_nomh.new_page()
    _a_nomh = _p_nomh.add_polygon_annot([(10, 10), (100, 10), (100, 100), (10, 100)])
    _a_nomh.set_info(content="A = 100 sq m"); _a_nomh.update()
    _d_nomh.save("/tmp/_ci_no_manholes.pdf"); _d_nomh.close()
    ck("no Circle annots -> manhole_count 0 (not a crash)",
       count_manholes_marked("/tmp/_ci_no_manholes.pdf") == 0)

    # Real Winvic marked yard PDF: as shipped in this repo it carries NO Circle annots
    # (Fortel has not yet placed manhole markers on it — confirmed by direct inspection;
    # its 18 Square annots are AutoCAD SHX Text bounding boxes for street names/numbers,
    # not manhole markers). count_manholes_marked must report that honestly (0), never
    # fabricate the Winvic costing sheet's "26 Nr" figure from thin air.
    _winvic_yard = "drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf"
    if Path(_winvic_yard).is_file():
        ck("real Winvic yard PDF has 0 Circle annots today (no markers placed yet -> honest 0, "
           "not a fabricated 26)", count_manholes_marked(_winvic_yard) == 0)
    else:
        print("  [SKIP] real Winvic yard manhole regression — fixture not present")
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] manhole counting (marked path) test — missing dependency or file: {_e}")

print("manhole counting — UNMARKED path (takeoff_unmarked.detect_manholes, conservative ESTIMATE)")
try:
    _require_fixture("drawings/_int_d77.pdf", "manhole counting (unmarked path) D77 test")
    import numpy as _np_mh, cv2 as _cv2_mh
    from takeoff_unmarked import detect_manholes, takeoff as _tu_takeoff3

    def _gen_yard_with_circles(n_circles, diam_m, k=0.05, S=2.0):
        """A grey yard rect rendered directly as a numpy image (no PDF round-trip needed —
        detect_manholes takes the rendered array + mask + k directly), with n_circles dark
        rings drawn inside at diam_m real-world diameter, converted to px via k/S."""
        H_px, W_px = 900, 1200
        im = _np_mh.full((H_px, W_px, 3), 255, _np_mh.uint8)
        im[100:800, 100:1100] = (214, 214, 214)   # yard hatch
        r_px = int(round((diam_m / 2) * (S / k)))
        centres = []
        cols = 6
        for i in range(n_circles):
            cx = 200 + (i % cols) * 150
            cy = 200 + (i // cols) * 150
            _cv2_mh.circle(im, (cx, cy), r_px, (60, 60, 60), thickness=2)
            centres.append((cx, cy))
        comp = _np_mh.zeros((H_px, W_px), bool)
        comp[100:800, 100:1100] = True
        return im, comp, centres

    # 6 manhole-sized circles (0.9 m diameter, mid-band) inside the yard -> detector finds them.
    _im_mh, _comp_mh, _true_centres = _gen_yard_with_circles(6, diam_m=0.9, k=0.05, S=2.0)
    _n_mh, _found_centres = detect_manholes(_im_mh, _comp_mh, k=0.05, S=2.0)
    ck("detect_manholes finds manhole-sized circles inside the yard (>=4 of 6)", _n_mh >= 4,
       f"found {_n_mh}")

    # No circles at all -> 0, not a crash (D77-style plain rect).
    _im_none = _np_mh.full((400, 400, 3), 255, _np_mh.uint8); _im_none[50:350, 50:350] = (214, 214, 214)
    _comp_none = _np_mh.zeros((400, 400), bool); _comp_none[50:350, 50:350] = True
    _n_none, _ = detect_manholes(_im_none, _comp_none, k=0.05, S=2.0)
    ck("no circular features -> manhole_count_estimate 0 (not a crash)", _n_none == 0)

    # Oversized circles (e.g. 6 m diameter — a roundabout/planter, not a manhole) must NOT
    # be counted: the radius band excludes anything outside MANHOLE_DIAM_M_MIN..MAX.
    _im_big, _comp_big, _ = _gen_yard_with_circles(2, diam_m=6.0, k=0.05, S=2.0)
    _n_big, _ = detect_manholes(_im_big, _comp_big, k=0.05, S=2.0)
    ck("oversized circles (6 m dia, not manhole-sized) excluded by radius band", _n_big == 0,
       f"found {_n_big}")

    # End-to-end: D77 (plain rect, no circular features) -> manhole_count_estimate present,
    # zero, and no false "confirm" flag fired when there's nothing to confirm.
    _d77_mh = _tu_takeoff3("drawings/_int_d77.pdf")
    ck("D77 takeoff() carries manhole_count_estimate field", "manhole_count_estimate" in _d77_mh)
    ck("D77 manhole_count_estimate is 0 (plain rect, no circular features)",
       _d77_mh.get("manhole_count_estimate") == 0)
    # Inderjit's rule (last Fortel call): no drainage layout / no drawn symbols -> ASSUME 1 per
    # 1,000 m². D77 measures ~3,159 m² with a legend label, so the assumed count = round(3159/1000)
    # = 3. It's a SEPARATE field (never manhole_count_estimate, which auto-prices) so it never
    # feeds the £75/Nr E/O line automatically — the assessor confirms first.
    ck("D77 takeoff() carries manhole_count_assumed field", "manhole_count_assumed" in _d77_mh)
    ck("D77 manhole_count_assumed == round(area/1000), floor 1 (Inderjit's 1-per-1,000 rule)",
       _d77_mh.get("manhole_count_assumed") == max(1, round((_d77_mh.get("area_m2") or 0) / 1000.0)),
       f"assumed={_d77_mh.get('manhole_count_assumed')} area={_d77_mh.get('area_m2')}")
    ck("D77 manhole_count_assumed is 3 for the ~3,159 m² fixture",
       _d77_mh.get("manhole_count_assumed") == 3, f"got {_d77_mh.get('manhole_count_assumed')}")
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] manhole counting (unmarked path) test — missing dependency or file: {_e}")

print("refuse-instead-of-guess guard — non-slab sheets must REFUSE, not emit a garbage area")
try:
    import os as _os_rg
    import takeoff_pipeline as _tp_rg
    # Four real tender-pack sheets that are NOT concrete slabs. Before the guard they emitted
    # confident 5,000-6,000 m² areas (no legend label + unverified scale). They must now REFUSE
    # cleanly. Files are gitignored client drawings, so this block skips in CI (drawings/ absent);
    # it runs locally as the regression that pins the fix.
    _fp_files = [
        "drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_GA_Elevations.pdf",
        "drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_GA_Office_Elevations.pdf",
        "drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_Gatehouse.pdf",
        "drawings/tender_pack/2-Enquiry/01-Tender/Planning-Documentation/Site_Location_Plan.pdf",
    ]
    _fp_present = [f for f in _fp_files if _os_rg.path.exists(f)]
    for _f in _fp_files:
        if not _os_rg.path.exists(_f):
            print(f"  [SKIP] refuse-guard regression for {_os_rg.path.basename(_f)} — fixture not present")
    for _f in _fp_present:
        _r = _tp_rg.takeoff(_f, send_approval=False)
        _b = _os_rg.path.basename(_f)
        ck(f"non-slab '{_b}' refuses -> area_m2 is None", _r.get("area_m2") is None,
           f"got area={_r.get('area_m2')}")
        ck(f"non-slab '{_b}' -> UNMEASURED", _r.get("measurement_state") == "UNMEASURED",
           f"got {_r.get('measurement_state')}")
        ck(f"non-slab '{_b}' carries a REFUSED flag",
           any("REFUSED" in _fl for _fl in _r.get("flags", [])))
    # Positive control: real D77 gold has a legend label, so the guard must NOT fire even though
    # its scale bar is unverified — it must still measure the slab (~3,156 m²).
    _d77_positive_control_ran = False
    for _d77f in ("drawings/real_sgp/D77_Hard_Landscaping.pdf", "drawings/_int_d77.pdf"):
        if _os_rg.path.exists(_d77f):
            _rd = _tp_rg.takeoff(_d77f, send_approval=False)
            ck(f"legend'd D77 '{_os_rg.path.basename(_d77f)}' NOT refused by guard (area still emitted)",
               _rd.get("area_m2") is not None and _rd.get("area_m2") > 2500,
               f"got area={_rd.get('area_m2')}")
            _d77_positive_control_ran = True
            break
    if not _d77_positive_control_ran:
        print("  [SKIP] refuse-guard D77 positive control — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] refuse-guard regression — missing dependency or file: {_e}")

print("hatch-drawn surfaces (MJM 9000 class: slanted legend hatching, not a solid fill)")
try:
    import os as _os_hx
    import json as _json_hx
    import cv2 as _cv_hx
    import fitz as _fitz_hx
    import numpy as _np_hx
    import takeoff_unmarked as _tu_hx

    # ── Router guards. No fixture needed, so these run everywhere and pin the behaviour
    # that protects the gold sheets: a solid fill must NEVER be routed to a wide kernel.
    _hx_solid = _np_hx.zeros((700, 700), bool)
    _hx_solid[50:650, 50:650] = True
    _k_s, _i_s = _tu_hx._hatch_closing_kernel(_hx_solid, 6, 0.1, 2.0)
    ck("hatch router: a solid filled block is NOT routed to the wide kernel",
       _k_s is None, _i_s.get("reason"))

    _hx_hatch = _np_hx.zeros((700, 700), bool)
    _hx_hatch[50:650, 50:650:26] = True          # strokes 26 px apart, MJM's own spacing
    _k_h, _i_h = _tu_hx._hatch_closing_kernel(_hx_hatch, 6, 0.1, 2.0)
    ck("hatch router: 26 px-spaced strokes ARE routed to a wider kernel",
       _k_h is not None and _k_h > 6, _i_h.get("reason"))

    # The cap is in METRES, not pixels: the same 26 px spacing that is a legitimate hatch on a
    # 1:500 sheet would bridge 13 m on a coarse one, which is fusing the drawing rather than
    # reading it. Same mask as above, coarser scale, and it must refuse.
    _k_w, _i_w = _tu_hx._hatch_closing_kernel(_hx_hatch, 6, 1.0, 2.0)
    ck("hatch router: a kernel bridging too much REAL distance REFUSES, however it looks in px",
       _k_w is None and "cap" in (_i_w.get("reason") or ""), _i_w.get("reason"))

    _hx_sparse = _np_hx.zeros((700, 700), bool)
    _hx_sparse[10:30, 10:30] = True              # far too little tint to classify
    _k_sp, _i_sp = _tu_hx._hatch_closing_kernel(_hx_sparse, 6, 0.1, 2.0)
    ck("hatch router: too little matching tint REFUSES to classify",
       _k_sp is None, _i_sp.get("reason"))

    # ── May several same-tint regions be summed? ────────────────────────────────────────
    # The old test was "do their bounding boxes overlap", which lies on diagonal strips: three
    # unit yards on the St Modwen Newport sheet sit 2.2-3.8 m apart with overlapping boxes and
    # disjoint ink, so two of the three were dropped from the total (2,520 of ~6,486 m²). The
    # same bug was silently doing it on the Tanro site plan the code comment itself cites.
    import glob as _glob_hx
    _amb = _tu_hx.same_tint_regions_ambiguous
    _px_per_m2 = 4.0  # 2 px per metre: keeps the fixtures small and the maths obvious

    def _rect(h, w, y0, x0, y1, x1):
        mask = _np_hx.zeros((h, w), bool)
        mask[y0:y1, x0:x1] = True
        return mask
    # Three diagonal strips: boxes overlap, ink does not — separate unit yards.
    _diagonal = [_rect(400, 400, 0, 0, 120, 160), _rect(400, 400, 140, 150, 260, 310),
                 _rect(400, 400, 280, 40, 399, 200)]
    ck("three separate extents with overlapping boxes are summed, not held out",
       _amb(_diagonal, px_per_m2=_px_per_m2) is False)
    # One region wrapping another: the same tint used for interleaved surfaces.
    _wrapper = _np_hx.zeros((400, 400), bool); _wrapper[0:400, 0:400] = True
    _inner = _rect(400, 400, 120, 120, 280, 280)
    _wrapper &= ~_inner
    ck("a region that wraps another IS ambiguous — one tint, interleaved surfaces",
       _amb([_wrapper, _inner], px_per_m2=_px_per_m2) is True)
    # A small satellite inside a big region's box must not make the set ambiguous by itself.
    _big = _rect(400, 400, 0, 0, 200, 400)
    _satellite = _rect(400, 400, 20, 20, 40, 40)   # 400 px = 100 m², under PLAUSIBLE_MIN_M2
    ck("a sub-plausible satellite inside another region's box does not block summing",
       _amb([_big, _satellite], px_per_m2=_px_per_m2) is False)
    ck("...and with no scale to judge plausibility, it stays with the blunt, safer bbox test",
       _amb([_big, _satellite], px_per_m2=None) is True)
    ck("an empty region is treated as ambiguous rather than assumed safe",
       _amb([_big, _np_hx.zeros((400, 400), bool)], px_per_m2=_px_per_m2) is True)

    # Geometry says "separate extents"; colour says whether they are the same SURFACE. The
    # segmentation band is wider than the agreement gate, so a kerb line or footpath a few RGB
    # levels off the swatch can sit inside it — on the SGP masterplan three such regions
    # (199,199,199 against a 208 swatch) were being added to a dock-apron total.
    _sgp_master = _glob_hx.glob("drawings/**/*131003-Phase-1-Masterplan*", recursive=True)
    if not _sgp_master:
        print("  [SKIP] SGP masterplan colour gate — client fixture not present")
    else:
        _sgp_res = _tu_hx.takeoff(_sgp_master[0], source="architect")
        _sgp_regions = sorted((r.get("area_m2") or 0) for r in (_sgp_res.get("yard_regions") or []))[::-1]
        ck("a same-band region that is NOT the legend's paint is left out of the total",
           abs((_sgp_res.get("area_m2") or 0) - sum(_sgp_regions[:3])) <= 1.0
           and len(_sgp_regions) > 3,
           f"total {_sgp_res.get('area_m2')} vs same-paint regions {[round(a, 1) for a in _sgp_regions[:3]]}")
        ck("...and the assessor is told which region was left out and why",
           any("not the legend's" in f or "not the measured surface's" in f
               for f in _sgp_res.get("flags") or []),
           [f[:120] for f in (_sgp_res.get("flags") or []) if "same-band region" in f][:1])

    _newport = ("drawings/inderjit_p9p10/11_Indurent_Park_Newport_"
                "22513-RLL-25-00-DR-C-3151_P02_Proposed_Pavement_Construction.pdf")
    if not _os_hx.path.exists(_newport):
        print(f"  [SKIP] Newport multi-unit gold — client fixture not present ({_newport})")
    else:
        _np_res = _tu_hx.takeoff(_newport, source="engineer")
        # THESE TWO CHECKS USED TO ASSERT 6,485.8 m² ACROSS "three unit yards". They were written
        # for bc3e97a and they were wrong about what they were measuring. Aryan's own Bluebeam
        # markup (7 Sep 2026, ~/fortel-ground-truth/22513-RLL-3151_ARYAN_MARKUP.pdf) puts the
        # three Service Yards at 3,492.3 + 3,195.6 + 4,438.0 = 11,125.9 m², somewhere else
        # entirely: 98.6% of this sheet's own `RL_Surfacing_Service Yard` CAD items fall inside
        # his polygons and 0.2% inside what we were measuring, IoU 0.0006. The 6,485.8 was the
        # P2 car parking, reached by substituting another client's grey for a legend tint we
        # could not find. The sheet now refuses. A test that pins a number nobody checked
        # against the client is not a gate — it is a way of not noticing.
        # ...and it is now measured, from the CAD layer rather than from any tint. Scored by
        # IoU against Aryan's three polygons, one region at a time: an area that matches
        # while outlining the wrong ground is the exact failure this sheet already produced
        # once, and only shape scoring can see it.
        ck("the Newport sheet measures the three service yards instead of the car park",
           _np_res.get("area_m2") is not None
           and _np_res.get("measurement_state") == "MEASURED_UNVERIFIED"
           and _np_res.get("needs_assessor") is True
           and len(_np_res.get("yard_regions") or []) == 3,
           f"area={_np_res.get('area_m2')} state={_np_res.get('measurement_state')} "
           f"regions={len(_np_res.get('yard_regions') or [])}")
        _np_gt = _json_hx.loads(Path("ground_truth_polygons.json").read_text()).get(_newport)
        if not _np_gt:
            print("  [SKIP] no ground-truth polygons recorded for the Newport sheet")
        else:
            import numpy as _np_np, cv2 as _np_cv

            def _np_iou(poly_a, poly_b):
                _a = _np_np.asarray(poly_a, dtype=float)
                _b = _np_np.asarray(poly_b, dtype=float)
                _o = _np_np.minimum(_a.min(0), _b.min(0))
                _s = _np_np.maximum(_a.max(0), _b.max(0)) - _o
                _w, _h = int(_s[0]) + 2, int(_s[1]) + 2
                _ma = _np_np.zeros((_h, _w), _np_np.uint8)
                _mb = _np_np.zeros((_h, _w), _np_np.uint8)
                _np_cv.fillPoly(_ma, [_np_np.round(_a - _o).astype(_np_np.int32)], 1)
                _np_cv.fillPoly(_mb, [_np_np.round(_b - _o).astype(_np_np.int32)], 1)
                _u = int((_ma | _mb).sum())
                return (int((_ma & _mb).sum()) / _u) if _u else 0.0

            _np_measured = [r["polygon_pts"] for r in _np_res["yard_regions"]]
            _np_floor = _np_gt["min_iou"]
            for _np_region in _np_gt["regions"]:
                _np_best = max((_np_iou(_np_region["polygon_pts"], _m) for _m in _np_measured),
                               default=0.0)
                ck(f"...{_np_region['region']} is outlined where Aryan outlined it "
                   f"(IoU >= {_np_floor})",
                   _np_best >= _np_floor,
                   f"best IoU {_np_best:.3f} vs {_np_floor}")
            # A stipple closed into an outline sits inside the true edge. Under-measuring is
            # survivable and disclosed; over-measuring would quote ground nobody is paving.
            ck("...and the total never exceeds the client's own markup",
               _np_res["area_m2"] <= _np_gt["area_m2"],
               f"measured {_np_res['area_m2']} vs markup {_np_gt['area_m2']} "
               f"({100 * _np_res['area_m2'] / _np_gt['area_m2'] - 100:+.1f}%)")

    # ── The bridging limit is DISCLOSED, not detected ───────────────────────────────────
    # A kernel wide enough to bridge this sheet's stroke gaps also bridges a real corridor of
    # clean paper between two separately drafted surfaces of the same tint, and nothing on this
    # path can tell the two apart. The fixture below reproduces it on the SHIPPED path: two
    # 45-degree hatched rectangles with a 15 pt corridor come back as ONE region. The sheet must
    # therefore state the width it can bridge, in metres, on every drawing that uses the kernel.
    import math as _math_fu, tempfile as _tmp_fu, fitz as _fitz_fu
    def _fuse_fixture(path, corridor_pt, spacing=13.0, tint=(1, 0, 0)):
        doc = _fitz_fu.open(); pg = doc.new_page(width=2384, height=1684); sh = pg.new_shape()
        # solid block: the hatch router lives inside the colour path, which needs solid fill
        sh.draw_rect(_fitz_fu.Rect(120, 1050, 1500, 1560))
        sh.finish(color=(0.55, 0.78, 0.55), fill=(0.55, 0.78, 0.55))
        def hatch(x0, y0, x1, y1):
            c = y0 - x1
            while c < y1 - x0:
                pts = []
                for (X, Y) in ((x0, x0 + c), (x1, x1 + c), (y0 - c, y0), (y1 - c, y1)):
                    if x0 - 1e-6 <= X <= x1 + 1e-6 and y0 - 1e-6 <= Y <= y1 + 1e-6: pts.append((X, Y))
                if len(pts) >= 2:
                    a, b = sorted(set(pts))[0], sorted(set(pts))[-1]
                    sh.draw_line(_fitz_fu.Point(*a), _fitz_fu.Point(*b))
                    sh.finish(color=tint, width=1.0)
                c += spacing * _math_fu.sqrt(2)
        hatch(300, 300, 900, 900); hatch(900 + corridor_pt, 300, 1400, 900)
        sh.draw_rect(_fitz_fu.Rect(1700, 300, 1740, 318)); sh.finish(color=tint, fill=tint)
        pg.insert_text((1750, 315), "CONCRETE SERVICE YARD", fontsize=9)
        pg.insert_text((1900, 1600), "Scale: 1:250", fontsize=10)
        pg.insert_text((1900, 1620), "EXTERNAL WORKS LAYOUT", fontsize=10)
        sh.commit(); doc.save(path); doc.close()
    _fu_dir = _tmp_fu.mkdtemp(prefix="hatch_fusion_")
    _fu_near = _os_hx.path.join(_fu_dir, "corridor_15pt.pdf"); _fuse_fixture(_fu_near, 15)
    _fu_far = _os_hx.path.join(_fu_dir, "corridor_60pt.pdf"); _fuse_fixture(_fu_far, 60)
    _fu_near_res = _tu_hx.takeoff(_fu_near, source="engineer")
    _fu_far_res = _tu_hx.takeoff(_fu_far, source="engineer")
    _fu_disc = [f for f in (_fu_near_res.get("flags") or []) if "LIMIT OF THIS METHOD" in f]
    ck("a corridor narrower than the kernel really does fuse two surfaces on the shipped path",
       len(_fu_near_res.get("yard_regions") or []) == 1,
       f"{_fu_near_res.get('area_m2')} m² in {len(_fu_near_res.get('yard_regions') or [])} region(s)")
    ck("...and the sheet says so, in metres, instead of leaving it silent",
       bool(_fu_disc) and "corridor narrower than about 1." in _fu_disc[0],
       (_fu_disc[0][_fu_disc[0].index("LIMIT OF THIS METHOD"):][:120] if _fu_disc else "no disclosure"))
    ck("...the disclosure is about the kernel, so a WIDER corridor keeps two regions and still says it",
       len(_fu_far_res.get("yard_regions") or []) == 2
       and any("LIMIT OF THIS METHOD" in f for f in _fu_far_res.get("flags") or []),
       [round(r.get("area_m2") or 0) for r in _fu_far_res.get("yard_regions") or []])
    ck("...and it never claims to have checked: it states a limit, not a clean bill of health",
       bool(_fu_disc) and "cannot tell such a corridor from a stroke gap" in _fu_disc[0]
       and "no fusion" not in _fu_disc[0].lower())

    # ── Real-sheet gold. Client drawings are gitignored, so skip VISIBLY when absent.
    _hx_pdf = ("drawings/inderjit_p7/"
               "7_25195-MJM-00-00-DR-C-9000-D2-P04-External_Works_Layout.pdf")
    if not (_os_hx.path.exists(_hx_pdf) and _os_hx.path.exists("ground_truth_polygons.json")):
        print(f"  [SKIP] MJM hatch gold — client fixture not present ({_hx_pdf})")
    else:
        _hx_gt = _json_hx.loads(Path("ground_truth_polygons.json").read_text())
        _hx_entry = _hx_gt[_hx_pdf]
        _hx_res = _tu_hx.takeoff(_hx_pdf, source="architect")
        _hx_disc = [f for f in (_hx_res.get("flags") or []) if "LIMIT OF THIS METHOD" in f]
        ck("MJM gold states the real-world width its kernel can bridge (51px at 1:250 = ~4.5 m)",
           bool(_hx_disc) and "corridor narrower than about 4.5 m" in _hx_disc[0],
           (_hx_disc[0][_hx_disc[0].index("LIMIT OF THIS METHOD"):][:110] if _hx_disc else "none"))
        _hx_area = _hx_res.get("area_m2")
        _hx_truth = _hx_entry["area_m2"]

        # Area alone is not enough (CLAUDE.md rule 4: an agent once matched a gold area to
        # 0.1% with the WRONG region), so the shape is scored too.
        ck("MJM hatch gold: measures the area the hatch ENCLOSES, not its ink",
           _hx_area is not None and abs(_hx_area - _hx_truth) / _hx_truth * 100 <= 2.0,
           f"got {_hx_area} vs truth {_hx_truth}")
        ck("MJM hatch gold: the widened-kernel path is FLAGGED, never silent",
           any("HATCH-DRAWN SURFACE" in _f for _f in _hx_res.get("flags") or []))

        _hx_doc = _fitz_hx.open(_hx_pdf)
        _hx_pg = _hx_doc[0]
        _hx_S = 2.0
        _hx_pix = _hx_pg.get_pixmap(matrix=_fitz_hx.Matrix(_hx_S, _hx_S))
        _hx_im = _np_hx.frombuffer(_hx_pix.samples, _np_hx.uint8).reshape(
            _hx_pix.height, _hx_pix.width, _hx_pix.n)[:, :, :3].copy()
        _hx_H, _hx_W = _hx_im.shape[:2]
        _hx_R = _hx_pg.rotation_matrix
        _hx_poly = _np_hx.array(
            [[(_fitz_hx.Point(_x, _y) * _hx_R).x * _hx_S,
              (_fitz_hx.Point(_x, _y) * _hx_R).y * _hx_S]
             for _x, _y in _hx_entry["polygon_pts"]], _np_hx.int32)
        _hx_truth_mask = _np_hx.zeros((_hx_H, _hx_W), _np_hx.uint8)
        _cv_hx.fillPoly(_hx_truth_mask, [_hx_poly], 1)
        _hx_truth_mask = _hx_truth_mask.astype(bool)

        _hx_mask = _np_hx.all(
            _np_hx.abs(_hx_im.astype(_np_hx.int16) - _np_hx.array([254, 0, 0], _np_hx.int16))
            <= 14, axis=2).astype(_np_hx.uint8)
        _hx_my = max(1, int(round(_hx_H * _tu_hx.MARGIN_FRAC)))
        _hx_mx = max(1, int(round(_hx_W * _tu_hx.MARGIN_FRAC)))
        _hx_mask[:_hx_my, :] = 0
        _hx_mask[-_hx_my:, :] = 0
        _hx_mask[:, :_hx_mx] = 0
        _hx_mask[:, -_hx_mx:] = 0
        _hx_lb = _tu_hx._legend_sample_bbox_for(_hx_pdf, _tu_hx.CONCRETE_LABELS)
        if _hx_lb:
            _a, _b, _c, _d = [int(round(_v * _hx_S)) for _v in _hx_lb]
            _hx_mask[max(0, _b):min(_hx_H, _d), max(0, _a):min(_hx_W, _c)] = 0
        _hx_closed = _cv_hx.morphologyEx(
            _hx_mask, _cv_hx.MORPH_CLOSE, _np_hx.ones((51, 51), _np_hx.uint8))
        _hx_n, _hx_lab, _hx_st, _ = _cv_hx.connectedComponentsWithStats(_hx_closed, 8)
        _hx_i = 1 + int(_np_hx.argmax(_hx_st[1:, _cv_hx.CC_STAT_AREA]))
        _hx_region = (_hx_lab == _hx_i)
        _hx_iou = ((_hx_region & _hx_truth_mask).sum()
                   / max((_hx_region | _hx_truth_mask).sum(), 1))
        ck("MJM hatch gold: the closed region is the RIGHT region (IoU vs client markup)",
           _hx_iou >= 0.90, f"IoU {_hx_iou:.3f}")

        # The runtime acceptance gate, not a dev-time one: production sheets have no truth
        # polygon, so a hatch is only measured when its outline corroborates a native CAD path.
        _hx_native, _hx_reason = _tu_hx._native_boundary_for_mask(
            _hx_pg, _hx_region, _hx_S, _hx_entry["k_m_per_pt"])
        ck("MJM hatch gold: closed outline corroborates a native CAD boundary at IoU 0.90",
           _hx_native is not None, _hx_reason)
        _hx_doc.close()
except (ImportError, FileNotFoundError, KeyError) as _e:
    print(f"  [SKIP] hatch-drawn surface regression — missing dependency or file: {_e}")

print("dock apron slabs on a joint-layout sheet (0720 class: slab named by build-up, no yard label)")
try:
    import os as _os_da
    import fitz as _fitz_da
    import numpy as _np_da
    import takeoff_unmarked as _tu_da

    ck("dock apron vocabulary is part of the concrete legend lookup",
       all(_t in _tu_da.CONCRETE_LABELS for _t in _tu_da.DOCK_APRON_LABELS),
       _tu_da.DOCK_APRON_LABELS)
    ck("the yard vocabulary is still intact alongside it",
       "concrete service yard" in _tu_da.CONCRETE_LABELS
       and "service yard" in _tu_da.CONCRETE_LABELS)
    ck("dock-apron terms are a SEPARATE tier, not mixed into the yard vocabulary",
       not any(_t in _tu_da.YARD_LABELS for _t in _tu_da.DOCK_APRON_LABELS),
       _tu_da.YARD_LABELS)

    # REGRESSION GUARD. Appending the dock-apron terms straight onto CONCRETE_LABELS silently
    # hijacked Inderjit's project-6 Construction Thickness sheets, which carry BOTH a
    # "service yard" legend AND a "dock apron construction (Cl. 5.1...)" note. _label_bbox_for
    # returns the FIRST line in document order matching ANY label, and the dock-apron note sits
    # above the yard legend there, so it locked onto (185,185,185) instead of (217,217,217):
    # 0710 32,966 -> 9,872 and 0711 5,445 -> 15,536, the latter being the sheet Aryan had just
    # confirmed as correct. No gold entry exists for either, so the corpus never saw it.
    for _p6 in ("6_31941-TTE-ZF-762-DR-C-0710-P01-Construction_Thicknessess_Plan.pdf",
                "6_31941-TTE-ZF-762-DR-C-0711-P01-Construction_Thicknessess_Plan.pdf"):
        _p6path = f"drawings/inderjit_p6/{_p6}"
        if not _os_da.path.exists(_p6path):
            print(f"  [SKIP] project-6 yard-precedence guard — fixture not present ({_p6})")
            continue
        _p6doc = _fitz_da.open(_p6path)
        _p6pix = _p6doc[0].get_pixmap(matrix=_fitz_da.Matrix(2.0, 2.0))
        _p6im = _np_da.frombuffer(_p6pix.samples, _np_da.uint8).reshape(
            _p6pix.height, _p6pix.width, _p6pix.n)[:, :, :3].copy()
        _p6rgb, _p6label = _tu_da.find_concrete_swatch_rgb(_p6path, im=_p6im, S=2.0)
        ck(f"{_p6[:28]}: a sheet naming a SERVICE YARD measures the yard, not the dock apron",
           _p6label is not None and "service yard" in _p6label
           and not any(_t in _p6label for _t in _tu_da.DOCK_APRON_LABELS),
           f"matched {_p6label!r} swatch {_p6rgb}")
        _p6doc.close()

    _da_pdf = ("drawings/inderjit_p6/"
               "6_31941-TTE-ZF-762-DR-C-0720-P01-Hardstanding_Joint_Layout.pdf")
    if not _os_da.path.exists(_da_pdf):
        print(f"  [SKIP] 0720 dock-apron regression — client fixture not present ({_da_pdf})")
    else:
        _da_doc = _fitz_da.open(_da_pdf)
        _da_pg = _da_doc[0]
        _da_pix = _da_pg.get_pixmap(matrix=_fitz_da.Matrix(2.0, 2.0))
        _da_im = _np_da.frombuffer(_da_pix.samples, _np_da.uint8).reshape(
            _da_pix.height, _da_pix.width, _da_pix.n)[:, :, :3].copy()
        _da_rgb, _da_label = _tu_da.find_concrete_swatch_rgb(_da_pdf, im=_da_im, S=2.0)
        ck("0720: the dock-apron legend is now found (it never was before)",
           _da_rgb is not None and "dock apron" in (_da_label or ""),
           f"{_da_rgb} {_da_label!r}")
        ck("0720: its swatch reads as a real surface tint, not near-black/white",
           _da_rgb is not None and _tu_da._is_plausible_surface_tint(_da_rgb), _da_rgb)
        # The scale is title-only on this sheet; that is exactly why naming the legend matters,
        # since the refuse gate is `not legend_found AND not verified`.
        _da_k, _da_verified, _da_note, _ = _tu_da.scale_for(_da_pdf)
        ck("0720: scale reads 1:500 from the title block and is honestly UNVERIFIED",
           abs(_da_k - 0.176389) < 1e-4 and _da_verified is False, f"{_da_k} verified={_da_verified}")

        _da_res = _tu_da.takeoff(_da_pdf, source="engineer")
        ck("0720: no longer refuses outright — it measures and routes to the assessor",
           _da_res.get("area_m2") is not None
           and _da_res.get("measurement_state") == "MEASURED_UNVERIFIED",
           f"{_da_res.get('area_m2')} {_da_res.get('measurement_state')}")
        ck("0720: a dock apron is NOT silently priced as a service yard — spec flag raised",
           any("DOCK APRON, not a service yard" in _f for _f in _da_res.get("flags") or []))
        ck("0720: it never claims a verified scale it does not have",
           _da_res.get("scale_verified") is False)
        _da_doc.close()
except (ImportError, FileNotFoundError, AttributeError) as _e:
    print(f"  [SKIP] dock-apron regression — missing dependency or file: {_e}")

print("manhole E/O costing line (costing.py Winvic rate: £75.00/Nr)")
try:
    from quotation import generate_quotation as _gen_q_mh

    MANHOLE_EO_RATE = 75.00   # £/Nr — "E/O for MH details" from the real Winvic costing sheet

    _demo_confirmed = {
        "file": "Yard.pdf", "type": "MARKED vector", "confidence": "high",
        "source_discipline": "engineer",
        "costing": {"area_m2": 26080, "rate": 44.89, "total_gbp": 1170731.20, "assumed": False,
                    "spec": {"depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40", "layers": 1, "conc_rate": 128}},
        "flags": [], "manhole_count": 26,
    }
    _extras_confirmed = [("E/O for MH details", 26, "Nr", MANHOLE_EO_RATE)]
    _q_confirmed = _gen_q_mh(_demo_confirmed, project="Winvic Yard", client="Winvic",
                             ref="TST-MH-CONFIRMED", extras=_extras_confirmed)
    _mh_line = next((li for li in _q_confirmed["line_items"] if "MH details" in li["description"]), None)
    ck("confirmed manhole_count -> E/O line present", _mh_line is not None)
    ck("confirmed E/O line value = 26 x £75.00 = £1,950.00",
       _mh_line is not None and _mh_line["value"] == 1950.00, _mh_line)
    ck("confirmed E/O line NOT marked ESTIMATE", _mh_line is not None and "ESTIMATE" not in _mh_line["description"])

    _demo_estimate = dict(_demo_confirmed)
    _demo_estimate["manhole_count_estimate"] = 3
    _demo_estimate.pop("manhole_count", None)
    _extras_estimate = [("E/O for MH details (ESTIMATE — assessor confirm)", 3, "Nr", MANHOLE_EO_RATE)]
    _q_estimate = _gen_q_mh(_demo_estimate, project="D77", client="Fortel",
                            ref="TST-MH-ESTIMATE", extras=_extras_estimate)
    _mh_line_est = next((li for li in _q_estimate["line_items"] if "MH details" in li["description"]), None)
    ck("estimated manhole_count_estimate -> E/O line present and marked ESTIMATE",
       _mh_line_est is not None and "ESTIMATE" in _mh_line_est["description"])
    ck("estimated E/O line value = 3 x £75.00 = £225.00",
       _mh_line_est is not None and _mh_line_est["value"] == 225.00, _mh_line_est)
except ImportError as _e:
    print(f"  [SKIP] manhole E/O costing test — missing dependency: {_e}")

