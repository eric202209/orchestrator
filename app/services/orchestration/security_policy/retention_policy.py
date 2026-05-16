"""Snapshot and archive retention enforcement."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

SNAPSHOT_MAX_COUNT: int = 20
SNAPSHOT_MAX_AGE_DAYS: int = 30


@dataclass
class RetentionResult:
    scanned: int
    removed: int
    errors: list[str] = field(default_factory=list)


def enforce_snapshot_retention(
    snapshot_dir: Path,
    max_count: int = SNAPSHOT_MAX_COUNT,
    max_age_days: int = SNAPSHOT_MAX_AGE_DAYS,
) -> RetentionResult:
    """Remove oldest snapshot subdirectories that exceed max_count or max_age_days.

    Operates on immediate subdirectories of snapshot_dir only.
    Errors on individual entries are collected rather than raised.
    """
    if not snapshot_dir.exists():
        return RetentionResult(scanned=0, removed=0)

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    entries = sorted(
        [d for d in snapshot_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )
    scanned = len(entries)
    removed = 0
    errors: list[str] = []
    removed_entries: set[Path] = set()

    # Remove by age
    for entry in entries:
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            try:
                shutil.rmtree(entry)
                removed += 1
                removed_entries.add(entry)
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")

    # Remove oldest to satisfy count limit
    remaining = [e for e in entries if e not in removed_entries and e.exists()]
    excess_count = max(0, len(remaining) - max_count)
    for entry in remaining[:excess_count]:
        try:
            shutil.rmtree(entry)
            removed += 1
        except Exception as exc:
            errors.append(f"{entry.name}: {exc}")

    return RetentionResult(scanned=scanned, removed=removed, errors=errors)
