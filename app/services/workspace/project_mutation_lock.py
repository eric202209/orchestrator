"""Project-scoped mutation locks for canonical-root write operations."""

from __future__ import annotations

import json
import hashlib
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


def _lock_path_for_project_root(project_root: Path) -> Path:
    resolved_root = project_root.resolve()
    workspace_key = hashlib.sha256(str(resolved_root).encode("utf-8")).hexdigest()[:16]
    return (
        resolved_root / ".agent" / "locks" / f"workspace-{workspace_key}.mutation.lock"
    )


def _pid_is_alive(pid: object) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


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
    project_root = project_root.resolve()
    lock_path = _lock_path_for_project_root(project_root)
    lock_dir = lock_path.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.chmod(0o777)
    except (PermissionError, FileNotFoundError):
        # Windows-mounted project folders can reject chmod even when the
        # directory is writable. A releasing concurrent writer can also remove
        # the empty lock directory after mkdir and before chmod. The atomic
        # lock file creation below recreates it when needed and is the
        # authority; chmod is only a permissive-mode best effort.
        pass
    token = str(uuid.uuid4())
    now = time.time()

    if lock_path.exists():
        try:
            metadata = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
            created_at = float(metadata.get("created_at_epoch") or 0)
        except (ValueError, OSError, json.JSONDecodeError):
            created_at = 0
        lock_owner_dead = "pid" in metadata and not _pid_is_alive(metadata["pid"])
        if lock_owner_dead or (created_at and now - created_at > stale_after_seconds):
            lock_path.unlink(missing_ok=True)

    metadata = {
        "project_id": project_id,
        "operation": operation,
        "owner": owner,
        "token": token,
        "pid": os.getpid(),
        "resolved_project_root": str(project_root),
        "created_at_epoch": now,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
    while True:
        try:
            fd = os.open(lock_path, flags, 0o666)
            break
        except FileNotFoundError:
            lock_dir.mkdir(parents=True, exist_ok=True)
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
        for path in (lock_dir, lock_dir.parent):
            try:
                path.rmdir()
            except OSError:
                pass
