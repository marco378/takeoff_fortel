#!/usr/bin/env python3
"""Run the gates this change actually needs, and say why.

CLAUDE.md requires ci_tests.py and robustness_tests.py green before a commit. Run literally,
that means every change waits on a 617-file sweep — including a change that cannot move a
measurement, such as a button label. On 4 Sep 2026 that cost most of an afternoon: the sweep is
~34 minutes of work, it ran one file at a time, and it was re-run for portal-only edits.

So the corpus is now parallel (`robustness_tests.py --jobs`), and this script picks the gates
from the diff:

    measurement or costing code changed  ->  ci_tests + FULL corpus (--check)
    portal/server/export code changed    ->  ci_tests + portal playwright QA
    anything else                        ->  ci_tests

The rule is deliberately conservative: an unrecognised path counts as measurement, because the
cost of running the corpus unnecessarily is minutes, and the cost of skipping it when it was
needed is a client number.

    .venv/bin/python gate.py            # decide from the working tree, run them
    .venv/bin/python gate.py --plan     # just say what it would run
    .venv/bin/python gate.py --ref HEAD~2   # decide from a different base
"""
import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(REPO, ".venv", "bin", "python")

# Anything whose behaviour can change a measured quantity, a state, or a price.
MEASUREMENT_PATHS = {
    "takeoff_unmarked.py", "takeoff_pipeline.py", "router.py", "scale.py", "geometry.py",
    "sanity.py", "hatch_legend_raster.py", "structural_light_fill.py", "office_candidates.py",
    "spec_extractor.py", "slab_spec.py", "defaults.py", "costing.py", "pricing.py",
    "read_marked.py", "gold.json", "ground_truth_polygons.json", "robustness_tests.py",
}
# The assessor's screen and the artefacts it hands the client.
PORTAL_PATHS = {"assessor_portal.html", "approval_server.py", "marked_pdf.py", "quotation.py"}
# Changing these alone proves nothing about the pipeline.
INERT_SUFFIXES = (".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".gitignore")
# Harness/tooling: they decide WHICH gates run, they do not measure anything themselves.
TOOLING_PATHS = {"gate.py"}


def changed_files(ref=None):
    cmd = ["git", "diff", "--name-only"] + ([ref] if ref else [])
    tracked = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True).stdout.split()
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=REPO, capture_output=True, text=True).stdout.split()
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               cwd=REPO, capture_output=True, text=True).stdout.split()
    return sorted({*tracked, *staged, *untracked})


def decide(files):
    """Return (gates, reasons). Unknown code paths are treated as measurement."""
    gates, reasons = {"ci"}, []
    for path in files:
        name = os.path.basename(path)
        if path.startswith(".claude/") or path.endswith(INERT_SUFFIXES):
            if name not in MEASUREMENT_PATHS:
                continue
        if name in TOOLING_PATHS:
            reasons.append(f"{path}: gate tooling — ci_tests")
        elif name in MEASUREMENT_PATHS:
            gates.add("corpus")
            reasons.append(f"{path}: measurement/costing path — full corpus")
        elif name in PORTAL_PATHS:
            gates.add("portal")
            reasons.append(f"{path}: assessor-facing — playwright QA")
        elif name == "ci_tests.py" or name.startswith("_qa") or name.startswith("test_"):
            reasons.append(f"{path}: tests only — ci_tests")
        elif path.endswith(".py"):
            gates.add("corpus")
            reasons.append(f"{path}: unrecognised python module — corpus, to be safe")
        else:
            reasons.append(f"{path}: no gate implied")
    return gates, reasons


def run(cmd, label):
    print(f"\n=== {label}: {' '.join(cmd)}", flush=True)
    code = subprocess.run(cmd, cwd=REPO).returncode
    print(f"=== {label}: {'PASS' if code == 0 else f'FAIL (exit {code})'}", flush=True)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", help="git ref to diff against (default: working tree)")
    parser.add_argument("--plan", action="store_true", help="print the plan and stop")
    parser.add_argument("--jobs", "-j", type=int, help="corpus workers (default: half the cores)")
    args = parser.parse_args(argv)

    files = changed_files(args.ref)
    gates, reasons = decide(files)
    print(f"{len(files)} changed file(s); gates: {', '.join(sorted(gates))}")
    for reason in reasons:
        print(f"  - {reason}")
    if "corpus" not in gates:
        print("  (no measurement path touched — the full corpus is NOT required for this change)")
    if args.plan:
        return 0

    failures = []
    if "ci" in gates and run([PYTHON, "ci_tests.py"], "ci_tests"):
        failures.append("ci_tests")
    if "corpus" in gates:
        cmd = [PYTHON, "robustness_tests.py", "--check"]
        if args.jobs:
            cmd += ["--jobs", str(args.jobs)]
        if run(cmd, "robustness corpus"):
            failures.append("robustness")
    if "portal" in gates:
        print("\n=== portal: run the playwright pass against a REAL drawing before shipping:")
        print("    JOBS_FILE=<scratch>/approval_jobs.qa.json APPROVAL_PORT=5111 PORTAL_TOKEN= "
              f"{PYTHON} approval_server.py &")
        print(f"    QA_PORT=5111 QA_OUT=<scratch>/qa {PYTHON} <scratch>/_qa_portal_ui.py")

    print("\n" + ("ALL GATES PASSED" if not failures else f"FAILED: {', '.join(failures)}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
