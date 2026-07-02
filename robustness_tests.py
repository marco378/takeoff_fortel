#!/usr/bin/env python3
"""
robustness_tests.py

Runs the Fortel takeoff pipeline against EVERY file found under drawings/
(winvic/, tender_pack/, info_used/, corpus/, eml_examples/, and any loose
files directly in drawings/) inside a subprocess, so a crash or hang in the
pipeline cannot kill this harness.

This is a REPORTING tool only. It does not fix or modify pipeline code.
Failures found here are expected findings to hand to the pipeline-hardening
agent, not bugs for this script to work around.

Classification per file:
    CRASH            - subprocess exited non-zero, or raised an exception
    HANG             - subprocess did not finish within the timeout
    SILENT_NUMBER    - an area_m2 was produced with no verified scale and no
                        "assessor"/"needs" flag telling a human to check it
    MEASURED_OK      - an area_m2 was produced with a verified scale or an
                        explicit assessor-confirmation flag
    REFUSED_CLEANLY  - no area_m2 produced, but the pipeline returned
                        cleanly with flags explaining why (mandatory human
                        review, no scale found, unreadable file, etc.)
    UNREADABLE_INPUT - the file is not something takeoff() can be handed at
                        all (e.g. .zip, .eml, .docx) - reported separately
                        from CRASH since raising early here is arguably
                        correct behaviour, not a bug

Where gold.json has an entry for the file, the emitted area is checked
against tol_pct -> GOLD_PASS / GOLD_FAIL is appended to the flags.

Output: printed summary table + written to ROBUSTNESS_REPORT.md.
The harness process itself always exits 0 (it is a report generator, not a
pass/fail gate) and its last stdout line is a compact summary like:
    "N files: X ok, Y refused, Z CRASH, ..."
"""
import glob
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
DRAWINGS = os.path.join(REPO, "drawings")
GOLD_PATH = os.path.join(REPO, "gold.json")
REPORT_PATH = os.path.join(REPO, "ROBUSTNESS_REPORT.md")
PYTHON = os.path.join(REPO, ".venv", "bin", "python")

TIMEOUT_S = 180

# Extensions takeoff_pipeline.takeoff() can plausibly be handed. Everything
# else (.zip, .eml, .docx, .xlsx) is a deliberate adversarial input to see
# how the intake layer / router reacts, not a pipeline-internal file.
PDF_EXTS = {".pdf"}


def load_gold():
    with open(GOLD_PATH) as f:
        data = json.load(f)
    # drop comment / pending-section keys (start with "_")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def resolve_gold_entry(gold, filepath):
    """gold.json keys are either bare filenames (synthetic_yard.pdf) or
    repo-relative paths (drawings/winvic/...). Try both."""
    rel = os.path.relpath(filepath, REPO)
    base = os.path.basename(filepath)
    if rel in gold:
        return gold[rel]
    if base in gold:
        return gold[base]
    return None


def find_all_files():
    """Every file under drawings/, in a stable sorted order."""
    out = []
    for root, dirs, files in os.walk(DRAWINGS):
        dirs.sort()
        for fn in sorted(files):
            out.append(os.path.join(root, fn))
    return sorted(out)


CHILD_SCRIPT = r"""
import sys, json, traceback
path = sys.argv[1]
try:
    import takeoff_pipeline
    r = takeoff_pipeline.takeoff(path, send_approval=False)
    # Ensure JSON-serialisable (drop anything exotic)
    def sanitize(o):
        if isinstance(o, dict):
            return {str(k): sanitize(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [sanitize(v) for v in o]
        if isinstance(o, (str, int, float, bool)) or o is None:
            return o
        return str(o)
    print("===RESULT_JSON===")
    print(json.dumps(sanitize(r)))
except SystemExit:
    raise
except BaseException as e:
    print("===EXCEPTION===")
    print(json.dumps({"error": str(e), "type": type(e).__name__, "traceback": traceback.format_exc()}))
    sys.exit(1)
"""


def run_one(filepath):
    """Run the pipeline against filepath in a subprocess. Returns a dict
    with outcome classification and details. Never raises."""
    rel = os.path.relpath(filepath, REPO)
    ext = os.path.splitext(filepath)[1].lower()

    result = {
        "file": rel,
        "ext": ext,
        "class": None,
        "area_m2": None,
        "gold_delta_pct": None,
        "flags": [],
        "exit_code": None,
        "elapsed_s": None,
        "stderr_tail": "",
        "notes": "",
    }

    if ext not in PDF_EXTS:
        # Deliberately hand non-PDF adversarial inputs to the pipeline too —
        # part of the point is to see whether it refuses cleanly or blows up.
        result["notes"] = "non-PDF adversarial input"

    start = time.time()
    try:
        proc = subprocess.run(
            [PYTHON, "-c", CHILD_SCRIPT, filepath],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result["elapsed_s"] = round(time.time() - start, 1)
        result["class"] = "HANG"
        result["notes"] = f"exceeded {TIMEOUT_S}s timeout"
        return result

    result["elapsed_s"] = round(time.time() - start, 1)
    result["exit_code"] = proc.returncode

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    result["stderr_tail"] = "\n".join(stderr.strip().splitlines()[-8:])

    if proc.returncode != 0:
        result["class"] = "CRASH"
        if "===EXCEPTION===" in stdout:
            try:
                payload = json.loads(stdout.split("===EXCEPTION===", 1)[1].strip())
                result["notes"] = f"{payload.get('type')}: {payload.get('error')}"
            except Exception:
                result["notes"] = "unparseable exception payload"
        else:
            result["notes"] = "nonzero exit, no exception payload captured"
        return result

    if "===RESULT_JSON===" not in stdout:
        result["class"] = "CRASH"
        result["notes"] = "exit 0 but no RESULT_JSON marker found (unexpected)"
        return result

    try:
        payload = json.loads(stdout.split("===RESULT_JSON===", 1)[1].strip())
    except Exception as e:
        result["class"] = "CRASH"
        result["notes"] = f"could not parse RESULT_JSON: {e}"
        return result

    flags = payload.get("flags", []) or []
    result["flags"] = flags
    area = payload.get("area_m2")
    result["area_m2"] = area
    scale_verified = payload.get("scale_verified", False)

    flags_join = " | ".join(str(f) for f in flags).lower()
    needs_assessor = any(
        kw in flags_join
        for kw in ("assessor", "needs", "mandatory human", "manually", "must supply", "must be")
    )

    # Prefer the pipeline's own four-state contract when present
    # (MEASURED_VERIFIED / MEASURED_UNVERIFIED / UNMEASURED / REJECTED).
    m_state = payload.get("measurement_state") or payload.get("status")
    result["state"] = m_state
    if m_state == "REJECTED":
        result["class"] = "REFUSED_CLEANLY"
        result["notes"] = (result["notes"] + " rejected-with-reason").strip()
    elif m_state == "UNMEASURED":
        result["class"] = "REFUSED_CLEANLY"
    elif m_state == "MEASURED_VERIFIED":
        result["class"] = "MEASURED_OK"
    elif m_state == "MEASURED_UNVERIFIED":
        # unverified is fine ONLY if the record demands an assessor (approve is blocked)
        result["class"] = "MEASURED_OK" if (payload.get("needs_assessor") or needs_assessor) \
                          else "SILENT_NUMBER"
    elif area is None:
        result["class"] = "REFUSED_CLEANLY"
    else:
        if scale_verified or (needs_assessor and "confirm extent + scale" in flags_join):
            # Explicit "assessor: confirm extent + scale" is the pipeline's
            # own standard disclaimer on every measured area — that's normal
            # practice, not a silent number. A silent number is one with NO
            # verified scale AND no flag at all telling anyone to check it.
            result["class"] = "MEASURED_OK"
        elif needs_assessor:
            result["class"] = "MEASURED_OK"
        else:
            result["class"] = "SILENT_NUMBER"

    # gold check
    gold = _GOLD_CACHE
    entry = resolve_gold_entry(gold, filepath)
    if entry and "net_m2" in entry and area is not None:
        gold_area = entry["net_m2"]
        tol_pct = entry.get("tol_pct", 2)
        if gold_area:
            delta_pct = abs(area - gold_area) / gold_area * 100
            result["gold_delta_pct"] = round(delta_pct, 2)
            result["gold_area_m2"] = gold_area
            result["gold_verdict"] = "GOLD_PASS" if delta_pct <= tol_pct else "GOLD_FAIL"
    elif entry and "net_m2" in entry and area is None:
        result["gold_verdict"] = "GOLD_FAIL"
        result["notes"] += " (gold expects an area but none was produced)"

    # manhole_count gold check (MARKED path — confirmed Circle-marker count). Independent
    # of the area gold_verdict above: a file can carry both, either, or neither entry.
    if entry and "manhole_count" in entry:
        gold_mh = entry["manhole_count"]
        got_mh = payload.get("manhole_count")
        result["gold_manhole_count"] = gold_mh
        result["manhole_count"] = got_mh
        if got_mh is not None and got_mh == gold_mh:
            result["gold_manhole_verdict"] = "GOLD_PASS"
        else:
            result["gold_manhole_verdict"] = "GOLD_FAIL"
            result["notes"] += f" (gold expects manhole_count={gold_mh}, got {got_mh})"

    return result


_GOLD_CACHE = {}


def main():
    global _GOLD_CACHE
    _GOLD_CACHE = load_gold()

    files = find_all_files()
    if not files:
        print("No files found under drawings/ — nothing to test.")
        return 0

    print(f"robustness_tests.py: running pipeline against {len(files)} files "
          f"(timeout {TIMEOUT_S}s each)\n")

    results = []
    for i, fp in enumerate(files, 1):
        rel = os.path.relpath(fp, REPO)
        print(f"[{i}/{len(files)}] {rel} ...", end=" ", flush=True)
        r = run_one(fp)
        print(f"{r['class']} ({r['elapsed_s']}s)"
              + (f" area={r['area_m2']}m2" if r["area_m2"] is not None else "")
              + (f" {r.get('gold_verdict')}" if r.get("gold_verdict") else ""))
        results.append(r)

    # ── Tally ──────────────────────────────────────────────────────────
    tally = {}
    for r in results:
        tally[r["class"]] = tally.get(r["class"], 0) + 1
    gold_pass = sum(1 for r in results if r.get("gold_verdict") == "GOLD_PASS")
    gold_fail = sum(1 for r in results if r.get("gold_verdict") == "GOLD_FAIL")
    mh_gold_pass = sum(1 for r in results if r.get("gold_manhole_verdict") == "GOLD_PASS")
    mh_gold_fail = sum(1 for r in results if r.get("gold_manhole_verdict") == "GOLD_FAIL")

    summary_parts = [f"{v} {k}" for k, v in sorted(tally.items())]
    summary_line = f"{len(results)} files: " + ", ".join(summary_parts)
    if gold_pass or gold_fail:
        summary_line += f" | gold: {gold_pass} PASS, {gold_fail} FAIL"
    if mh_gold_pass or mh_gold_fail:
        summary_line += f" | manhole gold: {mh_gold_pass} PASS, {mh_gold_fail} FAIL"

    # ── Write report ──────────────────────────────────────────────────
    lines = []
    lines.append("# Robustness Report\n")
    lines.append(f"Generated by `robustness_tests.py`. {len(results)} files tested, "
                  f"{TIMEOUT_S}s timeout per file, each run in an isolated subprocess.\n")
    lines.append(f"**Summary:** {summary_line}\n")
    lines.append("| File | Class | Exit | Elapsed(s) | Area(m2) | Gold(m2) | Delta% | Gold Verdict | Notes / Flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        flags_str = "; ".join(str(f) for f in r["flags"])[:200]
        notes = (r["notes"] + (" " + flags_str if flags_str else "")).strip()
        notes = notes.replace("|", "/").replace("\n", " ")[:300]
        lines.append(
            f"| {r['file']} | {r['class']} | {r['exit_code']} | {r['elapsed_s']} "
            f"| {r['area_m2'] if r['area_m2'] is not None else ''} "
            f"| {r.get('gold_area_m2', '')} "
            f"| {r.get('gold_delta_pct', '')} "
            f"| {r.get('gold_verdict', '')} "
            f"| {notes} |"
        )

    lines.append("\n## Notes\n")
    lines.append("- CRASH: subprocess exited non-zero or raised.")
    lines.append("- HANG: subprocess did not finish within timeout.")
    lines.append("- SILENT_NUMBER: area_m2 produced with no verified scale and no assessor/needs flag.")
    lines.append("- MEASURED_OK: area_m2 produced with verified scale or explicit assessor-confirm flag.")
    lines.append("- REFUSED_CLEANLY: no area_m2, pipeline returned cleanly with explanatory flags.")
    lines.append("- Non-PDF adversarial inputs (.zip/.eml/.docx/etc.) are handed to takeoff() directly to see how intake reacts; a clean refusal or classify()-level error is not itself a bug.")

    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("\n" + "=" * 100)
    print(summary_line)
    print(f"Full report written to {os.path.relpath(REPORT_PATH, REPO)}")
    print("=" * 100)

    return 0  # reporting tool: always exit 0


if __name__ == "__main__":
    sys.exit(main())
