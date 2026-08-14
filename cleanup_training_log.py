#!/usr/bin/env python3
"""One-time cleanup for historical training_log.jsonl contamination.

This script removes known CI / fixture / synthetic entries while preserving real assessor
history. It is intentionally conservative: only entries that match explicit fixture patterns
or clearly synthetic test project prefixes are removed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Iterable


KNOWN_FILE_EXACT = {
    "csrf_test.pdf",
    "csrf_test2.pdf",
    "Raw External.pdf",
    "Mixed perimeter case.pdf",
    "Office-GA.pdf",
    "External Markup Unit-9.pdf",
    "c123_1st_Floor.pdf",
}

KNOWN_JOB_EXACT = {
    "job-pending-1",
    "quotation-failure-visible",
    "persist-1",
}

KNOWN_JOB_PREFIXES = (
    "job-csrf-",
)

KNOWN_PROJECT_PREFIXES = (
    "QA-",
    "TEST-",
    "CASE-",
    "QUOTE-",
    "E2E-",
    "MULTI-",
    "PERSIST-",
    "FLOW",
    "IDENT-",
)

KNOWN_FILE_CONTAINS = (
    "E2E-ZONE-001",
)

KNOWN_JOB_IDS = {
    "55555555-5555-4555-8555-555555555555",
    "99999999-9999-4999-8999-999999999999",
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "ec0bd264-e2fc-41e4-a551-743d742b3490",
    "60688c6b-9329-4404-91f9-7a5cf516955a",
}


def _entry_value(entry: dict, *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def is_fixture_entry(entry: dict) -> tuple[bool, str]:
    job_id = _entry_value(entry, "job_id")
    file_name = _entry_value(entry, "file")
    project_ref = _entry_value(entry, "project_ref")
    environment = _entry_value(entry, "environment").lower()

    if job_id in KNOWN_JOB_IDS or job_id in KNOWN_JOB_EXACT:
        return True, f"job_id={job_id}"
    if any(job_id.startswith(prefix) for prefix in KNOWN_JOB_PREFIXES):
        return True, f"job_id prefix={job_id}"
    # Common-looking filenames and project refs can be legitimate client data. Only use
    # those broad signals when the producer explicitly labelled the event non-production,
    # or when both independent fixture signals agree.
    file_signal = (file_name in KNOWN_FILE_EXACT
                   or any(snippet in file_name for snippet in KNOWN_FILE_CONTAINS))
    project_signal = any(project_ref.startswith(prefix) for prefix in KNOWN_PROJECT_PREFIXES)
    if environment in {"test", "ci"} and (file_signal or project_signal):
        return True, f"environment={environment}; fixture filename/project"

    return False, ""


def clean_entries(lines: Iterable[str]) -> tuple[list[str], list[tuple[str, str]]]:
    kept: list[str] = []
    removed: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A malformed historical line may still be the only evidence of a real action.
            # Preserve it for a human repair pass instead of deleting it as "test noise".
            kept.append(line)
            continue
        if not isinstance(entry, dict):
            kept.append(line)
            continue
        drop, reason = is_fixture_entry(entry)
        if drop:
            removed.append((_entry_value(entry, "job_id", "file", "event"), reason))
            continue
        kept.append(json.dumps(entry, ensure_ascii=False))
    return kept, removed


def backup_path_for(path: Path) -> Path:
    stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S.%fZ")
    return path.with_name(f"{path.name}.bak-{stamp}-{uuid.uuid4().hex}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean historical training_log.jsonl noise")
    parser.add_argument("path", nargs="?", type=Path, default=Path("training_log.jsonl"))
    parser.add_argument("--apply", action="store_true", help="Write the cleaned file")
    parser.add_argument("--backup", type=Path, default=None, help="Backup path override")
    args = parser.parse_args(argv)

    path = args.path
    if not path.exists():
        raise SystemExit(f"{path} does not exist")

    kept, removed = clean_entries(path.read_text().splitlines())
    print(f"kept={len(kept)} removed={len(removed)}")
    for label, reason in removed[:20]:
        print(f"  - {label} ({reason})")
    if len(removed) > 20:
        print(f"  ... and {len(removed) - 20} more")

    if not args.apply:
        return 0

    backup = args.backup or backup_path_for(path)
    if backup.exists():
        raise SystemExit(f"refusing to overwrite existing backup: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    if backup.read_bytes() != path.read_bytes():
        raise SystemExit("backup verification failed; active log was not modified")
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
    os.replace(tmp, path)
    print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
