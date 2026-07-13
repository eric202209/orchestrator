"""Task Execution Sandbox allocator (Phase 23B).

Additive-only infrastructure
§8 Stage 1/2. Nothing in the existing dispatch path calls this yet --
wiring it into dispatch is Stage 3, explicitly out of scope here.
"""

from __future__ import annotations

import json
import fcntl
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from contextlib import contextmanager

from app.services.workspace.workspace_paths import (
    HYDRATION_EXCLUDED_NAMES,
    RUNTIME_METADATA_FILENAME,
)

RUNTIME_SCHEMA_VERSION = 1

VALID_RUNTIME_STATES = {
    "allocated",
    "running",
    "completed",
    "failed",
    "applied",
    "discarded",
}


class TaskSandboxError(Exception):
    """Raised when a Task Execution Sandbox cannot be allocated or disposed."""


@dataclass
class TaskSandbox:
    path: Path
    project_id: int
    task_execution_id: int
    executor: str
    is_git: bool
    branch: Optional[str] = None

    @property
    def metadata_path(self) -> Path:
        return self.path / RUNTIME_METADATA_FILENAME

    def read_metadata(self) -> Dict[str, Any]:
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def write_metadata(self, metadata: Dict[str, Any]) -> None:
        self.metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def update_runtime_state(self, runtime_state: str) -> None:
        if runtime_state not in VALID_RUNTIME_STATES:
            raise TaskSandboxError(f"Invalid runtime_state: {runtime_state}")
        metadata = self.read_metadata()
        metadata["runtime_state"] = runtime_state
        self.write_metadata(metadata)


def runtime_task_dir(
    runtime_root: Path, project_id: int, task_execution_id: int
) -> Path:
    """Pure path math: where a given task's sandbox lives under runtime_root."""
    return runtime_root / "tasks" / str(project_id) / str(task_execution_id)


def _is_git_repo(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _build_metadata(
    *,
    project_id: int,
    task_execution_id: int,
    executor: str,
    base_commit: Optional[str],
    runtime_state: str = "allocated",
) -> Dict[str, Any]:
    return {
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "project_id": project_id,
        "task_execution_id": task_execution_id,
        "executor": executor,
        "created_at": datetime.now(UTC).isoformat(),
        "base_commit": base_commit,
        "runtime_state": runtime_state,
    }


def _copy_project_tree(project_root: Path, destination: Path) -> None:
    def _ignore(_dirpath: str, names: List[str]) -> Set[str]:
        return {name for name in names if name in HYDRATION_EXCLUDED_NAMES}

    shutil.copytree(project_root, destination, ignore=_ignore, dirs_exist_ok=True)


@contextmanager
def _git_worktree_lock(project_root: Path):
    """Serialize Git worktree administration for one repository.

    Git updates the shared ``.git/worktrees`` administrative directory during
    both add and remove operations. A process-local lock is insufficient here
    because Celery workers and CI jobs may use separate processes, so use an
    advisory lock file in the repository's common Git directory.
    """
    common_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    common_dir = Path(common_dir_result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (project_root / common_dir).resolve()
    lock_path = common_dir / "orchestrator-worktree.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def allocate_task_sandbox(
    project_root: Path,
    *,
    project_id: int,
    task_execution_id: int,
    executor: str = "openclaw",
    runtime_root: Optional[Path] = None,
) -> TaskSandbox:
    """Allocate a Task Execution Sandbox for one task.

    git worktree add for git-backed project_root, plain filtered copy
    otherwise. Not called by any execution path yet.
    """
    project_root = Path(project_root).expanduser().resolve()
    if not project_root.is_dir():
        raise TaskSandboxError(
            f"project_root does not exist or is not a directory: {project_root}"
        )

    if runtime_root is None:
        from app.services.workspace.system_settings import get_effective_runtime_root

        runtime_root = get_effective_runtime_root()
    runtime_root = Path(runtime_root).expanduser().resolve()

    sandbox_dir = runtime_task_dir(runtime_root, project_id, task_execution_id)
    if sandbox_dir.exists():
        raise TaskSandboxError(
            f"Task Execution Sandbox already allocated at {sandbox_dir} "
            f"(project_id={project_id}, task_execution_id={task_execution_id})"
        )
    sandbox_dir.parent.mkdir(parents=True, exist_ok=True)

    is_git = _is_git_repo(project_root)
    base_commit: Optional[str] = None
    branch: Optional[str] = None

    if is_git:
        head = subprocess.run(
            ["git", "rev-parse", "--quiet", "--verify", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if head.returncode != 0:
            # Freshly initialized repo with no commits yet (unborn HEAD):
            # there is no base commit to anchor a worktree on, so use the
            # same filtered-copy sandbox that non-git projects get.
            is_git = False

    if is_git:
        branch = f"orchestrator/task-{task_execution_id}"
        base_commit = head.stdout.strip()

        with _git_worktree_lock(project_root):
            result = subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(sandbox_dir),
                    base_commit,
                ],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        if result.returncode != 0:
            raise TaskSandboxError(
                f"git worktree add failed for {project_root} -> {sandbox_dir}: "
                f"{result.stderr.strip()}"
            )
    else:
        try:
            _copy_project_tree(project_root, sandbox_dir)
        except OSError as exc:
            raise TaskSandboxError(
                f"Failed to copy project tree {project_root} -> {sandbox_dir}: {exc}"
            ) from exc

    metadata = _build_metadata(
        project_id=project_id,
        task_execution_id=task_execution_id,
        executor=executor,
        base_commit=base_commit,
    )

    sandbox = TaskSandbox(
        path=sandbox_dir,
        project_id=project_id,
        task_execution_id=task_execution_id,
        executor=executor,
        is_git=is_git,
        branch=branch,
    )
    sandbox.write_metadata(metadata)
    return sandbox


def dispose_task_sandbox(
    sandbox: TaskSandbox, *, project_root: Optional[Path] = None
) -> None:
    """Remove a Task Execution Sandbox.

    For git-backed sandboxes, pass project_root so `git worktree remove`
    can deregister the worktree from the owning repo; without it (or if
    removal fails), falls back to a raw directory delete plus a
    best-effort `git worktree prune`.
    """
    if not sandbox.path.exists():
        return

    if sandbox.is_git and project_root is not None:
        project_root = Path(project_root).expanduser().resolve()
        with _git_worktree_lock(project_root):
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(sandbox.path)],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                shutil.rmtree(sandbox.path, ignore_errors=True)
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        return

    shutil.rmtree(sandbox.path, ignore_errors=True)
