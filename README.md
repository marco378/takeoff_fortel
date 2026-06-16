# Fortel AI Takeoff — final product

End to end: drawing PDF -> classify -> measure -> price -> GBP, hardened against the failure
modes found in adversarial testing.

## Run
```
python3 takeoff_pipeline.py    # run on the real drawings
python3 final_tests.py         # 13/13 master test
```

## Pipeline (`takeoff_pipeline.py`)
```
ingest(pdf) -> router.classify ->
   MARKED vector    : read Bluebeam markups (exact, multi-region) — no scale needed
   UNMARKED vector  : Claude vision -> {regions, voids, scale_ref}
                      -> geometry.measure_regions -> assessor confirms extent + scale
   RASTER/flattened : vision + MANDATORY human
-> price_zone -> GBP
```

## Modules
- `takeoff_pipeline.py` — product entry point (classify -> measure -> price)
- `router.py` — input classifier (marked / unmarked / raster; layers; scales)
- `robust_takeoff.py` — marked reader (multi-region, exact) + per-viewport scale calibration + flags
- `vision_llm.py` — REAL Claude-vision identification (render -> {regions, voids, scale_ref})
- `geometry.py` — hardened measurement (voids, self-intersection, overlap; scale required)
- `costing.py` — Stage-2 build-up; self-validates to GBP 1,823,687.32 on run
- `final_tests.py` — 13/13 master test

## Verified (13/13)
- Marked: Yard 26,080 / Dock 930 / Office 3,479 / Transport 729 — exact
- Geometry: voids subtracted (23,900), self-intersection repaired (5,000), overlap unioned (17,500)
- Scale: reference -> exact (8,000); missing -> raises (never hardcoded)
- Costing: yard GBP 1,170,731.20; full BOQ GBP 1,823,687.32; unknown mesh / zero area flagged

## Honest accuracy / what needs a human
- Marked drawings & costing: EXACT.
- Unmarked: Claude identifies the region (~0.86 IoU on a good pass); geometry measures it exactly;
  the assessor confirms EXTENT and SCALE. Score by IoU, not area. Never trust the LLM's self-reported
  number — always measure the returned polygon.
- Scale is the #1 risk: read a scale bar / known dimension in the slab's OWN viewport, per-viewport,
  flagged until the assessor confirms.

## Adversarial breaks found & fixed (so far)
hardcoded scale (5x range, 63-900% err); multi-region slabs (4 each); unreliable scale sources
(title-block, perimeter, dim-lines); flattened drawings (0 layers); voids; self-intersecting traces;
overlapping double-count; unknown mesh; zero/negative area. All fixed or flagged — no silent wrong numbers.

## Carry forward (Claude Code repo)
1. Live Claude vision API (`vision_llm.identify_slab`) returning {regions, voids, scale_ref}.
2. Assessor UI: polygon + scale over the drawing, one-click confirm/nudge, log corrections as training data.
3. Scale-bar / grid CV detector as a second, cross-checked source.
4. Wire measured area -> `costing.py` -> quote draft in the n8n flow.
