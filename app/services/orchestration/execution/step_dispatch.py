"""Runtime dispatch and result normalization/persistence for the execution loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from app.models import TaskExecution
from app.services.agents.interfaces import RuntimeBackendResult
from app.services.orchestration.run_state import execution_progress_metadata
from app.services.orchestration.validation.runtime_pollution_guard import (
    build_runtime_pollution_provenance,
)


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
                    task_execution_id=task_execution_id,
                    session_instance_id=(_session.instance_id if _session else None),
                    level="INFO",
                    message="[TOKEN_USAGE_RECORDED]",
                    log_metadata=json.dumps(
                        execution_progress_metadata(
                            {
                                "task_execution_id": task_execution_id,
                                "task_id": task_execution.task_id,
                                "session_id": task_execution.session_id,
                                "tokens_in": result.tokens_in,
                                "tokens_out": result.tokens_out,
                                "token_source": result.token_source,
                            }
                        )
                    ),
                )
            )
        except Exception:
            pass
    if result.runtime_pollution:
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
                    task_execution_id=task_execution_id,
                    session_instance_id=(_session.instance_id if _session else None),
                    level=(
                        "ERROR"
                        if result.runtime_pollution.get("execution_must_stop")
                        else "INFO"
                    ),
                    message="[RUNTIME_POLLUTION_PROVENANCE]",
                    log_metadata=json.dumps(
                        result.runtime_pollution, default=str, sort_keys=True
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


def _runtime_process_provenance(
    *,
    runtime_service: Any,
    step_result: Dict[str, Any],
    context: Dict[str, Any],
    start_timestamp: str,
    end_timestamp: str,
) -> Dict[str, Any]:
    """Correlate the provider child with snapshots without claiming causation."""

    diagnostics = step_result.get("runtime_diagnostics") or {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    invocation = diagnostics.get("invocation") or {}
    if not isinstance(invocation, dict):
        invocation = {}
    runtime_contract = step_result.get("runtime_result") or {}
    if not isinstance(runtime_contract, dict):
        runtime_contract = {}
    runtime_root = runtime_contract.get("runtime_workspace") or getattr(
        runtime_service, "execution_cwd_override", None
    )
    cwd = invocation.get("cwd") or runtime_root
    command_identity = {
        "executable_path": invocation.get("executable_path"),
        "executable_args": invocation.get("executable_args") or [],
        "subcommand": invocation.get("subcommand"),
        "selected_agent": invocation.get("selected_agent"),
    }
    first_response_timestamp = diagnostics.get("first_response_timestamp")
    if first_response_timestamp is None:
        delay = diagnostics.get("first_output_after_seconds")
        if isinstance(delay, (int, float)) and delay >= 0:
            try:
                first_response_timestamp = (
                    datetime.fromisoformat(start_timestamp) + timedelta(seconds=delay)
                ).isoformat()
            except ValueError:
                first_response_timestamp = None
    return {
        "schema_version": "provider_process_provenance.v1",
        "pid": diagnostics.get("process_pid"),
        "parent_pid": os.getpid(),
        "argv": list(invocation.get("args_redacted") or []),
        "command_identity": command_identity,
        "cwd": cwd,
        "configured_workspace": runtime_root,
        "canonical_root": runtime_contract.get("project_workspace"),
        "runtime_root": runtime_root,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "first_response_timestamp": first_response_timestamp,
        "return_code": diagnostics.get("return_code"),
        "execution_step": context.get("execution_step"),
        "provider_phase": context.get("phase", "provider_initialization"),
        "invocation_kind": invocation.get("invocation_kind") or "execution",
        "causation_claim": "temporal_process_correlation_only",
    }


def _attach_runtime_pollution_provenance(
    *,
    runtime_service: Any,
    step_result: Dict[str, Any],
    context: Dict[str, Any],
    provider_start_timestamp: str,
    provider_end_timestamp: str,
) -> None:
    pollution = step_result.get("runtime_pollution")
    if not isinstance(pollution, dict):
        return
    runtime_contract = step_result.get("runtime_result") or {}
    if not isinstance(runtime_contract, dict):
        runtime_contract = {}
    diagnostics = step_result.get("runtime_diagnostics") or {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    phase_timestamps = dict(context.get("phase_timestamps") or {})
    phase_timestamps.setdefault("T2", provider_start_timestamp)
    phase_timestamps["T3"] = provider_start_timestamp
    phase_timestamps["T4"] = diagnostics.get("first_response_timestamp")
    phase_timestamps["T5"] = provider_end_timestamp
    phase_timestamps["T6"] = datetime.now(UTC).isoformat()
    process_provenance = _runtime_process_provenance(
        runtime_service=runtime_service,
        step_result=step_result,
        context=context,
        start_timestamp=provider_start_timestamp,
        end_timestamp=provider_end_timestamp,
    )
    project_id = runtime_contract.get("project_id")
    task_model = getattr(runtime_service, "task_model", None)
    if project_id is None and task_model is not None:
        project_id = getattr(task_model, "project_id", None)
    durable = build_runtime_pollution_provenance(
        pollution,
        project_id=project_id,
        session_id=runtime_contract.get("session_id")
        or getattr(runtime_service, "session_id", None),
        task_id=runtime_contract.get("task_id")
        or getattr(runtime_service, "task_id", None),
        task_execution_id=runtime_contract.get("task_execution_id")
        or getattr(runtime_service, "task_execution_id", None),
        execution_step=context.get("execution_step"),
        phase=context.get("phase", "provider_initialization"),
        canonical_root=runtime_contract.get("project_workspace"),
        runtime_root=runtime_contract.get("runtime_workspace")
        or getattr(runtime_service, "execution_cwd_override", None),
        phase_timestamps=phase_timestamps,
        process_provenance=[process_provenance],
    )
    phase_timestamps["T7"] = datetime.now(UTC).isoformat()
    durable["phase_timestamps"] = phase_timestamps
    durable["timestamp"] = durable.get("timestamp") or phase_timestamps["T7"]
    step_result["runtime_pollution"] = durable
    diagnostics["phase_timestamps"] = phase_timestamps
    diagnostics["process_provenance"] = [process_provenance]
    step_result["runtime_diagnostics"] = diagnostics


def dispatch_execution_runtime_step(
    *,
    runtime_service: Any,
    prompt: str,
    timeout_seconds: float,
    db: Any,
    task_execution_id: Optional[int],
    execution_step: Optional[int] = None,
    phase: str = "provider_initialization",
) -> RuntimeDispatchOutcome:
    """Dispatch one execution-runtime call and persist its normalized result.

    Mirrors the inline pattern previously duplicated at the primary and
    context-overflow-compact-retry call sites in ``execute_step_loop``:
    run the coroutine, normalize the raw result into a
    ``RuntimeBackendResult``, persist it, and stamp the normalized dict
    back onto ``step_result["_runtime_backend_result"]``.
    """

    runtime_started_at = time.monotonic()
    provider_start_timestamp = datetime.now(UTC).isoformat()
    previous_provenance_context = getattr(
        runtime_service, "_runtime_provenance_context", None
    )
    context = dict(previous_provenance_context or {})
    if execution_step is not None:
        context.update({"execution_step": execution_step, "phase": phase})
        setattr(runtime_service, "_runtime_provenance_context", context)
    try:
        step_result = _run_coroutine(
            runtime_service.execute_task(prompt, timeout_seconds=timeout_seconds)
        )
    finally:
        provider_end_timestamp = datetime.now(UTC).isoformat()
        if execution_step is not None:
            setattr(
                runtime_service,
                "_runtime_provenance_context",
                previous_provenance_context or {},
            )
    _attach_runtime_pollution_provenance(
        runtime_service=runtime_service,
        step_result=step_result,
        context=context,
        provider_start_timestamp=provider_start_timestamp,
        provider_end_timestamp=provider_end_timestamp,
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
