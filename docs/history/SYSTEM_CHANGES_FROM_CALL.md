# How to change the system — learnings from the Fortel estimation call

Source: 59-min screen-share where Fortel's estimator (Inderjit) demoed live takeoff in Bluebeam,
with Amarvir. Verified against the transcript **and** the actual frames (f0068 scale panel, f0081
drawing selection). This rewrites how we should do **scale** and **region**, which is where we were wrong.

## The big realisation (explains the 95,463 m² error)
Fortel **do not trust the stated/title-block scale**. They set it from the title-block ratio, then
**always verify it against a known real-world feature** before measuring — *because the PDF is often
not actually at its stated scale* ("sometimes they forgot to change the scale on their template").
Our pipeline trusted a scale (title block / a dimension from the wrong viewport) with no verification →
that is exactly how we got 95,463 vs ~26,080.

## How Fortel actually estimate (ground truth)

### 1. SCALE (the method to copy)
- Read the title-block ratio and enter it in Bluebeam's Measurements panel as **1 mm = (N/1000) m**:
  1:250 → `1 mm = 0.25 m`, 1:500 → `1 mm = 0.5 m`. (Seen on f0068.)
- **Then VERIFY — mandatory.** Order of preference:
  1. **A printed dimension** on the drawing — measure it with the length tool; it must match the printed value.
  2. **A UK car-parking bay** — measure its width; it must read **2.5 m**. If not, the scale is wrong → fix it.
- A printed **scale bar** (e.g. 25 m on f0068) is also present and scales *with* the drawing — the single
  most reliable source, because it survives any PDF rescaling.
- Quote: *"Once you have scaled the drawing correctly then ... your quantity is gonna be always right."*

### 2. DRAWING SELECTION (we get this wrong)
- Use the **external-works / external-surfacing / construction-thickness / kerbing** drawing
  (`...DR-C-...`, `...DR-CE-...`, "KER"), **not** the "Proposed Site Plan". The right drawing carries the
  **legend** that distinguishes concrete from tarmac. They explicitly rejected a site plan as "not ideal".

### 3. REGION = the concrete hatch, per the legend
- Read the **legend first**. The priced area is the region under the **concrete service-yard hatch**
  (called "Type C", "service yard", "internal/external yard", "GV areas/bays" — names vary per drawing).
- Trace it with the **Area** polygon tool (press **C** to close). Set units to **m²** (units can silently revert — re-check).
- Legends/hatch codes are **not standardised** — must be read per drawing.

### 4. EXCLUSIONS / SCOPING
- **Manholes & drains: count/measure only those inside the concrete boundary**, not the whole drainage sheet.
- Transition joints (concrete↔tarmac) and slot drains within the concrete area = linear metres.
- **Dock-leveller assumption:** if no drainage layout is supplied, assume channels sit at the dock-leveller
  openings and measure the width between the dock walls.

### 5. COSTING — not covered (deferred to a later call). Our build-up stands unchanged for now.

## Concrete changes to the system (priority order)

| # | Change | Where | Status |
|---|---|---|---|
| 1 | **Scale = scale bar / known-feature, VERIFIED — never the bare title block.** Add `scale_from_bay` (2.5 m), `verify_against_feature`; scale bar primary; flag if stated vs verified disagree | `scale.py` | implementing now |
| 2 | The vision step must return a **verification feature** (parking-bay rectangle, a printed dimension, or scale-bar endpoints) alongside the polygon | `vision_llm.py` prompt | prompt updated |
| 3 | **Drawing selection** — prefer `DR-C/DR-CE/kerbing/external works/construction thickness`; down-rank "site plan" | `router.py` | note added |
| 4 | **Region = concrete hatch per legend** — prompt the model to read the legend and trace the concrete-hatch area, not the whole boundary | `vision_llm.py` prompt | prompt updated |
| 5 | **Scope manholes/drains to the concrete boundary**; implement the **dock-leveller channel assumption** | future | documented |

## The single rule that fixes scale for good
**Calibrate scale from a feature whose real size is known (scale bar = its printed metres; or a car-parking
bay = 2.5 m; or a printed dimension), and reject the title-block scale if it disagrees.** A car park bay
reading anything other than ~2.5 m, or a slab area bigger than the site, means the scale is wrong — stop and flag.
