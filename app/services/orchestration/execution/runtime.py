"""Runtime support helpers for orchestration state and workspace management."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.task_service import TaskService


def get_state_manager_path(project_root: Path) -> Path:
    return project_root / ".openclaw" / "state_manager.json"


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
    highest_incomplete_position = None
    for task in ordered_tasks:
        if task.plan_position is None:
            continue
        if task.status != TaskStatus.DONE:
            highest_incomplete_position = task.plan_position
            break

    if highest_incomplete_position is not None:
        for task in ordered_tasks:
            if (
                task.plan_position is not None
                and task.plan_position > highest_incomplete_position
                and task.status == TaskStatus.DONE
            ):
                inconsistent_pairs.append(
                    {
                        "task_id": task.id,
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
    payload = build_project_state_snapshot(db, project, current_task, session_id)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def workspace_snapshot_key(task_id: int) -> str:
    return f"task-{task_id}-pre-run"


def snapshot_workspace_before_run(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    preserve_project_root_rules: bool,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.create_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=workspace_snapshot_key(task_id),
        preserve_project_root_rules=preserve_project_root_rules,
    )


def restore_workspace_after_abort(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    preserve_project_root_rules: bool,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.restore_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=workspace_snapshot_key(task_id),
        preserve_project_root_rules=preserve_project_root_rules,
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
