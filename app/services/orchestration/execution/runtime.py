"""Runtime support helpers for orchestration state and workspace management."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.permissions import ensure_shared_permissions
from app.services.tasks.service import TaskService
from app.services.workspace.task_sandbox_allocator import (
    TaskSandbox,
    allocate_task_sandbox,
    dispose_task_sandbox,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)


def get_state_manager_path(project_root: Path) -> Path:
    return project_root / ".agent" / "state_manager.json"


def build_project_state_snapshot(
    db: Session,
    project: Optional[Project],
    current_task: Optional[Task],
    session_id: Optional[int],
) -> Dict[str, Any]:
    if not project:
        return {
            "project_id": None,
            "project_name": None,
            "session_id": session_id,
            "status": "unknown",
            "updated_at": datetime.utcnow().isoformat(),
            "tasks": [],
        }

    task_service = TaskService(db)
    ordered_tasks = task_service.get_project_tasks(project.id)
    inconsistent_pairs = []
    plan_groups: dict[int | str, list[Task]] = {}
    for task in ordered_tasks:
        plan_key = task.plan_id if task.plan_id is not None else "legacy"
        plan_groups.setdefault(plan_key, []).append(task)

    for plan_key, plan_tasks in plan_groups.items():
        highest_incomplete_position = None
        for task in plan_tasks:
            if task.plan_position is None:
                continue
            if task.status != TaskStatus.DONE:
                highest_incomplete_position = task.plan_position
                break

        if highest_incomplete_position is not None:
            for task in plan_tasks:
                if (
                    task.plan_position is not None
                    and task.plan_position > highest_incomplete_position
                    and task.status == TaskStatus.DONE
                ):
                    inconsistent_pairs.append(
                        {
                            "task_id": task.id,
                            "plan_id": None if plan_key == "legacy" else plan_key,
                            "plan_position": task.plan_position,
                            "title": task.title,
                        }
                    )

    failed_or_cancelled = [
        task
        for task in ordered_tasks
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
    ]
    overall_status = "ready"
    if failed_or_cancelled or inconsistent_pairs:
        overall_status = "unsynced"
    elif any(task.status == TaskStatus.RUNNING for task in ordered_tasks):
        overall_status = "running"
    elif any(task.status == TaskStatus.PENDING for task in ordered_tasks):
        overall_status = "pending"

    return {
        "project_id": project.id,
        "project_name": project.name,
        "session_id": session_id,
        "current_task_id": current_task.id if current_task else None,
        "current_task_title": current_task.title if current_task else None,
        "status": overall_status,
        "updated_at": datetime.utcnow().isoformat(),
        "failed_or_cancelled_task_ids": [task.id for task in failed_or_cancelled],
        "inconsistent_completed_tasks": inconsistent_pairs,
        "tasks": [
            {
                "task_id": task.id,
                "title": task.title,
                "plan_id": getattr(task, "plan_id", None),
                "plan_position": task.plan_position,
                "status": task.status.value,
                "workspace_status": getattr(task, "workspace_status", None),
                "task_subfolder": getattr(task, "task_subfolder", None),
            }
            for task in ordered_tasks
        ],
    }


def write_project_state_snapshot(
    db: Session,
    project: Optional[Project],
    current_task: Optional[Task],
    session_id: Optional[int],
) -> None:
    if not project:
        return
    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    state_path = get_state_manager_path(project_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_shared_permissions(state_path.parent)
    payload = build_project_state_snapshot(db, project, current_task, session_id)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ensure_shared_permissions(state_path)


def workspace_snapshot_key(
    task_id: int, task_execution_id: Optional[int] = None
) -> str:
    if task_execution_id is not None:
        return f"task-{task_id}-execution-{task_execution_id}-pre-run"
    return f"task-{task_id}-pre-run"


def snapshot_workspace_before_run(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    task_execution_id: Optional[int] = None,
    preserve_project_root_rules: bool,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.create_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=workspace_snapshot_key(task_id, task_execution_id),
        preserve_project_root_rules=preserve_project_root_rules,
    )


def restore_workspace_after_abort(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    task_execution_id: Optional[int] = None,
    preserve_project_root_rules: bool,
    lock_already_held: bool = False,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.restore_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=workspace_snapshot_key(task_id, task_execution_id),
        preserve_project_root_rules=preserve_project_root_rules,
        skip_lock=lock_already_held,
    )


def extract_missing_path_from_error(error_message: str) -> Optional[str]:
    import re

    match = re.search(r"access '([^']+)'", str(error_message or ""))
    if match:
        return match.group(1)
    return None


def build_workspace_discovery_step(
    step: Dict[str, Any],
    project_dir: Path,
    error_message: str,
) -> Dict[str, Any]:
    missing_path = extract_missing_path_from_error(error_message or "")
    filename_hint = Path(missing_path).name if missing_path else ""
    targeted_command = None
    if filename_hint:
        targeted_command = f"rg --files . | grep -F '{filename_hint}' | head -50"

    commands = [
        "pwd",
        "rg --files . | head -200",
        "find . -maxdepth 4 -type f | sort | head -200",
    ]
    if targeted_command:
        commands.append(targeted_command)

    repaired_step = dict(step)
    repaired_step["description"] = (
        "Inspect the real workspace tree and locate existing implementation files "
        "before reading any specific path"
    )
    repaired_step["commands"] = commands
    repaired_step["verification"] = "test -d . && echo workspace-inspected"
    repaired_step["rollback"] = None
    repaired_step["expected_files"] = []
    return repaired_step


# ── Phase 23C/23D: runtime workspace redirection (Task Execution Sandbox) ──
#
# Pure, worker.py-independent helpers so the sandbox allocation, runtime
# executor context construction, workspace contract argument selection, and
# disposal logic used by dispatch can be unit tested without invoking the
# full Celery task. worker.py calls these directly; none of them are wired
# into any other execution path.


def build_runtime_executor_context(
    *,
    sandbox: Optional[TaskSandbox],
    project_workspace: Path,
    executor: str,
    project_id: Optional[int],
    task_execution_id: Optional[int],
    runtime_root: Optional[Path] = None,
) -> RuntimeExecutorContext:
    """Construct the one Runtime Executor Context for this dispatch (Phase
    23D Goal 1/2).

    Replaces the three independent ``orchestration_state._project_dir_override``
    assignments Phase 23C left in ``worker.py`` (sandboxed, non-sandboxed
    canonical, and -- unchanged, out of scope -- resume) with a single
    constructed object: ``sandbox`` present means the Task Execution Sandbox
    branch, ``sandbox is None`` means Model A (execute directly in the
    Project Workspace).
    """
    if sandbox is not None:
        return RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace, runtime_root=runtime_root
        )
    return RuntimeExecutorContext.for_project_workspace(
        project_workspace=project_workspace,
        executor=executor,
        project_id=project_id,
        task_execution_id=task_execution_id,
    )


def maybe_bind_runtime_cwd_override(
    runtime_service: Any,
    context: Optional[Any],
) -> bool:
    """Bind the Runtime Executor Context's runtime path onto a runtime
    backend that supports it (Goal 2: single resolved runtime path, closes
    F15 for the branch that constructs a context).

    ``context`` is normally a ``RuntimeExecutorContext`` (``.runtime_workspace``);
    a bare ``TaskSandbox`` (``.path``, the pre-23D calling convention) is
    also accepted for backward compatibility.

    Returns True if the override was set. No-ops (returns False) when there
    is no context for this dispatch, when it carries no resolvable runtime
    path, or when the backend has no ``execution_cwd_override`` attribute
    (e.g. non-OpenClaw backends) -- mirrors the existing ``hasattr`` guard
    pattern dispatch already uses for optional runtime attributes like
    ``task_execution_id``.
    """
    if context is None:
        return False
    if not hasattr(runtime_service, "execution_cwd_override"):
        return False
    runtime_workspace = getattr(context, "runtime_workspace", None)
    if runtime_workspace is None:
        runtime_workspace = getattr(context, "path", None)
    if runtime_workspace is None:
        return False
    runtime_service.execution_cwd_override = str(runtime_workspace)
    return True


def maybe_allocate_runtime_workspace(
    *,
    enabled: bool,
    project_id: int,
    task_execution_id: int,
    canonical_baseline_dir: Path,
    executor: str,
    runtime_root: Optional[Path] = None,
) -> Optional[TaskSandbox]:
    """Allocate a Task Execution Sandbox when runtime-workspace mode is on.

    Returns None (no allocation, no side effect) when ``enabled`` is False --
    the Phase 23B behavior is otherwise unchanged. Raises TaskSandboxError on
    allocation failure; callers must not fall back to executing directly in
    ``canonical_baseline_dir`` on that error.
    """
    if not enabled:
        return None
    return allocate_task_sandbox(
        canonical_baseline_dir,
        project_id=project_id,
        task_execution_id=task_execution_id,
        executor=executor,
        runtime_root=runtime_root,
    )


def resolve_workspace_contract_args(
    *,
    runtime_context: Optional[RuntimeExecutorContext] = None,
    project_workspace_path: Path,
    task_subfolder: Optional[str],
    runs_in_canonical_baseline: bool,
    runtime_sandbox: Optional[TaskSandbox] = None,
) -> Dict[str, Any]:
    """Return the expected_root/subfolder/allow_project_root_task_dir set for
    verify_workspace_contract.

    When a Task Execution Sandbox is active (via ``runtime_context`` or,
    kept for backward compatibility, a bare ``runtime_sandbox``), the
    Runtime Workspace is the only path the contract may validate against --
    the Project Workspace is not touched during execution and must not be
    compared to task_dir.
    """
    sandbox = runtime_sandbox
    if sandbox is None and runtime_context is not None:
        sandbox = runtime_context.sandbox
    if sandbox is not None:
        return {
            "expected_root": sandbox.path,
            "expected_task_subfolder": None,
            "allow_project_root_task_dir": True,
        }
    return {
        "expected_root": project_workspace_path,
        "expected_task_subfolder": task_subfolder,
        "allow_project_root_task_dir": runs_in_canonical_baseline,
    }


def dispose_runtime_workspace_safely(
    sandbox: Optional[TaskSandbox],
    *,
    project_root: Optional[Path],
    logger_obj: Optional[logging.Logger] = None,
) -> bool:
    """Dispose a Task Execution Sandbox, never raising.

    Returns True if disposal was attempted (sandbox was not None), regardless
    of whether it succeeded -- callers use this from a `finally` block, where
    a raised exception here must never mask the original outcome.
    """
    if sandbox is None:
        return False
    log = logger_obj or logging.getLogger(__name__)
    try:
        dispose_task_sandbox(sandbox, project_root=project_root)
    except Exception as exc:  # noqa: BLE001 - finally-block cleanup must not raise
        log.warning(
            "[ORCHESTRATION] Failed to dispose Runtime Workspace %s: %s",
            sandbox.path,
            exc,
        )
    return True
