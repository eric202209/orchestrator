"""Task-intent and project-gate helpers for orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

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
from app.services.tasks.service import TaskService


_IMPLEMENTATION_TITLE_INTENT_RE = re.compile(
    r"\b(?:add|build|change|create|delete|fix|implement|improve|introduce|"
    r"migrate(?:s|d|ing)?|modify(?:s|ied|ing)?|refactor|remove|replace|"
    r"set\s+up|setup|update(?:s|d|ing)?|"
    r"wire|write|extend)\b",
    re.IGNORECASE,
)
_NEGATED_IMPLEMENTATION_TITLE_INTENT_RE = re.compile(
    r"\b(?:without|do\s+not|don't|never)\s+(?:\w+\s+){0,2}"
    r"(?:add|build|change|create|delete|fix|implement|improve|introduce|"
    r"migrate(?:s|d|ing)?|modify(?:s|ied|ing)?|refactor|remove|replace|"
    r"set\s+up|setup|update(?:s|d|ing)?|"
    r"wire|write|extend)\b",
    re.IGNORECASE,
)
_EXPLICIT_VERIFICATION_TITLE_INTENT_RE = re.compile(
    r"\b(?:audit|certif\w*|inspect\w*|qa|review\w*|test\w*|" r"validat\w*|verif\w*)\b",
    re.IGNORECASE,
)
_INTEGRATION_VERIFICATION_TITLE_INTENT_RE = re.compile(
    r"\bintegration\s+(?:audit\w*|inspect\w*|review\w*|test\w*|"
    r"validat\w*|verif\w*)\b",
    re.IGNORECASE,
)


# Provenance of the execution profile handed to admission. The persisted task
# row is the only operator-visible authority; anything synthesized in-flight
# must identify itself so classification never claims operator intent it does
# not have.
EXECUTION_PROFILE_SOURCE_TASK_ROW = "task_execution_profile"
EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC = "review_intent_heuristic"

# Review markers are words, not arbitrary substrings: matching "inspect" inside
# "inspectable" silently converted implementation work into review work. Regular
# inflections are listed explicitly so genuine review wording still matches.
_REVIEW_INTENT_MARKERS = (
    "inspect",
    "inspects",
    "inspected",
    "inspecting",
    "inspection",
    "inspections",
    "analysis",
    "analyze",
    "analyzes",
    "analyzed",
    "analyzing",
    "inventory",
    "extension points",
    "current project structure",
    "current project architecture",
    "codebase walkthrough",
)
_REVIEW_INTENT_MARKER_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(marker) for marker in _REVIEW_INTENT_MARKERS)
    + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskIntentClassification:
    """The single admission-intent decision and its human-readable authority."""

    classification: str
    reason: str
    authority: str

    @property
    def is_verification_style(self) -> bool:
        return self.classification == "verification_style"

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "classification_reason": self.reason,
            "classification_authority": self.authority,
        }


class VirtualMergeGateFailure(str):
    """String-compatible gate failure carrying its admission diagnostics."""

    admission_metadata: dict[str, Any]

    def __new__(
        cls,
        message: str,
        *,
        classification: TaskIntentClassification,
        blocking_scope: str,
        blocking_task_ids: Sequence[int],
    ):
        metadata = {
            **classification.as_dict(),
            "blocking_scope": blocking_scope,
            "blocking_task_ids": list(blocking_task_ids),
        }
        task_ids = metadata["blocking_task_ids"]
        rendered_ids = ", ".join(str(task_id) for task_id in task_ids) or "none"
        decorated_message = (
            f"{message} [classification={metadata['classification']}; "
            f"classification_reason={metadata['classification_reason']}; "
            f"classification_authority={metadata['classification_authority']}; "
            f"blocking_scope={blocking_scope}; blocking_task_ids={rendered_ids}]"
        )
        instance = super().__new__(cls, decorated_message)
        instance.admission_metadata = metadata
        return instance


def _title_has_negated_implementation_intent(title: str) -> bool:
    return bool(_NEGATED_IMPLEMENTATION_TITLE_INTENT_RE.search(title))


def classify_task_intent(
    execution_profile: Optional[str],
    title: Optional[str],
    description: Optional[str],
    profile_source: str = EXECUTION_PROFILE_SOURCE_TASK_ROW,
) -> TaskIntentClassification:
    """Classify admission intent from structured profile or explicit title intent.

    Description text is deliberately not an authority: paths, commands,
    filenames, and acceptance criteria commonly mention tests while the task
    still delivers implementation work.

    ``profile_source`` records where ``execution_profile`` came from. A profile
    synthesized in-flight is reported under its own authority so the recorded
    provenance can never contradict the persisted task row.
    """

    normalized_profile = str(execution_profile or "").strip().lower()
    if normalized_profile in {"test_only", "review_only"}:
        if profile_source == EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC:
            return TaskIntentClassification(
                classification="verification_style",
                reason="review_intent_heuristic",
                authority=EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC,
            )
        return TaskIntentClassification(
            classification="verification_style",
            reason="explicit_execution_profile",
            authority="execution_profile",
        )

    normalized_title = str(title or "").strip().lower()
    has_implementation_intent = bool(
        _IMPLEMENTATION_TITLE_INTENT_RE.search(normalized_title)
    ) and not _title_has_negated_implementation_intent(normalized_title)
    if has_implementation_intent:
        return TaskIntentClassification(
            classification="implementation_style",
            reason="implementation_intent_in_title",
            authority="task_title",
        )

    if _EXPLICIT_VERIFICATION_TITLE_INTENT_RE.search(
        normalized_title
    ) or _INTEGRATION_VERIFICATION_TITLE_INTENT_RE.search(normalized_title):
        return TaskIntentClassification(
            classification="verification_style",
            reason="explicit_verification_intent_in_title",
            authority="task_title",
        )

    return TaskIntentClassification(
        classification="implementation_style",
        reason="no_explicit_verification_intent",
        authority="task_title_and_execution_profile",
    )


def is_verification_style_task(
    execution_profile: str, title: Optional[str], description: Optional[str]
) -> bool:
    """Backward-compatible boolean view of :func:`classify_task_intent`."""

    return classify_task_intent(
        execution_profile, title, description
    ).is_verification_style


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
    if _REVIEW_INTENT_MARKER_RE.search(combined):
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
    if _looks_like_plain_static_site_task(combined):
        return "frontend_only"

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


def _looks_like_plain_static_site_task(text: str) -> bool:
    """Plain HTML/CSS sites should not inherit app scaffold lifecycle phases."""

    static_markers = (
        "plain static site",
        "static site",
        "public/status-site",
        "index.html",
        "css/style.css",
    )
    if not any(marker in text for marker in static_markers):
        return False
    if any(marker in text for marker in ("react", "vite", "npm", "package.json")):
        return any(
            exclusion in text
            for exclusion in (
                "no react",
                "no vite",
                "no npm",
                "without react",
                "without vite",
                "without npm",
                "do not use react",
                "do not use vite",
            )
        )
    return True


def _contains_negated_stack_marker(text: str, markers: tuple[str, ...]) -> bool:
    """Return True when a stack term is only mentioned as an exclusion."""

    for marker in markers:
        escaped = re.escape(marker)
        patterns = (
            rf"\bdo\s+not\s+(?:create|build|add|include|use|make|run|start|set\s+up|setup)\s+(?:a\s+|an\s+)?(?:\w+\s+){{0,2}}{escaped}\b",
            rf"\bdon't\s+(?:create|build|add|include|use|make|run|start|set\s+up|setup)\s+(?:a\s+|an\s+)?(?:\w+\s+){{0,2}}{escaped}\b",
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
    profile_source: str = EXECUTION_PROFILE_SOURCE_TASK_ROW,
) -> Optional[str]:
    if not project or not current_task:
        return None
    classification = classify_task_intent(
        execution_profile,
        current_task.title,
        current_task.description,
        profile_source=profile_source,
    )
    # Free-standing implementation work is not ordered behind unrelated
    # historical project tasks. Explicit Plan membership remains governed by
    # the predecessor checks below.
    if not classification.is_verification_style and current_task.plan_id is None:
        return None
    if current_task.plan_position is None:
        return None

    blocking_scope = (
        "explicit_plan_predecessors"
        if current_task.plan_id is not None
        else "ordered_project_history"
    )

    def gate_failure(
        message: str,
        blocking_task_ids: Sequence[int] = (),
    ) -> VirtualMergeGateFailure:
        return VirtualMergeGateFailure(
            message,
            classification=classification,
            blocking_scope=blocking_scope,
            blocking_task_ids=blocking_task_ids,
        )

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
        return gate_failure(
            f"Virtual merge gate failed: earlier ordered tasks are incomplete: {summary}",
            [task.id for task in incomplete],
        )

    missing_reports = []
    missing_report_task_ids = []
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
            missing_report_task_ids.append(task.id)
    if missing_reports:
        return gate_failure(
            "Virtual merge gate failed: missing structured task reports for prior work: "
            + ", ".join(missing_reports[:3]),
            missing_report_task_ids,
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
                return gate_failure(
                    "Virtual merge gate failed: project state manager is UNSYNCED. "
                    "Resolve earlier task inconsistencies before verify/refine "
                    f"({unsynced_summary}).",
                    [task.id for task in prior_tasks],
                )
        except Exception:
            return gate_failure(
                "Virtual merge gate failed: state manager file is unreadable",
                [task.id for task in prior_tasks],
            )

    baseline_validation = task_service.validate_project_baseline(project, current_task)
    if prior_tasks and baseline_validation["baseline_file_count"] == 0:
        return gate_failure(
            "Virtual merge gate failed: canonical merged project state is empty even "
            "though earlier ordered tasks are completed.",
            [task.id for task in prior_tasks],
        )

    missing_expected_files = baseline_validation["missing_expected_files"]
    if missing_expected_files:
        summary = ", ".join(
            f"#{entry['plan_position']} {entry['title']} -> {entry['path']}"
            for entry in missing_expected_files[:5]
        )
        return gate_failure(
            "Virtual merge gate failed: canonical merged project state is missing "
            f"files declared by prior completed tasks: {summary}",
            [task.id for task in prior_tasks],
        )

    return None
