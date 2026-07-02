#!/usr/bin/env python3
"""Self-contained CI tests (NO client drawings — those are gitignored). Exit non-zero on failure."""
import sys, shutil
from pathlib import Path
from reportlab.pdfgen import canvas
from geometry import measure_regions
from scale import detect_scale_bar
from pricing import slab_rate, price_project

P = []
def ck(n, c, g=""):
    P.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n} {g}")

print("geometry")
K = 0.1
a, _ = measure_regions([[(0,0),(2000,0),(2000,1300),(0,1300)]], K,
                       holes={0: [[(200,200),(600,200),(600,500),(200,500)], [(1400,800),(1700,800),(1700,1100),(1400,1100)]]})
ck("voids 23,900", a == 23900)
a, f = measure_regions([[(0,0),(1000,1000),(1000,0),(0,1000)]], K); ck("self-intersect repaired+flagged", a == 5000 and f)
a, f = measure_regions([[(0,0),(1000,0),(1000,1000),(0,1000)], [(500,500),(1500,500),(1500,1500),(500,1500)]], K)
ck("overlap unioned 17,500", a == 17500 and f)
a, _ = measure_regions([[(0,0),(1,1)]], K); ck("degenerate <3 -> 0", a == 0.0)
try: measure_regions([[(0,0),(1,0),(1,1)]], None); ck("missing scale raises", False)
except ValueError: ck("missing scale raises", True)

print("scale")
c = canvas.Canvas("/tmp/_sb.pdf", pagesize=(1400,2200)); c.rect(200,1000,1000,800); c.line(100,150,600,150); c.drawString(250,160,"0          50 m"); c.save()
k, info = detect_scale_bar("/tmp/_sb.pdf"); ck("scale-bar k=0.1", k == 0.1, info)

print("pricing")
r, _ = slab_rate({"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11})
ck("yard rate 44.89", r == 44.89)
tot, rows = price_project([{"name":"Yard","area_m2":26080,"depth_mm":190,"conc_rate":128,"mesh":"A252","layers":1,"steel_rate_t":850,"margin":0.11}])
ck("yard slab line GBP1,170,731.20", rows[0][5] == 1170731.20)
ck("unknown mesh handled", slab_rate({"depth_mm":150,"conc_rate":128,"mesh":"A999","layers":1,"steel_rate_t":850,"margin":0.11})[0] is None)

print("guards (95,463 m² incident)")
from scale import scale_consensus
from sanity import plausible
ck("mixed-scale dimensions flagged (no auto-pick)", scale_consensus([(257.2,710),(166,420),(50,75),(35,80)])[0] is None)
ck("consistent dimensions accepted", abs(scale_consensus([(100,1000),(50,500)])[0] - 0.1) < 1e-6)
ck("impossible area blocked", len(plausible(95463, site_m2=34329)) >= 1)
ck("correct area passes", plausible(26080, site_m2=34329) == [])

print("Fortel scale verification (from the call)")
from scale import calibrate_verified, verify_against_feature, title_block_k
geom = 2235703  # real yard polygon area in pt²
k_v, _ = calibrate_verified(title_denominator=500, bay_width_pt=2.5/0.108)  # verify vs 2.5 m bay
ck("parking-bay verify flips wrong title scale to truth", abs(geom*k_v*k_v - 26080) < 50)
ck("title-only scale flagged as a lie", len(verify_against_feature(title_block_k(500), 2.5/0.108, 2.5)) >= 1)

print("drawing selection (from the call)")
from router import drawing_priority
ck("construction/kerbing drawing beats site plan",
   drawing_priority("RIBVE-XX-DR-CE-0750 construction kerbing") > drawing_priority("Proposed Site Plan"))
ck("engineer external-works beats architect hard-landscaping",
   drawing_priority("External Construction Thickness Layout", source="engineer")
   > drawing_priority("Unit 1 Hard Landscaping", source="architect"))

print("unmarked pipeline (legend-anchored colour segmentation)")
try:
    import numpy as _np
    from takeoff_unmarked import segment_hatch
    _im = _np.full((200, 300, 3), 255, _np.uint8); _im[50:150, 60:210] = (216, 216, 216)  # 100x150 grey
    _comp = segment_hatch(_im, (216, 216, 216))
    ck("segment grey hatch ~15,000 px", _comp is not None and abs(int(_comp.sum()) - 15000) < 900)
    ck("segment ignores white background", int(_comp.sum()) < 200 * 300 * 0.4)
    _px = int(_comp.sum()); _area = _px * (1 / 2.0) ** 2 * 0.1 * 0.1   # S=2 (1px=0.5pt), k=0.1 m/pt
    ck("unmarked area math (px->m2)", abs(_area - _px * 0.0025) < 1e-6)
    ck("white-segmentation blowup blocked by plausibility", len(plausible(279905)) >= 1)
    _w = segment_hatch(_im, (255, 0, 0))   # colour not present
    ck("absent hatch colour -> no region", _w is None or int(_w.sum()) == 0)

    print("team feedback fixes (DEMO4)")
    from takeoff_unmarked import drawing_style
    # (a) drawing-style guard: solid fill = colour-coded; thin lines = line/hatch (don't guess on engineer sheets)
    _solid = _np.full((300, 300, 3), 255, _np.uint8); _solid[40:260, 40:260] = (120, 170, 90)
    ck("colour-coded sheet detected", drawing_style(_solid)[0] == "colour-coded")
    _lines = _np.full((300, 300, 3), 255, _np.uint8)
    for _i in range(0, 300, 12):
        _lines[:, _i] = (80, 80, 80)
    ck("line/hatch sheet detected", drawing_style(_lines)[0] == "line/hatch")
    # (b) dock-bay/void fix: a large interior void is kept as a DEDUCTION, not filled (team: D77 dock bays)
    _v = _np.full((400, 400, 3), 255, _np.uint8); _v[40:360, 40:360] = (214, 214, 214); _v[150:250, 150:250] = 255
    _kept = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=1.0)   # void=6.25 m² > 1 -> kept out
    _fill = segment_hatch(_v, (214, 214, 214), k=0.05, S=2.0, max_void_m2=999)   # huge thresh -> filled
    ck("large interior void kept as deduction", int(_kept.sum()) < int(_fill.sum()))
    ck("void filled only when below threshold", int(_fill.sum()) - int(_kept.sum()) > 8000)

    print("polygon contour (fan/spoke regression)")
    import math as _math
    from takeoff_unmarked import _hatch_contour
    # Non-convex yard: rectangle with a deep notch cut from the top edge (loading dock).
    # The old angular-sort-from-centroid tracer produced spokes radiating across the slab
    # (lines from a corner) because rays from the centroid cross the boundary >2 times.
    # cv2.findContours walks the perimeter in order, so the outline must be clean.
    _cmp = _np.zeros((700, 1000), bool)
    _cmp[120:560, 160:840] = True
    _cmp[120:340, 480:680] = False          # deep top-edge notch -> strongly non-star-shaped
    _poly = _hatch_contour(_cmp, S=2.0, max_pts=80)
    ck("hatch contour returned", _poly is not None and len(_poly) >= 4)
    _xs = [q[0] for q in _poly]; _ys = [q[1] for q in _poly]
    # Bounding box must match the slab extent in PDF pt (mask px / S): x 80..420, y 60..280.
    ck("contour bbox matches slab extent",
       abs(min(_xs)-80) < 3 and abs(max(_xs)-420) < 3 and
       abs(min(_ys)-60) < 3 and abs(max(_ys)-280) < 3)
    # Perimeter sanity: true outer+notch boundary ~1440 pt. The fan bug inflated this to
    # ~2450+ pt (spokes shooting across the shape). Require it within ~25% of truth.
    _seg = [_math.hypot(_xs[i]-_xs[i-1], _ys[i]-_ys[i-1]) for i in range(1, len(_xs))]
    _seg.append(_math.hypot(_xs[0]-_xs[-1], _ys[0]-_ys[-1]))
    ck("contour perimeter not inflated by spokes", 1100 < sum(_seg) < 1800)
    # Spoke signature: count radius oscillations (near->far->near) about the centroid.
    # A clean traced outline has very few; the fan pattern had ~23/78.
    _cx = sum(_xs)/len(_xs); _cy = sum(_ys)/len(_ys)
    _rad = [_math.hypot(x-_cx, y-_cy) for x, y in zip(_xs, _ys)]
    _osc = sum(1 for i in range(1, len(_rad)-1)
               if (_rad[i] > _rad[i-1]) != (_rad[i+1] > _rad[i]))
    ck("no fan/spoke oscillation", _osc <= 6)
    # Plain convex rectangle -> exactly its 4 corners; degenerate masks -> None.
    _rect = _np.zeros((600, 800), bool); _rect[150:450, 200:650] = True
    ck("rectangle -> 4 corners", len(_hatch_contour(_rect, S=2.0)) == 4)
    ck("empty mask -> None", _hatch_contour(_np.zeros((40, 40), bool), S=2.0) is None)
    _tiny = _np.zeros((40, 40), bool); _tiny[10:12, 10:12] = True
    ck("sub-pixel blob -> None", _hatch_contour(_tiny, S=2.0) is None)
except ImportError as _e:
    print(f"  [SKIP] takeoff_unmarked tests — missing dependency: {_e}")

print("scale_for verification logic (scale bar vs title block)")
try:
    from takeoff_unmarked import scale_for as _scale_for, SCALE_BAR_AGREE_TOL as _TOL
    import scale as _SC
    # PT_PER_M = 0.0254/72; k for 1:500 = 500 * PT_PER_M ≈ 0.176389 m/pt
    _PT_PER_M = 0.0254 / 72
    _k500 = 500 * _PT_PER_M   # ≈ 0.176389 m/pt

    # --- CASE 1: scale bar AGREES with title block (bar within ±3%) -> verified=True ---
    # Bar: 88 m / 500 pt = 0.176 m/pt; diff vs k500 ≈ 0.22% < 3%
    _c1 = canvas.Canvas("/tmp/_sf_agree.pdf", pagesize=(1400, 2200))
    _c1.drawString(100, 2100, "Drawing Scale 1:500")   # title-block text
    _c1.drawString(200, 120, "0          88 m")        # scale-bar label (88 m over 500 pt bar)
    _c1.line(100, 110, 600, 110)                        # 500 pt horizontal bar
    _c1.save()
    _k1, _v1, _n1, _src1 = _scale_for("/tmp/_sf_agree.pdf")
    ck("bar agrees with title -> verified=True",  _v1 is True, f"k={_k1:.5f} note={_n1[:60]}")
    ck("agree: bar in scale_sources",             "scale_bar" in _src1)
    ck("agree: title_block in scale_sources",     "title_block" in _src1)
    ck("agree: returned k close to bar",          _k1 is not None and abs(_k1 - 88/500) < 1e-6)

    # --- CASE 2: scale bar DISAGREES with title block (>3%) -> verified=False ---
    # Bar: 150 m / 500 pt = 0.30 m/pt; diff vs k500 ≈ 70% >> 3%
    _c2 = canvas.Canvas("/tmp/_sf_disagree.pdf", pagesize=(1400, 2200))
    _c2.drawString(100, 2100, "Drawing Scale 1:500")
    _c2.drawString(200, 120, "0         150 m")
    _c2.line(100, 110, 600, 110)
    _c2.save()
    _k2, _v2, _n2, _src2 = _scale_for("/tmp/_sf_disagree.pdf")
    ck("bar disagrees with title -> verified=False", _v2 is False, f"k={_k2:.5f} note={_n2[:60]}")
    ck("disagree: note mentions disagrees",         "DISAGREES" in _n2 or "disagrees" in _n2.lower())
    ck("disagree: bar k still used",               _k2 is not None and abs(_k2 - 150/500) < 1e-6)

    # --- CASE 3: no scale bar, title block only -> verified=False ---
    _c3 = canvas.Canvas("/tmp/_sf_titleonly.pdf", pagesize=(1400, 2200))
    _c3.drawString(100, 2100, "Drawing Scale 1:500")   # title-block only, no bar line or label
    _c3.save()
    _k3, _v3, _n3, _src3 = _scale_for("/tmp/_sf_titleonly.pdf")
    ck("title-only -> verified=False",            _v3 is False, f"note={_n3[:60]}")
    ck("title-only: title_block in scale_sources", "title_block" in _src3)
    ck("title-only: no scale_bar in scale_sources", "scale_bar" not in _src3)
    ck("title-only: k close to k500",            _k3 is not None and abs(_k3 - _k500) < 1e-5)

except ImportError as _e:
    print(f"  [SKIP] scale_for tests — missing dependency: {_e}")

print("defaults (Fortel build-up assumptions)")
from defaults import spec_with_defaults, assumption_note, flag_assumed
_s, _assumed = spec_with_defaults()
ck("default spec depth 190mm", _s["depth_mm"] == 190)
ck("default spec mesh A252",   _s["mesh"] == "A252")
ck("default assumed=True",     _assumed is True)
ck("assumption note contains 190mm", "190 mm" in assumption_note(_s))
ck("flags empty when fully specified",
   flag_assumed({"depth_mm":200,"mesh":"A393","layers":1,"conc_mix":"C32/40"}, False) == [])
ck("flags non-empty when assumed", len(flag_assumed(_s, True)) >= 1)
_s2, _a2 = spec_with_defaults({"depth_mm": 175, "mesh": "A193", "layers": 1, "conc_mix": "C32/40"})
ck("full engineer spec -> assumed=False", _a2 is False)
ck("engineer depth 175 overrides default 190", _s2["depth_mm"] == 175)

print("spec extractor (construction-detail PDF text parsing)")
from spec_extractor import extract_spec_from_text
_e1 = extract_spec_from_text("175 mm thick with A193 mesh, C32/40 concrete")
ck("depth 175",  _e1.get("depth_mm") == 175)
ck("mesh A193",  _e1.get("mesh") == "A193")
ck("mix C32/40", _e1.get("conc_mix") == "C32/40")
_e2 = extract_spec_from_text("200mm slab with two layers of A393 reinforcement C35/45")
ck("depth 200",  _e2.get("depth_mm") == 200)
ck("2 layers A393", _e2.get("mesh") == "A393" and _e2.get("layers") == 2)
_e3 = extract_spec_from_text("No specification provided")
ck("empty text -> empty spec", not any(k in _e3 for k in ("depth_mm","mesh","conc_mix")))
_e4 = extract_spec_from_text("A393 x2 250 mm C40/50")
ck("x2 notation -> 2 layers", _e4.get("layers") == 2)

print("quotation generator")
from quotation import generate_quotation, quotation_text, quotation_html
_demo_result = {
    "file": "D77.pdf", "type": "UNMARKED vector", "confidence": "medium",
    "source_discipline": "architect",
    "costing": {
        "area_m2": 3172, "rate": 44.89, "total_gbp": 142391.08, "assumed": True,
        "spec": {"depth_mm": 190, "mesh": "A252", "conc_mix": "C32/40", "layers": 1, "conc_rate": 128},
        "breakdown": {"concrete": 25.05, "steel": 4.30, "dpm": 0.46,
                      "curing": 0.23, "labour": 10.00, "trim": 0.40, "nett": 40.44, "margin%": 11},
    },
    "flags": ["BUILD-UP ASSUMED: 190mm / A252 / C32/40"],
}
_q = generate_quotation(_demo_result, project="Test", client="Client", ref="TST-001")
ck("quotation total > 0",     _q["total_gbp"] > 0)
ck("quotation assumed=True",  _q["assumed"] is True)
ck("has declaration",         len(_q["declarations"]) >= 1)
ck("slab line item present",  any("slab" in li["description"].lower() for li in _q["line_items"]))
ck("text contains total",     "TOTAL NETT" in quotation_text(_q))
ck("html contains total",     "TOTAL NETT" in quotation_html(_q) or "Total" in quotation_html(_q))
ck("html is valid-ish",       quotation_html(_q).startswith("<!DOCTYPE html>"))

print("pipeline price_with_defaults")
import contextlib, io as _io
with contextlib.redirect_stdout(_io.StringIO()):
    from takeoff_pipeline import price_with_defaults, _needs_approval
_c = price_with_defaults(26080)
ck("26,080 m² at defaults -> £1,175,425.60", _c["total_gbp"] == 1175425.60)
ck("price_with_defaults assumed=True (no spec)", _c["assumed"] is True)
_c2 = price_with_defaults(3172, {"depth_mm": 200, "mesh": "A393", "layers": 1,
                                  "conc_mix": "C32/40", "conc_rate": 128})
ck("partial spec -> assumed=False", _c2["assumed"] is False)
ck("approval trigger on assessor flag",
   _needs_approval({"type":"UNMARKED vector","confidence":"medium",
                    "flags":["assessor: confirm extent + scale"]}))
ck("no approval trigger on clean marked",
   not _needs_approval({"type":"MARKED vector","confidence":"high","flags":[]}))

print("spec extractor — supplier fields")
from spec_extractor import extract_spec_from_text
_s5 = extract_spec_from_text("20mm crushed aggregate, 0.45 w/c ratio, S3 slump, air-entrained")
ck("aggregate 20mm extracted",  _s5.get("aggregate_mm") == 20)
ck("wc_ratio 0.45 extracted",   abs(_s5.get("wc_ratio", 0) - 0.45) < 0.001)
ck("slump S3 extracted",        _s5.get("slump_class") == "S3")
ck("air_entrained extracted",   _s5.get("air_entrained") is True)
_s6 = extract_spec_from_text("12mm aggregate 0.50 w/c S4 slump class CEM I")
ck("aggregate 12mm extracted",  _s6.get("aggregate_mm") == 12)
ck("slump S4 extracted",        _s6.get("slump_class") == "S4")

print("supplier inquiry generator")
from supplier_inquiry import generate_inquiry, format_cubes
ck("cubes calc 26080m² 190mm 3%",
   format_cubes(26080, 190, 0.03) == round(26080 * 0.190 * 1.03, 1))
_demo = {
    "area_m2": 3172, "project_name": "Test Project", "project_ref": "2132",
    "costing": {"spec": {"depth_mm": 190, "conc_mix": "C32/40", "cement_type": "CEM I",
                          "air_entrained": True, "aggregate_mm": 20, "wc_ratio": 0.45,
                          "slump_class": "S3", "conc_wastage": 0.03}}
}
_inq = generate_inquiry(_demo)
ck("inquiry has subject",        bool(_inq["subject"]))
ck("inquiry subject has mix",    "C32/40" in _inq["subject"])
ck("inquiry subject has cubes",  "m³" in _inq["subject"])
ck("inquiry text has slump",     "S3" in _inq["text"])
ck("inquiry text has aggregate", "20 mm" in _inq["text"])
ck("inquiry text has wc",        "0.45" in _inq["text"])
ck("inquiry html starts DOCTYPE","<!DOCTYPE" in _inq["html"])
ck("inquiry cubes > 0",          _inq["cubes_m3"] > 0)
_inq_no_proj = generate_inquiry({"area_m2": 1000, "costing": {}})
ck("inquiry works with no project info", bool(_inq_no_proj["subject"]))

print("measurement state machine (sanity.measurement_state)")
from sanity import measurement_state, MEASURED_VERIFIED, MEASURED_UNVERIFIED, UNMEASURED, REJECTED
ck("verified + high conf -> MEASURED_VERIFIED",
   measurement_state(26080, scale_verified=True, confidence="high")[0] == MEASURED_VERIFIED)
ck("unverified scale -> MEASURED_UNVERIFIED",
   measurement_state(26080, scale_verified=False)[0] == MEASURED_UNVERIFIED)
ck("low confidence -> MEASURED_UNVERIFIED",
   measurement_state(100, scale_verified=True, confidence="low")[0] == MEASURED_UNVERIFIED)
ck("implausible area -> MEASURED_UNVERIFIED (blocks pricing AND approval)",
   measurement_state(95463, site_m2=34329)[0] == MEASURED_UNVERIFIED)
ck("over single-zone bound -> MEASURED_UNVERIFIED",
   measurement_state(70000, scale_verified=True, confidence="high")[0] == MEASURED_UNVERIFIED)
ck("no area -> UNMEASURED", measurement_state(None)[0] == UNMEASURED)
ck("rejected_reason short-circuits -> REJECTED",
   measurement_state(26080, scale_verified=True, rejected_reason="encrypted PDF")[0] == REJECTED)
ck("plausible + verified -> no flags", measurement_state(26080, scale_verified=True, confidence="high")[1] == [])

print("multi-page routing (router.rank_pages / classify_page)")
try:
    from reportlab.pdfgen import canvas as _canvas
    import fitz as _fitz
    from router import rank_pages, classify_page, drawing_priority
    _d = _fitz.open()
    _p0 = _d.new_page(width=1000, height=1000)
    _p0.insert_text((50, 50), "Proposed Site Plan")
    for _i in range(5):
        _p0.draw_line((100 + _i, 100), (100 + _i, 200))     # < 50 vector paths -> low priority / raster-ish
    _p1 = _d.new_page(width=1000, height=1000)
    _p1.insert_text((50, 50), "External Construction Thickness Layout")
    for _i in range(60):
        _p1.draw_line((100 + _i, 300), (100 + _i, 400))     # >= 50 vector paths -> UNMARKED vector
    _d.save("/tmp/_ci_rank_test.pdf")

    _ranked = rank_pages("/tmp/_ci_rank_test.pdf")
    ck("rank_pages classifies every page", len(_ranked) == 2)
    ck("rank_pages best candidate is page 1 (construction-thickness beats site plan)",
       _ranked[0]["page"] == 1)
    ck("rank_pages best candidate score > runner-up", _ranked[0]["score"] > _ranked[1]["score"])
    ck("classify_page(1) matches rank_pages page-1 type",
       classify_page("/tmp/_ci_rank_test.pdf", 1)[0] == _ranked[0]["type"])
    ck("classify_page(0) is page-0-only (never assumes the whole doc)",
       classify_page("/tmp/_ci_rank_test.pdf", 0)[0] == "RASTER / scanned")
except ImportError as _e:
    print(f"  [SKIP] router multi-page tests — missing dependency: {_e}")

print("pipeline multi-page + raster UNMEASURED (takeoff_pipeline.takeoff)")
try:
    import os as _os
    _os.environ["SKIP_APPROVAL_LOG"] = "1"
    with contextlib.redirect_stdout(_io.StringIO()):
        from takeoff_pipeline import takeoff as _pipeline_takeoff
    import fitz as _fitz2

    # Multi-page MARKED pack: page 0 is a decoy site plan, page 1 has the priced markup.
    # takeoff() must measure page 1, not silently default to page 0.
    _mp = _fitz2.open()
    _mp0 = _mp.new_page(width=1400, height=2200)
    _mp0.insert_text((100, 100), "Proposed Site Plan")
    for _i in range(5):
        _mp0.draw_line((100 + _i, 300), (100 + _i, 400))
    _mp1 = _mp.new_page(width=1400, height=2200)
    _mp1.insert_text((100, 100), "External Construction Thickness Layout")
    for _i in range(60):
        _mp1.draw_line((100 + _i, 300), (100 + _i, 400))
    _annot = _mp1.add_polygon_annot([(100, 100), (600, 100), (600, 500), (100, 500)])
    _annot.set_info(content="Area = 3000.0 sq m")
    _annot.update()
    _mp.save("/tmp/_ci_pipeline_multipage.pdf")

    with contextlib.redirect_stdout(_io.StringIO()):
        _rmp = _pipeline_takeoff("/tmp/_ci_pipeline_multipage.pdf")
    ck("multi-page pipeline measures the ranked page, not page 0", _rmp.get("page") == 1)
    ck("multi-page pipeline area comes from page 1's markup", _rmp.get("area_m2") == 3000.0)
    ck("multi-page pipeline flags which page was chosen",
       any("MULTI-PAGE" in f and "page 1 of 2" in f for f in _rmp.get("flags", [])))
    ck("multi-page pipeline lists the other candidate page",
       any("other candidates" in f for f in _rmp.get("flags", [])))

    # Single-page MARKED vector -> MEASURED_VERIFIED, matches the four-state contract.
    _sp = _fitz2.open()
    _spp = _sp.new_page(width=1400, height=2200)
    for _i in range(60):
        _spp.draw_line((100 + _i, 100), (100 + _i, 200))
    _sannot = _spp.add_polygon_annot([(100, 100), (600, 100), (600, 500), (100, 500)])
    _sannot.set_info(content="Area = 2000.0 sq m")
    _sannot.update()
    _sp.save("/tmp/_ci_pipeline_marked.pdf")
    with contextlib.redirect_stdout(_io.StringIO()):
        _rsp = _pipeline_takeoff("/tmp/_ci_pipeline_marked.pdf")
    ck("MARKED vector -> MEASURED_VERIFIED", _rsp.get("measurement_state") == MEASURED_VERIFIED)
    ck("MARKED vector -> status mirrors measurement_state", _rsp.get("status") == MEASURED_VERIFIED)
    ck("MARKED vector -> needs_assessor False", _rsp.get("needs_assessor") is False)

    # RASTER/scanned (few vector paths, e.g. a scanned/flattened sheet) -> proper UNMEASURED
    # job, never a bare flag-only stub. area_m2 stays None; needs_assessor True.
    _rast = _fitz2.open()
    _rp = _rast.new_page(width=1400, height=2200)
    _rp.insert_text((100, 100), "Scanned Site Photo")   # < 50 vector paths -> RASTER / scanned
    _rast.save("/tmp/_ci_pipeline_raster.pdf")
    with contextlib.redirect_stdout(_io.StringIO()):
        _rr = _pipeline_takeoff("/tmp/_ci_pipeline_raster.pdf")
    ck("raster drawing -> area_m2 stays None", _rr.get("area_m2") is None)
    ck("raster drawing -> UNMEASURED (not a crash, not a bare flag)",
       _rr.get("measurement_state") == UNMEASURED)
    ck("raster drawing -> needs_assessor True", _rr.get("needs_assessor") is True)
    ck("raster drawing -> flag explains mandatory assessor trace",
       any("mandatory assessor trace" in f.lower() or "UNMEASURED" in f for f in _rr.get("flags", [])))
except ImportError as _e:
    print(f"  [SKIP] pipeline multi-page/raster tests — missing dependency: {_e}")

print("D77 accuracy invariant (measurement math unchanged)")
try:
    from takeoff_unmarked import takeoff as _tu_takeoff
    _d77 = _tu_takeoff("drawings/_int_d77.pdf")
    ck("D77 area unchanged at 3,159 m² (Smita gold 3,156)", _d77.get("area_m2") == 3159.0)
    ck("D77 scale verified True (bar agrees with title via scale_consensus)",
       _d77.get("scale_verified") is True)
    ck("D77 measurement_state MEASURED_VERIFIED", _d77.get("measurement_state") == MEASURED_VERIFIED)
    ck("D77 needs_assessor False", _d77.get("needs_assessor") is False)
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 accuracy test — missing dependency or file: {_e}")

print("D77 border/legend exclusion (Aryan field report: real SGP sheet over-measures by "
      "border strips + legend swatch that share the yard's grey)")
try:
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
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 border/legend exclusion test — missing dependency or file: {_e}")

print("manhole counting — MARKED path (robust_takeoff.count_manholes_marked)")
try:
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
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] manhole counting (marked path) test — missing dependency or file: {_e}")

print("manhole counting — UNMARKED path (takeoff_unmarked.detect_manholes, conservative ESTIMATE)")
try:
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
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] manhole counting (unmarked path) test — missing dependency or file: {_e}")

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

print("approval_server: upload format handling + approve hard-block")
try:
    import approval_server as _AS
    import fitz as _fitz3, zipfile as _zipfile, tempfile as _tempfile

    _tmpdir = Path(_tempfile.mkdtemp(prefix="ci_upload_"))

    # .zip with two PDFs -> both extracted, ranked by drawing_priority, no zip-slip
    _pdf_a = _tmpdir / "Proposed Site Plan.pdf"
    _pdf_b = _tmpdir / "External Construction Thickness Layout.pdf"
    for _pp in (_pdf_a, _pdf_b):
        _dd = _fitz3.open(); _dd.new_page(); _dd.save(str(_pp))
    _zip_path = _tmpdir / "pack.zip"
    with _zipfile.ZipFile(_zip_path, "w") as _zf:
        _zf.write(_pdf_a, _pdf_a.name)
        _zf.write(_pdf_b, _pdf_b.name)
    _extracted, _zflags = _AS._safe_extract_zip(_zip_path, _tmpdir)
    ck("zip extraction pulls both PDFs", len(_extracted) == 2)
    _ranked_zip = _AS._rank_pdfs_by_priority(_extracted)
    ck("zip PDFs ranked — construction-thickness beats site plan",
       "Construction_Thickness" in _ranked_zip[0].name or "Thickness" in _ranked_zip[0].name)

    # zip-slip guard: a malicious entry name must never escape dest_dir
    _evil_zip = _tmpdir / "evil.zip"
    with _zipfile.ZipFile(_evil_zip, "w") as _zf:
        _zf.writestr("../../etc/evil.pdf", b"%PDF-1.4 fake")
    _esc_before = set(_tmpdir.parent.glob("evil.pdf"))
    _extracted_evil, _eflags = _AS._safe_extract_zip(_evil_zip, _tmpdir)
    ck("zip-slip entry sanitised to a safe basename (stays inside dest_dir)",
       all(str(p).startswith(str(_tmpdir.resolve())) for p in _extracted_evil))

    # encrypted / zero-byte PDF -> rejected reason, not a crash
    _zero = _tmpdir / "zero.pdf"; _zero.write_bytes(b"")
    _doc, _reason = _AS._open_pdf_safely(_zero)
    ck("zero-byte PDF -> rejected with reason (not a crash)", _doc is None and "zero-byte" in _reason)

    _enc = _tmpdir / "enc.pdf"
    _ed = _fitz3.open(); _ed.new_page()
    _ed.save(str(_enc), encryption=_fitz3.PDF_ENCRYPT_AES_256, owner_pw="x", user_pw="y")
    _doc2, _reason2 = _AS._open_pdf_safely(_enc)
    ck("encrypted PDF -> rejected with reason (not a crash)",
       _doc2 is None and "encrypted" in _reason2.lower())

    # approve hard-block mirrors the >£200k escalation guard mechanism (fb5b92b)
    ck("UNMEASURED job blocks approve",
       _AS._approve_block_reason({"measurement_state": "UNMEASURED", "scale_confirmed": False}) is not None)
    ck("MEASURED_UNVERIFIED job blocks approve",
       _AS._approve_block_reason({"measurement_state": "MEASURED_UNVERIFIED", "scale_confirmed": False}) is not None)
    ck("MEASURED_VERIFIED job does not block approve",
       _AS._approve_block_reason({"measurement_state": "MEASURED_VERIFIED", "scale_confirmed": False}) is None)
    ck("assessor-confirmed UNMEASURED job no longer blocks approve",
       _AS._approve_block_reason({"measurement_state": "UNMEASURED", "scale_confirmed": True}) is None)
    ck("REJECTED job blocks approve", _AS._approve_block_reason({"measurement_state": "REJECTED"}) is not None)

    shutil.rmtree(_tmpdir, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server upload/approve tests — missing dependency: {_e}")

print("approval_server: /snapshot status codes for all four measurement states "
      "(Aryan field report — 'session which renders screenshots is not working properly')")
try:
    import approval_server as _AS2
    import fitz as _fitz4, uuid as _uuid2, tempfile as _tempfile2

    _client = _AS2.app.test_client()
    _tmpdir2 = Path(_tempfile2.mkdtemp(prefix="ci_snapshot_"))

    # Save/restore the real jobs file around this block — snapshot() reads via load_jobs()
    # which is a real file read, not mockable without a live Flask app context.
    _jobs_backup = _AS2.JOBS_FILE.read_text() if _AS2.JOBS_FILE.exists() else None

    def _mk_pdf(path, w=600, h=400, n_pages=1):
        d = _fitz4.open()
        for _ in range(n_pages):
            d.new_page(width=w, height=h)
        d.save(str(path))
        return path

    try:
        _jobs = _AS2.load_jobs()

        # 1. REJECTED job (no pdf_path at all) -> 404, not 500
        _jid_rej = str(_uuid2.uuid4())
        _jobs[_jid_rej] = {"id": _jid_rej, "status": "rejected", "measurement_state": "REJECTED",
                           "pdf_path": None, "result": {"measurement_state": "REJECTED"}}

        # 2. UNMEASURED job with a real PDF on disk -> 200 (assessor still needs to see it to trace)
        _pdf_unm = _mk_pdf(_tmpdir2 / "unmeasured.pdf")
        _jid_unm = str(_uuid2.uuid4())
        _jobs[_jid_unm] = {"id": _jid_unm, "status": "error", "measurement_state": "UNMEASURED",
                           "pdf_path": str(_pdf_unm),
                           "result": {"pdf_path": str(_pdf_unm), "page": 0, "measurement_state": "UNMEASURED"}}

        # 3. UNMEASURED job whose PDF is missing from disk (temp dir cleaned up) -> 404, not 500
        _jid_gone = str(_uuid2.uuid4())
        _jobs[_jid_gone] = {"id": _jid_gone, "status": "error", "measurement_state": "UNMEASURED",
                            "pdf_path": str(_tmpdir2 / "does_not_exist.pdf"),
                            "result": {"pdf_path": str(_tmpdir2 / "does_not_exist.pdf"),
                                      "measurement_state": "UNMEASURED"}}

        # 4. MEASURED_VERIFIED multi-page job whose result["page"] != 0 -> snapshot must render
        # THAT page (this was the root cause of "AI polygon not shown": /snapshot always
        # rendered page 0 regardless of which page the pipeline actually measured).
        _pdf_multi = _mk_pdf(_tmpdir2 / "multi.pdf", n_pages=3)
        _jid_page = str(_uuid2.uuid4())
        _jobs[_jid_page] = {"id": _jid_page, "status": "pending", "measurement_state": "MEASURED_VERIFIED",
                            "pdf_path": str(_pdf_multi),
                            "result": {"pdf_path": str(_pdf_multi), "page": 2,
                                      "polygon_pts": [[10, 10], [100, 10], [100, 100], [10, 100]],
                                      "measurement_state": "MEASURED_VERIFIED"}}

        # 5. Out-of-range page index (stale data) -> must fall back to page 0, never 500
        _jid_badpage = str(_uuid2.uuid4())
        _jobs[_jid_badpage] = {"id": _jid_badpage, "status": "pending", "measurement_state": "MEASURED_VERIFIED",
                               "pdf_path": str(_pdf_multi),
                               "result": {"pdf_path": str(_pdf_multi), "page": 99,
                                         "measurement_state": "MEASURED_VERIFIED"}}

        _AS2.save_jobs(_jobs)

        _r_rej = _client.get(f"/snapshot/{_jid_rej}")
        ck("REJECTED job snapshot -> 404 (not 500)", _r_rej.status_code == 404, _r_rej.status_code)

        _r_unm = _client.get(f"/snapshot/{_jid_unm}")
        ck("UNMEASURED job with PDF on disk -> 200 (assessor can still trace)",
           _r_unm.status_code == 200, _r_unm.status_code)

        _r_gone = _client.get(f"/snapshot/{_jid_gone}")
        ck("UNMEASURED job with missing PDF -> 404 (not 500)", _r_gone.status_code == 404, _r_gone.status_code)

        _r_page = _client.get(f"/snapshot/{_jid_page}")
        ck("multi-page job snapshot -> 200", _r_page.status_code == 200, _r_page.status_code)
        # Verify it actually rendered page 2's dimensions, not page 0's (both pages here are
        # the same size so we check indirectly: render page 2 directly and diff against the
        # response bytes' pixel dimensions via the PNG header — same width guaranteed by
        # construction, so the meaningful assertion is the X-Snapshot-Scale header matches
        # snapshot_scale() computed for page 2 specifically.
        from approval_email import snapshot_scale as _snap_scale_fn
        _expected_scale = f"{_snap_scale_fn(str(_pdf_multi), page=2):.6f}"
        ck("multi-page snapshot X-Snapshot-Scale computed for the MEASURED page (not page 0)",
           _r_page.headers.get("X-Snapshot-Scale") == _expected_scale,
           (_r_page.headers.get("X-Snapshot-Scale"), _expected_scale))

        _r_badpage = _client.get(f"/snapshot/{_jid_badpage}")
        ck("out-of-range page index falls back to page 0 (not 500)",
           _r_badpage.status_code == 200, _r_badpage.status_code)

        _r_404job = _client.get(f"/snapshot/{_uuid2.uuid4()}")
        ck("nonexistent job -> 404", _r_404job.status_code == 404, _r_404job.status_code)

    finally:
        if _jobs_backup is not None:
            _AS2.JOBS_FILE.write_text(_jobs_backup)
        shutil.rmtree(_tmpdir2, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server snapshot tests — missing dependency: {_e}")

print("approval_server: watchdog-vs-completion race (Aryan field report — 'server is unstable')")
try:
    import approval_server as _AS3
    import sys as _sys3, time as _time3, uuid as _uuid3
    from unittest import mock as _mock3

    _jobs_backup3 = _AS3.JOBS_FILE.read_text() if _AS3.JOBS_FILE.exists() else None
    try:
        _jid_wd = str(_uuid3.uuid4())
        _jobs3 = _AS3.load_jobs()
        _jobs3[_jid_wd] = {"id": _jid_wd, "status": "processing", "flags": []}
        _AS3.save_jobs(_jobs3)

        # _mark_job_unmeasured with watchdog_fired=True sets the sentinel used to detect the race
        _AS3._mark_job_unmeasured(_jid_wd, "PIPELINE TIMEOUT: took too long", watchdog_fired=True)
        _j_after_wd = _AS3.load_jobs()[_jid_wd]
        ck("watchdog fire sets _watchdog_fired sentinel", _j_after_wd.get("_watchdog_fired") is True)
        ck("watchdog fire flips job to UNMEASURED", _j_after_wd.get("measurement_state") == "UNMEASURED")
        ck("watchdog fire records a PIPELINE TIMEOUT flag",
           any("PIPELINE TIMEOUT" in f for f in _j_after_wd.get("flags", [])))

        # Now simulate the pipeline finishing LATE (after the watchdog already fired) by
        # driving the real _run_takeoff() with a stubbed takeoff_pipeline module whose
        # takeoff() sleeps past a 1s watchdog timeout — exercises the actual production
        # code path, not a re-implementation of its logic.
        _orig_timeout = _AS3.TAKEOFF_TIMEOUT_S
        _AS3.TAKEOFF_TIMEOUT_S = 1
        _jid_wd2 = str(_uuid3.uuid4())
        _jobs3 = _AS3.load_jobs()
        _jobs3[_jid_wd2] = {"id": _jid_wd2, "status": "processing", "flags": []}
        _AS3.save_jobs(_jobs3)

        _fake_pipeline = _mock3.MagicMock()
        def _slow_takeoff(pdf_path, project_name=None, project_ref=None):
            _time3.sleep(2.2)
            return {"measurement_state": "MEASURED_VERIFIED", "area_m2": 3159.0,
                    "flags": ["completed ok"], "project_name": project_name, "project_ref": project_ref}
        _fake_pipeline.takeoff = _slow_takeoff
        _real_module = _sys3.modules.get("takeoff_pipeline")
        _sys3.modules["takeoff_pipeline"] = _fake_pipeline
        try:
            _AS3._run_takeoff(_jid_wd2, "drawings/_int_d77.pdf", "QA WD race", "QA-PORTAL-CI-WDRACE")
        finally:
            if _real_module is not None:
                _sys3.modules["takeoff_pipeline"] = _real_module
            else:
                _sys3.modules.pop("takeoff_pipeline", None)
            _AS3.TAKEOFF_TIMEOUT_S = _orig_timeout

        _j_final = _AS3.load_jobs()[_jid_wd2]
        ck("late pipeline completion overwrites watchdog UNMEASURED with the real result",
           _j_final.get("measurement_state") == "MEASURED_VERIFIED", _j_final.get("measurement_state"))
        ck("stale 'PIPELINE TIMEOUT' flag stripped once the pipeline actually completes",
           not any("PIPELINE TIMEOUT" in f for f in _j_final.get("flags", [])), _j_final.get("flags"))
        ck("_watchdog_fired sentinel cleared after the race resolves",
           "_watchdog_fired" not in _j_final)

        _jobs3 = _AS3.load_jobs()
        _jobs3.pop(_jid_wd, None); _jobs3.pop(_jid_wd2, None)
        _AS3.save_jobs(_jobs3)
    finally:
        if _jobs_backup3 is not None:
            _AS3.JOBS_FILE.write_text(_jobs_backup3)
except ImportError as _e:
    print(f"  [SKIP] approval_server watchdog-race tests — missing dependency: {_e}")

print("approval_server: approval_jobs.json concurrent read/write does not raise "
      "(Aryan field report — 'the server is unstable')")
try:
    import approval_server as _AS4
    import threading as _threading4, tempfile as _tempfile4

    _tmp_jobs_file = Path(_tempfile4.mkdtemp(prefix="ci_atomic_")) / "jobs.json"
    _orig_jobs_file = _AS4.JOBS_FILE
    _AS4.JOBS_FILE = _tmp_jobs_file
    try:
        _big = {str(_i): {"x": "y" * 500} for _i in range(500)}
        _AS4.save_jobs(_big)

        _errors4 = []
        def _reader4():
            for _ in range(150):
                try:
                    _d = _AS4.load_jobs()
                    if not isinstance(_d, dict):
                        _errors4.append("load_jobs did not return a dict")
                except Exception as _e:
                    _errors4.append(str(_e))

        def _writer4():
            for _ in range(150):
                _AS4.save_jobs(_big)

        _t1 = _threading4.Thread(target=_reader4)
        _t2 = _threading4.Thread(target=_writer4)
        _t1.start(); _t2.start(); _t1.join(); _t2.join()

        ck("concurrent load_jobs()/save_jobs() never raises or returns a torn read",
           len(_errors4) == 0, _errors4[:3])
        ck("no leftover .tmp files after concurrent saves",
           list(_tmp_jobs_file.parent.glob("*.tmp*")) == [])
    finally:
        _AS4.JOBS_FILE = _orig_jobs_file
        shutil.rmtree(_tmp_jobs_file.parent, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server atomic-write tests — missing dependency: {_e}")

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
sys.exit(0 if all(P) else 1)
