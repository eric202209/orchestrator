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
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.workspace.task_sandbox_allocator import (
    MANAGED_SANDBOX_BRANCH_PATTERN,
    TaskSandboxError,
    delete_managed_sandbox_branch,
    managed_branch_commit_evidence,
    list_worktree_branches,
)

INVENTORY_SCHEMA_VERSION = "sandbox-branch-inventory/1.1"

ACTIVE_MANAGED_BRANCH = "ACTIVE_MANAGED_BRANCH"
RETAINED_HISTORICAL_BRANCH = "RETAINED_HISTORICAL_BRANCH"
SAFE_STALE_BRANCH = "SAFE_STALE_BRANCH"
AMBIGUOUS_FAIL_SAFE = "AMBIGUOUS_FAIL_SAFE"
UNRELATED_NAME_COLLISION = "UNRELATED_NAME_COLLISION"

TERMINAL_EXECUTION_STATUSES = {"done", "failed", "cancelled"}
LIVE_EXECUTION_STATUSES = {"pending", "running"}
UNSAFE_CLASSIFICATIONS = {
    ACTIVE_MANAGED_BRANCH,
    AMBIGUOUS_FAIL_SAFE,
    UNRELATED_NAME_COLLISION,
    RETAINED_HISTORICAL_BRANCH,
}


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
    )


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


def _identity(execution: Any) -> Dict[str, Any]:
    task = getattr(execution, "task", None)
    session = getattr(execution, "session", None)
    project = getattr(task, "project", None) if task is not None else None
    return {
        "project": (
            {
                "id": project.id,
                "name": project.name,
                "workspace_path": project.workspace_path,
                "branch": project.branch,
            }
            if project is not None
            else None
        ),
        "session": (
            {"id": session.id, "name": session.name, "status": session.status}
            if session is not None
            else None
        ),
        "task": (
            {
                "id": task.id,
                "title": task.title,
                "status": getattr(getattr(task, "status", None), "value", None)
                or str(getattr(task, "status", None) or ""),
            }
            if task is not None
            else None
        ),
    }


def _classification(
    *,
    branch: str,
    canonical_branch: Optional[str],
    checked_out_in: Optional[str],
    execution: Any,
    execution_status: Optional[str],
    identity_complete: bool,
    database_available: bool,
    unique_commits: Optional[int],
) -> tuple[str, str]:
    if branch == canonical_branch:
        return (
            UNRELATED_NAME_COLLISION,
            "canonical_branch_protected",
        )
    if not database_available:
        return (
            AMBIGUOUS_FAIL_SAFE,
            "task_execution_ownership_unavailable",
        )
    if checked_out_in is not None:
        return ACTIVE_MANAGED_BRANCH, "branch_checked_out"
    if execution is not None and not identity_complete:
        return AMBIGUOUS_FAIL_SAFE, "task_execution_identity_incomplete"
    if execution_status in LIVE_EXECUTION_STATUSES:
        return ACTIVE_MANAGED_BRANCH, "execution_not_terminal"
    if (
        execution_status is not None
        and execution_status not in TERMINAL_EXECUTION_STATUSES
    ):
        return AMBIGUOUS_FAIL_SAFE, "execution_status_unknown"
    if unique_commits is None:
        return AMBIGUOUS_FAIL_SAFE, "branch_reachability_ambiguous"
    if unique_commits > 0:
        if execution is not None:
            return (
                RETAINED_HISTORICAL_BRANCH,
                "unique_commits_retain_implementation_evidence",
            )
        return AMBIGUOUS_FAIL_SAFE, "missing_execution_with_unique_commits"
    return SAFE_STALE_BRANCH, "terminal_or_missing_execution_no_unique_commits"


def inventory_sandbox_branches(
    project_root: Path,
    db: Any = None,
    *,
    proposed_task_execution_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Read-only ownership, reachability, and collision inventory.

    A missing database session is deliberately ambiguous. Git evidence alone
    cannot prove that a matching TaskExecution is absent, so the maintenance
    command remains fail-closed rather than turning a database outage into
    automatic branch deletion.
    """

    project_root = Path(project_root).expanduser().resolve()
    canonical_result = _git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    canonical = canonical_result.stdout.strip() or None
    canonical_sha_result = (
        _git(project_root, "rev-parse", canonical) if canonical else None
    )
    canonical_sha = (
        canonical_sha_result.stdout.strip()
        if canonical_sha_result is not None and canonical_sha_result.returncode == 0
        else None
    )
    managed_branches = _managed_branches(project_root)
    worktree_branches = list_worktree_branches(project_root)
    branches: List[Dict[str, Any]] = []

    executions: Dict[int, Any] = {}
    database_available = db is not None
    max_task_execution_id: Optional[int] = None
    next_task_execution_id: Optional[int] = None
    if database_available:
        from sqlalchemy import func

        from app.models import TaskExecution

        execution_ids = [
            int(MANAGED_SANDBOX_BRANCH_PATTERN.match(name).group(1))
            for name in managed_branches
        ]
        if execution_ids:
            executions = {
                execution.id: execution
                for execution in db.query(TaskExecution)
                .filter(TaskExecution.id.in_(execution_ids))
                .all()
            }
        max_value = db.query(func.max(TaskExecution.id)).scalar()
        max_task_execution_id = int(max_value) if max_value is not None else 0
        next_task_execution_id = max_task_execution_id + 1

    proposed_id = (
        int(proposed_task_execution_id)
        if proposed_task_execution_id is not None
        else next_task_execution_id
    )

    for branch in sorted(managed_branches):
        execution_id = int(MANAGED_SANDBOX_BRANCH_PATTERN.match(branch).group(1))
        checked_out_in = worktree_branches.get(branch)
        execution = executions.get(execution_id)
        execution_status = (
            str(getattr(getattr(execution, "status", None), "value", None) or "")
            or None
            if execution is not None
            else None
        )
        identity = (
            _identity(execution)
            if execution is not None
            else {
                "project": None,
                "session": None,
                "task": None,
            }
        )
        identity_complete = all(identity.values())
        commit_evidence = managed_branch_commit_evidence(project_root, branch)
        unique_commits = commit_evidence.get("unique_commits")
        classification, rationale = _classification(
            branch=branch,
            canonical_branch=canonical,
            checked_out_in=checked_out_in,
            execution=execution,
            execution_status=execution_status,
            identity_complete=identity_complete,
            database_available=database_available,
            unique_commits=unique_commits,
        )
        exact_collision = proposed_id is not None and execution_id == proposed_id
        branches.append(
            {
                "branch": branch,
                "branch_tip_sha": commit_evidence.get("branch_tip_sha"),
                "commit": commit_evidence.get("branch_tip_sha"),
                "task_execution_id": execution_id,
                "task_execution_found": (
                    execution is not None if database_available else None
                ),
                "task_execution_identity": identity,
                "execution_status": execution_status,
                "execution_terminal": (
                    execution_status in TERMINAL_EXECUTION_STATUSES
                    if execution_status is not None
                    else None
                ),
                "worktree_present": checked_out_in is not None,
                "checked_out_in": checked_out_in,
                "canonical_branch": canonical,
                "canonical_sha": commit_evidence.get("canonical_sha", canonical_sha),
                "base_sha": commit_evidence.get("base_sha", canonical_sha),
                "unique_commits": unique_commits,
                "commits_reachable_elsewhere": commit_evidence.get(
                    "reachable_elsewhere"
                ),
                "reachable_elsewhere_refs": commit_evidence.get(
                    "reachable_elsewhere_refs", []
                ),
                "retained_implementation_evidence": bool(
                    unique_commits is not None and unique_commits > 0
                ),
                "can_collide_with_next_execution": exact_collision,
                "can_collide_with_future_execution": exact_collision,
                "exact_collision_unsafe": exact_collision
                and classification in UNSAFE_CLASSIFICATIONS,
                "cleanup_classification": classification,
                "classification_rationale": rationale,
                "safe_to_remove": classification == SAFE_STALE_BRANCH,
                "blocked_reason": (
                    None if classification == SAFE_STALE_BRANCH else rationale
                ),
            }
        )

    classification_counts = dict(
        Counter(item["cleanup_classification"] for item in branches)
    )
    exact_collisions = [
        item for item in branches if item["can_collide_with_next_execution"]
    ]
    unsafe_exact_collisions = [
        item for item in exact_collisions if item["exact_collision_unsafe"]
    ]
    unsafe_count = sum(
        count
        for classification, count in classification_counts.items()
        if classification in UNSAFE_CLASSIFICATIONS
    )

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_root": str(project_root),
        "canonical_branch": canonical,
        "canonical_sha": canonical_sha,
        "observed_at": datetime.now(UTC).isoformat(),
        "count": len(branches),
        "managed_branch_count": len(branches),
        "database_available": database_available,
        "max_task_execution_id": max_task_execution_id,
        "next_task_execution_id": next_task_execution_id,
        "proposed_task_execution_id": proposed_id,
        "classification_counts": classification_counts,
        "unsafe_count": unsafe_count,
        "exact_collision_count": len(exact_collisions),
        "exact_collision_branches": [item["branch"] for item in exact_collisions],
        "unsafe_exact_collision_count": len(unsafe_exact_collisions),
        "unsafe_exact_collision_branches": [
            item["branch"] for item in unsafe_exact_collisions
        ],
        "preflight_admission": (
            "admitted"
            if proposed_id is not None and not unsafe_exact_collisions
            else "ambiguous" if proposed_id is None else "blocked"
        ),
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
        if not item["safe_to_remove"]:
            results.append(
                {
                    "branch": item["branch"],
                    "deleted": False,
                    "reason": item["blocked_reason"],
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
