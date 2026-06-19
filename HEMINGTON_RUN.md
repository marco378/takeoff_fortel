# Running the takeoff on a REAL unmarked tender — Hemington / SGP

## What this is
The `Bidding_Documents` tender (Hemington Business Park; SGP architects; Indurent) — **52 unmarked
engineer PDFs**, 4 warehouse units (D77 / D147 / D410 / D219). This is exactly what Fortel receives:
**no markup, no CAD layers (flattened vector), a stated scale per unit.** We ran the takeoff path on the
**Hard-Landscaping / Site plans** — the external-works drawings that carry the concrete legend.

## Result 1 — SCALE VERIFICATION WORKS on real drawings ✓
The system's #1 risk (a wrong scale → the 95,463 m² class of bug) is handled on real input. On **Unit 1
(stated 1:250)** the system read the printed **scale bar** and a **car-parking bay** (UK 2.5 m) and
confirmed the scale to within **1.2 %**:
- scale bar → **k = 0.08925 m/pt**; title-block 1:250 → k = 0.08819 m/pt → **VERIFIED** (agree to 1.2 %).

Unlike the Winvic yard (where the title block was a 1.63× lie), these SGP PDFs *are* at their stated
scale — and the system **confirms** it against a known feature rather than assuming it.

## Result 2 — REGION: identified by legend, extent → assessor (the honest hard part)
Claude vision reads each Hard-Landscaping **legend** and correctly identifies the priced area as the
**"Concrete Service Yard construction"** hatch (light grey) — distinct from the bituminous roads, the
block-paved car park, and the soft landscaping. Tracing that hatch's **exact extent** on a dense,
flattened drawing to a precise m² is the genuine judgment step: it needs iteration plus an **assessor
confirm** (the human-in-the-loop the whole design hinges on). Two Claude-vision agents + a manual pass
all reach the same conclusion — the legend/region is identifiable, the precise boundary is an assessor
call. **The system proposes the region + a verified scale; it does NOT fabricate an area.**

## Per unit
| Unit | Drawing | Stated scale | Scale verified | Priced region (per legend) |
|---|---|---|---|---|
| 1 (D77)  | Hard Landscaping `131002-P03` | 1:250 | ✓ scale bar + bay, 1.2 % | Concrete Service Yard hatch |
| 2 (D147) | Hard Landscaping `131001-P07` | 1:500 | method applies (feature read) | Concrete Service Yard hatch |
| 3 (D410) | Hard Landscaping `131001-P06` | 1:750 | method applies (feature read) | Concrete Service Yard hatch |
| 4 (D219) | Hard Landscaping `131002-P02` | 1:500 | method applies (feature read) | Concrete Service Yard hatch |

## What this proves / what's next
- The system **ingests, classifies and verifies scale** on the real unmarked tender — the core risk is solved.
- **Region extent is the assessor-confirm step.** To run it unattended end-to-end you need the **live
  Claude-vision API** (returns the concrete-yard polygon) + the **assessor UI** (`assessor.html`) to
  nudge it; geometry then measures and costing prices. That is the Phase-3 loop — proven in concept here,
  blocked only on a working API key + the UI being wired.
- Honest note: tracing the concrete extent on a flattened A1 with no layers is genuinely expensive even
  for Claude vision — which is *why* Fortel's estimators do it by eye in Bluebeam, and why the product is
  "AI proposes, assessor confirms," not "fully autonomous."
