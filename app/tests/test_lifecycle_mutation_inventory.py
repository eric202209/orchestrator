"""Characterization tests for orchestration lifecycle mutation ownership.

These tests intentionally snapshot the current mutation surface before the
Phase 11H refactors begin. If they fail, update the inventory only after
reviewing whether a lifecycle responsibility moved intentionally.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

AUDITED_LIFECYCLE_FILES = [
    "app/tasks/worker.py",
    "app/tasks/worker_support/dispatch.py",
    "app/tasks/worker_support/execution_state.py",
    "app/services/session/session_lifecycle_service.py",
    "app/services/session/session_execution_service.py",
    "app/services/session/session_runtime_service.py",
    "app/services/session/intervention_service.py",
    "app/services/session/resume_service.py",
    "app/services/orchestration/run_state/transitions.py",
    "app/services/orchestration/state/session_state.py",
    "app/services/orchestration/lifecycle/completion.py",
    "app/services/orchestration/coordinators/completion_coordinator.py",
    "app/services/orchestration/coordinators/failure_coordinator.py",
    "app/services/orchestration/phases/planning_flow.py",
    "app/services/orchestration/phases/execution_loop.py",
    "app/services/orchestration/phases/completion_flow.py",
    "app/services/orchestration/phases/failure_flow.py",
    "app/services/orchestration/phases/planning_support.py",
    "app/services/orchestration/execution/step_assessment.py",
]

EXPECTED_SESSION_CALLS = {
    "app/tasks/worker.py": {
        "mark_session_failed": 1,
        "mark_session_paused": 5,
        "mark_session_running": 1,
    },
    "app/services/session/session_lifecycle_service.py": {
        "mark_session_failed": 1,
        "mark_session_paused": 2,
        "mark_session_running": 5,
        "mark_session_stopped": 6,
    },
    "app/services/session/session_runtime_service.py": {
        "mark_session_completed": 1,
        "mark_session_paused": 4,
        "mark_session_running": 1,
    },
    "app/services/session/intervention_service.py": {
        "mark_session_awaiting_input": 1,
        "mark_session_paused": 3,
        "mark_session_running": 1,
    },
    "app/services/session/resume_service.py": {
        "mark_session_paused": 1,
        "mark_session_resumed": 2,
    },
    "app/services/orchestration/phases/execution_loop.py": {
        "mark_session_paused": 4,
    },
    "app/services/orchestration/execution/step_assessment.py": {
        "mark_session_failed": 1,
    },
    "app/services/orchestration/lifecycle/completion.py": {
        "mark_session_completed": 1,
        "mark_session_paused": 3,
        "mark_session_running": 1,
    },
    "app/services/orchestration/coordinators/completion_coordinator.py": {
        "mark_session_paused": 6,
    },
    "app/services/orchestration/coordinators/failure_coordinator.py": {
        "mark_session_paused": 3,
        "mark_session_running": 3,
    },
    "app/services/orchestration/phases/completion_flow.py": {
        "mark_session_paused": 3,
    },
    "app/services/orchestration/phases/planning_support.py": {
        "mark_session_paused": 1,
    },
}

EXPECTED_TASK_ATTEMPT_CALLS = {
    "app/tasks/worker_support/execution_state.py": {
        "mark_task_attempt_cancelled": 2,
        "mark_task_attempt_done": 2,
        "mark_task_attempt_failed": 2,
        "mark_task_attempt_pending": 4,
        "mark_task_attempt_running": 2,
    },
    "app/services/orchestration/run_state/transitions.py": {
        "mark_task_attempt_cancelled": 3,
        "mark_task_attempt_done": 1,
        "mark_task_attempt_failed": 3,
    },
    "app/services/session/session_lifecycle_service.py": {
        "mark_task_attempt_cancelled": 1,
        "mark_task_attempt_pending": 6,
    },
    "app/services/session/session_execution_service.py": {
        "mark_task_attempt_cancelled": 1,
        "mark_task_attempt_done": 1,
        "mark_task_attempt_failed": 1,
        "mark_task_attempt_pending": 1,
        "mark_task_attempt_running": 1,
    },
    "app/services/session/session_runtime_service.py": {
        "mark_task_attempt_failed": 1,
        "mark_task_attempt_pending": 3,
    },
    "app/services/session/intervention_service.py": {
        "mark_task_attempt_pending": 1,
    },
    "app/services/orchestration/phases/execution_loop.py": {
        "mark_task_attempt_cancelled": 1,
        "mark_task_attempt_failed": 14,
    },
    "app/services/orchestration/execution/step_assessment.py": {
        "mark_task_attempt_failed": 1,
    },
    "app/services/orchestration/lifecycle/completion.py": {
        "mark_task_attempt_pending": 2,
    },
    "app/services/orchestration/coordinators/completion_coordinator.py": {
        "mark_task_attempt_failed": 6,
    },
    "app/services/orchestration/coordinators/failure_coordinator.py": {
        "mark_task_attempt_failed": 3,
        "mark_task_attempt_pending": 2,
    },
    "app/services/orchestration/phases/completion_flow.py": {
        "mark_task_attempt_failed": 2,
    },
    "app/services/orchestration/phases/failure_flow.py": {
        "mark_task_attempt_failed": 1,
    },
    "app/services/orchestration/phases/planning_support.py": {
        "mark_task_attempt_failed": 1,
    },
}

EXPECTED_DIRECT_STATUS_MUTATIONS = {
    "app/tasks/worker_support/dispatch.py": {
        "SessionTask.status": 1,
        "Task.status": 1,
    },
    "app/services/orchestration/run_state/transitions.py": {
        "execution.status": 1,
        "latest_link.status": 1,
        "link.status": 1,
        "session_task_link.status": 5,
        "task.status": 7,
        "task_execution.status": 5,
    },
}

GOLDEN_LIFECYCLE_TRANSITIONS = {
    "start": {
        "owner": "app/services/session/session_lifecycle_service.py",
        "session": "pending/stopped -> running",
        "task": "next selected task reset or queued as pending",
        "task_execution": "not created by start; claimed by worker execution state",
    },
    "claim": {
        "owner": "app/tasks/worker_support/dispatch.py",
        "session": "must be pending/running/paused/awaiting_input",
        "task": "pending -> running",
        "task_execution": "synchronized by worker execution state",
    },
    "planning_failure": {
        "owner": "app/services/orchestration/phases/planning_support.py",
        "session": "paused",
        "task": "failed",
        "task_execution": "failed",
    },
    "execution_failure": {
        "owner": "app/services/orchestration/phases/execution_loop.py",
        "session": "paused",
        "task": "failed",
        "task_execution": "failed",
    },
    "completion_success": {
        "owner": "app/services/orchestration/lifecycle/completion.py",
        "session": "completed or kept running for follow-up work",
        "task": "done",
        "task_execution": "done",
    },
    "pause": {
        "owner": "app/services/session/session_lifecycle_service.py",
        "session": "running/active -> paused",
        "task": "active attempts reset or cancelled",
        "task_execution": "active attempts reset or cancelled",
    },
    "stop": {
        "owner": "app/services/session/session_lifecycle_service.py",
        "session": "running/paused/active -> stopped",
        "task": "active attempts reset or cancelled",
        "task_execution": "active attempts reset or cancelled",
    },
    "resume": {
        "owner": "app/services/session/session_lifecycle_service.py",
        "session": "paused/stopped/awaiting_input -> running",
        "task": "resume target queued or fresh task selected",
        "task_execution": "new execution claimed later by worker",
    },
}

KNOWN_ATTEMPT_VARIABLES = {
    "current_execution",
    "execution",
    "latest_link",
    "link",
    "older_execution",
    "queued_execution",
    "session_task_link",
    "task",
    "task_execution",
}
MODEL_STATUS_KEYS = {"SessionTask.status", "Task.status", "TaskExecution.status"}


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _attribute_path(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_path(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _scan_file(
    relative_path: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    session_calls: Counter[str] = Counter()
    task_attempt_calls: Counter[str] = Counter()
    direct_status_mutations: Counter[str] = Counter()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name and name.startswith("mark_session_"):
                session_calls[name] += 1
            elif name and name.startswith("mark_task_attempt_"):
                task_attempt_calls[name] += 1

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    path = _attribute_path(target)
                    base = path.split(".", maxsplit=1)[0] if path else ""
                    if base in KNOWN_ATTEMPT_VARIABLES:
                        direct_status_mutations[path] += 1

        if isinstance(node, ast.Dict):
            for key in node.keys:
                path = _attribute_path(key)
                if path in MODEL_STATUS_KEYS:
                    direct_status_mutations[path] += 1

    return (
        dict(sorted(session_calls.items())),
        dict(sorted(task_attempt_calls.items())),
        dict(sorted(direct_status_mutations.items())),
    )


def _scan_all(index: int) -> dict[str, dict[str, int]]:
    snapshot = {}
    for relative_path in AUDITED_LIFECYCLE_FILES:
        values = _scan_file(relative_path)[index]
        if values:
            snapshot[relative_path] = values
    return snapshot


def test_mark_session_call_site_inventory_is_stable() -> None:
    assert _scan_all(0) == EXPECTED_SESSION_CALLS


def test_mark_task_attempt_call_site_inventory_is_stable() -> None:
    assert _scan_all(1) == EXPECTED_TASK_ATTEMPT_CALLS


def test_direct_task_status_mutation_inventory_is_stable() -> None:
    assert _scan_all(2) == EXPECTED_DIRECT_STATUS_MUTATIONS


def test_golden_lifecycle_transition_inventory_covers_phase11h_slice_1() -> None:
    assert set(GOLDEN_LIFECYCLE_TRANSITIONS) == {
        "start",
        "claim",
        "planning_failure",
        "execution_failure",
        "completion_success",
        "pause",
        "stop",
        "resume",
    }

    for transition in GOLDEN_LIFECYCLE_TRANSITIONS.values():
        assert transition["owner"] in AUDITED_LIFECYCLE_FILES
        assert transition["session"]
        assert transition["task"]
        assert transition["task_execution"]


def test_lifecycle_mutation_inventory_contract_matches_test_scope() -> None:
    expected_inventory_files = set(EXPECTED_SESSION_CALLS)
    expected_inventory_files.update(EXPECTED_TASK_ATTEMPT_CALLS)
    expected_inventory_files.update(EXPECTED_DIRECT_STATUS_MUTATIONS)

    assert expected_inventory_files <= set(AUDITED_LIFECYCLE_FILES)
    for transition in GOLDEN_LIFECYCLE_TRANSITIONS:
        assert transition
    assert EXPECTED_SESSION_CALLS
    assert EXPECTED_TASK_ATTEMPT_CALLS
    assert EXPECTED_DIRECT_STATUS_MUTATIONS
