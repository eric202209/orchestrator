"""Task-intent and project-gate helpers for orchestration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.models import Project, Task, TaskStatus
from app.services.orchestration.workflow_profiles import (
    WORKFLOW_PROFILES,
    get_implementation_intent_markers,
    get_workflow_markers,
)
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.workspace_paths import TASK_REPORT_ROOT
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

    return True


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
        "inspection",
        "analysis",
        "analyze",
        "inventory",
        "extension points",
        "current project structure",
        "current project architecture",
        "codebase walkthrough",
    )
    if any(marker in combined for marker in review_markers):
        return True

    implementation_markers = get_implementation_intent_markers()
    if any(marker in combined for marker in implementation_markers):
        return False
    return False


def get_workflow_profile(
    execution_profile: str,
    title: Optional[str],
    description: Optional[str],
) -> str:
    """Resolve task into a workflow-phase profile."""

    if execution_profile == "review_only":
        return "review_only"
    if execution_profile == "debug_only":
        return "debug_only"

    combined = " ".join([title or "", description or ""]).lower()
    marker_groups = get_workflow_markers("fullstack_scaffold")
    frontend_markers = tuple(marker_groups.get("frontend") or [])
    backend_markers = tuple(marker_groups.get("backend") or [])
    scaffold_markers = tuple(marker_groups.get("scaffold") or [])
    has_frontend = any(
        marker in combined for marker in frontend_markers
    ) and not _contains_negated_stack_marker(combined, frontend_markers)
    has_backend = any(
        marker in combined for marker in backend_markers
    ) and not _contains_negated_stack_marker(combined, backend_markers)
    if any(marker in combined for marker in scaffold_markers):
        if has_frontend and has_backend:
            return "fullstack_scaffold"
        if has_frontend:
            return "frontend_only"
        if has_backend:
            return "backend_only"

    return "default" if "default" in WORKFLOW_PROFILES else execution_profile


def _contains_negated_stack_marker(text: str, markers: tuple[str, ...]) -> bool:
    """Return True when a stack term is only mentioned as an exclusion."""

    for marker in markers:
        escaped = re.escape(marker)
        patterns = (
            rf"\bdo\s+not\s+(?:create|build|add|include|use|make|set\s+up|setup)\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bdon't\s+(?:create|build|add|include|use|make|set\s+up|setup)\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bwithout\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bno\s+(?:new\s+)?{escaped}\b",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return True
    return False


def get_task_report_path(project_root: Path, task: Task) -> Optional[Path]:
    if not task:
        return None
    return project_root / TASK_REPORT_ROOT / f"task_report_{task.id}.md"


def get_legacy_task_report_path(project_root: Path, task: Task) -> Optional[Path]:
    if not task:
        return None
    return project_root / f"task_report_{task.id}.md"


def _coerce_int_set(values: Any) -> set[int]:
    result: set[int] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _state_manager_unsynced_prior_summary(
    state_data: dict[str, Any],
    current_task: Task,
    prior_tasks: list[Task],
) -> Optional[str]:
    """Return a blocking summary only when unsynced state involves dependencies.

    Older project state-manager files can remain `unsynced` because a previous
    attempt of the same task failed. That is legacy/unknown retry state, not
    proof that prior canonical project work is unsafe to verify.
    """

    if state_data.get("status") != "unsynced":
        return None

    prior_task_ids = {task.id for task in prior_tasks}
    failed_prior_ids = (
        _coerce_int_set(state_data.get("failed_or_cancelled_task_ids")) & prior_task_ids
    )

    inconsistent_prior = []
    raw_inconsistent = state_data.get("inconsistent_completed_tasks")
    if isinstance(raw_inconsistent, list):
        for item in raw_inconsistent:
            if not isinstance(item, dict):
                continue
            try:
                task_id = int(item.get("task_id"))
            except (TypeError, ValueError):
                continue
            if task_id in prior_task_ids:
                inconsistent_prior.append(item)

    if failed_prior_ids:
        return "prior failed/cancelled tasks: " + ", ".join(
            str(task_id) for task_id in sorted(failed_prior_ids)[:5]
        )
    if inconsistent_prior:
        return "prior inconsistent completed tasks: " + ", ".join(
            str(item.get("task_id")) for item in inconsistent_prior[:5]
        )
    return None


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
        and (
            current_task.plan_id is None
            or getattr(task, "plan_id", None) == current_task.plan_id
        )
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
        legacy_report_path = get_legacy_task_report_path(project_root, task)
        if (
            report_path
            and not report_path.exists()
            and (legacy_report_path is None or not legacy_report_path.exists())
        ):
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
            unsynced_summary = _state_manager_unsynced_prior_summary(
                state_data,
                current_task,
                prior_tasks,
            )
            if unsynced_summary:
                return (
                    "Virtual merge gate failed: project state manager is UNSYNCED. "
                    "Resolve earlier task inconsistencies before verify/refine "
                    f"({unsynced_summary})."
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
