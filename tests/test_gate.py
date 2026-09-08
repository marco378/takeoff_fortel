#!/usr/bin/env python3
"""gate.decide: which suites a given diff actually needs.

Sections in this module (printed in this order):
  - the gate picks the checks a change actually needs

Imported by ci_tests.py, which owns the running total. Do not reorder.
"""
from tests import ck

print("the gate picks the checks a change actually needs")
# CLAUDE.md wants both suites green before a commit. Run literally that made a button label wait
# on a 617-file sweep, which on 4 Sep cost most of an afternoon. The corpus is parallel now and
# gate.py picks from the diff — conservatively: anything unrecognised still pulls the corpus.
import gate as _gate
for _paths, _must, _mustnt, _why in (
    (["assessor_portal.html"], {"ci", "portal"}, {"corpus"}, "a portal-only change"),
    (["quotation.py"], {"ci", "portal"}, {"corpus"}, "a quotation-only change"),
    (["takeoff_unmarked.py"], {"ci", "corpus"}, set(), "measurement code"),
    (["router.py", "assessor_portal.html"], {"ci", "corpus", "portal"}, set(), "both at once"),
    (["gold.json"], {"ci", "corpus"}, set(), "a gold change"),
    (["README.md", "ROBUSTNESS_REPORT.md"], {"ci"}, {"corpus", "portal"}, "documentation"),
    (["some_new_module.py"], {"ci", "corpus"}, set(), "an unrecognised module (fail safe)"),
    (["gate.py"], {"ci"}, {"corpus"}, "the gate script itself"),
):
    _gates, _ = _gate.decide(_paths)
    ck(f"gate: {_why} -> {sorted(_must)}",
       _must <= _gates and not (_mustnt & _gates), f"{_paths} -> {sorted(_gates)}")

