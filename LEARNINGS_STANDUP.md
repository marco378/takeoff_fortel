# Learnings — Fortel daily standup (Fri 19 Jun 2026)

Source: `Daily Stand up __ Fortel` transcript + recording. Speakers: Inderjit Singh (Fortel
estimator), Amarvir Sandhawalia (Fortel), Smita/Aryan (Audace, learning Bluebeam). Screen-share showed
Bluebeam Revu marking up the **Unit 1 (D77) Hard Landscaping** sheet at **1 mm = 0.25 m (1:250)** — the
estimator's red polygon traces the concrete service yard, matching our segmented region; a stray
diagonal line appears from closing the loop early (see #6).

## 1. Drawing hierarchy — ENGINEER first, architect is the fallback
> "the first priority is to check if there are any engineer's drawings — most of the time those are named civil and structural."

Order of search:
1. **Engineer (civil & structural)** folder — one combined or separate `civil` / `structural` folders.
   - For AREAS, open the drawing named: **external surfacing / external pavements / external works /
     external construction thickness / construction thickness layout** — "different hatches for different
     pavement types." ← this is the area source.
   - For the BUILD-UP, open **external construction details** — gives slab thickness, concrete mix, mesh.
     e.g. "175 mm thick with A193 mesh", or "200 thick with two layers of A393" → straight into costing.
2. **Architect** folder (fallback) — external works layout → else site plan / site layout → then
   **"guess where the service yard is"** ("a guessing business … you learn it from experience").

> "you could be **5% out on the area** if measuring on the architect's drawing rather than the engineer's."

## 2. SGP is the ARCHITECT (this changes how we frame Hemington)
> "SGP is the architect … they name the folder after the designer. If BWB is the engineer they name it BWB."

So **all our Hemington SGP Hard Landscaping sheets are ARCHITECT drawings.** Consequences:
- The areas we measured are architect-drawing areas → carry the **~5% tolerance**.
- **No construction details exist in the architect pack** → the slab build-up (thickness/mesh) must be
  **ASSUMED**, and the assumption stated in the quote (see #4). Our DEMO2 £'s are therefore *indicative on
  an assumed build-up*, which is exactly how Fortel themselves would issue it.

## 3. ~60% of enquiries arrive without proper engineer drawings
> "sixty percent inquiries came without proper details and proper drawings."

The architect/assumption path is not an edge case — it is the **majority** path. The product must be good
at: pick the best available drawing, measure the service-yard hatch, assume a sensible build-up, flag it.

## 4. Architect drawings → assume the build-up, declare it
> "We have to assume the thickness of slab, what type of mesh … and then we mention in our quotation that
> these assumptions have been made … if the client comes back with specs we change it accordingly."

Action: when the source is an architect drawing (no construction-details sheet found), the costing output
must carry an **ASSUMPTION flag** ("build-up assumed: 190 mm / A252 — no engineer detail; subject to
confirmation") rather than presenting the £ as final.

## 5. Costing sheet is one common template; only a few inputs change
> "The costing sheet … is common for every job. We change the figures — depth of slab, type of mesh, area,
> concrete rate, mesh rate — the formulas are already in there → per-m² rate."

Confirms our `rate_buildup`: the only per-job variables are **depth, mesh, area, concrete rate, mesh rate**.
Everything else (wastage, DPM, curing, labour, trim, margin) is constant in the template.

## 6. Markup technique — don't click the last point (it auto-closes)
> "I think you closed the loop before clicking the last point … delete it and try re-measuring."

In Bluebeam the polygon area tool **auto-closes** to the start point; clicking a final point creates a
stray edge / wrong area. **Assessor-UI action:** snap-to-start to close, never require a final click; and
detect/clear a near-duplicate last vertex. (We saw exactly this stray diagonal on screen.)

## 7. Scale must be checked every time
Smita questioned "is this one to fifty?" — it was **1:250** (panel read 1 mm = 0.25 m). Reinforces the
verify-the-scale rule; the title/calibration must be confirmed, never assumed.

---

## What changes in the system (actioned this session)
- **router.py** — encode the engineer-first hierarchy and the exact keyword list (external surfacing /
  pavements / works / construction thickness / construction details), and classify SGP-style packs as
  *architect → assume build-up*.
- **costing/pipeline** — when the drawing is an architect drawing, emit the **ASSUMPTION flag** and treat
  the £ as indicative (±5% area, assumed build-up).
- **takeoff_unmarked.py** — the region step is **legend-anchored colour segmentation** (reads the
  "Concrete Service Yard" legend, segments that hatch) — the method proven on all 4 units; this is the
  fix for "it works when you run the flow but the .py didn't."
- **assessor UI** — close-on-snap-to-start (no final click), drop duplicate last vertex.
- **API** — the supplied key is valid (vision models reachable); the LLM legend-read/confirm step is now
  wired and proven on Unit 1.
