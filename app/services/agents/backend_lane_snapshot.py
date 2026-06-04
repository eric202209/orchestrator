"""Read-only backend lane identity helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.models import LogEntry, TaskExecution

RUN_START_IDENTITY_SOURCE = "task_started_event"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lane_entry(role: str, backend_id: Any = None, model: Any = None) -> dict[str, Any]:
    return {
        "role": role,
        "backend_id": str(backend_id).strip() if backend_id else None,
        "model": str(model).strip() if model else None,
    }


def normalize_run_start_identity(identity: dict[str, Any] | None) -> dict[str, Any]:
    """Return a provider-neutral lane snapshot from existing run-start evidence."""

    raw = deepcopy(_as_dict(identity))
    lanes = _as_dict(raw.get("lanes"))
    models = _as_dict(raw.get("models"))
    config = _as_dict(raw.get("config"))
    effective = _as_dict(config.get("effective"))

    return {
        "source": raw.get("source") or RUN_START_IDENTITY_SOURCE,
        "captured_at": raw.get("captured_at"),
        "lanes": {
            "planning": _lane_entry(
                "planning",
                lanes.get("planning") or effective.get("planning_backend"),
                models.get("planner") or effective.get("planning_model"),
            ),
            "execution": _lane_entry(
                "execution",
                lanes.get("execution") or effective.get("execution_backend"),
                models.get("execution") or effective.get("execution_model"),
            ),
            "repair": _lane_entry(
                "repair",
                lanes.get("repair") or effective.get("repair_backend"),
                models.get("planning_repair") or effective.get("repair_model"),
            ),
            "debug_repair": _lane_entry(
                "debug_repair",
                lanes.get("debug_repair") or effective.get("debug_repair_backend"),
                models.get("debug_repair") or effective.get("debug_repair_model"),
            ),
        },
        "build": deepcopy(_as_dict(raw.get("build"))),
        "config": deepcopy(config),
        "legacy": {
            "agent_backend": effective.get("agent_backend"),
            "agent_model": effective.get("agent_model"),
        },
    }


def snapshot_from_task_execution(
    task_execution: TaskExecution | None,
    run_start_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only lane snapshot for a task execution attempt."""

    snapshot = normalize_run_start_identity(run_start_identity)
    if task_execution is None:
        snapshot["task_execution"] = None
        snapshot["legacy"]["task_execution_backend_id"] = None
        return snapshot

    backend_id = getattr(task_execution, "backend_id", None)
    snapshot["task_execution"] = {
        "id": getattr(task_execution, "id", None),
        "session_id": getattr(task_execution, "session_id", None),
        "task_id": getattr(task_execution, "task_id", None),
        "attempt_number": getattr(task_execution, "attempt_number", None),
        "status": (
            getattr(getattr(task_execution, "status", None), "value", None)
            or str(getattr(task_execution, "status", "") or "")
            or None
        ),
        "backend_id": backend_id,
    }
    snapshot["legacy"]["task_execution_backend_id"] = backend_id
    if backend_id and not snapshot["lanes"]["execution"]["backend_id"]:
        snapshot["lanes"]["execution"]["backend_id"] = backend_id
    return snapshot


def latest_run_start_identity_for_execution(
    db: Session,
    task_execution: TaskExecution,
) -> dict[str, Any] | None:
    """Read the latest task-start identity snapshot from existing log metadata."""

    entries = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == task_execution.session_id,
            LogEntry.task_id == task_execution.task_id,
            LogEntry.log_metadata.isnot(None),
        )
        .order_by(LogEntry.id.desc())
        .limit(100)
        .all()
    )
    for entry in entries:
        metadata = entry.log_metadata
        if isinstance(metadata, str):
            import json

            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                continue
        if not isinstance(metadata, dict):
            continue
        identity = metadata.get("run_start_runtime_identity")
        if isinstance(identity, dict):
            return identity
    return None


def snapshot_for_task_execution_id(
    db: Session,
    task_execution_id: int,
) -> dict[str, Any] | None:
    """Read-only convenience wrapper for callers with a task execution id."""

    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if execution is None:
        return None
    return snapshot_from_task_execution(
        execution,
        latest_run_start_identity_for_execution(db, execution),
    )
