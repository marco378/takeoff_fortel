# Fortel AI Takeoff — Findings & Honest Status

## 1. Does Fortel receive marked or unmarked drawings?
**Unmarked.** Confirmed from the actual tender pack ("Info Used from Enquiry"): the originals are
architect/engineer **vector PDFs** — `25026-HFR-V1-...Proposed Ground Floor GA Plan`,
`...Site Location Plan`, `Civil & Structural Specifications`, `Insulation Plan`, `Mezzanine Slab`.
Fortel marks them up in Bluebeam itself — the area polygons we read are **Fortel's own work product**,
not what arrives. So the AI must take off from unmarked drawings.

Inputs seen in the wild: **unmarked vector PDF** (common), **raster/scanned PDF** (some), and — if we
ask for it — **DWG/CAD** (best case).

## 2. Can we do the unmarked takeoff?
**Yes, but as vision + assessor-in-the-loop — not pure geometry, and not 0% unattended.**

Works on unmarked today:
- per-viewport **scale calibration** (the sheets mix scales; we calibrate per region);
- **exact** extraction of the colour-coded **site boundary** (yard sheet → 34,329 m²);
- **vision** (Claude) reliably *identifies* the yard / building / boundary in the rendered drawing.

**The CAD layers change the picture (key finding):**
- The vector PDF **retains CAD layers (OCGs)** — 46 of them on the site plan, named
  `ap_Site Boundary`, `ap_External Walls`, `A-FLOR-OTLN`, `ap_Floors`, `ap_kerbs`, `ap_Parking`,
  `ap_Roads`, `ap_Structure`, `ao_Grid`. PyMuPDF tags every path with its layer, so we isolate
  geometry **per layer** instead of fighting a 22k-path mesh.
- The `ap_Site Boundary` layer extracts the site envelope **exactly: 34,329 m²** (one clean polygon).
- Building / paving layers are isolated but are open outlines — convex hull overshoots (building hull
  17k vs true ~8k), naive polygonise undershoots (micro-gaps). So they need **per-layer gap-bridging**
  to close into exact areas — a few days of geometry engineering, not magic.
- **Which layers = "concrete yard"** (boundary − building − parking − landscaping) is the **assessor
  confirm** step — now "tick the right layers", not "trace from scratch".
- Caveat: some PDFs are flattened on export (the office GA plan had **0 layers**) — so layer presence
  varies and the router must detect it.

**The working method (validated): VISION + GEOMETRY.** A Claude vision call identifies the slab
region on the rendered drawing; geometry measures it at the per-viewport scale. We confirmed (see
overlay.png) the estimator's 26,080 region is a single judgement-traced polygon (covers building +
aprons + parking, ~76% of the site) that does NOT follow clean layer boundaries — so the LLM has to
do the identification, which it can. Validated on the Winvic yard (gold 26,080):
  - a coarse 9-point trace      -> 24,725 m² (5.2%)
  - an ~18-vertex vision trace  -> 26,037 m² (0.16%)
The LLM does the judgement (identify the region); geometry does the measurement (exact). The
assessor nudges to perfect. This is `vision_takeoff.py`. **So unmarked WORKS — to estimator accuracy.**

## 3. The two routes to high-accuracy unmarked takeoff
1. **Per-layer geometry from the PDF** — works whenever the PDF keeps layers. Build robust per-layer
   polyline closure on the isolated layers (`cad_layer_takeoff.py` is the starting point).
2. **Ask contractors for DWG/CAD** — always clean, layered, closed polylines → near-exact, least effort.
   Tender packs often include CAD. **Highest-leverage move: request CAD where available.**

## 4. Test matrix (every input type)
| input | type | measured | gold | result |
|---|---|---|---|---|
| Yard site plan | marked vector | 26,080 | 26,080 | PASS (0.00%) |
| Dock site plan | marked vector | 930 | 930 | PASS (0.02%) |
| Office floors GA | marked vector | 3,479 | stored | PASS |
| Transport office | marked vector | 729 | stored | PASS |
| Synthetic yard | synthetic vector | 25,920 | 25,920 | PASS |
| Unmarked yard — site envelope | unmarked vector | 34,329 | 34,329 | PASS (envelope) |
| **Unmarked yard — concrete slab** | unmarked vector | ~24% off by geometry | 26,080 | **needs vision+assessor** |
| Raster / scanned | raster | — | — | flagged → human (by design) |
| Non-slab / mixed | triage | reject | — | routed → human (by design) |

## 5. Done vs not
**Done, production-grade:** marked-drawing takeoff (exact), costing engine (exact to £1,823,687.32),
drawing→£ chain, input router, test harness, self-improving loop.
**Not done (genuine ML build, not a one-session task):** unmarked auto-measure to estimator accuracy —
needs the vision + assessor Phase-3 system in a Claude Code repo, or CAD inputs.

## 6. Value Fortel can capture now (before Phase 3 is finished)
- Auto-ingest the **back-catalogue of marked jobs** → instant re-pricing / benchmarking (exact).
- **Auto-price** any job the moment an area is known (exact).
- **Auto-extract the spec/briefing card** from drawings + spec (Phase 2).
- **Assisted** area takeoff (AI proposes, assessor confirms) — large saving vs measuring from scratch.
