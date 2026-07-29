#!/usr/bin/env python3
"""Accuracy scorecard for client-marked Fortel drawing sets.

Usage:
    .venv/bin/python accuracy_report.py <dir-or-files...> [--tol 5]
        [--json out.json] [--md out.md]

The marked PDF is truth only.  The measurement pipeline always receives either an
explicit corresponding raw PDF or a temporary copy with every annotation removed.
This is a reporting tool: individual file failures are records, never process failures,
and the CLI deliberately exits zero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

import fitz

from robust_takeoff import read_marked_zones


REPORT_VERSION = 1
DEFAULT_TOLERANCE_PCT = 5.0
PIPELINE_TIMEOUT_S = 240
RESULT_MARKER = "===ACCURACY_RESULT_JSON==="

AREA_ZONES = ("external_yard", "dock", "ground_floor", "upper_floor")
LENGTH_ZONES = ("channel", "transition")
ZONE_ORDER = AREA_ZONES + LENGTH_ZONES
PAIRING_NOISE = {
    "annotated", "annotation", "annotations", "bluebeam", "marked", "markup",
    "markups", "original", "raw", "reference", "stripped", "truth", "unmarked",
}

CHILD_SCRIPT = r"""
import json
import sys
import traceback

path = sys.argv[1]
try:
    from takeoff_pipeline import takeoff
    result = takeoff(path, send_approval=False)

    def clean(value):
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    print("===ACCURACY_RESULT_JSON===")
    print(json.dumps(clean(result)))
except BaseException as exc:
    print("===ACCURACY_CHILD_ERROR===")
    print(json.dumps({
        "type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }))
    sys.exit(1)
"""


def normalise_drawing_name(path_or_name: str | os.PathLike) -> str:
    """Normalise marked/raw naming variants to a conservative pairing key."""
    stem = Path(path_or_name).stem
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    noise = "|".join(sorted(PAIRING_NOISE, key=len, reverse=True))
    # Also handle common attached prefixes such as "MarkupProject A.pdf".
    ascii_stem = re.sub(rf"^(?:{noise})[\s_.-]*", "", ascii_stem, flags=re.I)
    ascii_stem = re.sub(rf"[\s_.-]*(?:{noise})$", "", ascii_stem, flags=re.I)
    tokens = re.findall(r"[a-z0-9]+", ascii_stem.casefold())
    kept = [token for token in tokens if token not in PAIRING_NOISE]
    return "".join(kept)


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except (OSError, ValueError):
        return str(path)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def signed_delta_pct(measured: float | None, truth: float | None) -> float | None:
    """Signed percentage delta; positive means over-measured."""
    if not _is_number(measured) or not _is_number(truth):
        return None
    if truth == 0:
        return 0.0 if measured == 0 else math.copysign(math.inf, measured)
    return (float(measured) - float(truth)) / abs(float(truth)) * 100


def within_tolerance(measured: float | None, truth: float | None,
                     tolerance_pct: float) -> bool:
    delta = signed_delta_pct(measured, truth)
    return delta is not None and math.isfinite(delta) and abs(delta) <= tolerance_pct


def _truth_from_pdf(path: Path) -> dict:
    truth = read_marked_zones(str(path))
    area_records = [
        record for record in truth.get("markup_annotations", [])
        if _is_number(record.get("area_m2"))
    ]
    if not area_records:
        raise ValueError("no Bluebeam Polygon area annotations found")
    if not _is_number(truth.get("area_m2")) or truth["area_m2"] <= 0:
        raise ValueError("Bluebeam area annotations did not produce a positive truth total")
    return truth


def _inspect_pdf(path: Path) -> dict:
    """Classify a PDF as marked truth, raw, or unreadable without raising."""
    try:
        truth = read_marked_zones(str(path))
        area_records = [
            record for record in truth.get("markup_annotations", [])
            if _is_number(record.get("area_m2"))
        ]
        return {
            "path": path,
            "kind": "marked" if area_records else "raw",
            "truth": truth if area_records else None,
            "error": None,
        }
    except BaseException as exc:
        return {
            "path": path,
            "kind": "error",
            "truth": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _expand_inputs(inputs: list[str]) -> tuple[list[Path], list[str]]:
    files = []
    issues = []
    for raw_input in inputs:
        path = Path(raw_input).expanduser()
        if not path.exists():
            issues.append(f"{raw_input}: path not found")
            continue
        if path.is_dir():
            found = sorted(candidate for candidate in path.rglob("*")
                           if candidate.is_file() and candidate.suffix.casefold() == ".pdf")
            if not found:
                issues.append(f"{raw_input}: directory contains no PDFs")
            files.extend(found)
        elif path.suffix.casefold() == ".pdf":
            files.append(path)
        else:
            issues.append(f"{raw_input}: not a PDF")

    unique = {}
    for path in files:
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        unique.setdefault(key, path)
    return sorted(unique.values(), key=lambda item: str(item).casefold()), issues


def _raw_pair_score(marked: Path, raw: Path) -> tuple[int, str]:
    """Higher is a less ambiguous correspondence between one truth and one raw file."""
    score = 0
    reasons = []
    if marked.name.casefold() == raw.name.casefold():
        score += 100
        reasons.append("same filename")
    if raw.parent.name.casefold() in {"_stripped", "stripped", "raw", "unmarked"}:
        score += 40
        reasons.append(f"raw folder '{raw.parent.name}'")
    if marked.parent == raw.parent:
        score += 10
        reasons.append("same folder")
    score -= abs(len(marked.stem) - len(raw.stem))
    return score, ", ".join(reasons) or "normalised filename"


def pair_marked_and_raw(marked: list[dict], raw: list[dict]) -> tuple[list[dict], list[str]]:
    """Pair classified records by normalised filename, without reusing a raw drawing."""
    raw_by_key: dict[str, list[Path]] = {}
    for record in raw:
        key = normalise_drawing_name(record["path"])
        if key:
            raw_by_key.setdefault(key, []).append(record["path"])

    used_raw = set()
    pairs = []
    for record in sorted(marked, key=lambda item: str(item["path"]).casefold()):
        marked_path = record["path"]
        key = normalise_drawing_name(marked_path)
        candidates = [
            candidate for candidate in raw_by_key.get(key, [])
            if candidate not in used_raw
        ]
        if candidates:
            ranked = sorted(
                ((_raw_pair_score(marked_path, candidate), candidate)
                 for candidate in candidates),
                key=lambda item: (-item[0][0], str(item[1]).casefold()),
            )
            (score, reason), raw_path = ranked[0]
            used_raw.add(raw_path)
            pairs.append({
                "marked_path": marked_path,
                "truth": record["truth"],
                "raw_path": raw_path,
                "raw_mode": "existing",
                "pairing": reason,
                "pairing_score": score,
            })
        else:
            pairs.append({
                "marked_path": marked_path,
                "truth": record["truth"],
                "raw_path": None,
                "raw_mode": "derived-stripped",
                "pairing": "no corresponding raw PDF; annotations stripped temporarily",
                "pairing_score": None,
            })

    unused = [
        f"{_display_path(record['path'])}: raw PDF has no matching marked truth"
        for record in raw if record["path"] not in used_raw
    ]
    return pairs, unused


def discover_pairs(inputs: list[str]) -> dict:
    """Expand inputs and return marked/raw pairs plus non-fatal discovery issues."""
    paths, input_issues = _expand_inputs(inputs)
    inspected = [_inspect_pdf(path) for path in paths]
    marked = [record for record in inspected if record["kind"] == "marked"]
    raw = [record for record in inspected if record["kind"] == "raw"]
    errors = [record for record in inspected if record["kind"] == "error"]
    pairs, unused_raw = pair_marked_and_raw(marked, raw)
    return {
        "pairs": pairs,
        "input_issues": input_issues,
        "unused_raw": unused_raw,
        "inspection_errors": errors,
        "pdf_count": len(paths),
    }


def strip_annotations(source: Path, destination: Path) -> int:
    """Save an annotation-free PDF and verify that no annotation survived."""
    document = fitz.open(str(source))
    removed = 0
    try:
        for page in document:
            # Deleting one annot invalidates PyMuPDF annot objects already materialised for
            # that page. Reacquire first_annot after each deletion instead of walking a list
            # of now-unbound wrappers.
            annotation = page.first_annot
            while annotation is not None:
                page.delete_annot(annotation)
                removed += 1
                annotation = page.first_annot
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(destination), garbage=4, deflate=True)
    finally:
        document.close()

    with fitz.open(str(destination)) as check:
        remaining = sum(1 for page in check for _ in (page.annots() or []))
    if remaining:
        raise RuntimeError(f"annotation stripping verification failed: {remaining} remain")
    return removed


def _run_pipeline(raw_path: Path) -> dict:
    start = time.monotonic()
    try:
        process = subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT, str(raw_path)],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=PIPELINE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"pipeline timeout after {PIPELINE_TIMEOUT_S}s",
            "elapsed_s": round(time.monotonic() - start, 2),
        }
    except BaseException as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - start, 2),
        }

    elapsed = round(time.monotonic() - start, 2)
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if process.returncode != 0:
        child_error = ""
        if "===ACCURACY_CHILD_ERROR===" in stdout:
            try:
                payload = json.loads(stdout.rsplit("===ACCURACY_CHILD_ERROR===", 1)[1].strip())
                child_error = f"{payload.get('type')}: {payload.get('error')}"
            except (ValueError, TypeError):
                child_error = "unparseable child exception"
        return {
            "ok": False,
            "error": child_error or f"pipeline exited {process.returncode}",
            "stderr_tail": "\n".join(stderr.strip().splitlines()[-5:]),
            "elapsed_s": elapsed,
        }
    if RESULT_MARKER not in stdout:
        return {
            "ok": False,
            "error": "pipeline exited without a result payload",
            "stderr_tail": "\n".join(stderr.strip().splitlines()[-5:]),
            "elapsed_s": elapsed,
        }
    try:
        payload = json.loads(stdout.rsplit(RESULT_MARKER, 1)[1].strip())
    except (ValueError, TypeError) as exc:
        return {
            "ok": False,
            "error": f"invalid pipeline JSON: {exc}",
            "stderr_tail": "\n".join(stderr.strip().splitlines()[-5:]),
            "elapsed_s": elapsed,
        }
    return {"ok": True, "payload": payload, "elapsed_s": elapsed}


def _zone_values(zones: list[dict] | None) -> dict[str, float]:
    values: dict[str, float] = {}
    for zone in zones or []:
        if not isinstance(zone, dict):
            continue
        category = str(zone.get("category") or "")
        field = "area_m2" if category in AREA_ZONES else (
            "length_lm" if category in LENGTH_ZONES else None)
        value = zone.get(field) if field else None
        if category in ZONE_ORDER and _is_number(value):
            values[category] = values.get(category, 0.0) + float(value)
    return values


def _compare_zones(truth: dict, pipeline: dict, tolerance_pct: float) -> list[dict]:
    truth_values = _zone_values(truth.get("zones"))
    measured_values = _zone_values(pipeline.get("zones"))
    measured_sources = {category: "pipeline zone" for category in measured_values}

    # A sole truth area category can be safely compared with an aggregate-only estimator.
    # Never make this inference for multi-zone drawings: that would conceal a BOQ mis-split.
    truth_area_categories = [
        category for category in AREA_ZONES if category in truth_values
    ]
    if (not any(category in measured_values for category in AREA_ZONES)
            and len(truth_area_categories) == 1
            and _is_number(pipeline.get("area_m2"))):
        category = truth_area_categories[0]
        measured_values[category] = float(pipeline["area_m2"])
        measured_sources[category] = "aggregate mapped to sole truth area zone"

    comparisons = []
    for category in ZONE_ORDER:
        truth_value = truth_values.get(category)
        measured_value = measured_values.get(category)
        field = "area_m2" if category in AREA_ZONES else "length_lm"
        unit = "m²" if category in AREA_ZONES else "Lm"
        if truth_value is None and measured_value is None:
            status = "NOT APPLICABLE"
            delta = None
        elif truth_value is None:
            status = "FAIL - unexpected measured zone"
            delta = None
        elif measured_value is None:
            status = "FAIL - missing measured zone"
            delta = None
        else:
            delta = signed_delta_pct(measured_value, truth_value)
            status = "PASS" if within_tolerance(
                measured_value, truth_value, tolerance_pct) else "FAIL"
        comparisons.append({
            "category": category,
            "metric": field,
            "unit": unit,
            "truth": round(truth_value, 2) if truth_value is not None else None,
            "measured": round(measured_value, 2) if measured_value is not None else None,
            "delta_pct": round(delta, 2) if delta is not None and math.isfinite(delta) else None,
            "status": status,
            "measured_source": measured_sources.get(category),
        })
    return comparisons


def _top_reasons(result: dict, pipeline: dict | None) -> list[str]:
    reasons = []
    failure_mode = result.get("failure_mode")
    delta = result.get("delta_pct")
    if failure_mode == "not measured":
        reasons.append(
            f"pipeline returned {result.get('measurement_state') or 'no measurement state'} "
            "with no usable area"
        )
    elif failure_mode == "over-measured" and delta is not None:
        reasons.append(f"aggregate is {abs(delta):.2f}% above marked truth")
    elif failure_mode == "under-measured" and delta is not None:
        reasons.append(f"aggregate is {abs(delta):.2f}% below marked truth")
    elif failure_mode == "zone mis-split":
        failed = [
            comparison["category"] for comparison in result.get("zones", [])
            if comparison["status"].startswith("FAIL")
        ]
        reasons.append("zone comparison failed: " + ", ".join(failed))
    elif failure_mode == "error":
        reasons.append(result.get("error") or "unknown processing error")

    flags = list((pipeline or {}).get("flags") or [])
    priority_words = (
        "refus", "no area", "no scale", "unmeasured", "mismatch", "missing",
        "unverified", "assessor", "manual", "candidate",
    )
    ranked = sorted(
        enumerate(flags),
        key=lambda item: (
            0 if any(word in str(item[1]).casefold() for word in priority_words) else 1,
            item[0],
        ),
    )
    for _, flag in ranked:
        clean = " ".join(str(flag).split())
        if clean and clean not in reasons:
            reasons.append(clean[:300])
        if len(reasons) >= 4:
            break
    return reasons


def score_drawing(*, marked_path: Path, raw_label: str, raw_mode: str,
                  pairing: str, truth: dict, pipeline_run: dict,
                  tolerance_pct: float) -> dict:
    """Create one JSON-safe drawing score. This function never raises."""
    base = {
        "drawing": _display_path(marked_path),
        "marked_file": _display_path(marked_path),
        "raw_file": raw_label,
        "raw_mode": raw_mode,
        "pairing": pairing,
        "truth_total_m2": round(float(truth["area_m2"]), 2),
        "measured_total_m2": None,
        "delta_pct": None,
        "measurement_state": None,
        "verdict": "ERROR",
        "failure_mode": "error",
        "zones": [],
        "top_flags": [],
        "elapsed_s": pipeline_run.get("elapsed_s"),
    }
    if not pipeline_run.get("ok"):
        base["error"] = pipeline_run.get("error") or "pipeline failed"
        base["top_flags"] = _top_reasons(base, None)
        return base

    pipeline = pipeline_run["payload"]
    state = pipeline.get("measurement_state") or pipeline.get("status") or "UNKNOWN"
    # Raw external takeoff keeps its historic top-level area_m2 as the service-yard
    # quantity so existing golds and Yard pricing do not change.  Once zones are
    # available, their explicit mutually-exclusive total is the like-for-like
    # comparison with read_marked_zones()' all-area truth.
    measured = pipeline.get("zones_total_area_m2")
    if not _is_number(measured):
        measured = pipeline.get("area_m2")
    measured = float(measured) if _is_number(measured) else None
    delta = signed_delta_pct(measured, truth["area_m2"])
    zones = _compare_zones(truth, pipeline, tolerance_pct)
    zone_failures = [
        comparison for comparison in zones if comparison["status"].startswith("FAIL")
    ]

    base.update({
        "measured_total_m2": round(measured, 2) if measured is not None else None,
        "delta_pct": round(delta, 2) if delta is not None and math.isfinite(delta) else None,
        "measurement_state": state,
        "zones": zones,
        "pipeline_type": pipeline.get("type"),
        "pipeline_method": pipeline.get("method"),
        "scale_verified": bool(pipeline.get("scale_verified", False)),
    })

    if state in {"UNMEASURED", "REJECTED"} or measured is None:
        base["verdict"] = "NOT MEASURED"
        base["failure_mode"] = "not measured"
    elif not within_tolerance(measured, truth["area_m2"], tolerance_pct):
        base["verdict"] = "FAIL"
        base["failure_mode"] = "over-measured" if (delta or 0) > 0 else "under-measured"
    elif zone_failures:
        base["verdict"] = "FAIL"
        base["failure_mode"] = "zone mis-split"
    else:
        base["verdict"] = "PASS"
        base["failure_mode"] = None
    base["top_flags"] = _top_reasons(base, pipeline)
    return base


def scorecard(results: list[dict], tolerance_pct: float) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result.get("verdict") == "PASS")
    aggregate_passed = sum(
        1 for result in results
        if (result.get("verdict") == "PASS"
            or (_is_number(result.get("delta_pct"))
                and abs(result["delta_pct"]) <= tolerance_pct
                and result.get("measurement_state") not in {"UNMEASURED", "REJECTED"}))
    )
    breakdown = {
        "not measured": 0,
        "over-measured": 0,
        "under-measured": 0,
        "zone mis-split": 0,
        "error": 0,
    }
    for result in results:
        failure = result.get("failure_mode")
        if failure in breakdown:
            breakdown[failure] += 1
    accuracy = passed / total * 100 if total else 0.0
    tolerance_label = f"{tolerance_pct:g}"
    return {
        "drawings": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy_pct": round(accuracy, 1),
        "aggregate_passed": aggregate_passed,
        "aggregate_accuracy_pct": round(
            aggregate_passed / total * 100 if total else 0.0, 1),
        "tolerance_pct": tolerance_pct,
        "breakdown": breakdown,
        "summary_line": (
            f"{passed} of {total} within {tolerance_label}% ({accuracy:.1f}%)"
        ),
        "aggregate_summary_line": (
            f"{aggregate_passed} of {total} aggregate totals within "
            f"{tolerance_label}% "
            f"({aggregate_passed / total * 100 if total else 0.0:.1f}%)"
        ),
    }


def _error_result(path: Path, error: str) -> dict:
    result = {
        "drawing": _display_path(path),
        "marked_file": _display_path(path),
        "raw_file": None,
        "raw_mode": None,
        "pairing": None,
        "truth_total_m2": None,
        "measured_total_m2": None,
        "delta_pct": None,
        "measurement_state": None,
        "verdict": "ERROR",
        "failure_mode": "error",
        "zones": [],
        "top_flags": [error],
        "error": error,
        "elapsed_s": None,
    }
    return result


def run_report(inputs: list[str], tolerance_pct: float) -> dict:
    discovery = discover_pairs(inputs)
    results = [
        _error_result(record["path"], record["error"])
        for record in discovery["inspection_errors"]
    ]

    with tempfile.TemporaryDirectory(prefix="fortel_accuracy_") as temp_dir:
        temp_root = Path(temp_dir)
        for index, pair in enumerate(discovery["pairs"], 1):
            marked_path = pair["marked_path"]
            try:
                truth = pair.get("truth") or _truth_from_pdf(marked_path)
                if pair["raw_path"] is None:
                    safe_stem = normalise_drawing_name(marked_path) or f"drawing-{index}"
                    raw_path = temp_root / f"{safe_stem}.pdf"
                    removed = strip_annotations(marked_path, raw_path)
                    raw_label = f"temporary stripped copy ({removed} annotations removed)"
                else:
                    raw_path = pair["raw_path"]
                    raw_label = _display_path(raw_path)
                    raw_check = _inspect_pdf(raw_path)
                    if raw_check["kind"] != "raw":
                        raise RuntimeError(
                            "paired raw PDF still contains answer annotations; refusing to measure it"
                        )
                pipeline_run = _run_pipeline(raw_path)
                result = score_drawing(
                    marked_path=marked_path,
                    raw_label=raw_label,
                    raw_mode=pair["raw_mode"],
                    pairing=pair["pairing"],
                    truth=truth,
                    pipeline_run=pipeline_run,
                    tolerance_pct=tolerance_pct,
                )
            except BaseException as exc:
                result = _error_result(
                    marked_path, f"{type(exc).__name__}: {exc}")
                result["pairing"] = pair.get("pairing")
                result["raw_mode"] = pair.get("raw_mode")
            results.append(result)

    results.sort(key=lambda result: result["drawing"].casefold())
    summary = scorecard(results, tolerance_pct)
    return {
        "schema_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tolerance_pct": tolerance_pct,
        "inputs": inputs,
        "scoring_rule": (
            "PASS requires aggregate and every truth-bearing area/length zone within tolerance; "
            "UNMEASURED/REJECTED/ERROR always fail"
        ),
        "summary": summary,
        "results": results,
        "discovery": {
            "pdf_count": discovery["pdf_count"],
            "input_issues": discovery["input_issues"],
            "unpaired_raw": discovery["unused_raw"],
        },
    }


def _format_number(value: Any, decimals: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and math.isinf(value):
        return "+inf" if value > 0 else "-inf"
    if _is_number(value):
        return f"{value:,.{decimals}f}"
    return str(value)


def markdown_report(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Fortel Accuracy Scorecard",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Tolerance: **+/-{report['tolerance_pct']:g}%**  ",
        f"Scoring: {report['scoring_rule']}",
        "",
        f"## {summary['summary_line']}",
        "",
        f"Aggregate-only diagnostic: **{summary['aggregate_summary_line']}**. "
        "The acceptance score above also requires the client BOQ zones/lengths to pass.",
        "",
        "| Failure mode | Count |",
        "|---|---:|",
    ]
    for mode, count in summary["breakdown"].items():
        lines.append(f"| {mode} | {count} |")

    lines.extend([
        "",
        "## Per drawing",
        "",
        "| Drawing | Truth m2 | Measured m2 | Delta | State | Result | Failure mode |",
        "|---|---:|---:|---:|---|---|---|",
    ])
    for result in report["results"]:
        delta = (_format_number(result["delta_pct"], 2) + "%"
                 if result["delta_pct"] is not None else "-")
        lines.append(
            f"| {result['drawing'].replace('|', '/')} "
            f"| {_format_number(result['truth_total_m2'], 1)} "
            f"| {_format_number(result['measured_total_m2'], 1)} "
            f"| {delta} | {result['measurement_state'] or '-'} "
            f"| **{result['verdict']}** | {result['failure_mode'] or '-'} |"
        )

    lines.extend([
        "",
        "## Per-zone comparison",
        "",
        "| Drawing | Zone | Metric | Truth | Measured | Delta | Result |",
        "|---|---|---|---:|---:|---:|---|",
    ])
    for result in report["results"]:
        for zone in result.get("zones", []):
            if zone["status"] == "NOT APPLICABLE":
                continue
            delta = (_format_number(zone["delta_pct"], 2) + "%"
                     if zone["delta_pct"] is not None else "-")
            lines.append(
                f"| {result['drawing'].replace('|', '/')} | {zone['category']} "
                f"| {zone['metric']} ({zone['unit']}) "
                f"| {_format_number(zone['truth'], 2)} "
                f"| {_format_number(zone['measured'], 2)} "
                f"| {delta} | {zone['status']} |"
            )

    lines.extend(["", "## Failure evidence", ""])
    for result in report["results"]:
        if result["verdict"] == "PASS":
            continue
        lines.append(f"### {result['drawing']}")
        lines.append("")
        for reason in result.get("top_flags", []):
            lines.append(f"- {reason}")
        lines.append("")

    discovery = report.get("discovery", {})
    issues = list(discovery.get("input_issues", [])) + list(discovery.get("unpaired_raw", []))
    if issues:
        lines.extend(["## Discovery warnings", ""])
        lines.extend(f"- {issue}" for issue in issues)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def print_report(report: dict) -> None:
    print(
        "| Drawing | Truth m2 | Measured m2 | Delta % | Four-state | Result | Failure |"
    )
    print("|---|---:|---:|---:|---|---|---|")
    for result in report["results"]:
        print(
            f"| {result['drawing']} "
            f"| {_format_number(result['truth_total_m2'], 1)} "
            f"| {_format_number(result['measured_total_m2'], 1)} "
            f"| {_format_number(result['delta_pct'], 2)} "
            f"| {result['measurement_state'] or '-'} "
            f"| {result['verdict']} | {result['failure_mode'] or '-'} |"
        )
        for zone in result.get("zones", []):
            if zone["status"] == "NOT APPLICABLE":
                continue
            print(
                f"  - {zone['category']}: truth={_format_number(zone['truth'], 2)} "
                f"{zone['unit']}, measured={_format_number(zone['measured'], 2)} "
                f"{zone['unit']}, delta={_format_number(zone['delta_pct'], 2)}%, "
                f"{zone['status']}"
            )
        if result["verdict"] != "PASS":
            for reason in result.get("top_flags", [])[:3]:
                print(f"    why: {reason}")

    summary = report["summary"]
    print()
    print("Failure modes: " + ", ".join(
        f"{mode}={count}" for mode, count in summary["breakdown"].items()
    ))
    print(summary["aggregate_summary_line"])
    print(summary["summary_line"])
    for issue in report.get("discovery", {}).get("input_issues", []):
        print(f"[WARN] {issue}")
    for issue in report.get("discovery", {}).get("unpaired_raw", []):
        print(f"[WARN] {issue}")


def _write_text(path_value: str, text: str) -> str | None:
    try:
        path = Path(path_value).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return None
    except BaseException as exc:
        return f"could not write {path_value}: {type(exc).__name__}: {exc}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Score raw Fortel takeoff results against corresponding Bluebeam-marked truth PDFs."
        )
    )
    parser.add_argument("inputs", nargs="+", metavar="DIR_OR_FILE")
    parser.add_argument(
        "--tol", type=float, default=DEFAULT_TOLERANCE_PCT,
        help="percentage tolerance for totals and zones (default: 5)",
    )
    parser.add_argument("--json", dest="json_path", help="write machine-readable JSON")
    parser.add_argument("--md", dest="md_path", help="write a Markdown review report")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return 0
    if not math.isfinite(args.tol) or args.tol < 0:
        print("[ERROR] --tol must be a finite non-negative number")
        return 0

    try:
        report = run_report(args.inputs, args.tol)
        print_report(report)
        write_errors = []
        if args.json_path:
            error = _write_text(
                args.json_path,
                json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            )
            if error:
                write_errors.append(error)
            else:
                print(f"JSON written to {args.json_path}")
        if args.md_path:
            error = _write_text(args.md_path, markdown_report(report))
            if error:
                write_errors.append(error)
            else:
                print(f"Markdown written to {args.md_path}")
        for error in write_errors:
            print(f"[ERROR] {error}")
    except BaseException as exc:
        # A reporting harness must not turn a bad client input into a broken workflow.
        print(f"[ERROR] scorecard could not complete: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    try:
        _exit_code = main()
    except BaseException as exc:
        print(f"[ERROR] scorecard could not start: {type(exc).__name__}: {exc}")
        _exit_code = 0
    sys.exit(_exit_code)
