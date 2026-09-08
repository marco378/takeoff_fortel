# Fortel AI Takeoff — the LLM identification piece (corrected)

You were right: the version sent earlier had the slab polygon **hardcoded** — the "LLM does the
identification" claim wasn't actually wired to an LLM. Now fixed. Identification is a real Claude
vision call: `vision_llm.py` does render(drawing) -> Claude vision returns the slab polygon as JSON
-> geometry measures it. (Set ANTHROPIC_API_KEY to run the live call.)

## Demonstrated on the Winvic yard (gold 26,080 m2)
Two INDEPENDENT Claude vision traces of the UNMARKED drawing (Bluebeam markup stripped first):

| identifier            | area      | area err | IoU (shape overlap) |
|-----------------------|-----------|----------|---------------------|
| Claude (this session) | 25,491 m2 | 2.2%     | 0.86                |
| Claude (separate agent) | 26,059 m2 | 0.1%   | 0.48                |

## The important lesson — score by IoU, not area
The separate agent nailed the AREA (0.1%) but its IoU was only 0.48: it traced a DIFFERENT region
(further right, missing the bottom forecourt) that happened to have nearly the same area. **Area can
match while the extent is wrong.** So the engineering rules are:
1. Score identification by IoU / overlap with the assessor-confirmed region — never by area alone.
2. Always MEASURE the returned polygon deterministically. Never trust the LLM's self-reported number
   — the separate agent even confabulated "seeing" the 26,080 label, which had been stripped from the
   image. The geometry is the source of truth; the LLM only proposes the region.
3. The assessor-confirm step is essential: the LLM gets the rough region (~0.86 IoU on a good pass),
   the human nudges the extent, and you're at estimator accuracy. This is the Phase-3 loop.

## Architecture (now real, end to end)
render(drawing) -> **Claude vision** -> slab polygon JSON -> geometry measures (deterministic,
per-viewport scale) -> IoU gate / assessor confirm -> area -> costing.py -> GBP.

## Honest accuracy story
Not "0.16% autonomous". It's: LLM identification ~0.86 IoU / ~2% area on a good first pass, assessor-
nudged to exact. Geometry and costing remain exact (marked drawings 0.00-0.02%; BOQ to the penny).

## Adversarial round — breaks found, then fixed
We then tried to BREAK the model across all four real drawings (yard, dock, office, transport):

| break | symptom | fix |
|---|---|---|
| Hardcoded scale (k=0.108) | dock 349 (63% err), office 8,325 (139%), transport 7,261 (896%) | scale never hardcoded |
| Multi-region slabs | dock/office/transport are 4 polygons each, not 1 | sum all slab polygons |
| Unreliable scale sources | title-block 1:500 makes the yard 69,560 vs 26,080; sheet says "do not scale"; perimeter-cal 780% err; dim-lines a different viewport | per-viewport calibration only; UNVERIFIED + flagged |
| Flattened drawings | office/transport have 0 CAD layers | no layer anchor -> vision-only |

After fixes:
- Marked path: 4/4 EXACT (yard 26,080, dock 930, office 3,479, transport 729) — multi-region summed, no scale needed.
- Vision path: scale REQUIRED + flagged — measure() raises without a calibrated scale; title-block scale returned UNVERIFIED so the system never silently emits a wrong number; assessor confirms scale per viewport.

Headline: scale calibration is the #1 risk and no shortcut solves it — production must read a scale bar / known dimension in the slab's own viewport (an LLM/CV reading task) and gate on assessor confirmation.

## Adversarial round 2 — geometry hardening + the scale fix
More breaks, all with KNOWN areas, all fixed in `geometry.py`:

| break | symptom | fix |
|---|---|---|
| Slab with voids | vision traces outer only -> 8.8% over (SOP omits dock voids/pits) | subtract void rings (Polygon holes) |
| Self-intersecting trace | bad LLM polygon -> area 0 (garbage) | make_valid + flag for IoU re-check |
| Overlapping regions | summed -> double-count (20,000 vs true 17,500) | union, not sum |
| Missing scale | silent wrong number | raise — never hardcode/guess |

Scale, the constructive fix: the vision call returns the polygon PLUS a SCALE REFERENCE (two points
on a scale bar / a known dimension + its real length). Demonstrated on a synthetic (50 m bar = 500 pt):
hardcoded k -> 9,331 (wrong); scale-from-reference -> 8,000 EXACT. Scale IS solvable when the LLM reads
a reference in the slab's own viewport.

## Files (new/updated)
- `geometry.py` — hardened measurement: voids, self-intersection repair, overlap union, scale required
- `vision_llm.py` — real Claude-vision identification; scale now REQUIRED (hardcode removed)
- `robust_takeoff.py` — multi-region marked path (4/4 exact) + per-viewport scale calibration with flagging
- `LLM_RESULTS.md` — this note
- `agent_vs_true.png` — the two LLM traces vs the estimator's region
- everything else (engine, costing, real_takeoff, router, all_tests) unchanged — you have these.
