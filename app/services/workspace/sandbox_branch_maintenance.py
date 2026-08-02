"""Inventory and supported cleanup for managed sandbox branch residue.

Phase 22B-1X1 §6. Disposal before this phase removed the worktree but left
``orchestrator/task-<execution_id>`` behind, so every historically disposed
sandbox left a branch that blocked re-allocation for the same TaskExecution.

Inventory is read-only and always available. Cleanup reuses exactly the same
ownership validation as normal disposal
(:func:`app.services.workspace.task_sandbox_allocator.delete_managed_sandbox_branch`)
so there is one branch-deletion authority, not two.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.workspace.task_sandbox_allocator import (
    MANAGED_SANDBOX_BRANCH_PATTERN,
    TaskSandboxError,
    delete_managed_sandbox_branch,
    list_worktree_branches,
)

INVENTORY_SCHEMA_VERSION = "sandbox-branch-inventory/1.0"


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _canonical_branch(project_root: Path) -> Optional[str]:
    result = _git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _managed_branches(project_root: Path) -> List[str]:
    result = _git(
        project_root, "for-each-ref", "--format=%(refname:short)", "refs/heads"
    )
    if result.returncode != 0:
        raise TaskSandboxError(
            f"git for-each-ref failed for {project_root}: {result.stderr.strip()}"
        )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if MANAGED_SANDBOX_BRANCH_PATTERN.match(line.strip())
    ]


def _unique_commits(
    project_root: Path, branch: str, base: Optional[str]
) -> Optional[int]:
    if not base:
        return None
    result = _git(project_root, "rev-list", "--count", f"{base}..{branch}")
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def inventory_sandbox_branches(project_root: Path, db: Any = None) -> Dict[str, Any]:
    """Read-only inventory of every managed sandbox branch in a repository."""

    project_root = Path(project_root).expanduser().resolve()
    canonical = _canonical_branch(project_root)
    worktree_branches = list_worktree_branches(project_root)
    branches: List[Dict[str, Any]] = []

    executions: Dict[int, Any] = {}
    if db is not None:
        from app.models import TaskExecution

        execution_ids = [
            int(MANAGED_SANDBOX_BRANCH_PATTERN.match(name).group(1))
            for name in _managed_branches(project_root)
        ]
        if execution_ids:
            executions = {
                execution.id: execution
                for execution in db.query(TaskExecution)
                .filter(TaskExecution.id.in_(execution_ids))
                .all()
            }

    for branch in sorted(_managed_branches(project_root)):
        execution_id = int(MANAGED_SANDBOX_BRANCH_PATTERN.match(branch).group(1))
        commit = _git(project_root, "rev-parse", branch).stdout.strip() or None
        checked_out_in = worktree_branches.get(branch)
        execution = executions.get(execution_id)
        execution_status = (
            str(getattr(getattr(execution, "status", None), "value", None) or "")
            or None
            if execution is not None
            else None
        )
        unique_commits = _unique_commits(project_root, branch, canonical)
        safe_to_remove = checked_out_in is None and branch != canonical
        branches.append(
            {
                "branch": branch,
                "commit": commit,
                "task_execution_id": execution_id,
                "task_execution_found": (
                    execution is not None if db is not None else None
                ),
                "execution_status": execution_status,
                "worktree_present": checked_out_in is not None,
                "checked_out_in": checked_out_in,
                "unique_commits": unique_commits,
                "safe_to_remove": safe_to_remove,
                "blocked_reason": (
                    None
                    if safe_to_remove
                    else (
                        "branch_checked_out" if checked_out_in else "canonical_branch"
                    )
                ),
            }
        )

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_root": str(project_root),
        "canonical_branch": canonical,
        "observed_at": datetime.now(UTC).isoformat(),
        "count": len(branches),
        "safe_to_remove_count": sum(1 for item in branches if item["safe_to_remove"]),
        "branches": branches,
    }


def cleanup_sandbox_branches(
    project_root: Path,
    *,
    branches: Optional[List[str]] = None,
    db: Any = None,
    apply: bool = False,
) -> Dict[str, Any]:
    """Delete managed sandbox residue through the canonical validation path.

    ``apply=False`` (the default) reports exactly what would happen and
    mutates nothing, so the before/after ledger can be captured around an
    operator-owned cleanup.
    """

    project_root = Path(project_root).expanduser().resolve()
    before = inventory_sandbox_branches(project_root, db=db)
    selected = (
        [item for item in before["branches"] if item["branch"] in set(branches)]
        if branches is not None
        else list(before["branches"])
    )

    results: List[Dict[str, Any]] = []
    for item in selected:
        if not apply:
            results.append(
                {
                    "branch": item["branch"],
                    "deleted": False,
                    "reason": (
                        "would_delete"
                        if item["safe_to_remove"]
                        else item["blocked_reason"]
                    ),
                }
            )
            continue
        results.append(
            delete_managed_sandbox_branch(
                project_root,
                item["branch"],
                expected_task_execution_id=item["task_execution_id"],
            )
        )

    after = inventory_sandbox_branches(project_root, db=db) if apply else before
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_root": str(project_root),
        "applied": apply,
        "requested": [item["branch"] for item in selected],
        "results": results,
        "deleted_count": sum(1 for result in results if result.get("deleted")),
        "before": before,
        "after": after,
    }
