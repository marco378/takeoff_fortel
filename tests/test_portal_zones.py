#!/usr/bin/env python3
"""zone classification approve gate, /adjust, scale flags, provisional markers.

Sections in this module (printed in this order):
  - [zone classification clears its own approve gate]
  - [/adjust preserves categorized zones even when the area is implausible]
  - [assessor-readable scale flags]
  - [provisional marker placement]
  - [the transition flag must not deny tarmac that is on the sheet]

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from pathlib import Path
from tests import ck

print("\n[zone classification clears its own approve gate]")
import approval_server as _AS_zb

def _zb_job(zone_specs, stale=True):
    zones = [{"zone_key": k, "area_m2": a, "category": c, "subjects": [k]}
             for k, a, c in zone_specs]
    return {
        "id": "zb", "status": "adjusted", "decision": "adjusted", "scale_confirmed": True,
        "zone_allocation_stale": stale,
        "zone_classification_required": any(z["category"] == "unclassified" for z in zones),
        "zones": zones,
        "result": {"measurement_state": "MEASURED_VERIFIED", "area_m2": 100.0,
                   "zone_allocation_stale": stale,
                   "zone_classification_required": any(
                       z["category"] == "unclassified" for z in zones),
                   "zones": zones, "flags": []},
    }

_zb_unclassified = _zb_job([("z1", 100.0, "unclassified")])
ck("stale allocation + an unclassified zone blocks approval (gate must still protect)",
   _AS_zb._zone_block_reason(_zb_unclassified) is not None,
   _AS_zb._zone_block_reason(_zb_unclassified))

# Every zone classified: the staleness the gate complains about has been resolved by the very
# act of classifying, so the block must lift without a second identical submission.
_zb_done = _zb_job([("z1", 100.0, "external_yard")], stale=False)
ck("once every zone is classified, approval is no longer blocked",
   _AS_zb._zone_block_reason(_zb_done) is None, _AS_zb._zone_block_reason(_zb_done))

# The gate must NOT be blanket-disabled: a partially classified job still blocks.
_zb_partial = _zb_job([("z1", 100.0, "external_yard"), ("z2", 50.0, "unclassified")])
ck("a PARTIALLY classified job still blocks — the gate is fixed, not removed",
   _AS_zb._zone_block_reason(_zb_partial) is not None,
   _AS_zb._zone_block_reason(_zb_partial))

# And the other genuine gates are untouched.
_zb_overlap = _zb_job([("z1", 100.0, "external_yard")], stale=False)
_zb_overlap["zone_geometry_overlap"] = True
ck("a genuine geometry overlap still blocks approval",
   _AS_zb._zone_block_reason(_zb_overlap) is not None)
_zb_mismatch = _zb_job([("z1", 100.0, "external_yard")], stale=False)
_zb_mismatch["zone_reference_mismatch"] = True
ck("a genuine zone-vs-BOQ mismatch still blocks approval",
   _AS_zb._zone_block_reason(_zb_mismatch) is not None)

# ── /adjust must not wipe a fully-categorized zone submission ──────────────────────────
# Aryan, 24 Aug: "approval sometimes stays blocked even after submitting it". Root cause:
# categorized_remeasure required `confirmed` (sanity.plausible() on the NEW area), so an
# assessor who categorized every region got their zones silently reset to [] and
# zone_allocation_stale=True whenever the resulting area tripped the plausibility guard (e.g.
# a legitimately large yard over the 60,000 m^2 single-zone bound) — a dead end identical in
# shape to the /zones phantom-block bug above, just reached through /adjust instead.
print("\n[/adjust preserves categorized zones even when the area is implausible]")
import tempfile as _zw_tempfile
_zw_orig_jobs_file = _AS_zb.JOBS_FILE
_zw_tmpdir = Path(_zw_tempfile.mkdtemp())
_AS_zb.JOBS_FILE = _zw_tmpdir / "jobs.json"
_zw_client = _AS_zb.app.test_client()
_zw_job_id = "zw1"
_zw_jobs = {_zw_job_id: {
    "id": _zw_job_id, "status": "adjusted", "decision": "adjusted",
    "scale_confirmed": False, "measurement_state": "MEASURED_UNVERIFIED",
    "zones": [{"zone_key": "external_yard", "area_m2": 5000.0, "category": "external_yard",
               "subjects": ["Yard"]},
              {"zone_key": "dock", "area_m2": 200.0, "category": "dock", "subjects": ["Dock"]}],
    "result": {"measurement_state": "MEASURED_UNVERIFIED", "area_m2": 5200.0,
               "zones": [{"zone_key": "external_yard", "area_m2": 5000.0,
                          "category": "external_yard", "subjects": ["Yard"]},
                         {"zone_key": "dock", "area_m2": 200.0, "category": "dock",
                          "subjects": ["Dock"]}], "flags": []},
}}
_AS_zb.save_jobs(_zw_jobs)
_zw_big = [[0, 0], [300000, 0], [300000, 300000], [0, 300000]]  # trips the >60,000 m^2 guard
_zw_small = [[0, 0], [10, 0], [10, 10], [0, 10]]
_zw_resp = _zw_client.post(f"/adjust/{_zw_job_id}", json={
    "regions": [_zw_big, _zw_small],
    "region_categories": ["external_yard", "dock"],
    "region_scopes": ["main", "main"],
    "scale_k": 1.0,
})
ck("categorized /adjust with an implausible area is still accepted",
   _zw_resp.status_code == 200, _zw_resp.status_code)
_zw_after = _AS_zb.load_jobs()[_zw_job_id]
_zw_categories = sorted(z.get("category") for z in (_zw_after.get("zones") or []))
ck("...its regions keep the categories the assessor actually submitted, not []",
   _zw_categories == ["dock", "external_yard"], _zw_categories)
ck("...zone_allocation_stale is NOT set — a categorized submission is not a stale aggregate one",
   _zw_after.get("zone_allocation_stale") is False, _zw_after.get("zone_allocation_stale"))
_zw_reason = _AS_zb._approve_block_reason(_zw_after)
ck("...the resulting block (if any) names the real problem (implausible/unverified area), "
   "not a false 'reclassify zones' demand for zones the assessor just classified",
   _zw_reason is not None and "reclassify/remeasure the drawing zones" not in _zw_reason,
   _zw_reason)
_AS_zb.JOBS_FILE = _zw_orig_jobs_file




# ── Assessor-readable scale flags ──────────────────────────────────────────────────────
# Inderjit read the old scale-disagreement flag aloud on 20 Aug and said "Nothing makes sense
# to me, really". The gate was RIGHT (two references 3x apart, refused to auto-pick) — the copy
# was unusable. The team's proposal was to hide yellow flags, which would turn a correct
# refusal into silence. Instead the first line is now an instruction an estimator can act on,
# with the technical detail retained behind a [detail] prefix.
print("\n[assessor-readable scale flags]")
from scale import scale_consensus as _sc_copy, calibrate_verified as _cal_copy
_k_copy, _f_copy = _sc_copy([(25.0, 120.0), (25.0, 40.0)])
ck("disagreeing references still REFUSE — copy change must not weaken the gate",
   _k_copy is None, _k_copy)
ck("the first flag tells the assessor what to DO, not what went wrong internally",
   _f_copy[0].startswith("Two scale readings") and "Confirm the scale before approving" in _f_copy[0],
   _f_copy[0][:90])
ck("...and it names the ways to set it (scale bar / parking bay / printed dimension)",
   all(t in _f_copy[0] for t in ("scale bar", "parking bay", "printed dimension")))
ck("the technical detail is kept for us, marked [detail]",
   any(f.startswith("[detail]") and "MIXED-SCALE" in f for f in _f_copy), _f_copy[-1][:80])
ck("no assessor-facing line shouts in block capitals at the estimator",
   not any(w.isupper() and len(w) > 4 for w in _f_copy[0].split()), _f_copy[0][:70])
_f_unver = _cal_copy(title_denominator=250)[1]
ck("title-block-only scale reads as an instruction, not a status code",
   _f_unver[0].startswith("Scale taken from the title block only"), _f_unver[0][:70])
ck("no-reference case tells the assessor to set it manually",
   "set the scale manually" in _cal_copy()[1][0], _cal_copy()[1][0])




# ── Provisional marker out of the DESCRIPTION cell ─────────────────────────────────────
# It was appended with a newline, so every provisional row rendered as two lines and looked
# untidy beside Fortel's own template. Their sheet keeps tender caveats in a column right of
# VALUE, so the marker lives there now — still impossible to miss, no longer wrapping the label.
print("\n[provisional marker placement]")
import io as _io_pm, openpyxl as _ox_pm
from quotation import (generate_quotation as _gq_pm, quotation_xlsx as _qx_pm,
                       PROVISIONAL_LABEL as _PL_pm, PROVISIONAL_COL as _PC_pm)
_doc_pm = {"file": "yard.pdf", "area_m2": 366.2,
           "costing": {"area_m2": 366.2, "rate": None, "spec": {}, "assumed": True},
           "zones": [{"category": "external_yard", "area_m2": 366.2}],
           "manhole_count_assumed": 12}
_ws_pm = _ox_pm.load_workbook(_io_pm.BytesIO(
    _qx_pm(_gq_pm([_doc_pm], project="P", client="C")))).active
_marked_pm = [r for r in range(1, _ws_pm.max_row + 1)
              if _ws_pm.cell(r, _PC_pm).value == _PL_pm]
ck("provisional rows still carry the marker (it was relocated, not dropped)",
   len(_marked_pm) > 0, f"{len(_marked_pm)} marked rows")
_line_rows_pm = [r for r in _marked_pm if "\n" in str(_ws_pm.cell(r, 1).value or "")]
ck("no marked row wraps its DESCRIPTION onto a second line any more",
   not _line_rows_pm, [str(_ws_pm.cell(r, 1).value)[:40] for r in _line_rows_pm])
ck("the marker never appears inside a line-item DESCRIPTION cell",
   not any(_PL_pm in str(_ws_pm.cell(r, 1).value or "") for r in _marked_pm))
ck("it sits to the right of VALUE, mirroring Fortel's REMEASURE caveat column",
   _PC_pm == 6, _PC_pm)




# ── Standards citations must never be read as a drawing scale ──────────────────────────
# Inderjit, 25 Aug call: "it took the wrong scale also it should have been like one is to five
# hundred". Cause, found by running his real project-6 sheet: the spec note "75mm sand:cement
# screed to BS 8204 Part 1: 2003" contains a literal "1: 2003", which beat the genuine 1:500 in
# the title block. k became 0.70661 instead of 0.17639 — 4x out linearly, 16x out on AREA.
# The guard is a standards-citation strip plus a four-digit scale SERIES whitelist; a year is in
# no series. It deliberately does NOT ban large scales — 1:1500 and 1:2000 are real and in live
# use on the CADIC site sheets, and banning them would silently break those instead.
print("\n[the transition flag must not deny tarmac that is on the sheet]")
try:
    import os as _os_bt
    import takeoff_unmarked as _tu_bt

    ck("bituminous detector reads BS EN 13108 designations, not the word 'tarmac'",
       [n for n, _ in _tu_bt.BITUMINOUS_SPEC_TOKENS] == ["AC20", "AC32", "SMA", "dense bin",
                                                         "BS EN 13108"],
       _tu_bt.BITUMINOUS_SPEC_TOKENS)

    _bt_mjm = ("drawings/inderjit_p7/"
               "7_25195-MJM-00-00-DR-C-9000-D2-P04-External_Works_Layout.pdf")
    if not _os_bt.path.exists(_bt_mjm):
        print(f"  [SKIP] MJM tarmac-honesty guard — client fixture not present")
    else:
        # That sheet carries 1,813 m2 of bituminous surfacing (HEAVY DUTY ACCESS = 40mm SMA 10
        # / 60mm AC20 / 200mm AC32) while the flag claimed none existed. Zero transitions is
        # still the right ANSWER there — tarmac and yard meet at a corner, 87 px of 233,102
        # within 0.5 m — but the stated REASON was false.
        ck("MJM: the sheet's bituminous surfacing is detected from its BS EN 13108 spec",
           _tu_bt._sheet_names_bituminous_surfacing(_bt_mjm) == ["AC20", "AC32", "SMA",
                                                                 "dense bin", "BS EN 13108"],
           _tu_bt._sheet_names_bituminous_surfacing(_bt_mjm))
        _bt_res = _tu_bt.takeoff(_bt_mjm, source="engineer")
        _bt_flags = _bt_res.get("flags") or []
        ck("MJM: the flag no longer claims there is no tarmac on it",
           not any("no tarmac/macadam/asphalt surface legend was found" in _f
                   for _f in _bt_flags))
        ck("MJM: it says the tarmac IS there but is named by build-up",
           any("DOES carry bituminous surfacing" in _f for _f in _bt_flags),
           [_f[:120] for _f in _bt_flags if "Transition" in _f])
        ck("MJM: still emits no transition, which remains the correct answer",
           not _bt_res.get("transition_candidates"))

    # A sheet with genuinely no bituminous surfacing must still say so plainly.
    _bt_d77 = "drawings/D77-REAL_D77_Hard_Landscaping.pdf"
    if _os_bt.path.exists(_bt_d77):
        ck("D77: no bituminous build-up detected, because there is none",
           _tu_bt._sheet_names_bituminous_surfacing(_bt_d77) == [])
except (ImportError, FileNotFoundError, AttributeError) as _e:
    print(f"  [SKIP] transition-flag honesty regression — missing dependency or file: {_e}")


# The message an assessor actually reads. On 16 Sep 2026 a live handover call hit
# "QuotationPricingBlocked: one or more measured markup zones are unclassified" with no
# indication of WHICH zone, so the assessor had to hunt for it in the Measured zones table.
# The classification control was on that screen all along; the message just never said where.
_zb_named = _zb_job([("z1", 100.0, "external_yard"), ("z2", 1234.5, "unclassified")],
                    stale=False)
_zb_named["zones"][1]["subjects"] = ["Concrete slab for road"]
_reason_named = _AS_zb._zone_block_reason(_zb_named)
ck("the unclassified-zone block names the zone it is complaining about",
   "Concrete slab for road" in str(_reason_named), _reason_named)
ck("...and its quantity, so it can be picked out of the table",
   "1,234.5" in str(_reason_named))
ck("...and points at the control that fixes it",
   "Measured zones table" in str(_reason_named))
# A zone with no Bluebeam subject still has to be identifiable.
_zb_keyed = _zb_job([("z2", 50.0, "unclassified")], stale=False)
ck("a zone with no subject is named by its key rather than left anonymous",
   "z2" in str(_AS_zb._zone_block_reason(_zb_keyed)),
   _AS_zb._zone_block_reason(_zb_keyed))

# The two messages an assessor read on the 16 Sep handover call, both of which sent him the
# wrong way. Neither is cosmetic: the first is the only hard failure on the named-area path,
# and it is what silently rejected his footpath measurements.
import re as _re_msg

_PORTAL_HTML = open("assessor_portal.html", encoding="utf-8").read()
_SERVER_SRC = open("approval_server.py", encoding="utf-8").read()

ck("no internal variable name reaches the assessor's screen",
   "positive scale_k is required" not in _SERVER_SRC)
ck("...the scale error says what to do instead",
   "Set the scale first" in _SERVER_SRC and "Calibrate" in _SERVER_SRC)

# "AI polygon cleared" read as if it had saved. It clears the canvas and nothing else: the
# stored measurement is untouched until an adjustment is submitted, so the AI's area was
# still in the downloaded sheet and the outlines came back on reload.
_clear_ai = _PORTAL_HTML[_PORTAL_HTML.index("btnClearAi').addEventListener"):][:900]
ck("Clear AI no longer claims to have cleared the measurement",
   "'AI polygon cleared'" not in _clear_ai)
ck("...it says the measurement is unchanged until an adjustment is submitted",
   "NOT changed" in _clear_ai and "Submit Adjustment" in _clear_ai)

# "It went in a flash" — the quotation failure was on screen for 3.5 seconds and the assessor
# could not say afterwards what it had told him.
_toast = _PORTAL_HTML[_PORTAL_HTML.index("function toast(msg"):][:900]
ck("an error toast holds long enough to be read", "20000" in _toast)
ck("...and any toast can be dismissed by clicking it", "el.onclick" in _toast)

# The classification control was in the last column of a table wider than the panel holding
# it, so on the 16 Sep call it sat off the right edge behind a horizontal scrollbar and took
# forty seconds of spoken directions to reach. It now has its own full-width row.
# Slice to the NEXT function, not a fixed byte count: a 6000-char window silently fell short
# of the markup once the function grew, and the checks below started reading empty text.
_zone_start = _PORTAL_HTML.index("function renderZoneSummary")
_zone_panel = _PORTAL_HTML[_zone_start:
                           _PORTAL_HTML.index("\nasync function classifyZone", _zone_start)]
ck("the Measured zones table no longer forces a horizontal scrollbar",
   "min-width:570px" not in _zone_panel)
ck("the classify control has its own full-width row under the zone",
   'colspan="3"' in _zone_panel and "CLASSIFY THIS ZONE" in _zone_panel)
ck("...and the zone's own row points down at it",
   "classify below ↓" in _zone_panel)
# Six columns overflowed a 280px panel by 100px, which is what pushed the control off the
# right edge in the first place. Three fit: kind and annotation count moved under the
# subject, and the action column went away with the control.
ck("...and the table is three columns, so it fits the panel it lives in",
   _zone_panel.count('<th style="padding:5px;text-align:') == 3,
   f"-> {_zone_panel.count(chr(60) + 'th style=')} th cells")

# Inderjit asked for a recentre button after losing the drawing off-screen. One already
# existed -- zoomFit centres as well as fits -- under an unlabelled ⊞ that nobody on the call
# recognised. Naming it is the fix; a second button would not have been.
ck("the recentre control says what it does rather than showing a bare glyph",
   ">Recentre</button>" in _PORTAL_HTML and "btnZoomFit" in _PORTAL_HTML)
ck("...and zoomFit really does recentre, not only rescale",
   "panX = Math.max(0, (wrapRect.width - scaledW) / 2)" in _PORTAL_HTML)

# The region review list. Nothing is removed from the offer -- every region is still listed,
# still tickable, still counted -- but it is ordered largest-first, the long tail is folded,
# and there is a way to answer it in one action instead of dozens.
_region_panel = _PORTAL_HTML[_PORTAL_HTML.index("function renderSegmentationComponentSummary"):][:9000]
ck("the region review lists the largest regions first",
   "sort((a, b) => (Number(b.area_m2) || 0) - (Number(a.area_m2) || 0))" in _region_panel)
ck("...folds the small ones away instead of making them the first thing on screen",
   "yard-region-tail" in _region_panel and "smaller region" in _region_panel)
ck("...and offers Tick all / Untick all",
   "setAllYardRegions(true)" in _region_panel and "setAllYardRegions(false)" in _region_panel)
_set_all = _PORTAL_HTML[_PORTAL_HTML.index("function setAllYardRegions"):][:700]
ck("Tick all means every region, not only the visible ones",
   "querySelectorAll('.yard-region-toggle')" in _set_all)

# The portal is one 200 KB inline script and the blank-screen bug of 4 Sep lived only in the
# browser: a syntax error there takes the whole assessor screen down and no python test sees
# it. Parse it.
import re as _re_js, subprocess as _sp_js, tempfile as _tf_js, os as _os_js
_blocks = _re_js.findall(r"<script[^>]*>(.*?)</script>", _PORTAL_HTML, _re_js.S)
ck("the portal has exactly one inline script block", len(_blocks) == 1, f"-> {len(_blocks)}")
_js_path = _os_js.path.join(_tf_js.mkdtemp(prefix="portal_js_"), "portal.js")
open(_js_path, "w", encoding="utf-8").write(_blocks[0])
try:
    _js = _sp_js.run(["node", "--check", _js_path], capture_output=True, text=True, timeout=60)
    _js_ok, _js_msg = _js.returncode == 0, (_js.stderr or "").strip().splitlines()[:2]
except FileNotFoundError:
    _js_ok, _js_msg = False, ["node is not installed — the portal script cannot be parsed, "
                              "and a syntax error here blanks the assessor's screen"]
ck("the portal's JavaScript parses", _js_ok, f"-> {_js_msg}")
