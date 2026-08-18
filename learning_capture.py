"""Atomic, non-commercial learning episodes stored with assessor jobs."""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any


_SNAPSHOT_KEYS = (
    "file", "page", "type", "source_discipline", "measurement_state", "area_m2",
    "scale_k", "scale_src", "method", "confidence",
    "polygon_pts", "perimeter_lm", "zones", "flags", "adjusted", "cutout_regions",
    "candidate_polygons", "exclusions", "exclusion_prompts", "exclusion_review_required",
    "user_channels", "region_scopes",
    "yard_regions", "yard_region_decisions", "channel_proposals",
    "channel_proposal_decisions", "transition_candidates", "transition_candidate_decisions",
    "accepted_channel_quantities", "accepted_transition_quantities", "brief_spec",
    "brief_specs", "spec_override", "zone_classification_required",
)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def document_sha256(path: str | Path | None) -> str:
    """Hash the uploaded bytes used for takeoff; an unavailable file yields no identity."""
    if not path:
        return ""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def learning_environment() -> str:
    explicit = (os.getenv("LEARNING_ENVIRONMENT") or "").strip().lower()
    if explicit:
        return explicit
    return "production" if os.getenv("RAILWAY_ENVIRONMENT") else "local"


def _without_commercial_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_commercial_values(item)
            for key, item in value.items()
            if key not in {"rate", "costing", "breakdown", "total_gbp", "grand_total_gbp"}
            and not key.endswith("_rate")
        }
    if isinstance(value, list):
        return [_without_commercial_values(item) for item in value]
    return copy.deepcopy(value)


def measurement_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Freeze measurement/review facts only; rates, totals and costing are excluded."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    snapshot: dict[str, Any] = {}
    for key in _SNAPSHOT_KEYS:
        if key in job:
            value = job[key]
        elif key in result:
            value = result[key]
        else:
            continue
        snapshot[key] = _without_commercial_values(value)
    snapshot["status"] = job.get("status")
    snapshot["decision"] = job.get("decision")
    return snapshot


def ensure_learning_episode(
    job_id: str,
    job: dict[str, Any],
    *,
    build: dict[str, Any] | None = None,
    source: str = "pipeline",
    original_available: bool = True,
    pdf_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create the immutable initial side of an episode before the job's first human edit."""
    existing = job.get("learning_episode")
    if isinstance(existing, dict):
        return existing
    file_hash = str(job.get("document_sha256") or document_sha256(pdf_path or job.get("pdf_path")))
    if file_hash:
        job["document_sha256"] = file_hash
    episode = {
        "schema_version": 1,
        "episode_id": str(uuid.uuid4()),
        "job_id": job_id,
        "document_sha256": file_hash,
        "environment": learning_environment(),
        "build": copy.deepcopy(build or {}),
        "initial": {
            "captured_at": utc_now(),
            "source": source,
            "original_available": bool(original_available),
            "snapshot": measurement_snapshot(job),
        },
        "revision": 0,
        "events": [],
        "terminal": None,
    }
    job["learning_episode"] = episode
    return episode


def append_learning_event(
    job_id: str,
    job: dict[str, Any],
    *,
    event_type: str,
    before_job: dict[str, Any],
    details: dict[str, Any] | None = None,
    terminal: bool = False,
    build: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a before/after assessor event to the episode embedded in the atomic job save."""
    episode = job.get("learning_episode")
    if not isinstance(episode, dict):
        # Legacy jobs predate atomic episodes. Freeze the actual pre-action record, not the
        # already-mutated object handed to this function; label provenance unavailable rather
        # than pretending it came from the original pipeline run.
        initial_job = copy.deepcopy(before_job)
        if job.get("document_sha256") and not initial_job.get("document_sha256"):
            initial_job["document_sha256"] = job["document_sha256"]
        episode = ensure_learning_episode(
            job_id,
            initial_job,
            build=build,
            source="legacy_pre_action",
            original_available=False,
            pdf_path=initial_job.get("pdf_path"),
        )
        job["learning_episode"] = copy.deepcopy(episode)
        episode = job["learning_episode"]
        if episode.get("document_sha256"):
            job["document_sha256"] = episode["document_sha256"]
    revision = int(episode.get("revision") or 0) + 1
    before_snapshot = measurement_snapshot(before_job)
    after_snapshot = measurement_snapshot(job)
    changed_keys = {
        key for key in set(before_snapshot) | set(after_snapshot)
        if before_snapshot.get(key) != after_snapshot.get(key)
    }
    event = {
        "event_id": str(uuid.uuid4()),
        "sequence": revision,
        "event": event_type,
        "at": utc_now(),
        "actor": "assessor",
        "before": {key: before_snapshot.get(key) for key in changed_keys},
        "after": {key: after_snapshot.get(key) for key in changed_keys},
        "details": copy.deepcopy(details or {}),
    }
    episode.setdefault("events", []).append(event)
    episode["revision"] = revision
    if terminal:
        episode["terminal"] = {
            "event": event_type,
            "at": event["at"],
            "snapshot": measurement_snapshot(job),
        }
    job["learning_episode"] = episode
    return event
