# Diagnosis: the 95,463 m² yard incident — and the permanent fix

## Reported
A live run on the **unmarked** yard returned **95,463 m²**. Correct ≈ **26,080 m²** (the Bluebeam
markup). Error ≈ **3.7×**.

## Root cause — TWO compounding errors (proven on the real drawing)

**1. Scale from the wrong viewport — the bigger error (~2.67×).**
The sheet is **mixed-scale**. The slab is drawn at **1:306** (k=0.108 m/pt) → 26,080 m². The flow keyed
scale off the **257.2 m dimension, which lives on the 1:500 part of the sheet** (k=0.176 m/pt, a
*different viewport*). Measuring the slab with that scale alone = **69,562 m²**.
The flow saw four dimensions implying wildly different scales (0.36–0.67 m/unit, **1.84× spread**) and
**auto-picked one** — the classic mixed-scale trap.

**2. Region too big (~1.37×).**
On an unmarked drawing Claude doesn't know the exact *priced* extent, so the traced polygon was ~1.37×
the true slab.  →  2.67 × 1.37 ≈ **3.7×** = 95,463.

**3. No guard caught it.** 95,463 m² is **bigger than the entire site** — physically impossible — yet it
shipped with no flag.

> So it is NOT just "Claude picked the wrong region." ~2/3 of the error is **scale**, ~1/3 is region.

## Why it happened
The live flow did **not** use the repo's hardened handling. It scaled from an arbitrary dimension label,
with no consistency check, no plausibility check, and no assessor confirmation.

## The permanent fix (in the repo now)
- **`scale.scale_consensus(refs)`** — if scale references disagree (mixed-scale), returns **None + flag**
  and refuses to auto-pick. On the dev's four dimensions it flags *"MIXED-SCALE — do not auto-pick."*
- **`sanity.plausible(area, site_m2)`** — blocks/flags an impossible area (bigger than the site, or above
  a single-zone bound). It flags 95,463 as **IMPOSSIBLE**. Wired into `takeoff_pipeline`.
- **Scale must come from the slab's OWN viewport** — a scale bar in that viewport, or a dimension
  confirmed on the same CAD layer/viewport, or the assessor. Never a dimension from elsewhere.
- **Region** — assessor confirms extent (`assessor.html`); score by **IoU**, not area.

## Why it can't recur
The two guards catch the incident **independently** — once at the scale step (refuse to auto-pick on a
mixed-scale sheet) and once at the output step (impossible magnitude). Route the live flow through
`takeoff_pipeline` and use `scale_consensus` instead of picking a dimension.
