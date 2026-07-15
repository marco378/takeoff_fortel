# Fix: Rate Build-up Preview & Measured Area Display

## Problem Analysis

### Bug 1: Rate Build-up shows stale area/rates after adjustment
**Root cause**: `renderJobDetails()` at `assessor_portal.html:789` reads `res.costing` (from `job.result.costing`) FIRST:
```javascript
renderCosting(res.costing || job.costing, job.status);
```
- `job.result.costing` is set during the takeoff pipeline (line 427 of `takeoff_pipeline.py`) and is **never updated**.
- When the assessor adjusts (spec-override or adjust), the new costing is stored at `job["costing"]` (top-level), NOT in `job["result"]["costing"]`.
- So the portal always shows the **original takeoff costing**, not the adjusted one.

### Bug 2: Measured Area shows `—` when AI has measured
**Root cause**: `updateAreaDisplay()` at `assessor_portal.html:1146-1162` only displays the assessor's traced polygon area. When no polygon is traced (`calcArea()` returns null), it shows `—` even though the AI measured an area stored in `currentJob.result.area_m2`.

After an adjust, `currentJob.adjusted.area_m2` also holds the corrected area, but is never shown in the Measured Area section.

### Bug 3: Rate not formatted to 2 decimal places
`£${c.rate}/m²` can show floating-point artifacts like `£45.070000000001/m²`.

## Fixes (all in `assessor_portal.html`)

### Fix 1: Prefer latest costing in rate build-up (line 789)
**Before**: `renderCosting(res.costing || job.costing, job.status);`
**After**: `renderCosting(job.costing || res.costing, job.status);`

This ensures `job.costing` (set by approve/adjust/spec-override, always the latest) takes priority over the stale `res.costing` from takeoff.

### Fix 2: Update `updateAreaDisplay()` (lines 1146-1162)
When no assessor trace (`calcArea()` returns null), fall back to:
1. `currentJob.adjusted.area_m2` (adjusted area)
2. `currentJob.result.area_m2` (AI-measured area)

Show the appropriate source label ("Adjusted" or "AI measured") instead of "No polygon traced yet".

### Fix 3: Format rate to 2 decimal places (line 861)
**Before**: `£${c.rate}/m²`
**After**: `£${(+c.rate).toFixed(2)}/m²`

## Verification
1. Start the portal: `APPROVAL_PORT=5001 .venv/bin/python approval_server.py`
2. Open portal, select a job that has been measured by AI
3. Verify "Measured Area" shows the AI area (not `—`) when no polygon is traced
4. Approve a job, then use spec-override to change depth/mesh
5. Verify the rate build-up updates with the new rate and spec
6. Adjust a job with a new polygon, verify rate build-up shows adjusted area and new total
7. Run tests: `.venv/bin/python ci_tests.py`
