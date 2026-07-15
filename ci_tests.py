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

class _FixtureNotPresent(Exception):
    pass

def _require_fixture(path, reason):
    if not Path(path).exists():
        raise _FixtureNotPresent(reason)

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

    # --- CASE 2: scale bar DISAGREES with title block (>3%) -> verified=False. Bar: 150 m / 500 pt
    # = 0.30 m/pt (implies ~1:850, an individually PLAUSIBLE drawing ratio) vs title k500 ≈ 70% off.
    # Both sources are plausible on their own -> this is the MIXED/DISAGREE branch (CLAUDE.md
    # invariant 3: disagreement -> refuse, don't auto-pick). Neither is silently adopted; the
    # title-block k is used for display and the assessor must set the scale explicitly.
    _c2 = canvas.Canvas("/tmp/_sf_disagree.pdf", pagesize=(1400, 2200))
    _c2.drawString(100, 2100, "Drawing Scale 1:500")
    _c2.drawString(200, 120, "0         150 m")
    _c2.line(100, 110, 600, 110)
    _c2.save()
    _k2, _v2, _n2, _src2 = _scale_for("/tmp/_sf_disagree.pdf")
    ck("bar disagrees with title -> verified=False", _v2 is False, f"k={_k2:.5f} note={_n2[:60]}")
    ck("disagree: note flags MIXED/DISAGREE",        "MIXED/DISAGREE" in _n2)
    ck("disagree: NOT auto-picked to bar k (title k used instead)",
       _k2 is not None and abs(_k2 - _k500) < 1e-6)

    # --- CASE 3: no scale bar, title block only -> verified=False ---
    _c3 = canvas.Canvas("/tmp/_sf_titleonly.pdf", pagesize=(1400, 2200))
    _c3.drawString(100, 2100, "Drawing Scale 1:500")   # title-block only, no bar line or label
    _c3.save()
    _k3, _v3, _n3, _src3 = _scale_for("/tmp/_sf_titleonly.pdf")
    ck("title-only -> verified=False",            _v3 is False, f"note={_n3[:60]}")
    ck("title-only: title_block in scale_sources", "title_block" in _src3)
    ck("title-only: no scale_bar in scale_sources", "scale_bar" not in _src3)
    ck("title-only: k close to k500",            _k3 is not None and abs(_k3 - _k500) < 1e-5)

    # --- CASE 4: bar DISAGREES with title AND the bar-implied ratio is IMPLAUSIBLE (false
    # scale-bar anchor, e.g. an unrelated dimension callout mis-paired to a nearby short line
    # fragment) -> reject the bar entirely, fall back to title-block k, still UNVERIFIED.
    # Reproduces the real corpus incident: Proposed_Gatehouse's "7016 m / 34 pt" bar candidate
    # implies k=205.868 m/pt (~1:583,563) which is nowhere near a real drawing scale.
    # Bar: 7016 m / 34 pt = 206.35 m/pt -> implied ~1:584,000, way outside 1:20-1:5000.
    _c4 = canvas.Canvas("/tmp/_sf_implausible.pdf", pagesize=(1400, 2200))
    _c4.drawString(100, 2100, "Drawing Scale 1:1250")
    _c4.drawString(200, 120, "0          7016 m")
    _c4.line(100, 110, 134, 110)                        # 34 pt bar
    _c4.save()
    _k4, _v4, _n4, _src4 = _scale_for("/tmp/_sf_implausible.pdf")
    _k1250 = 1250 * _PT_PER_M
    ck("implausible bar -> verified=False",        _v4 is False, f"k={_k4:.5f} note={_n4[:70]}")
    ck("implausible bar -> note says rejected",     "rejected as implausible" in _n4)
    ck("implausible bar -> falls back to title k",  _k4 is not None and abs(_k4 - _k1250) < 1e-5)
    ck("implausible bar -> sources still recorded", "scale_bar" in _src4 and "title_block" in _src4)

    # --- CASE 5: bar DISAGREES with title but BOTH are individually plausible drawing ratios
    # (e.g. a genuine 1:2500 site-location viewport vs a stale 1:1500 title block) -> MIXED/
    # DISAGREE. Must NOT auto-pick either side; verified stays False; title k shown for display.
    # Reproduces the real corpus incident: Site_Location_Plan's "100 m / 113 pt" bar (k=0.882,
    # ~1:2500 — a perfectly plausible ratio) disagreeing with the sheet's stated title 1:1500.
    _c5 = canvas.Canvas("/tmp/_sf_mixed.pdf", pagesize=(1400, 2200))
    _c5.drawString(100, 2100, "Drawing Scale 1:1500")
    _c5.drawString(200, 120, "0          100 m")
    _c5.line(100, 110, 213, 110)                        # 113 pt bar -> k=0.885 (~1:2504, plausible)
    _c5.save()
    _k5, _v5, _n5, _src5 = _scale_for("/tmp/_sf_mixed.pdf")
    _k1500 = 1500 * _PT_PER_M
    ck("mixed/disagree -> verified=False",          _v5 is False, f"k={_k5:.5f} note={_n5[:70]}")
    ck("mixed/disagree -> note says MIXED/DISAGREE", "MIXED/DISAGREE" in _n5)
    ck("mixed/disagree -> returns title k (no auto-pick of bar)",
       _k5 is not None and abs(_k5 - _k1500) < 1e-5)
    ck("mixed/disagree -> sources still recorded",  "scale_bar" in _src5 and "title_block" in _src5)

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
    _require_fixture("drawings/_int_d77.pdf", "D77 accuracy test")
    from takeoff_unmarked import takeoff as _tu_takeoff
    _d77 = _tu_takeoff("drawings/_int_d77.pdf")
    ck("D77 area unchanged at 3,159 m² (Smita gold 3,156)", _d77.get("area_m2") == 3159.0)
    ck("D77 scale verified True (bar agrees with title via scale_consensus)",
       _d77.get("scale_verified") is True)
    ck("D77 measurement_state MEASURED_VERIFIED", _d77.get("measurement_state") == MEASURED_VERIFIED)
    ck("D77 needs_assessor False", _d77.get("needs_assessor") is False)
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
    import fitz as _fitz3, zipfile as _zipfile, tempfile as _tempfile, io as _io3

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

    _orig_jobs_file_up = _AS.JOBS_FILE
    _orig_backup_dir_up = _AS.BACKUP_DIR
    _orig_server_file_up = _AS.__file__
    _orig_thread_up = _AS.threading.Thread
    _started_up = []

    class _NoStartThread:
        def __init__(self, target, args, daemon):
            self.target, self.args, self.daemon = target, args, daemon
        def start(self):
            _started_up.append(self.args)

    try:
        _AS.JOBS_FILE = _tmpdir / "multi_upload_jobs.json"
        _AS.BACKUP_DIR = _tmpdir / "multi_upload_backups"
        _AS.__file__ = str(_tmpdir / "approval_server.py")
        _AS.threading.Thread = _NoStartThread
        _client_up = _AS.app.test_client()
        _pdf_a_bytes = _pdf_a.read_bytes()
        _pdf_b_bytes = _pdf_b.read_bytes()

        _AS.save_jobs({})
        _started_up.clear()
        _multi_resp = _client_up.post("/upload", data={
            "project_ref": "MULTI-001",
            "project_name": "Four Slab Project",
            "client_name": "Fortel QA",
            "pdf": [(_io3.BytesIO(_pdf_a_bytes), "Yard.pdf"),
                    (_io3.BytesIO(_pdf_b_bytes), "Dock.pdf")],
        }, content_type="multipart/form-data")
        _multi_json = _multi_resp.get_json()
        _multi_jobs = _AS.load_jobs()
        ck("multi-file upload returns two job_ids", _multi_resp.status_code == 202 and
           len(_multi_json.get("job_ids", [])) == 2, _multi_json)
        ck("multi-file upload creates one job per drawing under one project",
           len(_multi_jobs) == 2 and
           {j.get("project_ref") for j in _multi_jobs.values()} == {"MULTI-001"} and
           {j.get("project_name") for j in _multi_jobs.values()} == {"Four Slab Project"})
        ck("multi-file upload preserves prefixed, non-overwriting source paths",
           len({j.get("pdf_path") for j in _multi_jobs.values()}) == 2 and
           all(Path(j["pdf_path"]).name.startswith("MULTI-001_") for j in _multi_jobs.values()))
        ck("multi-file upload launches one independent takeoff worker per drawing",
           len(_started_up) == 2)

        _AS.save_jobs({})
        _started_up.clear()
        _single_resp = _client_up.post("/upload", data={
            "project_ref": "SINGLE-001", "project_name": "Single Drawing Project",
            "pdf": (_io3.BytesIO(_pdf_a_bytes), "Yard.pdf"),
        }, content_type="multipart/form-data")
        _single_json = _single_resp.get_json()
        ck("single-file upload keeps legacy one-job response shape",
           _single_resp.status_code == 202 and "job_id" in _single_json and
           "job_ids" not in _single_json and len(_AS.load_jobs()) == 1, _single_json)

        _AS.save_jobs({})
        _started_up.clear()
        _zip_resp = _client_up.post("/upload", data={
            "project_ref": "ZIP-001", "project_name": "ZIP Slab Project",
            "pdf": (_io3.BytesIO(_zip_path.read_bytes()), "slabs.zip"),
        }, content_type="multipart/form-data")
        _zip_json = _zip_resp.get_json()
        _zip_jobs = _AS.load_jobs()
        ck("ZIP upload creates a job for every contained PDF",
           _zip_resp.status_code == 202 and len(_zip_json.get("job_ids", [])) == 2 and
           len(_zip_jobs) == 2 and len(_started_up) == 2, _zip_json)
        ck("ZIP jobs share the project ref and record all-drawings provenance",
           {j.get("project_ref") for j in _zip_jobs.values()} == {"ZIP-001"} and
           all(any("every PDF queued" in f for f in j.get("flags", []))
               for j in _zip_jobs.values()))

        _portal_html_up = (Path(_orig_server_file_up).parent / "assessor_portal.html").read_text()
        ck("portal file input allows multiple PDFs and ZIPs",
           'accept=".pdf,.zip" multiple' in _portal_html_up)
        ck("portal submits every selected file under the backward-compatible pdf field",
           "files.forEach(file => fd.append('pdf', file))" in _portal_html_up)
        ck("portal groups repeated project refs under collapsible project headers",
           "projectCounts.get(ref)" in _portal_html_up and
           'class="project-group-header' in _portal_html_up and
           "toggleProjectGroup(this)" in _portal_html_up)
    finally:
        _AS.threading.Thread = _orig_thread_up
        _AS.__file__ = _orig_server_file_up
        _AS.JOBS_FILE = _orig_jobs_file_up
        _AS.BACKUP_DIR = _orig_backup_dir_up

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

print("D77 swatch-locked grey band vs 'Footpaths (ancillary): Concrete' annexation "
      "(Aryan field report: real SGP sheet measured 3,172 vs Smita gold 3,156 — root cause was "
      "the generic 214±14 grey band admitting a darker, adjacent ancillary-concrete legend "
      "colour and binary_closing fusing it into the yard's own connected component)")
try:
    _require_fixture("drawings/_int_d77.pdf", "D77 swatch-locked grey band test")
    import fitz as _fitz_fp
    from takeoff_unmarked import (takeoff as _tu_takeoff_fp, segment_hatch as _seg_fp,
                                   PLAUSIBLE_MIN_M2 as _PMIN_FP, PLAUSIBLE_MAX_M2 as _PMAX_FP)

    def _gen_d77_footpath(out_path, chip_grey=0.878, yard_grey=0.878):
        """Same D77 yard geometry (page 1067.766x824.854pt, scale bar, 1:250 title) plus:
          - a darker 'Footpaths (ancillary): Concrete' strip (204 grey) sitting 0.65pt below
            the yard's bottom edge — close enough for binary_closing (any close>=2) to bridge,
            reproducing the real-sheet CONNECTED over-measure (not a satellite blob).
          - a legend swatch chip + label 'Concrete Service Yard construction' (readable by
            find_concrete_swatch_rgb) so the swatch-lock path engages.
          - a second, non-matching legend line 'Footpaths (ancillary): Concrete' with its own
            (darker) swatch chip — must NOT be picked up as the concrete-yard label anchor.
        Title text deliberately avoids CONCRETE_LABELS substrings (unlike drawings/_int_d77.pdf,
        whose title text IS the label match and has no nearby swatch chip -> unreadable swatch,
        which is why that fixture stays on the generic-band fallback path untouched by this fix).
        """
        d = _fitz_fp.open()
        W, H = 1067.7659912109375, 824.853515625
        pg = d.new_page(width=W, height=H)
        pg.insert_text((130.0, 80), "PROPOSED HARD LANDSCAPING - UNIT 1 SITE PLAN    Scale 1:250",
                       fontsize=13)
        pg.draw_line(_fitz_fp.Point(130.0, 714.853515625), _fitz_fp.Point(696.9290771484375, 714.853515625),
                     color=(0, 0, 0), width=2.0)
        pg.insert_text((126.0, 731), "0", fontsize=11)
        pg.insert_text((678.9, 731), "50 m", fontsize=11)

        yg = (yard_grey, yard_grey, yard_grey)
        pg.draw_rect(_fitz_fp.Rect(130.0, 120.0, 937.765625, 624.853515625),
                     color=(0, 0, 0), fill=yg, width=1.0)

        # Ancillary footpath strip: 230x9pt = 16.1 m² at k=0.08819, darker grey (204), 0.65pt
        # gap below the yard's own bottom edge (bridged by binary_closing regardless of the
        # exact close value, same mechanism as the real sheet's kerb-line gap).
        strip_grey = (0.80, 0.80, 0.80)
        pg.draw_rect(_fitz_fp.Rect(350.0, 625.503515625, 580.0, 634.503515625),
                     color=None, fill=strip_grey, width=0)

        # Legend: matching swatch chip + label (concrete-yard anchor).
        cg = (chip_grey, chip_grey, chip_grey)
        pg.draw_rect(_fitz_fp.Rect(330.0, 762.0, 360.0, 776.0), color=(0, 0, 0), fill=cg, width=0.5)
        pg.insert_text((400.0, 772.0), "Concrete Service Yard construction", fontsize=9)

        # Second legend line: non-matching label + its own (darker) swatch chip — must not be
        # mistaken for the concrete-yard anchor, and is small/isolated -> satellite-dropped.
        pg.draw_rect(_fitz_fp.Rect(330.0, 784.0, 360.0, 796.0), color=(0, 0, 0), fill=strip_grey, width=0.5)
        pg.insert_text((400.0, 792.0), "Footpaths (ancillary): Concrete", fontsize=9)

        d.save(out_path)
        d.close()

    _p_fp = "drawings/_int_d77_footpath.pdf"
    _gen_d77_footpath(_p_fp)

    # BEFORE: old generic-band segmentation (direct segment_hatch call, mirroring the borders
    # test's "WITHOUT exclusion" pattern) — proves the annexation is real and CONNECTED (not
    # something the satellite-fraction filter would already have dropped).
    _pgfp = _fitz_fp.open(_p_fp)[0]
    _pixfp = _pgfp.get_pixmap(matrix=_fitz_fp.Matrix(2.0, 2.0))
    import numpy as _np_fp
    _imfp = _np_fp.frombuffer(_pixfp.samples, _np_fp.uint8).reshape(_pixfp.height, _pixfp.width, _pixfp.n)[..., :3]
    _k_fp = 0.08819
    _comp_old_fp = _seg_fp(_imfp, (214, 214, 214), k=_k_fp, S=2.0, exclude_border=True)
    _area_old_fp = round(int(_comp_old_fp.sum()) * (1 / 2.0) ** 2 * _k_fp * _k_fp, 0)
    ck("BEFORE fix (generic 214 band): footpath strip annexed, area > 3,159 + 10 m² "
       "(connected over-measure, not a dropped satellite)",
       _area_old_fp > 3159.0 + 10, f"got {_area_old_fp}")

    # AFTER: full takeoff() with the swatch-lock fix — flags show the lock, area back to gold.
    _r_fp = _tu_takeoff_fp(_p_fp)
    _area_fp = _r_fp.get("area_m2")
    ck("AFTER fix: swatch (224ish) LOCKED — footpath strip excluded, area within 0.5% of 3,159 m²",
       _area_fp is not None and abs(_area_fp - 3159.0) / 3159.0 <= 0.005, f"got {_area_fp}")
    ck("AFTER fix: flags show the swatch-locked band",
       any("LOCKED" in f for f in _r_fp.get("flags", [])), _r_fp.get("flags"))
    ck("AFTER fix: measurement_state MEASURED_VERIFIED",
       _r_fp.get("measurement_state") == MEASURED_VERIFIED, _r_fp.get("measurement_state"))

    # DEMO-4 REGRESSION GUARD: swatch reads far enough from the yard's own fill (232 vs 214)
    # that the locked band [218,246] misses the 214 yard entirely -> must FALL BACK, never
    # silently return area=None on a perfectly measurable sheet.
    _p_fp_d4 = "/tmp/_ci_d77_footpath_demo4.pdf"
    _gen_d77_footpath(_p_fp_d4, chip_grey=0.910, yard_grey=0.84)
    _r_fp_d4 = _tu_takeoff_fp(_p_fp_d4)
    ck("DEMO-4 GUARD: swatch-locked band misses the yard fill -> FELL BACK (flag present)",
       any("FELL BACK" in f for f in _r_fp_d4.get("flags", [])), _r_fp_d4.get("flags"))
    ck("DEMO-4 GUARD: fallback still produces a measurable area (never area=None)",
       _r_fp_d4.get("area_m2") is not None, _r_fp_d4.get("area_m2"))
    ck("DEMO-4 GUARD: fallback state is MEASURED_VERIFIED (not silently UNMEASURED)",
       _r_fp_d4.get("measurement_state") == MEASURED_VERIFIED, _r_fp_d4.get("measurement_state"))

    # GOLD GUARDS unchanged: both pre-existing synthetic fixtures have unreadable swatches
    # (title text IS the label match, no nearby swatch chip) -> always take the fallback path,
    # golds untouched by this change.
    _d77_regress = _tu_takeoff_fp("drawings/_int_d77.pdf")
    ck("GOLD GUARD: _int_d77.pdf still exactly 3,159 m² (swatch-lock did not touch it)",
       _d77_regress.get("area_m2") == 3159.0, _d77_regress.get("area_m2"))
except _FixtureNotPresent as _e:
    print(f"  [SKIP] {_e} — fixture not present")
except (ImportError, FileNotFoundError) as _e:
    print(f"  [SKIP] D77 swatch-locked grey band test — missing dependency or file: {_e}")

print("approval_server: soft-delete (archive/unarchive) — Aryan's portal delete-estimation request")
try:
    import approval_server as _AS5
    import tempfile as _tempfile5

    _tmpdir5 = Path(_tempfile5.mkdtemp(prefix="ci_archive_"))
    _orig_jobs_file5 = _AS5.JOBS_FILE
    _orig_archive_file5 = _AS5.JOBS_ARCHIVE_FILE
    _AS5.JOBS_FILE = _tmpdir5 / "jobs.json"
    _AS5.JOBS_ARCHIVE_FILE = _tmpdir5 / "jobs_archive.json"
    try:
        _app5 = _AS5.app
        _app5.testing = True
        _client5 = _app5.test_client()

        # Ordinary pending job -> archivable
        _jid5 = "job-pending-1"
        _AS5.save_jobs({_jid5: {"id": _jid5, "status": "pending", "decision": None,
                                 "project_name": "Test Yard", "flags": []}})
        _r5 = _client5.post(f"/archive/{_jid5}", json={"note": "duplicate upload"})
        ck("archive: pending job archives with 200", _r5.status_code == 200, _r5.status_code)
        _jobs_after5 = _AS5.load_jobs()
        ck("archive: job removed from hot jobs file", _jid5 not in _jobs_after5)
        _archive5 = _AS5._load_archive()
        ck("archive: job present in archive file", _jid5 in _archive5)
        ck("archive: archived record carries archived=True + archived_at",
           _archive5.get(_jid5, {}).get("archived") is True and _archive5.get(_jid5, {}).get("archived_at"),
           _archive5.get(_jid5))
        ck("archive: status set to 'deleted' in the archive record",
           _archive5.get(_jid5, {}).get("status") == "deleted", _archive5.get(_jid5, {}).get("status"))
        ck("archive: no data lost — project_name preserved",
           _archive5.get(_jid5, {}).get("project_name") == "Test Yard")

        # /jobs/archived surfaces it, default /jobs does not
        _r_list5 = _client5.get("/jobs/archived")
        ck("GET /jobs/archived includes the archived job", _jid5 in _r_list5.get_json())
        _r_hot5 = _client5.get("/jobs")
        ck("GET /jobs (default) excludes the archived job", _jid5 not in _r_hot5.get_json())

        # Unarchive restores it
        _r_un5 = _client5.post(f"/unarchive/{_jid5}")
        ck("unarchive: restores with 200", _r_un5.status_code == 200, _r_un5.status_code)
        _jobs_restored5 = _AS5.load_jobs()
        ck("unarchive: job back in hot jobs file", _jid5 in _jobs_restored5)
        ck("unarchive: archived flag cleared", _jobs_restored5.get(_jid5, {}).get("archived") is False)
        _archive_after_un5 = _AS5._load_archive()
        ck("unarchive: removed from archive file", _jid5 not in _archive_after_un5)

        # Approved job -> BLOCKED (needs Jas, not a portal button)
        _jid5b = "job-approved-1"
        _AS5.save_jobs({_jid5b: {"id": _jid5b, "status": "approved", "decision": "approved",
                                  "project_name": "Approved Yard", "flags": []}})
        _r5b = _client5.post(f"/archive/{_jid5b}")
        ck("archive: approved job is BLOCKED (409)", _r5b.status_code == 409, _r5b.status_code)
        ck("archive: blocked-job error mentions Jas / manual",
           "jas" in _r5b.get_json().get("error", "").lower(), _r5b.get_json())
        ck("archive: approved job NOT removed from hot jobs file after a blocked attempt",
           _jid5b in _AS5.load_jobs())

        # Processing job -> BLOCKED (409), same pattern as approve/reject/adjust
        _jid5c = "job-processing-1"
        _AS5.save_jobs({_jid5c: {"id": _jid5c, "status": "processing", "decision": None, "flags": []}})
        _r5c = _client5.post(f"/archive/{_jid5c}")
        ck("archive: processing job is BLOCKED (409)", _r5c.status_code == 409, _r5c.status_code)

        # Unknown job -> 404, never a crash
        _r5d = _client5.post("/archive/does-not-exist")
        ck("archive: unknown job -> 404 (not a crash)", _r5d.status_code == 404, _r5d.status_code)
        _r5e = _client5.post("/unarchive/does-not-exist")
        ck("unarchive: unknown archived job -> 404 (not a crash)", _r5e.status_code == 404, _r5e.status_code)
    finally:
        _AS5.JOBS_FILE = _orig_jobs_file5
        _AS5.JOBS_ARCHIVE_FILE = _orig_archive_file5
        shutil.rmtree(_tmpdir5, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server soft-delete tests — missing dependency: {_e}")

print("approval_server: PORTAL_TOKEN auth gate (prod-audit MUST — unauthenticated approve/reject/adjust)")
try:
    import approval_server as _AS6
    import tempfile as _tempfile6

    _tmpdir6 = Path(_tempfile6.mkdtemp(prefix="ci_auth_"))
    _orig_jobs_file6 = _AS6.JOBS_FILE
    _AS6.JOBS_FILE = _tmpdir6 / "jobs.json"
    _orig_token6 = _AS6.APPROVAL_TOKEN
    _AS6.APPROVAL_TOKEN = "test-secret-token-123"
    try:
        _app6 = _AS6.app
        _app6.testing = True
        _client6 = _app6.test_client()

        _jid6 = "job-auth-1"
        _AS6.save_jobs({_jid6: {"id": _jid6, "status": "pending", "decision": None, "flags": []}})

        # No token at all -> 401, never a silent pass-through
        _r6 = _client6.get("/jobs")
        ck("no token -> /jobs is 401 when APPROVAL_TOKEN is set", _r6.status_code == 401, _r6.status_code)

        # Wrong token -> 401
        _r6b = _client6.get("/jobs", headers={"Authorization": "Bearer wrong-token"})
        ck("wrong Bearer token -> 401", _r6b.status_code == 401, _r6b.status_code)

        # Correct Bearer token -> 200
        _r6c = _client6.get("/jobs", headers={"Authorization": "Bearer test-secret-token-123"})
        ck("correct Bearer token -> 200", _r6c.status_code == 200, _r6c.status_code)

        # /status always exempt (health-check must work for deploy monitoring pre-auth)
        _r6d = _client6.get("/status")
        ck("/status is exempt from the token gate", _r6d.status_code == 200, _r6d.status_code)

        # / should also stay reachable so it can redirect users into the portal
        _r6d0 = _client6.get("/")
        ck("/ is exempt from the token gate so the landing redirect works",
           _r6d0.status_code in (301, 302), _r6d0.status_code)

        # /portal?token=<correct> sets a cookie and redirects
        _r6e = _client6.get(f"/portal?token=test-secret-token-123")
        ck("/portal?token=<correct> redirects (sets cookie)", _r6e.status_code in (301, 302), _r6e.status_code)
        _set_cookie6 = _r6e.headers.get("Set-Cookie", "")
        ck("/portal?token=<correct> Set-Cookie contains the token cookie name",
           "approval_token" in _set_cookie6, _set_cookie6)

        # /portal?token=<wrong> does not authorise — use a FRESH client (no cookie carried
        # over from the earlier correct-token request on _client6, which would mask this).
        # It falls through to the same "no valid cookie/token" case as no token at all, which
        # now renders the login form (200) instead of a bare 401 — see the /portal login test
        # below for the full flow; here just confirm real portal content is NOT served.
        _client6fresh = _app6.test_client()
        _r6f = _client6fresh.get("/portal?token=nope")
        ck("/portal?token=<wrong> -> login form, not silently served",
           _r6f.status_code == 200 and b"Access code" in _r6f.data
           and b"Fortel Approval Portal" in _r6f.data, _r6f.status_code)

        # Cookie-based auth works for a mutating route (mirrors what the portal's own fetch()
        # calls will do once the browser holds the cookie from the bootstrap redirect above)
        _client6.set_cookie("approval_token", "test-secret-token-123")
        _r6g = _client6.get(f"/job/{_jid6}")
        ck("cookie auth authorises a normal route", _r6g.status_code == 200, _r6g.status_code)

        # With APPROVAL_TOKEN unset, auth is fully disabled (back-compat / local dev)
        _AS6.APPROVAL_TOKEN = ""
        _client6b = _app6.test_client()
        _r6h = _client6b.get("/jobs")
        ck("no APPROVAL_TOKEN configured -> auth disabled, /jobs open", _r6h.status_code == 200, _r6h.status_code)
    finally:
        _AS6.JOBS_FILE = _orig_jobs_file6
        _AS6.APPROVAL_TOKEN = _orig_token6
        shutil.rmtree(_tmpdir6, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server auth-gate tests — missing dependency: {_e}")

print("approval_server: /portal login form (no-token case posts a code instead of a bare 401)")
try:
    import approval_server as _AS6b

    _orig_token6b = _AS6b.APPROVAL_TOKEN
    _AS6b.APPROVAL_TOKEN = "test-login-secret-789"
    try:
        _app6b = _AS6b.app
        _app6b.testing = True

        # 1) No cookie, no token at all -> login form (200), not the real portal
        _client6b1 = _app6b.test_client()
        _r6b1 = _client6b1.get("/portal")
        ck("GET /portal with no cookie/token -> login form",
           _r6b1.status_code == 200 and b"Access code" in _r6b1.data
           and b"Review Portal" not in _r6b1.data, _r6b1.status_code)

        # 2) Wrong code -> re-shows the form with an error, still 200 (a login failure, not an
        # API auth failure)
        _r6b2 = _client6b1.post("/portal", data={"code": "wrong-code"})
        ck("POST /portal wrong code -> re-shown with error, 200",
           _r6b2.status_code == 200 and b"Incorrect code" in _r6b2.data, _r6b2.status_code)

        # 3) Correct code -> redirect + sets the cookie
        _r6b3 = _client6b1.post("/portal", data={"code": "test-login-secret-789"})
        ck("POST /portal correct code -> redirect", _r6b3.status_code in (301, 302), _r6b3.status_code)
        ck("POST /portal correct code -> Set-Cookie contains the token cookie name",
           "approval_token" in _r6b3.headers.get("Set-Cookie", ""),
           _r6b3.headers.get("Set-Cookie", ""))

        # 4) Follow-up GET with that cookie -> real portal content, not the login form
        _r6b4 = _client6b1.get("/portal")
        ck("GET /portal with cookie from login -> real portal, not the login form",
           _r6b4.status_code == 200 and b"Access code" not in _r6b4.data, _r6b4.status_code)
    finally:
        _AS6b.APPROVAL_TOKEN = _orig_token6b
except ImportError as _e:
    print(f"  [SKIP] approval_server /portal login-form tests — missing dependency: {_e}")

print("approval_server: jobs-file backup rotation + corrupt-file preservation (prod-audit MUST)")
try:
    import approval_server as _AS7
    import tempfile as _tempfile7

    _tmpdir7 = Path(_tempfile7.mkdtemp(prefix="ci_backup_"))
    _orig_jobs_file7 = _AS7.JOBS_FILE
    _orig_backup_dir7 = _AS7.BACKUP_DIR
    _AS7.JOBS_FILE = _tmpdir7 / "jobs.json"
    _AS7.BACKUP_DIR = _tmpdir7 / "backups"
    try:
        # First-ever save: JOBS_FILE doesn't exist yet, so there's nothing to snapshot —
        # _rotate_backup is a correct no-op here (never backs up a file that isn't there yet).
        _AS7.save_jobs({"a": {"id": "a"}})
        # Backup filenames are keyed off JOBS_FILE.stem ("jobs" here, not "approval_jobs") —
        # see approval_server._rotate_backup's stem-based naming (item 5, QA-instance isolation).
        _backups7 = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("no backup created on the very first save (nothing existed yet to snapshot)",
           len(_backups7) == 0, [str(p) for p in _backups7])

        # Second save the same day: JOBS_FILE now exists from the first save, so THIS save's
        # rotation check snapshots it before overwriting -> exactly one dated backup appears.
        _AS7.save_jobs({"a": {"id": "a"}, "b": {"id": "b"}})
        _backups7b = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("save_jobs creates a same-day backup once a prior file exists to snapshot",
           len(_backups7b) == 1, [str(p) for p in _backups7b])

        # A third save the same day must NOT create a second backup file for today
        _AS7.save_jobs({"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}})
        _backups7b2 = list(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck("no duplicate backup for a third save on the same day",
           len(_backups7b2) == 1, [str(p) for p in _backups7b2])

        # Pruning: force more than BACKUP_KEEP dated backup files to exist, then trigger a
        # rotation check that should prune down to the newest BACKUP_KEEP.
        import datetime as _dt7
        for _i in range(20):
            _fake_date = (_dt7.date(2020, 1, 1) + _dt7.timedelta(days=_i)).isoformat()
            (_AS7.BACKUP_DIR / f"jobs.{_fake_date}.json").write_text("{}")
        _AS7._rotate_backup()  # today's backup already exists, so this call only prunes
        _backups7c = sorted(_AS7.BACKUP_DIR.glob("jobs.*.json"))
        ck(f"backup pruning keeps at most BACKUP_KEEP={_AS7.BACKUP_KEEP} files",
           len(_backups7c) <= _AS7.BACKUP_KEEP, len(_backups7c))

        # Corrupt (non-empty, unparseable) jobs file -> preserved as .corrupt-*, load returns {}
        _AS7.JOBS_FILE.write_text("{not valid json!!")
        _loaded7 = _AS7.load_jobs()
        ck("corrupt jobs file -> load_jobs returns {} (never raises)", _loaded7 == {}, _loaded7)
        _corrupt_copies7 = list(_tmpdir7.glob("jobs.json.corrupt-*"))
        ck("corrupt jobs file -> a .corrupt-* copy is preserved for recovery",
           len(_corrupt_copies7) == 1, [str(p) for p in _corrupt_copies7])
    finally:
        _AS7.JOBS_FILE = _orig_jobs_file7
        _AS7.BACKUP_DIR = _orig_backup_dir7
        shutil.rmtree(_tmpdir7, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server backup-rotation tests — missing dependency: {_e}")

print("approval_server: startup sweep clears stranded 'processing' jobs on restart (prod-audit MUST)")
try:
    import approval_server as _AS8
    import tempfile as _tempfile8

    _tmpdir8 = Path(_tempfile8.mkdtemp(prefix="ci_sweep_"))
    _orig_jobs_file8 = _AS8.JOBS_FILE
    _AS8.JOBS_FILE = _tmpdir8 / "jobs.json"
    try:
        _jid8 = "job-stranded-1"
        _AS8.save_jobs({_jid8: {"id": _jid8, "status": "processing", "decision": None, "flags": []}})
        _AS8._sweep_stranded_processing_jobs()
        _swept8 = _AS8.load_jobs()[_jid8]
        ck("stranded 'processing' job flipped to UNMEASURED by the startup sweep",
           _swept8.get("measurement_state") == "UNMEASURED", _swept8.get("measurement_state"))
        ck("startup sweep flag mentions PIPELINE INTERRUPTED",
           any("PIPELINE INTERRUPTED" in f for f in _swept8.get("flags", [])), _swept8.get("flags"))
        ck("startup sweep sets needs_assessor=True", _swept8.get("needs_assessor") is True)

        # A job that is NOT processing must be left untouched
        _jid8b = "job-approved-untouched"
        _AS8.save_jobs({_jid8b: {"id": _jid8b, "status": "approved", "decision": "approved", "flags": ["ok"]}})
        _AS8._sweep_stranded_processing_jobs()
        _unswept8 = _AS8.load_jobs()[_jid8b]
        ck("non-processing job untouched by the startup sweep",
           _unswept8.get("status") == "approved" and _unswept8.get("flags") == ["ok"], _unswept8)
    finally:
        _AS8.JOBS_FILE = _orig_jobs_file8
        shutil.rmtree(_tmpdir8, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server startup-sweep tests — missing dependency: {_e}")

print("approval_server: /webhook/n8n pdf_path containment guard (prod-audit MUST — arbitrary file read)")
try:
    import approval_server as _AS9
    import approval_email as _AE9
    import tempfile as _tempfile9
    import json as _json9

    # /webhook/n8n -> approval_email.create_job() writes straight to approval_email.JOBS_FILE.
    # Left pointed at the real approval_jobs.json, every POST in this test (three per run)
    # permanently wrote a junk "x.pdf" / area=100 pending job into the LIVE jobs file — this is
    # exactly how the ~17 junk jobs that were polluting approval_jobs.json got there. Point
    # approval_email.JOBS_FILE at a tempfile.mkdtemp() scratch path for the duration of this
    # test (never a hardcoded /tmp path) and restore it in finally, so CI is byte-stable
    # against the live jobs file no matter how many times it runs.
    _tmpdir9 = Path(_tempfile9.mkdtemp(prefix="ci_webhook_n8n_"))
    _orig_ae_jobs_file9 = _AE9.JOBS_FILE
    _AE9.JOBS_FILE = _tmpdir9 / "jobs.json"
    try:
        _app9 = _AS9.app
        _app9.testing = True
        _client9 = _app9.test_client()

        _r9 = _client9.post("/webhook/n8n", json={
            "pdf_path": "/etc/passwd",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("pdf_path outside drawings/ is rejected with 400, not read",
           _r9.status_code == 400, _r9.status_code)

        _r9b = _client9.post("/webhook/n8n", json={
            "pdf_path": "../../etc/passwd",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("path-traversal pdf_path is rejected with 400",
           _r9b.status_code == 400, _r9b.status_code)

        # Empty pdf_path (legit use case — result created without a snapshot) still works
        _r9c = _client9.post("/webhook/n8n", json={
            "pdf_path": "",
            "result": {"area_m2": 100, "file": "x.pdf"},
        })
        ck("empty pdf_path (no snapshot) is not blocked by the containment guard",
           _r9c.status_code == 200, _r9c.status_code)

        # The job this test creates must land in the scratch JOBS_FILE, never the live one.
        ck("webhook test job landed in the scratch jobs file, not the live approval_jobs.json",
           _AE9.JOBS_FILE.exists() and len(_json9.loads(_AE9.JOBS_FILE.read_text())) >= 1,
           str(_AE9.JOBS_FILE))
    finally:
        _AE9.JOBS_FILE = _orig_ae_jobs_file9
        shutil.rmtree(_tmpdir9, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server webhook containment tests — missing dependency: {_e}")

print("approval_server: GET on /approve /reject does NOT mutate; POST does "
      "(top-level-navigation CSRF fix — SameSite=Lax cookies + a mutating GET meant an "
      "email client's link-preview prefetch, or any page merely linking here, could "
      "silently approve/reject a job)")
try:
    import approval_server as _AS10
    import tempfile as _tempfile10

    _tmpdir10 = Path(_tempfile10.mkdtemp(prefix="ci_csrf_"))
    _orig_jobs_file10 = _AS10.JOBS_FILE
    _AS10.JOBS_FILE = _tmpdir10 / "jobs.json"
    try:
        _app10 = _AS10.app
        _app10.testing = True
        _client10 = _app10.test_client()

        # --- /approve: GET must not mutate ---
        _jid10a = "job-csrf-approve"
        _AS10.save_jobs({_jid10a: {
            "id": _jid10a, "status": "pending", "decision": None,
            "measurement_state": "MEASURED_VERIFIED", "scale_confirmed": True,
            "result": {"area_m2": 1000, "file": "csrf_test.pdf"}, "flags": [],
        }})
        _rg10a = _client10.get(f"/approve/{_jid10a}")
        ck("GET /approve/<id> returns 200 (confirm page, not a mutation)",
           _rg10a.status_code == 200, _rg10a.status_code)
        ck("GET /approve/<id> renders an HTML confirm page (not JSON)",
           "text/html" in _rg10a.content_type, _rg10a.content_type)
        _job_after_get10a = _AS10.load_jobs()[_jid10a]
        ck("GET /approve/<id> did NOT change job status (still 'pending')",
           _job_after_get10a["status"] == "pending", _job_after_get10a["status"])
        ck("GET /approve/<id> did NOT set a decision",
           _job_after_get10a.get("decision") is None, _job_after_get10a.get("decision"))
        # The confirm page must contain a POST form targeting the real action, not a link
        # that itself mutates (otherwise it's just moved the vulnerability one click later).
        _body10a = _rg10a.get_data(as_text=True)
        ck("confirm page's form method is POST", 'method="POST"' in _body10a, _body10a[:200])
        ck(f"confirm page's form posts to /approve/{_jid10a}",
           f"/approve/{_jid10a}" in _body10a)

        # Now POST actually mutates
        _rp10a = _client10.post(f"/approve/{_jid10a}", json={})
        ck("POST /approve/<id> returns 200", _rp10a.status_code == 200, _rp10a.status_code)
        _job_after_post10a = _AS10.load_jobs()[_jid10a]
        ck("POST /approve/<id> DID change job status to 'approved'",
           _job_after_post10a["status"] == "approved", _job_after_post10a["status"])

        # --- /reject: GET must not mutate ---
        _jid10b = "job-csrf-reject"
        _AS10.save_jobs({_jid10b: {
            "id": _jid10b, "status": "pending", "decision": None,
            "result": {"file": "csrf_test2.pdf"}, "flags": [],
        }})
        _rg10b = _client10.get(f"/reject/{_jid10b}")
        ck("GET /reject/<id> returns 200 (confirm page, not a mutation)",
           _rg10b.status_code == 200, _rg10b.status_code)
        _job_after_get10b = _AS10.load_jobs()[_jid10b]
        ck("GET /reject/<id> did NOT change job status (still 'pending')",
           _job_after_get10b["status"] == "pending", _job_after_get10b["status"])

        _rp10b = _client10.post(f"/reject/{_jid10b}", json={})
        ck("POST /reject/<id> returns 200", _rp10b.status_code == 200, _rp10b.status_code)
        _job_after_post10b = _AS10.load_jobs()[_jid10b]
        ck("POST /reject/<id> DID change job status to 'rejected'",
           _job_after_post10b["status"] == "rejected", _job_after_post10b["status"])

        # --- /adjust: GET already only redirects (never mutated) — confirm that holds ---
        _jid10c = "job-csrf-adjust"
        _AS10.save_jobs({_jid10c: {
            "id": _jid10c, "status": "pending", "decision": None,
            "result": {"file": "csrf_test3.pdf"}, "flags": [],
        }})
        _rg10c = _client10.get(f"/adjust/{_jid10c}", follow_redirects=False)
        ck("GET /adjust/<id> redirects into the portal (302/301), never mutates",
           _rg10c.status_code in (301, 302), _rg10c.status_code)
        _job_after_get10c = _AS10.load_jobs()[_jid10c]
        ck("GET /adjust/<id> did NOT change job status", _job_after_get10c["status"] == "pending")

        # --- Unknown job on GET -> 404, not a 200 confirm page for a job that doesn't exist ---
        _r404_10 = _client10.get("/approve/does-not-exist-10")
        ck("GET /approve/<unknown> -> 404, not a confirm page for a nonexistent job",
           _r404_10.status_code == 404, _r404_10.status_code)
    finally:
        _AS10.JOBS_FILE = _orig_jobs_file10
        shutil.rmtree(_tmpdir10, ignore_errors=True)
except ImportError as _e:
    print(f"  [SKIP] approval_server GET-no-mutation tests — missing dependency: {_e}")

print("approval_email: emailed approve/reject/adjust links carry ?token= when the token "
      "gate is enabled (token mode previously 401'd every emailed action link)")
try:
    import approval_email as _AE11
    import importlib as _importlib11

    _orig_token11 = _AE11.APPROVAL_TOKEN
    try:
        # --- Token configured: every action link + the portal link carries ?token= ---
        _AE11.APPROVAL_TOKEN = "test-email-token-456"
        _html11 = _AE11.build_html_email(
            "job-email-1",
            {"area_m2": 500, "file": "email_test.pdf", "flags": []},
            png_b64="",
        )
        ck("approve link carries ?token= when APPROVAL_TOKEN is set",
           "/approve/job-email-1?token=test-email-token-456" in _html11)
        ck("reject link carries ?token= when APPROVAL_TOKEN is set",
           "/reject/job-email-1?token=test-email-token-456" in _html11)
        ck("adjust link carries ?token= when APPROVAL_TOKEN is set",
           "/adjust/job-email-1?token=test-email-token-456" in _html11)
        ck("portal review link carries ?token= when APPROVAL_TOKEN is set",
           "/review/job-email-1?token=test-email-token-456" in _html11)

        # --- No token configured: links are unchanged (no bare '?token=' with an empty value) ---
        _AE11.APPROVAL_TOKEN = ""
        _html11b = _AE11.build_html_email(
            "job-email-2",
            {"area_m2": 500, "file": "email_test.pdf", "flags": []},
            png_b64="",
        )
        ck("no token configured -> approve link has no ?token= param at all",
           "token=" not in _html11b.split('href="')[1].split('"')[0]
           if 'href="' in _html11b else True)
        ck("no token configured -> approve link is the plain job URL",
           "/approve/job-email-2" in _html11b)
    finally:
        _AE11.APPROVAL_TOKEN = _orig_token11
except ImportError as _e:
    print(f"  [SKIP] approval_email token-link tests — missing dependency: {_e}")

print("scale: detect_scale_bar rotation-agnostic + segmented-bar + crash-guard "
      "(Aryan field report — real SGP sheet 'title 1:250 only — no scale bar detected'; root "
      "cause: every real Fortel A0/A1 sheet is landscape content in a portrait MediaBox with "
      "page /Rotate 90/270, and PyMuPDF returns RAW pre-rotation coordinates, so a visually- "
      "horizontal bar is a stack of near-VERTICAL strokes the old horizontal-only test could "
      "never match; also the real bar is SEGMENTED [alternating-fill tick blocks] with a fused "
      "'25m' terminal label, and ms[0]-anchoring on text-extraction order crashed with "
      "'max() arg is an empty sequence' on two real Winvic sheets)")
try:
    import fitz as _fitz_sb

    def _gen_rotated_segmented_bar_sb(out_path, rotation=270):
        """Portrait-mediabox page (mimics the real 2384x3370 Winvic sheets) with a scale bar drawn
        as 4 stacked alternating-fill blocks (reportlab 're' rects) + a fused '25m' terminal tick,
        then rotated via PyMuPDF post-process — reproducing 'visually horizontal bar, raw-space
        near-vertical strokes' exactly as found on drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf."""
        _c = canvas.Canvas(out_path, pagesize=(850, 1200))
        _c.setFont("Helvetica", 10)
        _c.drawString(50, 150, "Scale 1:250")
        x0, y0, block_h = 120, 400, 30
        for i in range(4):
            y = y0 + i * block_h
            _c.setFillColorRGB(0, 0, 0) if i % 2 == 0 else _c.setFillColorRGB(1, 1, 1)
            _c.rect(x0, y, 6, block_h, fill=1, stroke=1)
        for i, lab in enumerate(["0", "5", "10", "15", "20", "25m"]):
            _c.drawString(x0 + 10, y0 + i * block_h - 3, lab)
        _c.save()
        _d = _fitz_sb.open(out_path)
        _d[0].set_rotation(rotation)
        _d.saveIncr()
        _d.close()

    _sb_expected_k = 25 / 120   # 25 m over the 4x30pt stacked-block span

    _path_sb270 = "/tmp/_sb_rotated270.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb270, rotation=270)
    _k_sb270, _info_sb270 = detect_scale_bar(_path_sb270)
    ck("rotation=270 segmented tick-block bar (real Winvic sheet style) now detects",
       _k_sb270 is not None and abs(_k_sb270 - _sb_expected_k) < 1e-9, (_k_sb270, _info_sb270))

    _path_sb90 = "/tmp/_sb_rotated90.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb90, rotation=90)
    _k_sb90, _info_sb90 = detect_scale_bar(_path_sb90)
    ck("rotation=90 segmented tick-block bar also detects",
       _k_sb90 is not None and abs(_k_sb90 - _sb_expected_k) < 1e-9, (_k_sb90, _info_sb90))

    _path_sb0 = "/tmp/_sb_unrotated_control.pdf"
    _gen_rotated_segmented_bar_sb(_path_sb0, rotation=0)
    _k_sb0, _info_sb0 = detect_scale_bar(_path_sb0)
    ck("rotation=0 control (same fixture, no rotation) also detects — proves the fix is "
       "additive, not rotation-only", _k_sb0 is not None and abs(_k_sb0 - _sb_expected_k) < 1e-9,
       (_k_sb0, _info_sb0))

    # Unrotated segmented bar with a WIDE fused terminal tick ('50m'), several alternating blocks —
    # exercises the horizontal branch of the same clustering/merge logic.
    def _gen_segmented_bar_h_sb(out_path):
        _c = canvas.Canvas(out_path, pagesize=(1400, 900))
        _c.setFont("Helvetica", 10)
        _c.drawString(100, 800, "Scale 1:200")
        x0, y0, block_w = 200, 300, 40
        for i in range(5):
            x = x0 + i * block_w
            _c.setFillColorRGB(0, 0, 0) if i % 2 == 0 else _c.setFillColorRGB(1, 1, 1)
            _c.rect(x, y0, block_w, 8, fill=1, stroke=1)
        for i, lab in enumerate(["0", "10", "20", "30", "40", "50m"]):
            _c.drawString(x0 + i * block_w - 5, y0 - 15, lab)
        _c.save()

    _path_sb_h = "/tmp/_sb_segmented_h.pdf"
    _gen_segmented_bar_h_sb(_path_sb_h)
    _k_sbh, _info_sbh = detect_scale_bar(_path_sb_h)
    _expected_sbh = 50 / (5 * 40)
    ck("horizontal segmented alternating-fill bar with fused '50m' terminal tick",
       _k_sbh is not None and abs(_k_sbh - _expected_sbh) < 1e-9, (_k_sbh, _info_sbh))

    # Crash-guard regression: an early, text-order-first 'm' token with NO nearby bar/digits at
    # all must not raise (old code: max() on an empty generator -> ValueError, reproduced directly
    # on drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf and Dock_Slab_Area_Proposed_Site_Plan.pdf).
    # The real scale bar (a plain line + bare 'm' label, further down the page) must still be found.
    def _gen_bad_anchor_sb(out_path):
        _c = canvas.Canvas(out_path, pagesize=(1400, 900))
        _c.setFont("Helvetica", 10)
        _c.drawString(700, 850, "m")                      # unrelated early 'm', no nearby digits
        _c.line(100, 150, 500, 150)
        _c.drawString(250, 160, "0          40 m")
        _c.save()

    _path_bad = "/tmp/_sb_bad_anchor.pdf"
    _gen_bad_anchor_sb(_path_bad)
    try:
        _k_bad, _info_bad = detect_scale_bar(_path_bad)
        ck("no crash when the first text-order 'm' token has zero nearby bar/digits "
           "(old ms[0] anchor -> max() on empty sequence -> ValueError)", True, (_k_bad, _info_bad))
        ck("bad-anchor fixture still finds the REAL bar via a later, valid label",
           _k_bad is not None and abs(_k_bad - 0.1) < 1e-9, (_k_bad, _info_bad))
    except Exception as _e:
        ck("no crash when the first text-order 'm' token has zero nearby bar/digits "
           "(old ms[0] anchor -> max() on empty sequence -> ValueError)", False,
           f"{type(_e).__name__}: {_e}")

    # A page with literally no scale-bar shape at all must still return cleanly, never raise.
    _path_none = "/tmp/_sb_no_bar_at_all.pdf"
    _c_none = canvas.Canvas(_path_none, pagesize=(800, 600))
    _c_none.drawString(100, 100, "no bar here, just some m words and 5 10 15 numbers")
    _c_none.save()
    try:
        _k_none, _info_none = detect_scale_bar(_path_none)
        ck("page with no real scale-bar shape returns (None, ...) cleanly, never raises",
           _k_none is None, (_k_none, _info_none))
    except Exception as _e:
        ck("page with no real scale-bar shape returns (None, ...) cleanly, never raises",
           False, f"{type(_e).__name__}: {_e}")

    # Detection improvements must never bypass verification: scale_consensus still gates a
    # disagreeing bar-vs-title pair (a rotated segmented bar detected via the fix, paired with a
    # deliberately wrong title-block scale) exactly as it does for the unrotated path.
    _k_gate, _flags_gate = scale_consensus([(_k_sb270, 1), (25 / 40, 1)], tol=0.03)
    ck("scale_consensus still REFUSES when the (correctly-detected, rotation-fixed) bar "
       "disagrees with a second reference beyond tol — detection fix does not bypass the gate",
       _k_gate is None and any("DISAGREE" in f for f in _flags_gate), _flags_gate)

    _k_agree, _flags_agree = scale_consensus([(_k_sb270, 1), (_k_sb270 * 1.01, 1)], tol=0.03)
    ck("scale_consensus VERIFIES the rotation-fixed bar reading when a second reference agrees "
       "within tol", _k_agree is not None, _flags_agree)

except ImportError as _e:
    print(f"  [SKIP] scale.py rotation/segmented-bar tests — missing dependency: {_e}")

print("scale: real-sheet proof — Winvic sheets that already detected still detect after the fix, "
      "and the SGP-family real sheet that previously missed entirely now detects + VERIFIES "
      "against its title-block scale (never bypassing scale_consensus)")
try:
    import os as _os_sb

    _real_sheets_unchanged = [
        ("drawings/_int_d77.pdf", 0.08819445326652144),
        ("drawings/_int_d77_borders.pdf", 0.08819445326652144),
    ]
    for _pdf_path, _expected_k in _real_sheets_unchanged:
        if _os_sb.path.exists(_pdf_path):
            _k_chk, _info_chk = detect_scale_bar(_pdf_path)
            ck(f"unrotated gold fixture {_pdf_path} still detects the same k as before the fix",
               _k_chk is not None and abs(_k_chk - _expected_k) < 1e-6, (_k_chk, _info_chk))
        else:
            print(f"  [SKIP] real-sheet scale regression for {_pdf_path} — fixture not present")

    # The real Winvic sheets (270/90-rotated) must no longer crash, and the two with a genuine
    # readable segmented bar (Yard, Dock — same title-block template) must now agree with each
    # other (same physical bar) instead of one crashing and the other silently mis-anchoring.
    _winvic_rotated = [
        "drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf",
        "drawings/winvic/Dock_Slab_Area_Proposed_Site_Plan.pdf",
    ]
    _winvic_ks = {}
    for _wp in _winvic_rotated:
        if _os_sb.path.exists(_wp):
            try:
                _k_w, _info_w = detect_scale_bar(_wp)
                _winvic_ks[_wp] = _k_w
                ck(f"{_wp} (rotation 270) no longer crashes calling detect_scale_bar",
                   True, (_k_w, _info_w))
            except Exception as _e:
                ck(f"{_wp} (rotation 270) no longer crashes calling detect_scale_bar",
                   False, f"{type(_e).__name__}: {_e}")
        else:
            print(f"  [SKIP] rotated Winvic scale regression for {_wp} — fixture not present")

    if len(_winvic_ks) == 2 and all(_v is not None for _v in _winvic_ks.values()):
        _vals = list(_winvic_ks.values())
        ck("Yard and Dock (same rotated title-block template, same '0 5 10 15 20 25m' bar) "
           "agree on k within 0.1% — both correctly read the same physical scale bar",
           abs(_vals[0] - _vals[1]) / _vals[1] < 0.001, _winvic_ks)

    # Full scale_for() (takeoff_unmarked's consensus-gated wrapper) on the real UNMARKED-vector,
    # rotated, segmented-bar sheet that most closely matches Aryan's real SGP sheet's shape
    # (same title-block family: rotated A0/A1, printed 1:N scale + graphical bar) — must now
    # VERIFY rather than fall back to 'title only — no scale bar detected'.
    _tp_site_plan = ("drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_Site_Plan.pdf")
    if _os_sb.path.exists(_tp_site_plan):
        import takeoff_unmarked as _TU_sb
        _k_tp, _verified_tp, _note_tp, _sources_tp = _TU_sb.scale_for(_tp_site_plan)
        ck("real rotated tender-pack Proposed_Site_Plan.pdf: scale bar now VERIFIED against "
           "title-block (was previously undetectable/unverified pre-fix)",
           _verified_tp is True, _note_tp)
        ck("...and it went through scale_consensus (both sources present), not a bypass",
           "scale_bar" in _sources_tp and "title_block" in _sources_tp, _sources_tp)
    else:
        print(f"  [SKIP] tender-pack scale verification for {_tp_site_plan} — fixture not present")
except ImportError as _e:
    print(f"  [SKIP] scale.py real-sheet regression tests — missing dependency: {_e}")

print(f"\n==== {sum(P)}/{len(P)} PASS ====")
sys.exit(0 if all(P) else 1)
