"""Task Execution Sandbox allocator (Phase 23B).

Additive-only infrastructure
§8 Stage 1/2. Nothing in the existing dispatch path calls this yet --
wiring it into dispatch is Stage 3, explicitly out of scope here.
"""

from __future__ import annotations

import json
import fcntl
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from contextlib import contextmanager

from app.services.workspace.workspace_paths import (
    HYDRATION_EXCLUDED_NAMES,
    RUNTIME_METADATA_FILENAME,
)

RUNTIME_SCHEMA_VERSION = 1

# Phase 22B-1X1: the managed sandbox branch namespace. Only a branch matching
# this exact shape may ever be deleted by sandbox disposal or maintenance --
# an operator branch that merely starts with "orchestrator/task" (say,
# "orchestrator/task-244-manual") is not managed and is never touched.
MANAGED_SANDBOX_BRANCH_PREFIX = "orchestrator/task-"
MANAGED_SANDBOX_BRANCH_PATTERN = re.compile(r"^orchestrator/task-(\d+)$")

# Exact workspace-cleanup states (Phase 22B-1X1 §8). "un-restored" conflates
# file state with Git-ref state; these do not.
WORKSPACE_SANDBOX_NEVER_CREATED = "sandbox_never_created"
WORKSPACE_CANONICAL_UNCHANGED = "canonical_workspace_unchanged"
WORKSPACE_SANDBOX_FULLY_DISPOSED = "sandbox_fully_disposed"
WORKSPACE_FILE_STATE_RESTORED_BRANCH_CLEANUP_INCOMPLETE = (
    "file_state_restored_branch_cleanup_incomplete"
)
WORKSPACE_CLEANUP_INCOMPLETE = "cleanup_incomplete"

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


def managed_sandbox_branch(task_execution_id: int) -> str:
    """The one branch name a sandbox for this TaskExecution may own."""
    return f"{MANAGED_SANDBOX_BRANCH_PREFIX}{int(task_execution_id)}"


def managed_sandbox_branch_execution_id(branch: str) -> Optional[int]:
    """Return the TaskExecution id a managed sandbox branch belongs to."""
    match = MANAGED_SANDBOX_BRANCH_PATTERN.match(str(branch or "").strip())
    return int(match.group(1)) if match else None


def _git(
    project_root: Path, *args: str, timeout: int = 30
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def list_worktree_branches(project_root: Path) -> Dict[str, str]:
    """Map branch name -> worktree path for every registered worktree."""
    result = _git(project_root, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise TaskSandboxError(
            f"git worktree list failed for {project_root}: {result.stderr.strip()}"
        )
    branches: Dict[str, str] = {}
    current_path: Optional[str] = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            if current_path is not None:
                branches[branch] = current_path
    return branches


def branch_exists(project_root: Path, branch: str) -> bool:
    result = _git(
        project_root, "rev-parse", "--quiet", "--verify", f"refs/heads/{branch}"
    )
    return result.returncode == 0


def delete_managed_sandbox_branch(
    project_root: Path,
    branch: str,
    *,
    expected_task_execution_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Delete one managed sandbox branch after proving ownership.

    Returns ``{"deleted": bool, "reason": str}``. Never raises for an
    ordinary safety refusal -- the refusal *is* the evidence. A branch is
    deleted only when it matches the managed pattern exactly, matches the
    expected TaskExecution when one is given, exists, and is not checked out
    by any worktree.
    """
    project_root = Path(project_root).expanduser().resolve()
    execution_id = managed_sandbox_branch_execution_id(branch)
    if execution_id is None:
        return {"deleted": False, "reason": "branch_not_managed", "branch": branch}
    if expected_task_execution_id is not None and execution_id != int(
        expected_task_execution_id
    ):
        return {
            "deleted": False,
            "reason": "branch_owner_mismatch",
            "branch": branch,
        }

    if not branch_exists(project_root, branch):
        return {"deleted": False, "reason": "branch_absent", "branch": branch}

    try:
        checked_out_in = list_worktree_branches(project_root).get(branch)
    except TaskSandboxError as exc:
        return {
            "deleted": False,
            "reason": f"worktree_inspection_failed:{exc}",
            "branch": branch,
        }
    if checked_out_in is not None:
        return {
            "deleted": False,
            "reason": "branch_checked_out",
            "branch": branch,
            "checked_out_in": checked_out_in,
        }

    # -D, not -d: a disposable sandbox branch is unmerged by design. The
    # ownership checks above, not Git's merge heuristic, are the safeguard.
    result = _git(project_root, "branch", "-D", branch)
    if result.returncode != 0:
        return {
            "deleted": False,
            "reason": f"branch_delete_failed:{result.stderr.strip()[:200]}",
            "branch": branch,
        }
    return {"deleted": True, "reason": "branch_deleted", "branch": branch}


@dataclass
class SandboxDisposal:
    """Structured evidence for one sandbox disposal attempt."""

    worktree_removed: bool = False
    worktree_remove_fallback: bool = False
    branch: Optional[str] = None
    branch_deleted: bool = False
    branch_reason: Optional[str] = None
    cleanup_complete: bool = False
    workspace_status: str = WORKSPACE_SANDBOX_NEVER_CREATED
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worktree_removed": self.worktree_removed,
            "worktree_remove_fallback": self.worktree_remove_fallback,
            "branch": self.branch,
            "branch_deleted": self.branch_deleted,
            "branch_reason": self.branch_reason,
            "cleanup_complete": self.cleanup_complete,
            "workspace_status": self.workspace_status,
            **self.details,
        }


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
        branch = managed_sandbox_branch(task_execution_id)
        base_commit = head.stdout.strip()

        with _git_worktree_lock(project_root):
            # Phase 22B-1X1: a retry of this same TaskExecution must not fail
            # deterministically on residue left by the attempt before it.
            # Prune first so a removed-but-unregistered worktree stops
            # claiming the branch, then clear our own branch when nothing
            # references it. Ownership validation is identical to disposal's.
            _git(project_root, "worktree", "prune")
            residue = delete_managed_sandbox_branch(
                project_root, branch, expected_task_execution_id=task_execution_id
            )
            if not residue["deleted"] and residue["reason"] not in {"branch_absent"}:
                raise TaskSandboxError(
                    f"Task Execution Sandbox branch {branch} cannot be re-allocated: "
                    f"{residue['reason']}"
                )
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
) -> SandboxDisposal:
    """Remove a Task Execution Sandbox, worktree and managed branch alike.

    For git-backed sandboxes, pass project_root so `git worktree remove`
    can deregister the worktree from the owning repo; without it (or if
    removal fails), falls back to a raw directory delete plus a
    best-effort `git worktree prune`.

    Phase 22B-1X1: disposal is only complete when the repository can host a
    fresh sandbox for the same TaskExecution again, so after the worktree is
    gone the exact managed branch is deleted under the ownership validation
    in :func:`delete_managed_sandbox_branch`. Idempotent: a repeated call on
    an already disposed sandbox removes nothing and reports the same
    terminal state.
    """
    disposal = SandboxDisposal(branch=sandbox.branch)

    if not sandbox.is_git or project_root is None:
        if sandbox.path.exists():
            shutil.rmtree(sandbox.path, ignore_errors=True)
            disposal.worktree_removed = True
        disposal.cleanup_complete = not sandbox.path.exists()
        disposal.workspace_status = (
            WORKSPACE_SANDBOX_FULLY_DISPOSED
            if disposal.cleanup_complete
            else WORKSPACE_CLEANUP_INCOMPLETE
        )
        return disposal

    project_root = Path(project_root).expanduser().resolve()
    with _git_worktree_lock(project_root):
        if sandbox.path.exists():
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(sandbox.path)],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                disposal.worktree_remove_fallback = True
                disposal.details["worktree_remove_error"] = result.stderr.strip()[:200]
                shutil.rmtree(sandbox.path, ignore_errors=True)
        # Prune unconditionally: an interrupted earlier disposal can leave
        # administrative metadata that still claims the branch.
        _git(project_root, "worktree", "prune")
        disposal.worktree_removed = not sandbox.path.exists()

        if sandbox.branch:
            branch_result = delete_managed_sandbox_branch(
                project_root,
                sandbox.branch,
                expected_task_execution_id=sandbox.task_execution_id,
            )
            disposal.branch_deleted = bool(branch_result["deleted"])
            disposal.branch_reason = branch_result["reason"]
            if branch_result.get("checked_out_in"):
                disposal.details["branch_checked_out_in"] = branch_result[
                    "checked_out_in"
                ]
        else:
            disposal.branch_reason = "branch_absent"

    branch_clean = disposal.branch_deleted or disposal.branch_reason == "branch_absent"
    disposal.cleanup_complete = disposal.worktree_removed and branch_clean
    if disposal.cleanup_complete:
        disposal.workspace_status = WORKSPACE_SANDBOX_FULLY_DISPOSED
    elif disposal.worktree_removed:
        disposal.workspace_status = (
            WORKSPACE_FILE_STATE_RESTORED_BRANCH_CLEANUP_INCOMPLETE
        )
    else:
        disposal.workspace_status = WORKSPACE_CLEANUP_INCOMPLETE
    return disposal
