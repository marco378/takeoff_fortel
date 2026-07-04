# Fortel AI Takeoff — session rules

AI estimation for Fortel (concrete service yards from tender PDFs): measure → cost → quote,
with an assessor portal (`approval_server.py`). Every rule below exists because a session
got it wrong once. Do not relax them to make your work look done.

## Environment — get this right first
- ALWAYS `.venv/bin/python`. System `python3` (3.14) has no deps and fails misleadingly.
- Tests: `.venv/bin/python ci_tests.py` (all must pass) and
  `.venv/bin/python robustness_tests.py` (59+ file corpus; 0 CRASH / 0 SILENT_NUMBER required).
- Portal: `APPROVAL_PORT=<port> .venv/bin/python approval_server.py`; UI QA via playwright
  (`_qa_portal_switch_test.py`, `_ui_screens.py`).
- Client drawings live in `drawings/` — **gitignored, never commit them**. Ground-truth sources
  and Drive folder IDs: see memory `fortel-ground-truth-drive`.

## Hard invariants (never break, never "temporarily" bypass)
1. **Four-state contract** — every input file ends in exactly one of MEASURED_VERIFIED /
   MEASURED_UNVERIFIED (approve blocked) / UNMEASURED (assessor trace) / REJECTED (visible
   reason). Never a crash, never a number without a verified scale or an assessor gate.
   New measurement paths must map onto these states (`sanity.measurement_state`).
2. **Gold values are facts, not knobs.** Yard 26,080 / Dock 930 / Office 3,479 / Transport 729 /
   D77 3,156 (Smita's Bluebeam). If your change moves a gold number out of tolerance, your
   change is wrong — do not edit `gold.json` to pass. Adding new gold requires a documented
   source (costing sheet, Bluebeam measurement, README-recorded exact value).
3. **Scale is never trusted, only verified** — title-block ratios lie (the 95,463 m² incident:
   scale from the wrong viewport × unguarded region = 3.7× error). Scale comes from a scale
   bar / known feature (parking bay 2.5 m / printed dimension) in the slab's OWN viewport,
   cross-checked via `scale.scale_consensus`; disagreement → refuse, don't auto-pick.
4. **Never trust an LLM's self-reported number.** Vision proposes a polygon; geometry measures
   it. Score identification by IoU, not area (an agent once matched the area 0.1% with the
   WRONG region, and confabulated "seeing" a label that had been stripped from the image).
5. **Refuse instead of guess.** If the method doesn't apply (line/hatch sheet, no scale,
   scan), emit no number and route to the assessor. A clean refusal is a success state.

## Claims require execution (the handoff-honesty rule)
- Never state a test count, area, or "it works" without having run the command in THIS
  session. A previous handoff claimed "60/60 passing" with instructions that didn't even run.
- Never write docstrings/READMEs describing capabilities that don't exist yet ("CV + OCR",
  "LLM identification") — earlier sessions shipped a demo with a HARDCODED polygon while
  claiming the LLM did it. If it's aspirational, label it TODO, not present tense.
- Treat inherited claims (handoffs, docs, memory) as unverified until re-run.

## Anything the team sees
- **Demos/screenshots: real Fortel drawings only** (`drawings/winvic/`, `drawings/tender_pack/`).
  Synthetic fixtures (`_int_d77*.pdf`, `synthetic_yard.pdf`) are for tests — showing them to
  the team got called out, hard.
- **Check the recipient can open every link before sending.** This repo is PRIVATE — raw
  GitHub links 404 for non-collaborators (Smita has no access; Aryan = AryanTavish must be
  logged in). Prefer the shared Drive folder or files posted in Slack.
- No placeholders in outbound messages (a Slack message shipped with commit hash "f9b9c8?").
  Fill in real values BEFORE sending; Slack messages can't be edited via the connector.
- Clean up after yourself in shared spaces: QA jobs out of `approval_jobs.json`, stray test
  files out of `drawings/` and the shared Drive folder. Smita has complained about test junk.
- Slack: #fortel C0BCF2Z7JR5; Aryan U0BCUEBAAFQ, Smita U08LU1EQ9GF. The connector cannot
  attach images — say where the image lives instead of pretending it's attached.

## Engineering traps already paid for (don't rediscover)
- Multi-page PDFs: never assume page 0 — rank pages (`router.rank_pages`) and record which
  page was measured; render snapshots from THAT page.
- Shared state files (`approval_jobs.json`): atomic write (`os.replace`) + fail-safe read.
  A 15s UI poll against a plain `write_text` produced "the server is unstable".
- Extracted zip/eml files: prefix with `project_ref` — every tender pack contains a
  `Proposed_Site_Plan.pdf`; unprefixed names silently overwrite across jobs.
- Dependencies: if code imports it, it goes in `requirements.txt` (cv2 was missing for weeks;
  tests "passed" because the sections silently skipped).
- No hardcoded absolute paths in tests (a stress test pointed at a dead `/sessions/...` path
  and silently skipped forever).
- Never pass large binary/base64 blobs through model context — models cannot transcribe them
  faithfully (a 129KB upload corrupted 3×). Move bytes with tools (curl, cp, APIs), never
  by "copying" them.
- UI changes: drive the real portal (playwright) before shipping. The blank-screen bug lived
  only in the browser; unit tests never saw it.

## Working conventions
- Commit directly to main after CI is green; push without asking (Jas's standing instruction).
  Descriptive commit messages listing evidence (test counts, gold results).
- Conserve context: delegate bulk reading/execution to Sonnet subagents; keep synthesis here.
- When Aryan reports a bug: reproduce it first, fix the root cause, prove it with a test AND
  a screenshot from a live run, then reply in #fortel with evidence he can open.
