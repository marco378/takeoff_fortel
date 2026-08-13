#!/usr/bin/env python3
"""Read-only analytics and approved-only pattern memory for assessor jobs."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _iso_now() -> str:
    return datetime.utcnow().isoformat()


def _job_timestamp(job: dict) -> str:
    for key in ("decided_at", "created_at", "created", "timestamp"):
        value = job.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _job_file(job: dict) -> str:
    res = job.get("result") or {}
    file_value = res.get("file") or job.get("file") or res.get("pdf_path") or job.get("pdf_path")
    if not file_value:
        return ""
    return Path(str(file_value)).name


def _approved_job_record(job_id: str, job: dict) -> dict:
    res = job.get("result") or {}
    costing = job.get("costing") or res.get("costing") or {}
    return {
        "job_id": job_id,
        "approved_at": _job_timestamp(job),
        "project_ref": job.get("project_ref") or res.get("project_ref") or "",
        "project_name": job.get("project_name") or res.get("project_name") or "",
        "file": _job_file(job),
        "area_m2": job.get("area_m2") or res.get("area_m2"),
        "flags": list(job.get("flags") or res.get("flags") or []),
        "scale_src": job.get("scale_src") or res.get("scale_src") or "",
        "scale_k": job.get("scale_k") or res.get("scale_k"),
        "method": job.get("method") or res.get("method") or "",
        "source_discipline": job.get("source_discipline") or res.get("source_discipline") or "",
        "confidence": job.get("confidence") or res.get("confidence"),
        "measurement_state": job.get("measurement_state") or res.get("measurement_state") or "",
        "build_up_assumed": bool(costing.get("assumed")),
    }


def iter_approved_jobs(jobs: dict[str, dict]) -> Iterable[tuple[str, dict]]:
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        decision = (job.get("decision") or "").strip().lower()
        status = (job.get("status") or "").strip().lower()
        if decision == "approved" or status == "approved":
            yield job_id, job


def build_learned_patterns(jobs: dict[str, dict]) -> dict[str, Any]:
    by_file: dict[str, dict[str, Any]] = {}
    by_project_ref: dict[str, dict[str, Any]] = {}

    def update_bucket(bucket: dict[str, dict[str, Any]], key: str, match_type: str,
                      record: dict[str, Any]) -> None:
        if not key:
            return
        entry = dict(record)
        entry.update({"match_type": match_type, "match_value": key})
        current = bucket.get(key)
        if current is None:
            bucket[key] = {"latest": entry, "history": [entry]}
            return
        history = list(current.get("history") or [])
        history.append(entry)
        latest = current.get("latest") or {}
        latest_ts = latest.get("approved_at") or ""
        entry_ts = entry.get("approved_at") or ""
        if entry_ts >= latest_ts:
            current["latest"] = entry
        current["history"] = history

    for job_id, job in iter_approved_jobs(jobs):
        record = _approved_job_record(job_id, job)
        update_bucket(by_file, record["file"], "file", record)
        update_bucket(by_project_ref, record["project_ref"], "project_ref", record)

    return {
        "generated_at": _iso_now(),
        "source": "approved jobs only",
        "by_file": by_file,
        "by_project_ref": by_project_ref,
    }


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a short, file-local temp name so the replacement remains atomic on the same volume.
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def load_or_build_patterns(jobs: dict[str, dict], path: Path) -> dict[str, Any]:
    loaded = load_json(path)
    if loaded is not None:
        return loaded
    patterns = build_learned_patterns(jobs)
    save_json_atomic(path, patterns)
    return patterns


def prior_approval_for_job(job: dict, patterns: dict[str, Any]) -> dict[str, Any] | None:
    res = job.get("result") or {}
    file_value = _job_file(job)
    ref_value = (job.get("project_ref") or res.get("project_ref") or "").strip()
    by_file = patterns.get("by_file") if isinstance(patterns, dict) else {}
    by_project_ref = patterns.get("by_project_ref") if isinstance(patterns, dict) else {}
    for match_type, match_value, bucket in (
        ("file", file_value, by_file),
        ("project_ref", ref_value, by_project_ref),
    ):
        if not match_value or not isinstance(bucket, dict):
            continue
        entry = bucket.get(match_value) or {}
        latest = entry.get("latest")
        if latest:
            prior = dict(latest)
            prior["matched_on"] = match_type
            return prior
    return None


def attach_prior_approval(job_id: str, job: dict, patterns: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    prior = prior_approval_for_job(job, patterns)
    if prior:
        payload["prior_approval"] = prior
    payload["id"] = payload.get("id") or job_id
    return payload


def analytics_report(jobs: dict[str, dict]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_project: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "deltas": []})
    for job_id, job in iter_approved_jobs(jobs):
        res = job.get("result") or {}
        area = job.get("area_m2") or res.get("area_m2")
        assessed = (job.get("adjusted") or {}).get("area_m2")
        delta = None
        if isinstance(area, (int, float)) and isinstance(assessed, (int, float)):
            delta = round(float(assessed) - float(area), 3)
        row = {
            "job_id": job_id,
            "file": _job_file(job),
            "project_ref": job.get("project_ref") or res.get("project_ref") or "",
            "project_name": job.get("project_name") or res.get("project_name") or "",
            "approved_at": _job_timestamp(job),
            "area_m2": area,
            "assessed_area_m2": assessed,
            "delta_m2": delta,
        }
        rows.append(row)
        ref = row["project_ref"] or "__no_project_ref__"
        by_project[ref]["count"] += 1
        if delta is not None:
            by_project[ref]["deltas"].append(delta)

    return {
        "generated_at": _iso_now(),
        "approved_jobs": rows,
        "by_project_ref": {
            ref: {
                "count": info["count"],
                "mean_delta_m2": round(sum(info["deltas"]) / len(info["deltas"]), 3)
                if info["deltas"] else None,
            }
            for ref, info in by_project.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approved-only pattern memory and analytics")
    parser.add_argument("jobs_file", type=Path, help="Path to approval jobs JSON")
    parser.add_argument("--patterns-out", type=Path, help="Write learned patterns JSON here")
    parser.add_argument("--report", action="store_true", help="Print analytics summary")
    args = parser.parse_args(argv)

    jobs = json.loads(args.jobs_file.read_text()) if args.jobs_file.exists() else {}
    if not isinstance(jobs, dict):
        raise SystemExit("jobs file must contain a JSON object")

    patterns = build_learned_patterns(jobs)
    if args.patterns_out:
        save_json_atomic(args.patterns_out, patterns)
    if args.report:
        print(json.dumps(analytics_report(jobs), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
