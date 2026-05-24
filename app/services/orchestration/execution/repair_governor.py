"""Repair churn hard stop policy.

Reads existing execution and session state to decide whether automatic repair
should be stopped and routed to operator review. Never writes state itself.

Three triggers:

same_signature_repeat
    The task has accumulated N or more failed TaskExecution rows in this
    session. Each failed execution represents a repair attempt. When the
    threshold is reached, the repair loop is unlikely to converge.

strategy_pivot_without_progress
    Completion repair has been attempted at least twice (strategy pivot from
    execution repair to completion repair, then a second completion attempt)
    AND there is still a failed execution in this session. Pivoting strategy
    twice without clearing the failure is a churn signal.

constrained_lane_repair_failure_streak
    The session is running on a local_constrained model lane AND has
    accumulated N or more consecutive failed executions for this task.
    Constrained lanes have lower repair success rates; early escalation
    reduces wasted time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

_SAME_SIGNATURE_THRESHOLD = 3
_STRATEGY_PIVOT_COMPLETION_ATTEMPTS = 2
_CONSTRAINED_LANE_STREAK_THRESHOLD = 2


def check_repair_churn(
    db: "DBSession",
    *,
    session_id: int,
    task_id: int,
    completion_repair_attempts: int = 0,
    model_lane_label: str | None = None,
) -> tuple[bool, str | None]:
    """Return (should_stop, trigger_reason) based on current session state.

    Reads only — does not write or modify any DB row.
    """
    from app.models import TaskExecution, TaskStatus

    failed_count = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
            TaskExecution.status == TaskStatus.FAILED,
        )
        .count()
    )

    if failed_count >= _SAME_SIGNATURE_THRESHOLD:
        return True, "same_signature_repeat"

    if (
        completion_repair_attempts >= _STRATEGY_PIVOT_COMPLETION_ATTEMPTS
        and failed_count >= 1
    ):
        return True, "strategy_pivot_without_progress"

    lane = str(model_lane_label or "").lower()
    if "constrained" in lane and failed_count >= _CONSTRAINED_LANE_STREAK_THRESHOLD:
        return True, "constrained_lane_repair_failure_streak"

    return False, None
