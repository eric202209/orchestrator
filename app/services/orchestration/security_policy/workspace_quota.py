"""Workspace size and change-set file count limit detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

WORKSPACE_QUOTA_MAX_BYTES: int = 500 * 1024 * 1024  # 500 MB total workspace
WORKSPACE_MAX_CHANGED_FILES: int = 100  # max files changed per task
WORKSPACE_MAX_FILE_WRITE_BYTES: int = 50 * 1024 * 1024  # 50 MB per single write


@dataclass(frozen=True)
class QuotaViolation:
    kind: str  # "total_size" | "changed_files" | "single_write"
    value: int
    limit: int


def check_workspace_size(
    path: Path,
    max_bytes: int = WORKSPACE_QUOTA_MAX_BYTES,
) -> QuotaViolation | None:
    """Return a violation if the directory tree exceeds max_bytes."""
    if not path.exists():
        return None
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if total > max_bytes:
        return QuotaViolation(kind="total_size", value=total, limit=max_bytes)
    return None


def check_change_set_file_count(
    changed_files: Sequence[str],
    max_files: int = WORKSPACE_MAX_CHANGED_FILES,
) -> QuotaViolation | None:
    """Return a violation if the change set touches more files than the limit."""
    count = len(changed_files)
    if count > max_files:
        return QuotaViolation(kind="changed_files", value=count, limit=max_files)
    return None


def check_write_size(
    content: str | bytes,
    max_bytes: int = WORKSPACE_MAX_FILE_WRITE_BYTES,
) -> QuotaViolation | None:
    """Return a violation if a single write content exceeds max_bytes."""
    size = len(content.encode() if isinstance(content, str) else content)
    if size > max_bytes:
        return QuotaViolation(kind="single_write", value=size, limit=max_bytes)
    return None
