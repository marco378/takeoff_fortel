# Fortel takeoff — call-derived edge-case register

Audit date: 2026-08-08. Sources reviewed newest to oldest: Fortel transcripts dated
2026-08-07, 2026-08-06, 2026-08-05, 2026-08-04, 2026-08-03, 2026-07-31,
2026-07-30 and 2026-07-29. The 5 August transcript is treated as the primary description
of Inderjit's live CADIC markup; code and summaries never override it.

Status meanings:

- **IMPLEMENTED** — the current code has an evidence-gated path and a regression test.
- **PARTIAL** — a safe assisted/manual path exists, but automatic detection or a downstream
  hand-off is incomplete.
- **MISSING** — client behaviour is known, but no honest implementation exists yet.
- **DEFERRED-with-reason** — deliberately not built because evidence/client input is missing.
- **NOT-A-CODE-RULE** — delivery target, operational discussion or human process, retained for
  traceability rather than presented as software behaviour.

Audited register count: **51 entries** — **25 IMPLEMENTED**, **17 PARTIAL**, **5 MISSING**,
**1 DEFERRED-with-reason**, **3 NOT-A-CODE-RULE**.

## Visual cross-checks

| Recording | Frames inspected | What the screen actually establishes |
|---|---|---|
| 2026-07-30, Tenro/Leumonics | 12:00, 13:00, 13:30, 14:00, 14:30, 15:00 | At 13:00 Bluebeam shows a 5.000 m measurement over two parking bays and `1 mm = 0.5 m`; at 14:30 the legend visibly says External Service Yard `190mm (175mm Unit 1)`, C32/40 air-entrained concrete and A252 fabric. The image supports per-spec separation and independent dimension checks; it does not prove a generic colour constant. |
| 2026-08-05, CADIC | 09:20–11:50 | The supplied clip ends while Inderjit is selecting the 189-file CADIC/Inova pack (`Site 1 Masterplan-P03` visible at 11:50). It does not contain the later boundary clicks described by the transcript, so no geometric claim below is attributed to unseen video. |

## Measurement, quotation and workflow rules

| Date / source | Rule, defect or promise | Status | Current evidence | Remaining gap / safety decision |
|---|---|---|---|---|
| 5 Aug | Prefer an engineer drawing; use the architect plan only when no engineer drawing exists. | PARTIAL | Page/drawing ranking exists in `router.py`; architect-origin results are explicitly provisional in `quotation.py:314-318`. | Ranking cannot prove that the full tender pack contains no better engineer sheet. Assessor remains responsible for sheet selection. |
| 5 Aug | External/service Yard follows the explicit grey/yellow concrete hatch boundary. | IMPLEMENTED | Legend-anchored surface segmentation and component evidence are in `takeoff_unmarked.py:147-221` and `takeoff_unmarked.py:1518-2089`; ambiguous co-components stay visible. | No colour is accepted merely because it is grey/yellow; legend/body agreement and assessor gates remain mandatory. |
| 5 Aug | Verify scale against a known printed dimension (the demonstrated Yard width was 40 m). | PARTIAL | Bar/title consensus is in `takeoff_unmarked.py:1415-1515`; the portal has an assessor two-point calibration control in `assessor_portal.html:328` and its `/calibrate` flow. | Automatic arbitrary dimension-line association is not reliable and is not claimed. |
| 5 Aug | A transition is each tarmac-to-concrete Yard entrance, measured in Lm per unit. | PARTIAL | Marked `Transition` line annotations retain per-unit Lm in `robust_takeoff.py:72`, `robust_takeoff.py:470-474`, and quote as an aggregated blank-rate quantity in `quotation.py:400-433`. | Raw black entrance linework cannot yet be distinguished reliably from walls/grid lines, so raw sheets do not emit transition Lm. |
| 5 Aug | Units with dock levellers have a dock-level channel and a full-yard-width channel; no-dock units have only full width. | IMPLEMENTED | Evidence-gated proposals and no-dock branching are in `takeoff_unmarked.py:900-1005`; accepted proposals quote provisionally with blank rates in `quotation.py:435-488`. | Proposals remain assumptions outside measured zones until assessor action. |
| 5 Aug | Channel interrupted by an access road is an assumption and must be declared. | IMPLEMENTED | Proposal basis/confidence and portal assumption treatment are in `takeoff_unmarked.py:900-1005` and `assessor_portal.html:1490-1515`. | No automatic access-road semantic detector is claimed. The assessor edits/removes the proposal. |
| 5 Aug | Exclude Gatehouse and Hub office from external Yard. | PARTIAL | Explicit labels and persistent assessor checklist prompts are in `measurement_rules.py:11-25,112-143`; marked exclusion annotations are removed from totals in `robust_takeoff.py:433-530`. | Their footprints are not deterministically detected from raw linework. Unresolved text produces a visible prompt, never an invented subtraction. |
| 5 Aug | Exclude lift shafts/lift pits from office slabs. | PARTIAL | Strict shaft/pit/`lift bit` classification and prompts are in `measurement_rules.py:27-38,112-143`. | No reliable raw outline detector. A lift lobby is deliberately not classified as a void. |
| 5 Aug | Exclude service/data risers. | PARTIAL | Both word orders and ASR `raiser` are handled in `measurement_rules.py:39-47`; prompt/marked exclusion flow is shared with lift exclusions. | No reliable raw outline detector. |
| 5 Aug | A pit beside the lift may be a precast-stair foundation; exclude it from slab and price separately only if asked (detail 300/345/600 mm). | PARTIAL | Explicit stair-foundation semantics are recognised in `measurement_rules.py:49-66`; a bare `Pit` is refused. | The dimensions alone cannot establish identity. No price or automatic footprint is created. |
| 5 Aug | Ground slabs of differing thickness/mesh must be separate BOQ lines (e.g. Unit 5 150 mm vs other 190 mm). | IMPLEMENTED | Specification/provenance is part of the grouping key at `quotation.py:186-191,287-309`; regression coverage is in `ci_tests.py:607-636`. | The 5-Aug ASR phrase `193 ground slab` is unresolved; see Open questions. |
| 5 Aug | Units 4A–4D are one combined slab. | IMPLEMENTED | A complete explicit 4A–4D marked-subject set is combined in `robust_takeoff.py:407-426,576-595`; incomplete sets remain separate and flagged. | Raw office GA plans still require assisted trace. |
| 5 Aug | Count real steel columns for box-outs/isolation; do not count visually similar channel-section symbols. | MISSING | No column-box-out extractor exists. Existing `annotation_count` is polygon provenance, not a steel-column count. | No labelled Count-annotation fixture or deterministic symbol discriminator was found in the corpus. A number would be a guess. |
| 5 Aug | Green CJ line is internal office construction joint; emit Lm only, blank rate; detail is 150×150×8 square dowel plates at 600 centres, sleeve one side, mid-depth, 60 mm embedment. | IMPLEMENTED | Marked subjects and raw legend/vector evidence are handled in `robust_takeoff.py:72-79,566-576` and `takeoff_unmarked.py:568-704`; quotation quantity is blank-rate in `quotation.py:400-433`. | Roller-shutter/warehouse details are intentionally excluded. |
| 5 Aug | Upper-floor boundary is the edge of metal decking. | PARTIAL | Marked zones carry the boundary rule in `robust_takeoff.py:564-567`; office assisted candidates show the instruction in `office_candidates.py:329-346`. | The estimator does not automatically identify which of parallel CAD loops is the decking edge; assessor tracing remains authoritative. |
| 5 Aug | Plant deck and Unit-5 POD first floor stay separate from the main upper floor. | IMPLEMENTED | Explicit marked subjects map to distinct BOQ scopes in `robust_takeoff.py:98-116`; scope is part of quotation grouping in `quotation.py:287-309,565-580`. | Requires explicit subject/assessor category; unknown regions stay unclassified. |
| 5 Aug | Internal warehouse slabs belong to Neil's separate quotation and are outside this tool's scope. | IMPLEMENTED | Explicit warehouse subjects become recorded scope exclusions in `robust_takeoff.py:173-186,433-530`; tested in `ci_tests.py:925-943`. | A generic unlabelled polygon cannot be excluded on this basis. |
| 5 Aug | 1:1500 and 1:2000 sheets need a visible boundary-click precision warning. | IMPLEMENTED | Risk flag is display-only at `takeoff_unmarked.py:1369-1388`; it caps confidence without changing area/tolerance. | None; assessor must zoom and confirm. |
| 6 Aug | Cut-outs and channel drawing/editing must be available during assessor review. | IMPLEMENTED | Cut-out/channel canvas, endpoint/vertex edits, deletion and submission are in `assessor_portal.html:2384-2982`; server persistence is handled by `/adjust`. | Visual browser regression remains desirable for every deploy; core DOM/API tests exist. |
| 6 Aug | Manual cut-out is a backup; the desired AI should identify real voids/exclusions itself. | PARTIAL | Marked polygon holes are net and raw office jobs expose exclusion checklists/candidate traces. | No raw semantic outline detector can yet distinguish lift/riser/stair features safely. The portal backup does not make that automatic capability claim. |
| 6 Aug | Add a Count tool for repetitive column/pillar elements. | MISSING | The portal has trace/cut-out/channel controls, but not an evidence-backed steel-column Count quantity path. | Same real-fixture requirement as the column-box-out rule; do not treat arbitrary clicks as priced counts without category/provenance. |
| 6 Aug | Every drawing is human-approved before quotation. | IMPLEMENTED | Approval gates and case quotation creation are in `approval_server.py:444-535,1843-2125`; zoned-unverified end-to-end coverage exists in `ci_tests.py:2550-2655`. | Email delivery is environment/SMTP dependent and records visible failure rather than inventing a send. |
| 6 Aug | Approval generates downloadable text/HTML/JSON/XLSX artifacts and keeps them on shared persistent storage. | IMPLEMENTED | Formats are generated through `quotation.py`; volume-aware paths and case-download routes are in `approval_server.py`. | An external Fortel shared-drive sync still depends on deployment/infrastructure credentials. |
| 6 Aug | Reach 95% across ten client jobs by 14 Aug. | NOT-A-CODE-RULE | `accuracy_report.py` produces an objective scorecard and counts NOT MEASURED as a miss. | Acceptance is a delivery target; it cannot be declared by code or by the existing eight Castle fixtures. |
| 7 Aug | `A0` is sheet size, never scale. | IMPLEMENTED | `takeoff_unmarked.py:1399-1413` only accepts explicit plausible `1:N`; `ci_tests.py` now proves `A0 / AS INDICATED / NTS` yields no numeric scale. | None. |
| 7 Aug | Scales printed beside detail/layout viewports apply only locally; multiple layouts may have different scales. | IMPLEMENTED | `takeoff_unmarked.py:1455-1478` now prevents page-global VERIFIED state whenever multiple denominators exist without spatial bar/title/region association. | Automatic viewport association is TODO; assessor calibration is required instead. |
| 7 Aug | `NTS` requires calibration from a known dimension. | PARTIAL | NTS cannot become a numeric scale; manual two-point calibration exists in the portal. | Automatic dimension selection is deliberately absent because unrelated viewports/dimensions are common. |
| 7 Aug | Quote-area rows are rounded to whole m² (examples 2911.99→2912 and 12026.96→12027). | PARTIAL | XLSX keeps editable numeric cells and a live formula (`quotation.py:1072-1105`), but currently displays up to two decimals. | Whether rounded quantity must also drive the price (rather than display only) changes commercial totals; client confirmation is required before altering it. |
| 7 Aug | If manholes are not shown, assume roughly one per 1,000 m² and mark provisional. | IMPLEMENTED | Separate assumed provenance is generated at `takeoff_unmarked.py:2054-2059` and quoted visibly at `quotation.py:355-372`. | Python `round` reflects the previously accepted behaviour; exact floor/ceiling policy is not reinterpreted from the call's “30 or 31” example. |
| 7 Aug | Aggregate each project's channel and transition source quantities into quotation rows. | IMPLEMENTED | Per-unit source rows aggregate by `(section, description, unit)` in `quotation.py:398-433`; rates remain blank. | Raw transition detection remains partial as above. |
| 7 Aug | Architect-only slab build-up must say provisional/no details, not masquerade as engineer-confirmed. | IMPLEMENTED | Field-level provenance comes from `slab_spec.py`; all outputs use `PROVISIONAL — NO DETAILS PROVIDED` (`quotation.py:64,313-393,625-641`). | The 190/193 question remains open; no value was changed here. |
| 7 Aug | Perimeter shown in mm must be divided by 1,000 to Lm and totalled. | IMPLEMENTED | Polygon geometry uses PDF points × verified `scale_k` in `geometry.py:20-43`; per-zone/top-level perimeters aggregate in `quotation.py:340-433`. | A raw office candidate is not a measured perimeter until assessor acceptance. |
| 7 Aug | Put column-box-out counts into the ground-floor BOQ. | MISSING | Same underlying gap as the 5-Aug column rule. | Requires a real marked count fixture and explicit symbol evidence; do not reuse manhole circles or polygon annotation counts. |
| 7 Aug | Copy Fortel's exact standard ground-floor exclusions into every quote. | MISSING | `quotation.py:47-53` has generic terms and dynamic measured exclusion declarations, but not the exact client template block shown on screen. | The screen text is not legible in the supplied recording/transcript. Import the actual template wording rather than inventing it. |
| 7 Aug | Use latest client design and revise quantities when drawings change. | PARTIAL | Revision/header fields exist in `quotation.py:702-708`; assessor can re-upload/review. | There is no drawing-revision diff or superseded-document resolver. |
| 7 Aug | Header carries project/reference/client/date/revision/location/client code; upper floors list per-unit source rows. | PARTIAL | Project/ref/client/date/revision and per-unit area rows exist in `quotation.py:682-708,959-1105`. | Location and client code are not structured capture fields. Do not infer them from filenames. |
| 7 Aug | Concrete-pump calculation remains to be finalised. | DEFERRED-with-reason | No new pump calculation was added. | Inderjit explicitly deferred it; implementing a commercial calculation without the rule would violate the rate/calculation denylist. |
| 30 Jul | Parking bay gives an independent 2.5 m scale check. | IMPLEMENTED | `scale.py:248-289` has the 2.5 m feature calibration/check; visual frame 13:00 shows a 5.000 m two-bay check. | The automatic path does not guess which CAD lines form a bay. |
| 30 Jul | External Yard spec is 190 mm, except Unit 1 175 mm, C32/40 AE and A252; differing specs stay separate. | PARTIAL | The video visibly confirms the values; schema capture and spec-keyed grouping exist (`slab_spec.py`, `quotation.py:186-191`). | Automatic association of a legend note to the correct zone/unit is not proven. Assessor capture is required. |
| 30 Jul | Include concrete footpaths; exclude block paving. | MISSING | Yard swatch locking prevents unlike surfaces being silently annexed, but there is no generic footpath/block-paving classifier. | Needs a labelled real markup/BOQ quantity; do not infer scope from hue. |
| 30 Jul | Missing drainage information is an assumption and later design changes require revision. | PARTIAL | Manhole fallback and channel proposals are explicit assumptions; pending proposals are declared, not priced (`quotation.py:435-488`). | Raw transitions and design-revision comparison remain missing. |
| 30 Jul | Office is the dark/office region, not the warehouse; prefer engineer drawings. | PARTIAL | Explicit internal-warehouse subjects are excluded; office raw plans produce assisted candidates rather than an auto-number (`office_candidates.py`). | Colour/darkness alone is not a safe semantic classifier. |
| 30 Jul | Split adjacent upper-floor layouts exactly at grid lines and avoid overlaps/gaps. | PARTIAL | Assisted office candidates are de-duplicated and expose unresolved levels (`office_candidates.py:233-450`). | No trustworthy semantic grid-line splitter; assessor edits each region. |
| 31 Jul | Assumed channels must be straight, non-diagonal and retaining-wall adjacent; assessor can drag/trim/edit. | IMPLEMENTED | Axis/wall-contained geometry is in `takeoff_unmarked.py:746-1005`; portal endpoint and numeric edits are in `assessor_portal.html:1490-1584,2687-2820`. | If no valid run is found, that component refuses rather than falling back to a diagonal. |
| 31 Jul | Assumed/proposed/provisional values must be visually distinct from measured values. | IMPLEMENTED | One assumption badge system is used across zones, candidates, channels, specs and costing in `assessor_portal.html:1211-1230,1286-1316,1458-1515,1620-1888`. | None. |
| 31 Jul | Unit-labelled markup subjects use corroborating drawing context; unknown categories preserve area and require classification. | IMPLEMENTED | Evidence hierarchy and `unclassified` fallback are in `robust_takeoff.py:118-286,433-595`; portal classification control is present. | Context never overrides a meaningful conflicting subject. |
| 31 Jul | Assessor edits to region/spec/rates flow into the generated spreadsheet. | IMPLEMENTED | Per-region categories flow through `/adjust`; spec capture and client-rate override provenance are exposed in the portal and quotation. | Edits affect newly generated pricing only; prior issued quotations remain immutable snapshots. |
| 31 Jul | Ten drawings will be judged as nine correct out of ten. | NOT-A-CODE-RULE | The CLI reports `N of M within tolerance`; no test can certify unseen client drawings. | The ten-drawing reference set has not arrived in this workspace. |
| 29 Jul | Current accuracy was described as 75–80%; review needs a reproducible per-drawing measure. | IMPLEMENTED | `accuracy_report.py` pairs raw/marked files, strips answer annotations, reports four-state and strict per-zone outcomes. | The current measured Castle score is recorded below; it does not equal the verbal estimate. |
| 3–4 Aug | Process multiple drawings together and preserve assessor workflow/state. | IMPLEMENTED | Multi-file upload/project grouping and atomic case persistence are covered in `approval_server.py` and portal tests. | Tender-pack files over infrastructure limits are an operations issue, not a claimed in-process PDF capability. |
| 6–7 Aug | Tender packs over roughly 1 GB need a separate transfer/storage workflow. | NOT-A-CODE-RULE | The portal persists accepted uploads on the configured Railway volume; it does not claim to ingest an arbitrary 10–20 GB folder. | Requires infrastructure/security design outside measurement logic. |

## Open client questions

1. **CADIC Unit 1 ground-core thickness:** the 5-Aug transcript ASR says “193 ground slab,
   1 number layer, 252 mesh in bottom”, while later in the same call it describes other units
   as 190 mm and Unit 5 as 150 mm; the 7-Aug call uses 190 mm only as a provisional external
   Yard standard. The 30-Jul video shows a different project's *external Yard* as 190 mm
   (175 mm Unit 1). These are different slab categories/projects and do not prove CADIC Unit 1.
   Confirm the engineer note or source PDF; no value is selected by this audit.
2. **Quotation rounding basis:** confirm whether whole-m² rounding is presentation only or the
   contractual quantity that drives row values.
3. **Exact standard exclusions:** provide the editable Fortel template text or a legible source;
   generic wording must not be substituted.
4. **Column box-outs:** provide an Inderjit-marked drawing whose Count annotations retain the
   steel-column vs channel-symbol distinction and its expected BOQ count.

## Accuracy today

### Castle Donington raw/stripped scorecard (`--tol 5`)

The raw external path measures Yard and Dock areas but intentionally does not call channel or
transition proposals “measured” truth. Raw office GA sheets refuse auto-area and offer assisted
trace; a refusal counts as a client miss in the scorecard.

| Drawing | Truth m² | Before | After | Delta | State | Aggregate | Strict zone contract |
|---|---:|---:|---:|---:|---|---|---|
| External Markup Unit-1 | 3,185.8 | 3,244.4 | 3,244.4 | +1.84% | MEASURED_UNVERIFIED | PASS | FAIL — channel + transition absent |
| External Markup Unit-2 | 6,769.3 | 6,759.6 | 6,759.6 | -0.14% | MEASURED_UNVERIFIED | PASS | FAIL — channel + transition absent |
| External Markup Unit-3 | 17,139.3 | 17,081.4 | 17,081.4 | -0.34% | MEASURED_UNVERIFIED | PASS | FAIL — channel + transition absent |
| External Markup Unit-4 | 7,624.6 | 7,674.5 | 7,674.5 | +0.65% | MEASURED_UNVERIFIED | PASS | FAIL — channel + transition absent |
| Office Floors Unit-1 | 760.4 | — | — | — | UNMEASURED | NOT MEASURED | FAIL — ground + upper absent |
| Office Floors Unit-2 | 1,103.1 | — | — | — | UNMEASURED | NOT MEASURED | FAIL — ground + upper absent |
| Office Floors Unit-3 | 1,113.2 | — | — | — | UNMEASURED | NOT MEASURED | FAIL — ground + upper absent |
| Office Floors Unit-4 | 904.1 | — | — | — | UNMEASURED | NOT MEASURED | FAIL — ground + upper absent |

Result: **4 of 8 aggregate totals within 5% (50.0%); 0 of 8 strict zone contracts (0.0%)**.
The Phase-4 fixes in this audit change classification/prompt and scale-verification safety, not
the measured geometry, so these numeric before/after values are intentionally identical.

| Drawing / zone | Truth | Raw measured | Delta / outcome |
|---|---:|---:|---|
| External U1 / Yard | 3,080.26 m² | 3,138.00 m² | +1.87% · PASS |
| External U1 / Dock | 105.57 m² | 106.40 m² | +0.79% · PASS |
| External U1 / Channel | 96.71 Lm | — | NOT MEASURED |
| External U1 / Transition | 8.75 Lm | — | NOT MEASURED |
| External U2 / Yard | 6,378.03 m² | 6,357.00 m² | -0.33% · PASS |
| External U2 / Dock | 391.30 m² | 402.60 m² | +2.89% · PASS |
| External U2 / Channel | 235.72 Lm | — | NOT MEASURED |
| External U2 / Transition | 14.08 Lm | — | NOT MEASURED |
| External U3 / Yard | 16,115.03 m² | 16,074.00 m² | -0.25% · PASS |
| External U3 / Dock | 1,024.26 m² | 1,007.40 m² | -1.65% · PASS |
| External U3 / Channel | 545.36 Lm | — | NOT MEASURED |
| External U3 / Transition | 18.19 Lm | — | NOT MEASURED |
| External U4 / Yard | 7,222.08 m² | 7,270.00 m² | +0.66% · PASS |
| External U4 / Dock | 402.57 m² | 404.50 m² | +0.48% · PASS |
| External U4 / Channel | 242.91 Lm | — | NOT MEASURED |
| External U4 / Transition | 10.71 Lm | — | NOT MEASURED |
| Office U1 / Ground + Upper | 104.65 + 655.72 m² | — | NOT MEASURED |
| Office U2 / Ground + Upper | 104.62 + 998.50 m² | — | NOT MEASURED |
| Office U3 / Ground + Upper | 115.52 + 997.69 m² | — | NOT MEASURED |
| Office U4 / Ground + Upper | 111.68 + 792.45 m² | — | NOT MEASURED |

### Gold corpus

The required 178-file run produced 17/17 aggregate gold passes, 8/8 zone-gold passes and
1/1 manhole-gold pass. Percentages below are calculated from the reported actual value without
rounding a non-zero difference to `0%`.

| Gold file | Expected | Actual | Error / result |
|---|---:|---:|---|
| synthetic_yard | 25,920.0 | 25,920.0 | +0.0000% · GOLD_PASS |
| Winvic Yard | 26,080.0 | 26,080.2 | +0.0008% · GOLD_PASS |
| Winvic Dock | 930.0 | 929.8 | -0.0215% · GOLD_PASS |
| Winvic Office | 3,479.0 | 3,478.6 | -0.0115% · GOLD_PASS |
| Winvic Transport | 729.0 | 728.6 | -0.0549% · GOLD_PASS |
| _int_d77 | 3,156.0 | 3,159.0 | +0.0951% · GOLD_PASS |
| _int_d77_borders | 3,159.0 | 3,163.0 | +0.1266% · GOLD_PASS |
| _int_d77_footpath | 3,159.0 | 3,159.0 | +0.0000% · GOLD_PASS |
| real_sgp D77 | 3,156.0 | 3,138.0 | -0.5703% · GOLD_PASS |
| Castle External U1 | 3,185.0 | 3,185.8 | +0.0251% · GOLD_PASS |
| Castle External U2 | 6,762.0 | 6,769.3 | +0.1080% · GOLD_PASS |
| Castle External U3 | 17,139.0 | 17,139.3 | +0.0018% · GOLD_PASS |
| Castle External U4 | 7,624.0 | 7,624.6 | +0.0079% · GOLD_PASS |
| Castle Office U1 | 761.0 | 760.4 | -0.0788% · GOLD_PASS |
| Castle Office U2 | 1,102.0 | 1,103.1 | +0.0998% · GOLD_PASS |
| Castle Office U3 | 1,111.0 | 1,113.2 | +0.1980% · GOLD_PASS |
| Castle Office U4 | 904.0 | 904.1 | +0.0111% · GOLD_PASS |

### Other gold-bearing directories

`drawings/winvic`, `drawings/real_sgp` and root gold fixtures are exercised by
`robustness_tests.py`. The pairing scorecard is also run where marked-vs-raw pairing is possible;
a directory with no truth annotations/pair is reported as such rather than assigned a score.

| Winvic marked truth stripped to raw | Truth m² | Raw measured | Outcome |
|---|---:|---:|---|
| Area_Hub_Office_Transport | 728.6 | — | UNMEASURED — line/hatch refusal |
| Area_Office_Floors_GA | 3,478.6 | — | UNMEASURED — line/hatch refusal |
| Dock_Slab_Area_Proposed_Site_Plan | 929.8 | — | UNMEASURED — line/hatch refusal |
| Yard_Area_Proposed_Site_Plan | 26,080.2 | — | UNMEASURED — line/hatch refusal |

Winvic score: **0 of 4 aggregate totals within 5% (0.0%)**. This is an honest raw-path
capability gap, not a robustness failure: the marked files still pass their read-back golds.
`drawings/real_sgp` produced **0 pairs** and the warning `raw PDF has no matching marked truth`;
its 3,156 m² gold is therefore evaluated only by the robustness harness (actual 3,138.0 m²).

## Deliberate non-fixes

- No raw office area was promoted: all four stripped Castle office sheets still fail the strict
  automatic-measurement bar, so assisted trace remains the honest path.
- No raw transition/CJ/channel line is inferred from anonymous black strokes.
- No Gatehouse, Hub office, lift/riser or stair-foundation area is subtracted without an explicit
  annotation outline or assessor trace.
- No column count, footpath quantity, commercial rounding rule, standard exclusion wording,
  concrete-pump calculation, rate or specification value was invented.
- No existing gold/tolerance was changed.
