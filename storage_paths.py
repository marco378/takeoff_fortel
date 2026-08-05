"""Single source of truth for Fortel's persistent server-side paths.

Railway's application filesystem is ephemeral.  When a volume is mounted, every artifact
needed to resume an assessment must therefore live below that mount: the job store, uploaded
drawings, generated quotations, archives, and backups.  Explicit per-path environment
overrides always win; without a volume local development retains the repository-local paths.
"""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class StoragePaths:
    jobs_file: Path
    jobs_archive_file: Path
    backup_dir: Path
    drawings_dir: Path
    quotations_dir: Path


def resolve_storage_paths(environ: Mapping[str, str] | None = None,
                          app_dir: str | Path | None = None) -> StoragePaths:
    """Resolve storage paths without touching disk, so startup behaviour is testable.

    Precedence is explicit path override, then ``RAILWAY_VOLUME_MOUNT_PATH``, then the
    application directory.  Archive/backup naming preserves the existing isolation rule for
    custom ``JOBS_FILE`` stems.
    """
    env = os.environ if environ is None else environ
    root = Path(app_dir) if app_dir is not None else Path(__file__).parent
    volume_value = str(env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    default_base = Path(volume_value) if volume_value else root

    jobs_file = Path(env.get("JOBS_FILE") or (default_base / "approval_jobs.json"))
    storage_base = jobs_file.parent
    archive_default = storage_base / f"{jobs_file.stem}_archive.json"
    backup_name = "backups" if jobs_file.stem == "approval_jobs" else f"backups_{jobs_file.stem}"

    return StoragePaths(
        jobs_file=jobs_file,
        jobs_archive_file=Path(env.get("JOBS_ARCHIVE_FILE") or archive_default),
        backup_dir=Path(env.get("BACKUP_DIR") or (storage_base / backup_name)),
        drawings_dir=Path(env.get("DRAWINGS_DIR") or (storage_base / "drawings")),
        quotations_dir=Path(env.get("QUOTATIONS_DIR") or (storage_base / "quotations")),
    )
