#!/usr/bin/env python3
"""Fortel CI suite entry point. Run: `.venv/bin/python ci_tests.py` (repo root).

The checks live in tests/ , one module per area under test; this file imports them in
the order they must execute and prints the single running total. tests/__init__.py holds
the shared ck()/P ledger, so the count below is the whole suite, not a per-module tally.

Modules, in execution order:
  tests/test_geometry.py — geometry.measure_regions: voids, self-intersection, unions, missing scale
  tests/test_scale.py — scale.detect_scale_bar: plain bar and fused intermediate ticks
  tests/test_gate.py — gate.decide: which suites a given diff actually needs
  tests/test_router_refusal.py — router build-up vocabulary + takeoff_pipeline fast refusal of report PDFs
  tests/test_office_ga.py — office_candidates assisted-trace vectors + the accuracy scorecard harness
  tests/test_pricing.py — pricing.slab_rate / price_project
  tests/test_scale_guards.py — scale_consensus + sanity.plausible guards, and verified calibration
  tests/test_router_priority.py — router.drawing_priority: which sheet in a pack to measure
  tests/test_takeoff_unmarked.py — takeoff_unmarked legend-anchored colour segmentation
  tests/test_scale_for.py — scale_for: scale bar vs title block, and when to refuse
  tests/test_defaults.py — defaults.spec_with_defaults / assumption_note / flag_assumed
  tests/test_spec_extractor.py — spec_extractor text parsing + the Brief_Spec schema and provenance
  tests/test_quotation.py — quotation generation, BOQ rows and the xlsx/html/json/text exports
  tests/test_marked_zones.py — marked zone-aware measurement, BOQ allocation, Yard/Dock split, channels
  tests/test_costing_rates.py — price_with_defaults, the client rate-override layer, supplier fields
  tests/test_pipeline_states.py — sanity.measurement_state, router.rank_pages, takeoff_pipeline routing
  tests/test_takeoff_surfaces.py — D77 gold, manhole counting, refusals, hatch surfaces, dock aprons
  tests/test_marked_export.py — marked_pdf export: Bluebeam-ready markup and the recoverable manifest
  tests/test_portal_upload.py — approval_server upload/approve gate, queue, /snapshot, watchdog, jobs file
  tests/test_d77_swatch_band.py — takeoff_unmarked swatch-locked grey band vs the ancillary-concrete strip
  tests/test_portal_auth.py — approval_server soft-delete, PORTAL_TOKEN gate, rates API, login form
  tests/test_storage_learning.py — storage_paths resolver, approved-only pattern memory, learning episodes
  tests/test_portal_safeguards.py — approval quotation errors, backups, startup sweep, webhook guard, email
  tests/test_scale_rotation.py — detect_scale_bar rotation/segmented bars, proved on real Winvic sheets
  tests/test_deploy_identity.py — deploy survival, job identity, case export, upload size, BOQ misfiling
  tests/test_portal_zones.py — zone classification approve gate, /adjust, scale flags, provisional markers
  tests/test_hatch_legend.py — hatch-legend surfaces: a sheet that is only hatch plus linework
  tests/test_scale_citations.py — prose metre tokens and BS standards citations are not scales
"""
import sys
from tests import P

# Importing a module RUNS its checks. An import that raises must therefore be a FAILURE, not a
# gap: before this, one ImportError inside the portal module dropped ~150 checks and the suite
# still printed a green "==== 72x/72x PASS ====", because the total is whatever ran. CLAUDE.md
# already records this exact shape — "tests passed because the sections silently skipped".
MODULES = [
    "tests.test_geometry",
    "tests.test_scale",
    "tests.test_gate",
    "tests.test_router_refusal",
    "tests.test_office_ga",
    "tests.test_pricing",
    "tests.test_scale_guards",
    "tests.test_router_priority",
    "tests.test_takeoff_unmarked",
    "tests.test_scale_for",
    "tests.test_defaults",
    "tests.test_spec_extractor",
    "tests.test_quotation",
    "tests.test_marked_zones",
    "tests.test_costing_rates",
    "tests.test_pipeline_states",
    "tests.test_takeoff_surfaces",
    "tests.test_marked_export",
    "tests.test_portal_upload",
    "tests.test_d77_swatch_band",
    "tests.test_portal_auth",
    "tests.test_storage_learning",
    "tests.test_portal_safeguards",
    "tests.test_scale_rotation",
    "tests.test_deploy_identity",
    "tests.test_portal_zones",
    "tests.test_hatch_legend",
    "tests.test_scale_citations",
]

# The suite runs 873 checks with drawings/ present; client sheets are gitignored, so a clean
# checkout legitimately runs fewer. The floor catches a module going missing, which is a much
# bigger drop than any skip. Raise both numbers deliberately when you add checks.
EXPECTED_CHECKS = 873
MIN_CHECKS = 820

broken = []
for _name in MODULES:
    try:
        __import__(_name)
    except Exception as exc:                      # noqa: BLE001 - any failure to run is a failure
        broken.append(f"{_name}: {type(exc).__name__}: {exc}")
        print(f"  [FAIL] {_name} did not run at all — {type(exc).__name__}: {exc}")


print(f"\n==== {sum(P)}/{len(P)} PASS ====")
if broken:
    print(f"!! {len(broken)} test module(s) never ran: " + "; ".join(broken))
if len(P) < MIN_CHECKS:
    print(f"!! only {len(P)} checks ran, floor is {MIN_CHECKS} (expected {EXPECTED_CHECKS} with "
          f"drawings/ present) — a whole module is missing, not merely skipped")
elif len(P) < EXPECTED_CHECKS:
    print(f"   ({EXPECTED_CHECKS - len(P)} checks skipped — client drawings absent, expected on a "
          f"clean checkout)")
sys.exit(0 if (all(P) and not broken and len(P) >= MIN_CHECKS) else 1)
