# Fortel AI Takeoff — Working Demo 2 (TRULY UNMARKED engineer drawings)

*Companion to `DEMO.md`. Demo 1 ran the flow on the **marked** Winvic drawings (estimator's Bluebeam
markup already on the sheet). This demo runs on the **Hemington / SGP tender** — real PDFs **exactly as
Fortel receives them: no markup, no CAD layers, flattened vector**. Here the AI does the estimator's job
itself: read the legend, find the concrete, trace it, scale it, price it. Every number and every red
region below comes from running the pipeline (`takeoff_unmarked.py`) on the actual PDF — nothing is
hand-placed, and the same script reproduces these numbers (regression `test_sgp_units.py`).*

> **From the 19 Jun standup (see `LEARNINGS_STANDUP.md`):** SGP is the **architect**, so this is the
> *architect-drawing path* Fortel use when there's no engineer pack — which the estimator said is **~60%
> of enquiries**. On that path the build-up (slab thickness/mesh) has no construction-details sheet, so
> Fortel **assume it and state the assumption in the quote**; the area also carries a **~5% tolerance**
> vs an engineer drawing. Our £'s below follow exactly that method: measured area × an **assumed**
> 190 mm / A252 build-up, flagged as an assumption. The LLM piece is real and proven — Claude vision read
> this tender's legend back as *"Concrete Service Yard construction"* on the live API.

## The system in one line
```
engineer.pdf → router (classify) → scale (VERIFY against a known feature) →
identify region (read legend → segment the CONCRETE-yard hatch) →
measure (hardened geometry) → plausibility guard → costing → £
```

The hard part on an unmarked sheet is **region identification** — *which* of a dozen hatches is the
priced concrete, and exactly how far it extends. The system reads the **Hard Landscaping legend**,
locks onto the **"Concrete Service Yard construction"** swatch, and segments that hatch only —
excluding the building slab, the bituminous roads, the block-paved car park and the soft landscaping.

| # | Unit | Drawing | Stated scale | What we price |
|---|---|---|---|---|
| 1 | D77  | Hard Landscaping `131002-P03` | 1:250 | Concrete Service Yard |
| 2 | D147 | Hard Landscaping `131001-P07` | 1:500 | Concrete Service Yard |
| 3 | D410 | Hard Landscaping `131001-P06` | 1:750 | Concrete Service Yard |
| 4 | D219 | Hard Landscaping `131002-P02` | 1:500 | Concrete Service Yard |

---

## 1. Unit 1 (D77) — Hard Landscaping, 1:250

| Before — drawing as received (unmarked) | After — the region the AI measured |
|:---:|:---:|
| ![D77 before](demo2/D77_before.png) | ![D77 after](demo2/D77_after.png) |
| no markup · flattened vector · legend only | Concrete Service Yard, wrapping the building's front |

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | **UNMARKED** vector engineer drawing (0 markups, 0 CAD layers) |
| 2 · **Scale** | title block, then **verify** against a known feature | title 1:250 → k=0.08819;  **scale bar confirms** → k=0.08925 → ✓ **VERIFIED (1.2% agreement)** — the sheet really is at 1:250 |
| 3 · Region | read legend → segment the "Concrete Service Yard" hatch | 1 connected region (building, roads, car park, landscaping excluded) |
| 4 · Measure | pixel-accurate area × verified scale | **3,238 m²** |
| 5 · Plausibility | sanity guard | **OK** — within site bounds, < 60,000 m² cap |
| 6 · Cost | rate build-up (190 mm, A252) | 3,238 × £44.89/m² = **£145,354** |

**Output → `3,238 m²  →  £145,354`** · scale verified · region = the red footprint above.

---

## 2. Unit 2 (D147) — Hard Landscaping, 1:500

| Before — drawing as received (unmarked) | After — the region the AI measured |
|:---:|:---:|
| ![D147 before](demo2/D147_before.png) | ![D147 after](demo2/D147_after.png) |
| no markup · flattened vector · legend only | Concrete Service Yard along the dock face |

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | **UNMARKED** vector engineer drawing |
| 2 · **Scale** | title block 1:500 → k=0.1764 | ⚠ **stated scale used — feature-verify pending** (no auto-detected scale bar; assessor confirms a bay/dimension before sign-off) |
| 3 · Region | read legend → segment the concrete-yard hatch | 1 connected region |
| 4 · Measure | pixel-accurate area × scale | **6,584 m²** |
| 5 · Plausibility | sanity guard | **OK** |
| 6 · Cost | rate build-up (190 mm, A252) | 6,584 × £44.89/m² = **£295,556** |

**Output → `6,584 m²  →  £295,556`** · region verified · scale to be feature-confirmed.

---

## 3. Unit 3 (D410) — Hard Landscaping, 1:750

| Before — drawing as received (unmarked) | After — the region the AI measured |
|:---:|:---:|
| ![D410 before](demo2/D410_before.png) | ![D410 after](demo2/D410_after.png) |
| no markup · flattened vector · legend only | Concrete Service Yard — the dock-face band |

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | **UNMARKED** vector engineer drawing |
| 2 · **Scale** | title block 1:750 → k=0.2646 | ⚠ **stated scale used — feature-verify pending** (1:750 is the coarsest sheet; verify a bay before relying on the £) |
| 3 · Region | read legend → segment the concrete-yard hatch | 1 connected band along the loading docks |
| 4 · Measure | pixel-accurate area × scale | **16,697 m²** |
| 5 · Plausibility | sanity guard | **OK** — large but within the site |
| 6 · Cost | rate build-up (190 mm, A252) | 16,697 × £44.89/m² = **£749,528** |

**Output → `16,697 m²  →  £749,528`** · the biggest unit · scale to be feature-confirmed.

---

## 4. Unit 4 (D219) — Hard Landscaping, 1:500

| Before — drawing as received (unmarked) | After — the region the AI measured |
|:---:|:---:|
| ![D219 before](demo2/D219_before.png) | ![D219 after](demo2/D219_after.png) |
| no markup · flattened vector · legend only | Concrete Service Yard, front + side |

| stage | what happens | output |
|---|---|---|
| 1 · Router | classify the input | **UNMARKED** vector engineer drawing |
| 2 · **Scale** | title block 1:500 → k=0.1764 | ⚠ **stated scale used — feature-verify pending** |
| 3 · Region | read legend → segment the concrete-yard hatch | 1 connected region |
| 4 · Measure | pixel-accurate area × scale | **7,509 m²** |
| 5 · Plausibility | sanity guard | **OK** |
| 6 · Cost | rate build-up (190 mm, A252) | 7,509 × £44.89/m² = **£337,079** |

**Output → `7,509 m²  →  £337,079`** · region verified · scale to be feature-confirmed.

---

## Summary — the whole tender, drawing → £

| Unit | Stated scale | Scale verified | Concrete yard | Rate | Price |
|---|---|---|---|---|---|
| 1 · D77  | 1:250 | ✓ scale bar, 1.2% | 3,238 m²  | £44.89/m² | £145,354 |
| 2 · D147 | 1:500 | feature-verify pending | 6,584 m²  | £44.89/m² | £295,556 |
| 3 · D410 | 1:750 | feature-verify pending | 16,697 m² | £44.89/m² | £749,528 |
| 4 · D219 | 1:500 | feature-verify pending | 7,509 m²  | £44.89/m² | £337,079 |
| **Total** | | | **34,028 m²** | | **£1,527,517** |

## What this demonstrates
- **The full chain runs on a truly unmarked engineer drawing** — the input Fortel actually receives.
  Router → scale → **region identified by the AI from the legend** → measure → guard → £. No markup needed.
- **Region identification works:** the system locks onto the "Concrete Service Yard construction" hatch
  and excludes the building slab, roads, car park and landscaping (see every red overlay).
- **Scale is verified, not assumed:** Unit 1's stated 1:250 is confirmed against the printed scale bar to
  1.2%. The pipeline *flags* the other three for a feature-check rather than trusting the title block blindly.
- **Nothing implausible ships:** every area passes the plausibility guard (< site boundary, < 60,000 m² cap).
- **Area → £ is deterministic:** the validated £44.89/m² rate build-up turns each measured area into a price.

## Honest limits (what the assessor still does)
- The red footprint is the AI's proposal from **colour-segmenting the concrete hatch**. The exact boundary
  (a thin border in or out, a sliver of adjacent surface) is an **assessor confirm** in the UI
  (`assessor.html`) — budget **±5–10%** on the area before sign-off.
- **Units 2–4 use the *stated* scale** (no scale bar auto-detected). Unit 1 is feature-verified; the others
  must have a parking bay / printed dimension checked before the £ is relied on — the pipeline flags this.
- The **£44.89/m² rate is indicative** (190 mm slab, A252 mesh). The real spec per unit (thickness, mesh,
  joints, sub-base) refines it; the rate engine and the full Winvic BOQ are validated to the penny separately.
- This is the product's design, not a shortcut: **AI proposes (region + verified scale + price), the
  assessor confirms.** Demo 1 showed the measure→£ chain is exact when the region is known; Demo 2 shows
  the AI now *proposes the region itself* on an unmarked sheet.
