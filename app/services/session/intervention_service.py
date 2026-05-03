"""Human-in-the-loop intervention service.

Lifecycle:
  1. Runtime (or operator) calls ``create_intervention_request`` — session
     transitions to ``awaiting_input``, Celery tasks are revoked, a
     checkpoint is saved, and a HUMAN_INTERVENTION_REQUESTED event is appended.
  2. Operator submits a reply, approval, or denial via one of the reply helpers
     — the InterventionRequest is updated, the operator guidance is injected
     into the latest checkpoint context, session transitions to ``paused``, and
     a HUMAN_INTERVENTION_REPLIED event is appended.
  3. Operator calls the normal resume endpoint — session picks up from the
     checkpoint with the guidance available in context.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from app.models import (
    InterventionRequest,
    LogEntry,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
)

logger = logging.getLogger(__name__)

INTERVENTION_TYPES = {"guidance", "approval", "information"}
INTERVENTION_STATUSES = {"pending", "replied", "approved", "denied", "expired"}

_DEFAULT_EXPIRY_MINUTES = 120


# ── helpers ──────────────────────────────────────────────────────────────────


def _get_session_or_404(db: DBSession, session_id: int) -> SessionModel:
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _get_intervention_or_404(
    db: DBSession, intervention_id: int
) -> InterventionRequest:
    req = (
        db.query(InterventionRequest)
        .filter(InterventionRequest.id == intervention_id)
        .first()
    )
    if not req:
        raise HTTPException(status_code=404, detail="Intervention request not found")
    return req


def _emit_intervention_event(
    db: DBSession,
    session_id: int,
    task_id: Optional[int],
    event_type: str,
    details: Dict[str, Any],
    session_instance_id: Optional[str] = None,
) -> None:
    from app.services.orchestration.persistence import append_orchestration_event
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )
    from app.models import Project, Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    project = (
        db.query(Project).filter(Project.id == session.project_id).first()
        if session and session.project_id
        else None
    )
    if project and project.workspace_path and task_id:
        from pathlib import Path

        workspace_path = str(
            resolve_project_workspace_path(project.workspace_path, project.name)
        )
        try:
            append_orchestration_event(
                project_dir=workspace_path,
                session_id=session_id,
                task_id=task_id,
                event_type=event_type,
                details=details,
            )
        except Exception:
            pass

    db.add(
        LogEntry(
            session_id=session_id,
            task_id=task_id,
            session_instance_id=session_instance_id
            or (session.instance_id if session else None),
            level="INFO",
            message=f"Intervention event: {event_type}",
            log_metadata=json.dumps({"event_type": event_type, **details}),
        )
    )
    db.commit()


# ── public API ────────────────────────────────────────────────────────────────


def create_intervention_request(
    db: DBSession,
    *,
    session_id: int,
    project_id: int,
    intervention_type: str,
    prompt: str,
    task_id: Optional[int] = None,
    context_snapshot: Optional[Dict[str, Any]] = None,
    expires_in_minutes: int = _DEFAULT_EXPIRY_MINUTES,
    initiated_by: str = "ai",
    revoke_running_tasks: bool = True,
) -> InterventionRequest:
    """Pause session execution and persist a HITL intervention request.

    Sets session.status = 'awaiting_input', revokes any running Celery
    tasks, saves the current checkpoint, and emits an event.

    Pass revoke_running_tasks=False when calling from inside the Celery task
    itself (e.g. sentinel detection in the execution loop) — the task is
    voluntarily stopping, so there is nothing to revoke.
    """
    if intervention_type not in INTERVENTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"intervention_type must be one of {sorted(INTERVENTION_TYPES)}",
        )

    session = _get_session_or_404(db, session_id)

    if session.status not in {"running", "paused", "awaiting_input"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot request intervention for session in state '{session.status}'",
        )

    from app.services.workspace.checkpoint_service import CheckpointService

    if revoke_running_tasks:
        from app.services.session.session_runtime_service import (
            revoke_session_celery_tasks,
        )

        revoke_session_celery_tasks(db, session_id, terminate=True)

    checkpoint_name: Optional[str] = None
    try:
        checkpoint_service = CheckpointService(db)
        latest = checkpoint_service.load_checkpoint(session_id)
        if latest:
            checkpoint_name = (
                f"intervention_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            )
            checkpoint_service.save_checkpoint(
                session_id=session_id,
                checkpoint_name=checkpoint_name,
                context_data=latest.get("context", {}),
                orchestration_state=latest.get("orchestration_state", {}),
                current_step_index=latest.get("current_step_index"),
                step_results=latest.get("step_results", []),
            )
    except Exception:
        checkpoint_name = None

    now = datetime.now(timezone.utc)
    req = InterventionRequest(
        session_id=session_id,
        task_id=task_id,
        project_id=project_id,
        intervention_type=intervention_type,
        initiated_by=initiated_by,
        prompt=prompt,
        context_snapshot=json.dumps(context_snapshot) if context_snapshot else None,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(minutes=expires_in_minutes),
    )
    db.add(req)

    session.status = "awaiting_input"
    session.is_active = True
    db.commit()
    db.refresh(req)

    _emit_intervention_event(
        db,
        session_id=session_id,
        task_id=task_id,
        event_type="human_intervention_requested",
        details={
            "intervention_id": req.id,
            "intervention_type": intervention_type,
            "checkpoint_name": checkpoint_name,
            "prompt_preview": prompt[:200],
        },
        session_instance_id=session.instance_id,
    )

    logger.info(
        "Intervention request %s created for session %s (type=%s)",
        req.id,
        session_id,
        intervention_type,
    )

    # For human-initiated queries, dispatch a background task so the AI answers.
    if initiated_by == "human":
        try:
            from app.tasks.worker import answer_human_intervention_query

            answer_human_intervention_query.delay(req.id, session_id)
        except Exception as _e:
            logger.warning("Could not dispatch AI answer task: %s", _e)

    return req


def _inject_reply_into_checkpoint(
    db: DBSession,
    session_id: int,
    reply_text: str,
    intervention_type: str,
    intervention_id: int,
) -> Optional[str]:
    """Prepend operator guidance into the latest checkpoint context."""
    from app.services.workspace.checkpoint_service import CheckpointService

    try:
        checkpoint_service = CheckpointService(db)
        latest = checkpoint_service.load_checkpoint(session_id)
        if not latest:
            return None
        ctx = latest.get("context", {}) or {}
        existing_guidance = ctx.get("human_guidance", "")
        entry = f"[Operator {intervention_type} #{intervention_id}]: {reply_text}"
        ctx["human_guidance"] = (
            (existing_guidance + "\n" + entry).strip() if existing_guidance else entry
        )
        reply_checkpoint_name = (
            f"intervention_reply_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        )
        checkpoint_service.save_checkpoint(
            session_id=session_id,
            checkpoint_name=reply_checkpoint_name,
            context_data=ctx,
            orchestration_state=latest.get("orchestration_state", {}),
            current_step_index=latest.get("current_step_index"),
            step_results=latest.get("step_results", []),
        )
        return reply_checkpoint_name
    except Exception as exc:
        logger.warning("Could not inject reply into checkpoint: %s", exc)
        return None


def _sync_linked_permission(
    db: DBSession,
    req: InterventionRequest,
    *,
    approved: bool,
    operator_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """If req was bridged from permission_service, sync the PermissionRequest status."""
    try:
        ctx = json.loads(req.context_snapshot) if req.context_snapshot else {}
    except Exception:
        return
    permission_request_id = ctx.get("permission_request_id")
    if not permission_request_id:
        return
    try:
        from app.services.permission_service import PermissionApprovalService

        svc = PermissionApprovalService(db)
        if approved:
            svc.approve_permission(
                permission_request_id, approved_by=operator_id or "operator"
            )
        else:
            svc.deny_permission(permission_request_id, reason=reason)
    except Exception as exc:
        logger.warning(
            "Could not sync permission_request %s after intervention %s: %s",
            permission_request_id,
            req.id,
            exc,
        )


def _dispatch_resume(
    db: DBSession,
    session: SessionModel,
    task_id: Optional[int],
    checkpoint_name: str,
) -> None:
    """Dispatch a Celery task to resume session execution from checkpoint.

    Called after operator reply/approve/deny so the session auto-continues
    without requiring a manual /resume call.
    """
    try:
        from app.tasks.worker import execute_orchestration_task

        task = db.query(Task).filter(Task.id == task_id).first() if task_id else None
        if not task:
            logger.warning(
                "Auto-resume skipped: no task found for session %s task_id=%s",
                session.id,
                task_id,
            )
            return
        prompt = task.description or task.title or ""
        session_task_link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session.id, SessionTask.task_id == task.id
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        if session_task_link:
            session_task_link.status = TaskStatus.PENDING
            session_task_link.started_at = None
            session_task_link.completed_at = None
        task.status = TaskStatus.PENDING
        task.started_at = None
        task.completed_at = None
        task.error_message = None
        db.commit()
        execute_orchestration_task.delay(
            session_id=session.id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=1800,
            resume_checkpoint_name=checkpoint_name,
            expected_session_instance_id=session.instance_id,
        )
        session.status = "running"
        session.is_active = True
        db.commit()
        logger.info(
            "Auto-resumed session %s from checkpoint %s after operator reply",
            session.id,
            checkpoint_name,
        )
    except Exception as exc:
        logger.warning(
            "Auto-resume dispatch failed for session %s: %s", session.id, exc
        )


def submit_intervention_reply(
    db: DBSession,
    *,
    intervention_id: int,
    operator_reply: str,
    operator_id: Optional[str] = None,
) -> InterventionRequest:
    """Record operator guidance and transition session back to paused."""
    req = _get_intervention_or_404(db, intervention_id)

    if req.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Intervention is already '{req.status}'",
        )

    now = datetime.now(timezone.utc)
    req.operator_reply = operator_reply
    req.operator_id = operator_id
    req.status = "replied"
    req.replied_at = now
    db.commit()

    reply_checkpoint = _inject_reply_into_checkpoint(
        db, req.session_id, operator_reply, req.intervention_type, req.id
    )

    session = db.query(SessionModel).filter(SessionModel.id == req.session_id).first()
    if session and session.status == "awaiting_input":
        session.status = "paused"
        session.paused_at = now
        db.commit()

    _emit_intervention_event(
        db,
        session_id=req.session_id,
        task_id=req.task_id,
        event_type="human_intervention_replied",
        details={
            "intervention_id": req.id,
            "intervention_type": req.intervention_type,
            "reply_checkpoint": reply_checkpoint,
            "reply_preview": operator_reply[:200],
        },
        session_instance_id=session.instance_id if session else None,
    )

    if reply_checkpoint and session:
        _dispatch_resume(db, session, req.task_id, reply_checkpoint)

    logger.info(
        "Intervention %s replied by %s; session %s → auto-resuming",
        req.id,
        operator_id or "unknown",
        req.session_id,
    )
    db.refresh(req)
    return req


def approve_intervention(
    db: DBSession,
    *,
    intervention_id: int,
    operator_id: Optional[str] = None,
) -> InterventionRequest:
    """Approve an approval-type intervention and resume as paused."""
    req = _get_intervention_or_404(db, intervention_id)

    if req.intervention_type != "approval":
        raise HTTPException(
            status_code=400,
            detail="Only 'approval' type interventions can be approved",
        )
    if req.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Intervention is already '{req.status}'",
        )

    now = datetime.now(timezone.utc)
    req.status = "approved"
    req.operator_id = operator_id
    req.replied_at = now
    req.operator_reply = "approved"
    db.commit()

    # Sync linked PermissionRequest if this intervention was bridged from permission_service
    _sync_linked_permission(db, req, approved=True, operator_id=operator_id)

    reply_checkpoint = _inject_reply_into_checkpoint(
        db, req.session_id, "Operator approved the proposed action.", "approval", req.id
    )

    session = db.query(SessionModel).filter(SessionModel.id == req.session_id).first()
    if session and session.status == "awaiting_input":
        session.status = "paused"
        session.paused_at = now
        db.commit()

    _emit_intervention_event(
        db,
        session_id=req.session_id,
        task_id=req.task_id,
        event_type="human_intervention_replied",
        details={
            "intervention_id": req.id,
            "intervention_type": "approval",
            "decision": "approved",
            "reply_checkpoint": reply_checkpoint,
        },
        session_instance_id=session.instance_id if session else None,
    )

    if reply_checkpoint and session:
        _dispatch_resume(db, session, req.task_id, reply_checkpoint)

    db.refresh(req)
    return req


def deny_intervention(
    db: DBSession,
    *,
    intervention_id: int,
    reason: Optional[str] = None,
    operator_id: Optional[str] = None,
) -> InterventionRequest:
    """Deny an approval-type intervention; session stays paused with context."""
    req = _get_intervention_or_404(db, intervention_id)

    if req.intervention_type != "approval":
        raise HTTPException(
            status_code=400,
            detail="Only 'approval' type interventions can be denied",
        )
    if req.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Intervention is already '{req.status}'",
        )

    now = datetime.now(timezone.utc)
    req.status = "denied"
    req.operator_id = operator_id
    req.replied_at = now
    req.operator_reply = reason or "denied"
    db.commit()

    # Sync linked PermissionRequest if this intervention was bridged from permission_service
    _sync_linked_permission(db, req, approved=False, reason=reason)

    denial_text = (
        f"Operator denied the proposed action. Reason: {reason or 'none provided'}"
    )
    reply_checkpoint = _inject_reply_into_checkpoint(
        db, req.session_id, denial_text, "approval", req.id
    )

    session = db.query(SessionModel).filter(SessionModel.id == req.session_id).first()
    if session and session.status == "awaiting_input":
        session.status = "paused"
        session.paused_at = now
        db.commit()

    _emit_intervention_event(
        db,
        session_id=req.session_id,
        task_id=req.task_id,
        event_type="human_intervention_replied",
        details={
            "intervention_id": req.id,
            "intervention_type": "approval",
            "decision": "denied",
            "reason": reason,
            "reply_checkpoint": reply_checkpoint,
        },
        session_instance_id=session.instance_id if session else None,
    )

    # Auto-resume even on denial: the agent picks up with denial context and
    # adjusts its approach (e.g. proposes an alternative).
    if reply_checkpoint and session:
        _dispatch_resume(db, session, req.task_id, reply_checkpoint)

    db.refresh(req)
    return req


def get_pending_interventions(
    db: DBSession,
    *,
    session_id: Optional[int] = None,
    project_id: Optional[int] = None,
    limit: int = 50,
) -> List[InterventionRequest]:
    query = db.query(InterventionRequest).filter(
        InterventionRequest.status == "pending"
    )
    if session_id is not None:
        query = query.filter(InterventionRequest.session_id == session_id)
    if project_id is not None:
        query = query.filter(InterventionRequest.project_id == project_id)
    return query.order_by(InterventionRequest.created_at.desc()).limit(limit).all()


def get_intervention_history(
    db: DBSession,
    *,
    session_id: int,
    limit: int = 100,
) -> List[InterventionRequest]:
    return (
        db.query(InterventionRequest)
        .filter(InterventionRequest.session_id == session_id)
        .order_by(InterventionRequest.created_at.desc())
        .limit(limit)
        .all()
    )
