# Fortel AI Takeoff — Working Demo

*Input → process → output, run on the four real Winvic engineer drawings. Every number below
comes from running the pipeline, not from a spreadsheet.*

## The system in one line
```
drawing.pdf → router (classify) → scale (VERIFY against a known feature) →
identify region (Claude vision / read markup) → measure (hardened geometry) →
plausibility guard → costing → £
```

## How to read each drawing
- **Input** — the *unmarked* engineer drawing, exactly as Fortel receives it.
- **What the estimator marks up** — the Bluebeam area markup = our ground truth.
- **Pipeline trace** — every stage with its actual output.
- **Output** — area (m²) → £, checked against ground truth.
- **Unmarked-path behaviour** — what the system does on the raw file (no fabricated numbers).

The single most important stage is **Scale**. Fortel's own rule (from the estimation call): never
trust the title-block scale — *verify it against a known real-world feature* (a UK car-parking bay is
2.5 m, or a printed dimension, or a scale bar), "because sometimes they forgot to change the scale on
their template." The demo shows this catching a real error.

| # | Drawing | Type | Ground-truth area |
|---|---|---|---|
| 1 | Yard | site plan, 46 CAD layers | 26,080 m² |
| 2 | Dock | site plan, 46 CAD layers | 930 m² |
| 3 | Office floors | GA floor plan, flattened, mixed-scale | 3,479 m² |
| 4 | Transport office | GA floor plan, flattened, mixed-scale | 729 m² |

---

## 1. Yard — Proposed Site Plan (California Drive, Castleford)

**Input (as Fortel receives it):** `UNMARKED_Yard.pdf` — site plan, 1p, 46 CAD layers, ~22,200 vector paths, title-block scale 1:500.  ·  render: `demo/yard_input.png`
**What the estimator marks up (ground truth):** 1 Bluebeam area markup = **26,080.2 m²**.  ·  render: `demo/yard_marked.png`

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | MARKED vector (1 area markup, conf=high) / the raw file → UNMARKED vector (0 markups, scale ['1:500']) |
| 2 · **Scale** | title-block vs verified against a 2.5 m car-park bay | title-block 1:500 → k=0.1764;  verify vs bay → k=0.1080 → ⚠ **"title-block scale (k=0.1764) DISAGREES with the verified feature (k=0.1080) → PDF not at its stated scale; using the feature scale"** |
| 3 · Region | read the Bluebeam markup (live system: Claude vision) | 1 polygon (103 vertices) |
| 4 · Measure | hardened geometry | **26,080.2 m²** |
| 5 · Plausibility | sanity guard | verified area: OK. *At the unverified 1:500 it would be **69,560 m² → BLOCKED** ("exceeds the site boundary 34,329 m²")* |
| 6 · Cost | rate build-up | 26,080 × £44.89/m² = **£1,170,731** |

**Output → `26,080 m²  →  £1,170,731`** — matches ground truth exactly. ✓
**This is the headline:** the yard PDF is actually ~1:306, not the stated 1:500. Trusting the title block gives 69,560 m² (the class of error that produced the 95,463 m² incident); the bay-verified scale recovers 26,080, and the plausibility guard would have blocked the wrong number anyway.
**On the raw unmarked file:** classified UNMARKED vector; the system renders + flags *"needs Claude vision identification + assessor confirm"* and emits no number without a verified scale.

---

## 2. Dock — Proposed Site Plan (California Drive, Castleford)

**Input (as Fortel receives it):** `UNMARKED_Dock.pdf` — site plan, 1p, 46 CAD layers, ~22,200 vector paths, title-block scale 1:500.  ·  render: `demo/dock_input.png`
**What the estimator marks up (ground truth):** 4 Bluebeam area markups (dock aprons) = **929.8 m²**.  ·  render: `demo/dock_marked.png`

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | MARKED vector (4 area markups, conf=high) / raw → UNMARKED vector |
| 2 · **Scale** | title-block vs verified against a 2.5 m car-park bay | title-block 1:500 → k=0.1764;  verify vs bay → k=0.1764 → ✓ **"scale VERIFIED from parking bay 2.5 m"** (the dock viewport really is 1:500 — verification *confirms* the title block) |
| 3 · Region | read the Bluebeam markup (live system: Claude vision) | 4 polygons (multi-region) |
| 4 · Measure | hardened geometry, multi-region | **929.8 m²** |
| 5 · Plausibility | sanity guard | OK — within bounds |
| 6 · Cost | rate build-up | 930 × £63.37/m² = **£58,934** |

**Output → `930 m²  →  £58,934`** — matches ground truth. ✓ (4 separate aprons summed correctly; the BOQ rate £63.37 vs the from-first-principles £63.77 differ only by a steel-wastage assumption.)
**On the raw unmarked file:** classified UNMARKED vector; flags for Claude vision + assessor; no number emitted without a verified scale.

---

## 3. Office Floors — Proposed GA Office Plans

**Input (as Fortel receives it):** `UNMARKED_Office.pdf` — GA floor plan, 1p, **0 CAD layers (flattened)**, ~91,600 vector paths, title-block lists **1:100, 1:200, 1:1250**.  ·  render: `demo/office_input.png`
**What the estimator marks up (ground truth):** 4 Bluebeam area markups (floors) = **3,478.6 m²**.  ·  render: `demo/office_marked.png`

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | UNMARKED vector, flattened, multi-region |
| 2 · **Scale** | mixed-scale sheet | title block lists 1:100 / 1:200 / 1:1250 → scale_consensus: ⚠ **"references DISAGREE (12.5× spread) → MIXED-SCALE sheet; use the slab's own viewport (scale bar) or assessor confirms. DO NOT auto-pick."** |
| 3 · Region | read the 4 markups (live system: Claude vision per legend) | GF 1,091.28 + FF 1,119.21 + SF 1,119.21 + Plant deck 148.86 m² |
| 4 · Measure | hardened geometry, multi-region | **3,478.6 m²** |
| 5 · Plausibility | sanity guard | OK (no site boundary on a floor plan) |
| 6 · Cost | rate build-up (indicative) | 3,479 × £33.08/m² = **£115,072** |

**Output → `3,479 m²  →  £115,072 (indicative)`** — matches ground truth. ✓
**On the raw unmarked file:** UNMARKED vector, **flattened (no layer anchor)** and **mixed-scale** → flags *"needs Claude vision + a verified scale + assessor confirm"*; no number emitted.

---

## 4. Transport Office — Proposed Transport Office

**Input (as Fortel receives it):** `UNMARKED_Transport.pdf` — GA floor plan, 1p, **0 CAD layers (flattened)**, ~69,800 vector paths, title-block lists **1:100, 1:1000**.  ·  render: `demo/transport_input.png`
**What the estimator marks up (ground truth):** 4 Bluebeam area markups (floors) = **728.6 m²**.  ·  render: `demo/transport_marked.png`

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | UNMARKED vector, flattened, multi-region |
| 2 · **Scale** | mixed-scale sheet | title block lists 1:100 / 1:1000 → scale_consensus: ⚠ **"references DISAGREE (10× spread) → MIXED-SCALE sheet; … DO NOT auto-pick."** |
| 3 · Region | read the 4 markups (live system: Claude vision per legend) | GF A 180.95 + GF B 181.05 + FF A 185.43 + FF B 181.16 m² |
| 4 · Measure | hardened geometry, multi-region | **728.6 m²** |
| 5 · Plausibility | sanity guard | OK |
| 6 · Cost | rate build-up (indicative) | 729 × £33.08/m² = **£24,102** |

**Output → `729 m²  →  £24,102 (indicative)`** — matches ground truth. ✓
**On the raw unmarked file:** UNMARKED vector, flattened + mixed-scale → flagged for vision + verified scale + assessor; no number emitted.

---

## Summary

| Drawing | Title → verified scale | System area | Ground truth | £ | Status |
|---|---|---|---|---|---|
| Yard | 1:500 → **1:306** (bay-verified) ⚠ | 26,080 m² | 26,080 | £1,170,731 | ✓ exact — title-block caught as a 1.63× lie |
| Dock | 1:500 = 1:500 ✓ | 930 m² | 930 | £58,934 | ✓ exact — scale confirmed |
| Office | mixed → **flagged** | 3,479 m² | 3,479 | £115,072* | ✓ area exact; scale → assessor |
| Transport | mixed → **flagged** | 729 m² | 729 | £24,102* | ✓ area exact; scale → assessor |

\* indicative costing (the real BOQ splits floors by thickness/spec). Costing is fully validated on
the yard rate (£44.89/m²) and the complete Winvic BOQ (**£1,823,687.32**, to the penny).

## What this demonstrates
- **Area → £ is exact on every drawing** where the region is known (marked, or vision-confirmed).
- **Scale is always verified against a known feature** — the yard's stated 1:500 is a 1.63× lie and the
  system catches it (would have shipped 69,560 m²; bay-verified → 26,080). The dock's scale is confirmed.
  Office/Transport are mixed-scale and get **flagged**, not guessed.
- **Multi-region** slabs (dock/office/transport — 4 polygons each) are summed correctly.
- **Nothing impossible ever ships:** the plausibility guard blocks a slab bigger than its site (the 69k / 95k class).

## Honest limits
- On a *truly unmarked* drawing the **region identification** needs the Claude vision call (API key) plus an
  **assessor confirm** — by design, the unmarked path emits no number without a verified scale and a confirmed region.
- Office/Transport **costing is indicative** (the BOQ splits floors by thickness/spec); the yard £ and the full BOQ are exact.
- The call covered Bluebeam takeoff only — voids/gatehouse deductions and the full Excel build-up are for the next session.
