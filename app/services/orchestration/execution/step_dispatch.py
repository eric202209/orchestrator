"""Runtime dispatch and result normalization/persistence for the execution loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.models import TaskExecution
from app.services.agents.interfaces import RuntimeBackendResult


def _run_coroutine(coro: Any) -> Any:
    # asyncio.run() deadlocks inside a Celery ForkPoolWorker because os.fork()
    # inherits Python's asyncio internal mutexes in a locked state from the
    # parent process. Running in a fresh ThreadPoolExecutor thread avoids this:
    # the thread is not forked, so it starts with a clean event loop state.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _executor:
        return _executor.submit(asyncio.run, coro).result()


def _get_task_execution(
    db: Any, task_execution_id: Optional[int]
) -> Optional[TaskExecution]:
    if task_execution_id is None:
        return None
    return db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()


def _normalize_runtime_execution_result(
    runtime_service: Any,
    result: Dict[str, Any],
    *,
    duration_seconds: float,
) -> RuntimeBackendResult | None:
    normalizer = getattr(runtime_service, "normalize_execution_result", None)
    if not callable(normalizer):
        return None
    return normalizer(
        result,
        role="execution",
        duration_seconds=duration_seconds,
    )


def _persist_runtime_backend_result(
    db: Any,
    task_execution_id: Optional[int],
    result: RuntimeBackendResult | None,
) -> None:
    """Persist normalized backend metadata for the active execution attempt."""

    if task_execution_id is None or result is None:
        return
    task_execution = _get_task_execution(db, task_execution_id)
    if task_execution is None:
        return
    task_execution.backend_id = result.backend_id
    if not result.success and result.failure_category:
        task_execution.failure_category = result.failure_category
    try:
        if result.tokens_in is not None:
            task_execution.tokens_in = result.tokens_in
        if result.tokens_out is not None:
            task_execution.tokens_out = result.tokens_out
        if result.token_source:
            task_execution.token_source = result.token_source
    except Exception:
        pass
    if result.tokens_in is not None or result.tokens_out is not None:
        try:
            from app.models import LogEntry, Session as SessionModel

            _session = (
                db.query(SessionModel)
                .filter(SessionModel.id == task_execution.session_id)
                .first()
            )
            db.add(
                LogEntry(
                    session_id=task_execution.session_id,
                    task_id=task_execution.task_id,
                    session_instance_id=(_session.instance_id if _session else None),
                    level="INFO",
                    message="[TOKEN_USAGE_RECORDED]",
                    log_metadata=json.dumps(
                        {
                            "task_execution_id": task_execution_id,
                            "task_id": task_execution.task_id,
                            "session_id": task_execution.session_id,
                            "tokens_in": result.tokens_in,
                            "tokens_out": result.tokens_out,
                            "token_source": result.token_source,
                        }
                    ),
                )
            )
        except Exception:
            pass
    db.flush()


@dataclass
class RuntimeDispatchOutcome:
    """Loop-local result of a single execution-runtime dispatch call."""

    step_result: Dict[str, Any]
    runtime_backend_result: Optional[RuntimeBackendResult]


def dispatch_execution_runtime_step(
    *,
    runtime_service: Any,
    prompt: str,
    timeout_seconds: float,
    db: Any,
    task_execution_id: Optional[int],
) -> RuntimeDispatchOutcome:
    """Dispatch one execution-runtime call and persist its normalized result.

    Mirrors the inline pattern previously duplicated at the primary and
    context-overflow-compact-retry call sites in ``execute_step_loop``:
    run the coroutine, normalize the raw result into a
    ``RuntimeBackendResult``, persist it, and stamp the normalized dict
    back onto ``step_result["_runtime_backend_result"]``.
    """

    runtime_started_at = time.monotonic()
    step_result = _run_coroutine(
        runtime_service.execute_task(prompt, timeout_seconds=timeout_seconds)
    )
    runtime_backend_result = _normalize_runtime_execution_result(
        runtime_service,
        step_result,
        duration_seconds=time.monotonic() - runtime_started_at,
    )
    if runtime_backend_result is not None:
        _persist_runtime_backend_result(db, task_execution_id, runtime_backend_result)
        step_result["_runtime_backend_result"] = runtime_backend_result.to_dict()
    return RuntimeDispatchOutcome(
        step_result=step_result, runtime_backend_result=runtime_backend_result
    )
