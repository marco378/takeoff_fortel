"""Persistent client-editable pricing inputs, layered above Fortel's defaults.

Only values that differ from the current built-in defaults are stored.  Callers provide
their already-resolved default/spec values, then this module overlays the saved client
values before the existing pricing functions are called.  It deliberately contains no
default rate values and no pricing calculation.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import threading
from pathlib import Path


RATE_FIELDS = {
    "conc_rate": {
        "label": "Concrete rate", "unit": "£/m³",
        "help": "Concrete supply input used by the existing slab build-up.",
    },
    "steel_rate_t": {
        "label": "Steel rate", "unit": "£/tonne",
        "help": "Reinforcement steel input used by the existing slab build-up.",
    },
    "margin": {
        "label": "Margin", "unit": "fraction",
        "help": "Enter 0.11 for 11%.",
    },
    "labour": {"label": "Labour", "unit": "£/m²"},
    "dpm": {"label": "DPM", "unit": "£/m²"},
    "curing": {"label": "Curing compound", "unit": "£/m²"},
    "trim": {"label": "Final trim", "unit": "£/m²"},
    "conc_wastage": {
        "label": "Concrete wastage", "unit": "fraction",
        "help": "Enter 0.03 for 3%.",
    },
    "steel_wastage": {
        "label": "Steel wastage", "unit": "fraction",
        "help": "Enter 0.15 for 15%.",
    },
    "lap_acc": {
        "label": "Laps + accessories", "unit": "fraction",
        "help": "Enter 0.18 for 18%.",
    },
    "manhole_eo_rate": {"label": "Manhole E/O rate", "unit": "£/Nr"},
}

_rates_lock = threading.Lock()


class ClientRatesError(ValueError):
    """The persisted override store or a submitted value is invalid."""


def rates_path_for_jobs(jobs_file: str | Path) -> Path:
    """Keep client rates beside the selected jobs store (including Railway volumes)."""
    return Path(jobs_file).parent / "client_rates.json"


def default_rates_path() -> Path:
    """Resolve the same volume-aware directory convention used by approval_server."""
    explicit_jobs = os.getenv("JOBS_FILE")
    volume_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if explicit_jobs:
        jobs_file = Path(explicit_jobs)
    elif volume_dir:
        jobs_file = Path(volume_dir) / "approval_jobs.json"
    else:
        jobs_file = Path(__file__).with_name("approval_jobs.json")
    return rates_path_for_jobs(jobs_file)


def _empty_store() -> dict:
    return {"version": 0, "updated_at": None, "overrides": {}, "audit": []}


def _number(field: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClientRatesError(f"{field} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ClientRatesError(f"{field} must be a finite non-negative number")
    if field in {"margin", "conc_wastage", "steel_wastage", "lap_acc"} and value > 1:
        raise ClientRatesError(f"{field} must be a fraction between 0 and 1")
    return value


def load_rate_store(path: str | Path | None = None) -> dict:
    """Load and validate the store.  An absent file is the exact empty/default state."""
    path = Path(path or default_rates_path())
    if not path.exists():
        return _empty_store()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientRatesError(f"client rates store is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ClientRatesError("client rates store must be a JSON object")
    version = raw.get("version", 0)
    overrides = raw.get("overrides", {})
    audit = raw.get("audit", [])
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ClientRatesError("client rates version must be a non-negative integer")
    if not isinstance(overrides, dict) or not isinstance(audit, list):
        raise ClientRatesError("client rates overrides/audit have invalid structure")
    unknown = sorted(set(overrides) - set(RATE_FIELDS))
    if unknown:
        raise ClientRatesError(f"unknown client rate field(s): {', '.join(unknown)}")
    clean_overrides = {field: _number(field, value) for field, value in overrides.items()}
    return {
        "version": version,
        "updated_at": raw.get("updated_at"),
        "overrides": clean_overrides,
        "audit": audit,
    }


def apply_client_rates(spec: dict, manhole_rate: float, *, path: str | Path | None = None,
                       manhole_in_scope: bool = True):
    """Overlay saved values and return ``(spec, manhole_rate, provenance)``.

    With no store, or with an empty override map, the returned spec is value-identical and
    provenance is empty so legacy costing dictionaries remain byte-for-byte unchanged.
    """
    store = load_rate_store(path)
    overrides = store["overrides"]
    effective_spec = dict(spec)
    for field, value in overrides.items():
        if field != "manhole_eo_rate":
            effective_spec[field] = value
    effective_manhole_rate = overrides.get("manhole_eo_rate", manhole_rate)
    applied_fields = [field for field in overrides
                      if field != "manhole_eo_rate" or manhole_in_scope]
    provenance = {}
    if applied_fields:
        provenance = {
            "client_rates_applied": True,
            "rates_version": store["version"],
            "rates_updated_at": store.get("updated_at"),
            "client_rate_fields": applied_fields,
        }
    return effective_spec, effective_manhole_rate, provenance


def effective_rate_payload(default_values: dict, *, path: str | Path | None = None) -> dict:
    """Presentation payload containing every effective value and its provenance."""
    missing = sorted(set(RATE_FIELDS) - set(default_values))
    if missing:
        raise ClientRatesError(f"missing default value(s): {', '.join(missing)}")
    store = load_rate_store(path)
    overrides = store["overrides"]
    latest_audit = {}
    for entry in store["audit"]:
        if isinstance(entry, dict) and entry.get("field") in RATE_FIELDS:
            latest_audit[entry["field"]] = entry
    fields = []
    for key, metadata in RATE_FIELDS.items():
        client_edited = key in overrides
        field_payload = {
            "key": key,
            **metadata,
            "value": overrides[key] if client_edited else float(default_values[key]),
            "default_value": float(default_values[key]),
            "provenance": "CLIENT-EDITED" if client_edited else "DEFAULT",
        }
        if client_edited and key in latest_audit:
            field_payload["version"] = latest_audit[key].get("version")
            field_payload["updated_at"] = latest_audit[key].get("when")
        fields.append(field_payload)
    return {
        "version": store["version"],
        "updated_at": store.get("updated_at"),
        "fields": fields,
        "audit_count": len(store["audit"]),
    }


def save_client_rates(values: dict, default_values: dict, *, path: str | Path | None = None,
                      who: str = "assessor", when: str | None = None) -> tuple[dict, list]:
    """Atomically save changed effective values and append one audit row per field.

    Values equal to the built-in default remove that override, restoring the untouched
    default flow for that field.  A no-op submission does not count as a save/version.
    """
    if not isinstance(values, dict) or not values:
        raise ClientRatesError("rates must be a non-empty object")
    unknown = sorted(set(values) - set(RATE_FIELDS))
    if unknown:
        raise ClientRatesError(f"unknown client rate field(s): {', '.join(unknown)}")
    missing_defaults = sorted(set(RATE_FIELDS) - set(default_values))
    if missing_defaults:
        raise ClientRatesError(f"missing default value(s): {', '.join(missing_defaults)}")

    path = Path(path or default_rates_path())
    with _rates_lock:
        store = load_rate_store(path)
        overrides = dict(store["overrides"])
        changes = []
        for field, submitted in values.items():
            new_value = _number(field, submitted)
            default_value = _number(field, default_values[field])
            old_value = overrides.get(field, default_value)
            if new_value == old_value:
                continue
            if new_value == default_value:
                overrides.pop(field, None)
            else:
                overrides[field] = new_value
            changes.append({"field": field, "old": old_value, "new": new_value})

        if not changes:
            return store, []

        version = store["version"] + 1
        timestamp = when or datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit = list(store["audit"])
        audit.extend({
            "who": who,
            "when": timestamp,
            "field": change["field"],
            "old": change["old"],
            "new": change["new"],
            "version": version,
        } for change in changes)
        saved = {
            "version": version,
            "updated_at": timestamp,
            "overrides": overrides,
            "audit": audit,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(saved, indent=2))
        os.replace(tmp, path)
        return saved, changes


__all__ = [
    "ClientRatesError", "RATE_FIELDS", "apply_client_rates", "default_rates_path",
    "effective_rate_payload", "load_rate_store", "rates_path_for_jobs", "save_client_rates",
]
