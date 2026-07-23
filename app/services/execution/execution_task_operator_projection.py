"""API-safe operator projection for the canonical Execution Task lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import ExecutionTask
from app.services.execution.execution_task_transition_service import (
    is_terminal_execution_task_state,
)


EXECUTION_TASK_ALLOWED_ACTIONS: dict[str, tuple[str, ...]] = {
    "awaiting_validation": ("validate", "pause", "cancel"),
    "awaiting_recovery": ("evaluate_recovery", "pause", "cancel"),
}


@dataclass(frozen=True)
class ExecutionTaskOperatorProjection:
    execution_task_id: int | None
    current_state: str
    state_version: int | None
    is_terminal: bool
    is_successful: bool
    satisfies_dependencies: bool
    allowed_actions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_task_id": self.execution_task_id,
            "current_state": self.current_state,
            "state_version": self.state_version,
            "is_terminal": self.is_terminal,
            "is_successful": self.is_successful,
            "satisfies_dependencies": self.satisfies_dependencies,
            "allowed_actions": list(self.allowed_actions),
        }


def project_execution_task_state(
    state: str,
    *,
    execution_task_id: int | None = None,
    state_version: int | None = None,
) -> ExecutionTaskOperatorProjection:
    return ExecutionTaskOperatorProjection(
        execution_task_id=execution_task_id,
        current_state=state,
        state_version=state_version,
        is_terminal=is_terminal_execution_task_state(state),
        is_successful=state == "succeeded",
        satisfies_dependencies=state == "succeeded",
        allowed_actions=EXECUTION_TASK_ALLOWED_ACTIONS.get(state, ()),
    )


def project_execution_task(task: ExecutionTask) -> ExecutionTaskOperatorProjection:
    return project_execution_task_state(
        task.status,
        execution_task_id=int(task.id),
        state_version=int(task.state_version),
    )


__all__ = [
    "EXECUTION_TASK_ALLOWED_ACTIONS",
    "ExecutionTaskOperatorProjection",
    "project_execution_task",
    "project_execution_task_state",
]
