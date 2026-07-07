"""Backend-capacity retry helpers for orchestration task execution.

Decides whether a capacity-blocked task run should retry or give up, and
returns a capacity-only attempt to a retryable state without failing the
task, ahead of the Celery-level retry itself.
"""

from __future__ import annotations

from app.models import SessionTask, Task, TaskExecution

# Phase 19F: raised from 20 (300s ceiling) to 60 (900s ceiling) at the same
# 15s countdown. Phase 19E found real completed task_executions run p50=217s,
# p90=279s, max=605s; with LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS=1 a session
# queued behind others must wait for their full duration, not just capacity
# becoming free. The old 300s ceiling was shorter than one p50 execution plus
# any queueing, and historically caused 28 permanent FAILED / 176 total
# task_executions (16%) whose capacity genuinely would have freed up given
# more time (10 sessions did recover at 242-303s, right at the old ceiling).
# MAX_PARALLEL_SESSIONS itself is left at 1 — see phase19f report Task 3.
BACKEND_CAPACITY_RETRY_MAX_RETRIES = 60


def backend_capacity_retry_state(
    request, max_retries: int | None = None
) -> tuple[int, bool]:
    """Return current capacity retry count and whether capacity retries are exhausted."""

    retry_count = int(getattr(request, "retries", 0) or 0)
    retry_limit = (
        BACKEND_CAPACITY_RETRY_MAX_RETRIES if max_retries is None else int(max_retries)
    )
    return retry_count, retry_count >= retry_limit


def prepare_backend_capacity_retry(
    *,
    task: Task | None,
    session_task_link: SessionTask | None,
    task_execution: TaskExecution | None,
    backend_id: str,
) -> None:
    """Return capacity-only attempts to a retryable state without task failure."""

    from app.services.session.session_execution_service import (
        mark_execution_pending,
    )

    mark_execution_pending(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        reset_started_at=True,
        reset_steps=False,
        workspace_status=getattr(task, "workspace_status", None) if task else None,
        error_message=None,
    )
    if task_execution is not None:
        task_execution.failure_category = "backend_capacity_limit"
        task_execution.backend_id = backend_id
