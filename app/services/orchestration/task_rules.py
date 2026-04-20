"""Task-intent and project-gate helpers for orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.models import Project, Task, TaskStatus
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.task_service import TaskService


def is_verification_style_task(
    execution_profile: str, title: Optional[str], description: Optional[str]
) -> bool:
    combined = f"{execution_profile} {title or ''} {description or ''}".lower()
    markers = (
        "verify",
        "verification",
        "refine",
        "review",
        "qa",
        "audit",
        "integration",
        "test",
    )
    return execution_profile in {"test_only", "review_only"} or any(
        marker in combined for marker in markers
    )


def should_execute_in_canonical_project_root(
    task: Optional[Task],
    execution_profile: Optional[str],
    title: Optional[str],
    description: Optional[str],
) -> bool:
    if not task:
        return False

    if getattr(task, "plan_position", None) is not None:
        return True

    return is_verification_style_task(
        execution_profile or "",
        title or getattr(task, "title", None),
        description or getattr(task, "description", None),
    )


def should_force_review_execution_profile(
    execution_profile: str,
    task_prompt: str,
    title: Optional[str],
    description: Optional[str],
) -> bool:
    if execution_profile in {"review_only", "test_only", "debug_only"}:
        return False
    combined = " ".join([task_prompt or "", title or "", description or ""]).lower()
    review_markers = (
        "inspect",
        "analysis",
        "analyze",
        "architecture",
        "inventory",
        "current project structure",
        "current project architecture",
        "codebase walkthrough",
    )
    return any(marker in combined for marker in review_markers)


def get_task_report_path(project_root: Path, task: Task) -> Optional[Path]:
    if not task:
        return None
    if getattr(task, "plan_position", None) is not None:
        return project_root / f"task_report_{task.id}.md"
    if not getattr(task, "task_subfolder", None):
        return None
    return project_root / task.task_subfolder / f"task_report_{task.id}.md"


def run_virtual_merge_gate(
    db: Any,
    project: Optional[Project],
    current_task: Optional[Task],
    execution_profile: str,
    get_state_manager_path_fn: Any,
) -> Optional[str]:
    if not project or not current_task:
        return None
    if not is_verification_style_task(
        execution_profile, current_task.title, current_task.description
    ):
        return None
    if current_task.plan_position is None:
        return None

    task_service = TaskService(db)
    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    prior_tasks = [
        task
        for task in task_service.get_project_tasks(project.id)
        if task.id != current_task.id
        and task.plan_position is not None
        and task.plan_position < current_task.plan_position
    ]
    incomplete = [task for task in prior_tasks if task.status != TaskStatus.DONE]
    if incomplete:
        summary = ", ".join(
            f"#{task.plan_position} {task.title} ({task.status.value})"
            for task in incomplete[:3]
        )
        return f"Virtual merge gate failed: earlier ordered tasks are incomplete: {summary}"

    missing_reports = []
    for task in prior_tasks:
        report_path = get_task_report_path(project_root, task)
        if report_path and not report_path.exists():
            missing_reports.append(
                f"#{task.plan_position} {task.title} (missing {report_path.name})"
            )
    if missing_reports:
        return (
            "Virtual merge gate failed: missing structured task reports for prior work: "
            + ", ".join(missing_reports[:3])
        )

    state_path = get_state_manager_path_fn(project_root)
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            if state_data.get("status") == "unsynced":
                return (
                    "Virtual merge gate failed: project state manager is UNSYNCED. "
                    "Resolve earlier task inconsistencies before verify/refine."
                )
        except Exception:
            return "Virtual merge gate failed: state manager file is unreadable"

    baseline_validation = task_service.validate_project_baseline(project, current_task)
    if prior_tasks and baseline_validation["baseline_file_count"] == 0:
        return (
            "Virtual merge gate failed: canonical merged project state is empty even "
            "though earlier ordered tasks are completed."
        )

    missing_expected_files = baseline_validation["missing_expected_files"]
    if missing_expected_files:
        summary = ", ".join(
            f"#{entry['plan_position']} {entry['title']} -> {entry['path']}"
            for entry in missing_expected_files[:5]
        )
        return (
            "Virtual merge gate failed: canonical merged project state is missing "
            f"files declared by prior completed tasks: {summary}"
        )

    return None
