"""Read-only session query, filtering, and serialization helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from app.models import (
    InterventionRequest,
    LogEntry,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.auth.authorization import get_session_for_user
from app.services.orchestration.reporting.decision_timeline import (
    event_project_dir_candidates,
)

MAX_TOOL_TRACK_JSON_CHARS = 20_000

_SESSION_ORDER_COLUMNS = {
    "created_at": SessionModel.created_at,
    "updated_at": SessionModel.updated_at,
    "status": SessionModel.status,
    "name": SessionModel.name,
    "started_at": SessionModel.started_at,
}

_SESSION_ATTENTION_STATUSES = ("failed", "awaiting_input", "stopped")


def require_session_access(
    db: DBSession, session_id: int, current_user
) -> SessionModel:
    return get_session_for_user(db, session_id, current_user)


def serialize_session_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def json_payload_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return MAX_TOOL_TRACK_JSON_CHARS + 1


def serialize_intervention(req: InterventionRequest) -> Dict[str, Any]:
    return {
        "id": req.id,
        "session_id": req.session_id,
        "task_id": req.task_id,
        "project_id": req.project_id,
        "intervention_type": req.intervention_type,
        "initiated_by": req.initiated_by,
        "prompt": req.prompt,
        "context_snapshot": req.context_snapshot,
        "status": req.status,
        "operator_reply": req.operator_reply,
        "operator_id": req.operator_id,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "replied_at": req.replied_at.isoformat() if req.replied_at else None,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "updated_at": req.updated_at.isoformat() if req.updated_at else None,
    }


def get_session_intervention_or_404(
    db: DBSession, session_id: int, intervention_id: int
) -> InterventionRequest:
    intervention = (
        db.query(InterventionRequest)
        .filter(
            InterventionRequest.id == intervention_id,
            InterventionRequest.session_id == session_id,
        )
        .first()
    )
    if not intervention:
        raise HTTPException(status_code=404, detail="Intervention request not found")
    return intervention


def apply_session_filters(
    query,
    *,
    status: Optional[str],
    needs_attention: Optional[bool],
    project_id: Optional[int],
    search: Optional[str],
    created_before: Optional[str],
    created_after: Optional[str],
    is_active: Optional[bool],
):
    if project_id is not None:
        query = query.filter(SessionModel.project_id == project_id)
    if is_active is not None:
        query = query.filter(SessionModel.is_active == is_active)
    if status:
        query = query.filter(SessionModel.status == status)
    if needs_attention is True:
        query = query.filter(SessionModel.status.in_(_SESSION_ATTENTION_STATUSES))
    if search:
        query = query.filter(SessionModel.name.ilike(f"%{search}%"))
    if created_after:
        try:
            dt = datetime.fromisoformat(created_after)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            query = query.filter(SessionModel.created_at >= dt)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid created_after date format"
            )
    if created_before:
        try:
            dt = datetime.fromisoformat(created_before)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            query = query.filter(SessionModel.created_at <= dt)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid created_before date format"
            )
    return query


def apply_session_ordering(query, *, order_by: str, order_dir: str):
    col = _SESSION_ORDER_COLUMNS.get(order_by, SessionModel.created_at)
    if order_dir.lower() == "asc":
        query = query.order_by(col.asc().nullslast())
    else:
        query = query.order_by(col.desc().nullslast())
    return query


def serialize_failure_summary(db: DBSession, record) -> Dict[str, Any]:
    replan_session = None
    if record.replan_planning_session_id:
        replan_session = (
            db.query(PlanningSession)
            .filter(PlanningSession.id == record.replan_planning_session_id)
            .first()
        )

    latest_execution = (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == record.session_id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    from app.services.tasks.execution import task_execution_identity_payload

    return {
        "session_id": record.session_id,
        "summary": record.summary,
        "operator_feedback": record.operator_feedback,
        "generated_at": (
            record.generated_at.isoformat() if record.generated_at else None
        ),
        "feedback_at": record.feedback_at.isoformat() if record.feedback_at else None,
        "replan_planning_session_id": record.replan_planning_session_id,
        "replan_planning_session_status": (
            replan_session.status if replan_session else None
        ),
        "replan_planning_session_title": (
            replan_session.title if replan_session else None
        ),
        "latest_execution_identity": task_execution_identity_payload(latest_execution),
    }


_FAILURE_DIAGNOSTIC_KEYS = (
    "reason",
    "contract_violation_type",
    "validation_reasons",
    "contract_violations",
    "semantic_violation_codes",
    "brittle_command_subcodes",
    "brittle_command_step_details",
    "brittle_command_step_command_lengths",
    "max_command_length",
    "command_total_chars",
    "heredoc_command_count",
    "weak_verification_steps",
    "missing_verification_steps",
)


def latest_failure_diagnostics(db: DBSession, session_id: int) -> Dict[str, Any] | None:
    latest_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status == TaskStatus.FAILED,
        )
        .order_by(
            TaskExecution.completed_at.desc().nullslast(),
            TaskExecution.id.desc(),
        )
        .first()
    )
    query = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.log_metadata.isnot(None),
    )
    if latest_execution:
        query = query.filter(LogEntry.task_execution_id == latest_execution.id)

    candidates: list[Dict[str, Any]] = []
    for entry in query.order_by(LogEntry.created_at.desc(), LogEntry.id.desc()).limit(
        80
    ):
        try:
            metadata = json.loads(entry.log_metadata or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        diagnostic = {
            key: metadata[key]
            for key in _FAILURE_DIAGNOSTIC_KEYS
            if key in metadata and metadata[key] not in (None, [], {})
        }
        if not diagnostic:
            continue
        diagnostic["log_id"] = entry.id
        diagnostic["level"] = entry.level
        diagnostic["message"] = entry.message
        diagnostic["created_at"] = (
            entry.created_at.isoformat() if entry.created_at else None
        )
        diagnostic["task_id"] = entry.task_id
        diagnostic["task_execution_id"] = entry.task_execution_id
        candidates.append(diagnostic)

    if not candidates:
        return None

    terminal_reasons = {
        "planning_validation_failed_after_repair",
        "planning_invalid_commands_after_repair",
    }
    for candidate in candidates:
        if candidate.get("reason") in terminal_reasons:
            return candidate
    return candidates[0]


def latest_session_task_link(db: DBSession, session_id: int) -> Optional[SessionTask]:
    return (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id)
        .order_by(
            SessionTask.started_at.desc().nullslast(),
            SessionTask.id.desc(),
        )
        .first()
    )


def read_task_events_from_candidates(
    *,
    db: DBSession,
    project: Project,
    task: Optional[Task],
    session_id: int,
    task_id: int,
    event_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from app.services.orchestration import read_orchestration_events

    for project_dir in event_project_dir_candidates(
        db=db,
        project=project,
        task=task,
    ):
        events = read_orchestration_events(
            project_dir,
            session_id,
            task_id,
            event_type_filter=event_type_filter,
        )
        if events:
            return events
    return []


def resolve_replay_task_id(
    db: DBSession, session_id: int, requested_task_id: Optional[int]
) -> int:
    query = db.query(SessionTask).filter(SessionTask.session_id == session_id)
    if requested_task_id is not None:
        link = query.filter(SessionTask.task_id == requested_task_id).first()
        if not link:
            raise HTTPException(
                status_code=404,
                detail="Task is not linked to this session",
            )
        return int(requested_task_id)

    link = query.order_by(
        SessionTask.started_at.desc().nullslast(),
        SessionTask.id.desc(),
    ).first()
    if link and link.task_id is not None:
        return int(link.task_id)

    log_task_id = (
        db.query(LogEntry.task_id)
        .filter(LogEntry.session_id == session_id, LogEntry.task_id.isnot(None))
        .order_by(LogEntry.id.desc())
        .first()
    )
    if log_task_id and log_task_id[0] is not None:
        return int(log_task_id[0])
    raise HTTPException(status_code=404, detail="No task found for replay")


def build_replay_boundary(
    *,
    boundary_mode: Optional[str],
    event_id: Optional[str],
    timestamp: Optional[str],
    snapshot_index: Optional[int],
    checkpoint_name: Optional[str],
) -> Dict[str, Any]:
    mode = boundary_mode or "full"
    allowed_modes = {
        "full",
        "to_event_id",
        "to_timestamp",
        "to_snapshot_index",
        "to_checkpoint_name",
    }
    if mode not in allowed_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown boundary_mode '{mode}'",
        )
    if mode == "full":
        return {"mode": "full"}
    if mode == "to_event_id":
        if not event_id:
            raise HTTPException(
                status_code=400,
                detail="event_id is required for to_event_id replay",
            )
        return {"mode": mode, "requested": event_id}
    if mode == "to_timestamp":
        if not timestamp:
            raise HTTPException(
                status_code=400,
                detail="timestamp is required for to_timestamp replay",
            )
        return {"mode": mode, "requested": timestamp}
    if mode == "to_snapshot_index":
        if snapshot_index is None:
            raise HTTPException(
                status_code=400,
                detail="snapshot_index is required for to_snapshot_index replay",
            )
        return {"mode": mode, "requested": snapshot_index}
    if not checkpoint_name:
        raise HTTPException(
            status_code=400,
            detail="checkpoint_name is required for to_checkpoint_name replay",
        )
    return {"mode": mode, "requested": checkpoint_name}


def truncate_replay_report(report: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(report.get("state") or {})
    for key in ("timestamps", "tool_events", "reasoning_artifacts"):
        values = state.get(key)
        if isinstance(values, list) and len(values) > 20:
            state[key] = values[-20:]
    values = state.get("validation_verdict_status_history")
    if isinstance(values, list) and len(values) > 20:
        state["validation_verdict_status_history"] = values[-20:]

    integrity = dict(report.get("integrity") or {})
    findings = integrity.get("findings")
    if isinstance(findings, list):
        integrity["finding_count"] = len(findings)
        integrity["findings"] = findings[:25]

    drift_findings = report.get("drift_findings") or []
    return {
        "reducer_version": report.get("reducer_version"),
        "compatibility_version": report.get("compatibility_version"),
        "session_id": report.get("session_id"),
        "task_id": report.get("task_id"),
        "boundary": report.get("boundary"),
        "state": state,
        "field_classification": report.get("field_classification"),
        "integrity": integrity,
        "determinism": report.get("determinism"),
        "drift_findings": drift_findings[:25],
        "workspace_evidence": report.get("workspace_evidence"),
        "checkpoint_comparison": report.get("checkpoint_comparison"),
    }
