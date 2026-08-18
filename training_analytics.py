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
    episode = job.get("learning_episode") if isinstance(job.get("learning_episode"), dict) else {}
    return {
        "job_id": job_id,
        "approved_at": _job_timestamp(job),
        "project_ref": job.get("project_ref") or res.get("project_ref") or "",
        "file": _job_file(job),
        "document_sha256": str(job.get("document_sha256")
                                or episode.get("document_sha256") or ""),
        "measurement_state": job.get("measurement_state") or res.get("measurement_state") or "",
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
    by_document_sha256: dict[str, dict[str, Any]] = {}

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
        update_bucket(by_document_sha256, record["document_sha256"],
                      "document_sha256", record)

    return {
        "generated_at": _iso_now(),
        "source": "approved jobs only",
        "by_file": by_file,
        "by_project_ref": by_project_ref,
        "by_document_sha256": by_document_sha256,
    }


def update_learned_patterns(patterns: dict[str, Any] | None, job_id: str, job: dict) -> dict[str, Any]:
    """Increment one approved job without rebuilding the full jobs store."""
    payload = dict(patterns or {})
    payload.setdefault("source", "approved jobs only")
    payload["generated_at"] = _iso_now()
    record = _approved_job_record(job_id, job)
    allowed_record_keys = set(record) | {"match_type", "match_value"}
    # Older cache versions carried area, scale, confidence and flags. They were never
    # auto-applied, but they are unnecessary client data in a cross-job derivative; scrub
    # them opportunistically on the next approved update.
    for old_bucket_name in ("by_file", "by_project_ref", "by_document_sha256"):
        old_bucket = payload.get(old_bucket_name)
        if not isinstance(old_bucket, dict):
            continue
        for old_key, old_value in list(old_bucket.items()):
            if not isinstance(old_value, dict):
                continue
            clean_history = [
                {key: value for key, value in item.items() if key in allowed_record_keys}
                for item in (old_value.get("history") or []) if isinstance(item, dict)
            ]
            clean_latest = old_value.get("latest")
            if isinstance(clean_latest, dict):
                clean_latest = {
                    key: value for key, value in clean_latest.items()
                    if key in allowed_record_keys
                }
            old_bucket[old_key] = {"latest": clean_latest, "history": clean_history}
    for bucket_name, key, match_type in (
        ("by_file", record["file"], "file"),
        ("by_project_ref", record["project_ref"], "project_ref"),
        ("by_document_sha256", record["document_sha256"], "document_sha256"),
    ):
        bucket = payload.setdefault(bucket_name, {})
        if not key:
            continue
        entry = dict(record, match_type=match_type, match_value=key)
        current = dict(bucket.get(key) or {})
        history = [item for item in (current.get("history") or [])
                   if item.get("job_id") != job_id]
        history.append(entry)
        current["history"] = history
        current["latest"] = max(history, key=lambda item: item.get("approved_at") or "")
        bucket[key] = current
    return payload


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


def prior_approval_for_job(job: dict, patterns: dict[str, Any],
                           exclude_job_id: str | None = None) -> dict[str, Any] | None:
    file_hash = str(job.get("document_sha256") or "").strip().lower()
    by_hash = patterns.get("by_document_sha256") if isinstance(patterns, dict) else {}
    if file_hash and isinstance(by_hash, dict):
        bucket = by_hash.get(file_hash) or {}
        candidates = list(bucket.get("history") or [])
        if bucket.get("latest") and not candidates:
            candidates = [bucket["latest"]]
        candidates.sort(key=lambda item: item.get("approved_at") or "", reverse=True)
        latest = next((item for item in candidates
                       if item.get("job_id") != exclude_job_id), None)
        if latest:
            # The API/UI receives identity and provenance only. Learned measurements,
            # geometry, costing and rates are never copied into a current job payload.
            return {
                "job_id": latest.get("job_id"),
                "approved_at": latest.get("approved_at"),
                "document_sha256": file_hash,
                "matched_on": "exact document SHA-256",
            }
    return None


def attach_prior_approval(job_id: str, job: dict, patterns: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    prior = prior_approval_for_job(job, patterns, exclude_job_id=job_id)
    if prior:
        payload["prior_approval"] = prior
    payload["id"] = payload.get("id") or job_id
    return payload


def analytics_report(jobs: dict[str, dict]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_project: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "deltas": []})
    for job_id, job in iter_approved_jobs(jobs):
        res = job.get("result") or {}
        episode = job.get("learning_episode") if isinstance(job.get("learning_episode"), dict) else {}
        initial = episode.get("initial") if isinstance(episode.get("initial"), dict) else {}
        initial_snapshot = (initial.get("snapshot")
                            if isinstance(initial.get("snapshot"), dict) else {})
        terminal = episode.get("terminal") if isinstance(episode.get("terminal"), dict) else {}
        terminal_snapshot = (terminal.get("snapshot")
                             if isinstance(terminal.get("snapshot"), dict) else {})
        original_available = bool(initial.get("original_available"))
        ai_area = initial_snapshot.get("area_m2") if original_available else None
        assessed = (terminal_snapshot.get("area_m2")
                    if terminal_snapshot else (job.get("adjusted") or {}).get("area_m2"))
        final_area = assessed if isinstance(assessed, (int, float)) else (
            job.get("area_m2") or res.get("area_m2"))
        delta = None
        if isinstance(ai_area, (int, float)) and isinstance(final_area, (int, float)):
            delta = round(float(final_area) - float(ai_area), 3)
        row = {
            "job_id": job_id,
            "file": _job_file(job),
            "project_ref": job.get("project_ref") or res.get("project_ref") or "",
            "project_name": job.get("project_name") or res.get("project_name") or "",
            "approved_at": _job_timestamp(job),
            "area_m2": final_area,
            "ai_area_m2": ai_area,
            "assessed_area_m2": final_area,
            "delta_m2": delta,
            "learning_outcome": terminal.get("event") or None,
            "original_available": original_available,
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
