"""Project-scoped mutation locks for canonical-root write operations."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


class ProjectMutationLockError(RuntimeError):
    def __init__(self, *, project_id: int, operation: str, lock_path: Path):
        self.project_id = project_id
        self.operation = operation
        self.lock_path = lock_path
        super().__init__(
            "Project already has active canonical-root writer/execution in progress. "
            f"Wait for the current writer to finish, then retry. "
            f"project_id={project_id} operation={operation} lock_path={lock_path}"
        )


@contextmanager
def project_mutation_lock(
    *,
    project_id: int,
    project_root: Path,
    operation: str,
    owner: Optional[str] = None,
    stale_after_seconds: int = 60 * 60 * 6,
    wait_timeout_seconds: float = 2.0,
    poll_interval_seconds: float = 0.1,
) -> Iterator[Path]:
    lock_dir = project_root / ".openclaw" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.chmod(0o777)
    except PermissionError:
        # Windows-mounted project folders can reject chmod even when the
        # directory is writable. The atomic lock file creation below is the
        # authority; chmod is only a permissive-mode best effort.
        pass
    lock_path = lock_dir / f"project-{project_id}.mutation.lock"
    token = str(uuid.uuid4())
    now = time.time()

    if lock_path.exists():
        try:
            metadata = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
            created_at = float(metadata.get("created_at_epoch") or 0)
        except (ValueError, OSError, json.JSONDecodeError):
            created_at = 0
        if created_at and now - created_at > stale_after_seconds:
            lock_path.unlink(missing_ok=True)

    metadata = {
        "project_id": project_id,
        "operation": operation,
        "owner": owner,
        "token": token,
        "created_at_epoch": now,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
    while True:
        try:
            fd = os.open(lock_path, flags, 0o666)
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise ProjectMutationLockError(
                    project_id=project_id,
                    operation=operation,
                    lock_path=lock_path,
                ) from exc
            time.sleep(max(0.01, poll_interval_seconds))

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle)
    try:
        lock_path.chmod(0o666)
    except OSError:
        pass

    try:
        yield lock_path
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            lock_path.unlink(missing_ok=True)
