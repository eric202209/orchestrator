"""Context helpers for run-state transitions."""

from __future__ import annotations

from typing import Any


def task_execution_id_from_context(ctx: Any | None) -> int | None:
    """Return a valid task execution id from orchestration context."""

    if ctx is None:
        return None
    task_execution_id = getattr(ctx, "task_execution_id", None)
    if isinstance(task_execution_id, bool) or not isinstance(task_execution_id, int):
        return None
    return task_execution_id
