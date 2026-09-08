#!/usr/bin/env python3
"""Shared harness for the Fortel CI suite.

Split out of the 7,759-line ci_tests.py with no behaviour change: this module is the
former prologue, verbatim and in its original order (the project imports still run
before the LEARNING_ENVIRONMENT/TRAINING_LOG_FILE defaults, as they always did).
It owns the single pass/fail ledger `P` and the `ck()` recorder, so every test module
appends to one list and ci_tests.py can still print one total.

Run the suite with `.venv/bin/python ci_tests.py` from the repo root.
"""
import os, sys, shutil
from pathlib import Path
from reportlab.pdfgen import canvas
from geometry import measure_regions
from scale import detect_scale_bar
from pricing import slab_rate, price_project

# Every training derivative produced by CI is explicitly labelled and isolated from the
# repository/live volume, so cleanup never has to infer test pollution from client-like names.
os.environ.setdefault("LEARNING_ENVIRONMENT", "test")
os.environ.setdefault("TRAINING_LOG_FILE", f"/tmp/fortel_ci_training_{os.getpid()}.jsonl")
os.environ.setdefault("LEARNED_PATTERNS_FILE", f"/tmp/fortel_ci_patterns_{os.getpid()}.json")

P = []
def ck(n, c, g=""):
    P.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n} {g}")

class _FixtureNotPresent(Exception):
    pass

def _require_fixture(path, reason):
    if not Path(path).exists():
        raise _FixtureNotPresent(reason)


# Absolute repo root. ci_tests.py used to read sibling files via Path(__file__).parent;
# from inside tests/ that would resolve to tests/, so the anchor is explicit now.
REPO_ROOT = Path(__file__).resolve().parent.parent
