"""Session log, workspace, and checkpoint inspection helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LogEntry, Session as SessionModel
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.agents.interfaces import AgentRuntimeError
from app.services.model_adaptation import get_adaptation_profile
from app.services.orchestration.policy import get_policy_profile
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.log_utils import deduplicate_logs
from app.services.workspace.overwrite_protection_service import (
    OverwriteProtectionError,
    OverwriteProtectionService,
)
from app.services.workspace.system_settings import (
    get_effective_adaptation_profile,
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_policy_profile,
)
from app.services.session.session_runtime_service import get_session_task_subfolder


def _get_session_or_404(db: Session, session_id: int) -> SessionModel:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def get_session_logs_payload(
    db: Session, session_id: int, *, limit: Optional[int] = 100, offset: int = 0
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    effective_limit = min(limit if limit else 100, 1000)

    logs_query = db.query(LogEntry).filter(LogEntry.session_id == session_id)
    if session.instance_id:
        logs_query = logs_query.filter(
            LogEntry.session_instance_id == session.instance_id
        )

    logs = (
        logs_query.order_by(LogEntry.created_at.desc())
        .offset(offset)
        .limit(effective_limit)
        .all()
    )
    return {"logs": logs, "total": logs_query.count()}


def get_sorted_logs_payload(
    db: Session,
    session_id: int,
    *,
    order: str = "asc",
    deduplicate: bool = True,
    level: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    effective_limit = min(limit if limit else 100, 1000)

    logs_query = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.session_instance_id == session.instance_id,
    )
    if level:
        logs_query = logs_query.filter(LogEntry.level == level)

    total_logs = logs_query.count()
    if order == "desc":
        logs_query = logs_query.order_by(LogEntry.created_at.desc())
    else:
        logs_query = logs_query.order_by(LogEntry.created_at.asc())

    logs_entries = logs_query.offset(offset).limit(effective_limit).all()
    logs = [
        {
            "id": log.id,
            "session_id": log.session_id,
            "task_id": log.task_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
        }
        for log in logs_entries
    ]
    if deduplicate:
        logs = deduplicate_logs(logs)

    return {
        "session_id": session_id,
        "session_instance_id": session.instance_id,
        "total_logs": total_logs,
        "returned_logs": len(logs),
        "offset": offset,
        "limit": effective_limit,
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": logs,
        "has_more": (offset + len(logs)) < total_logs,
    }


def check_session_overwrites_payload(
    db: Session,
    session_id: int,
    *,
    project_id: int,
    task_subfolder: str,
    planned_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    protection = OverwriteProtectionService(db)
    try:
        result = protection.check_and_warn(
            project_id=project_id,
            task_subfolder=task_subfolder,
            planned_files=planned_files or [],
            action="warn",
        )
    except OverwriteProtectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "safe_to_proceed": result["safe_to_proceed"],
        "workspace_exists": result.get("workspace_exists", False),
        "file_count": result.get("file_count", 0),
        "would_overwrite": result.get("has_conflicts", False),
        "warning_message": result.get("warning_message"),
        "conflicting_files": result.get("conflict_info", {}).get(
            "conflicting_files", []
        ),
    }


def create_session_backup_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    protection = OverwriteProtectionService(db)
    project_id = session.project_id or 1

    backup_result = protection.create_backup_of_existing(
        project_id=project_id,
        task_subfolder=get_session_task_subfolder(db, session),
    )
    return {
        "success": backup_result["success"],
        "backup_path": backup_result.get("backup_path"),
        "files_backed_up": backup_result.get("file_count", 0),
        "error": backup_result.get("error"),
    }


def get_session_workspace_info_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    protection = OverwriteProtectionService(db)
    project_id = session.project_id or 1

    workspace_info = protection.check_workspace_exists(
        project_id=project_id,
        task_subfolder=get_session_task_subfolder(db, session),
    )
    return {
        "exists": workspace_info.get("exists", False),
        "path": workspace_info.get("path"),
        "file_count": workspace_info.get("file_count", 0),
        "last_modified": workspace_info.get("last_modified"),
        "would_overwrite": workspace_info.get("would_overwrite", False),
    }


async def save_session_checkpoint_payload(
    db: Session, session_id: int
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    runtime = create_agent_runtime(db, session_id)
    try:
        await runtime.pause_session()
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "success": True,
        "message": "Checkpoint saved successfully",
        "session_id": session_id,
    }


def list_session_checkpoints_payload(db: Session, session_id: int) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    checkpoint_service = CheckpointService(db)
    checkpoints = checkpoint_service.list_checkpoints(session_id)
    recommended_checkpoint_name = checkpoint_service.resolve_resume_checkpoint_name(
        session_id
    )
    return {
        "session_id": session_id,
        "total_count": len(checkpoints),
        "recommended_checkpoint_name": recommended_checkpoint_name,
        "checkpoints": checkpoints,
    }


def inspect_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    checkpoint_service = CheckpointService(db)
    payload = checkpoint_service.load_checkpoint(session_id, checkpoint_name)
    orchestration_state = payload.get("orchestration_state", {}) or {}
    validation_history = orchestration_state.get("validation_history", []) or []
    plan = orchestration_state.get("plan", []) or []
    step_results = payload.get("step_results", []) or []

    latest_validation = validation_history[-1] if validation_history else None
    latest_plan_validation = orchestration_state.get("last_plan_validation")
    latest_completion_validation = orchestration_state.get("last_completion_validation")
    runtime_metadata = (
        payload.get("runtime_metadata")
        or orchestration_state.get("runtime_metadata")
        or {}
    )
    current_backend = get_effective_agent_backend(
        settings.ORCHESTRATOR_AGENT_BACKEND, db=db
    )
    current_model_family = get_effective_agent_model_family(
        settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
    )
    current_policy = get_effective_policy_profile(db=db)
    current_adaptation = get_effective_adaptation_profile(db=db)
    validation_verdicts = {
        "latest_status": (
            (
                latest_completion_validation
                or latest_plan_validation
                or latest_validation
            )
            or {}
        ).get("status"),
        "plan_status": (latest_plan_validation or {}).get("status"),
        "completion_status": (latest_completion_validation or {}).get("status"),
    }

    return {
        "session_id": session_id,
        "checkpoint_name": payload.get("checkpoint_name", checkpoint_name),
        "created_at": payload.get("created_at"),
        "current_step_index": payload.get("current_step_index"),
        "summary": {
            "plan_step_count": len(plan),
            "completed_step_count": len(step_results),
            "execution_result_count": len(
                orchestration_state.get("execution_results", []) or []
            ),
            "status": orchestration_state.get("status"),
            "relaxed_mode": bool(orchestration_state.get("relaxed_mode")),
            "completion_repair_attempts": int(
                orchestration_state.get("completion_repair_attempts") or 0
            ),
        },
        "context": {
            "project_name": (payload.get("context", {}) or {}).get("project_name"),
            "task_subfolder": (payload.get("context", {}) or {}).get("task_subfolder"),
            "project_dir_override": (payload.get("context", {}) or {}).get(
                "project_dir_override"
            ),
        },
        "latest_validation": latest_validation,
        "latest_plan_validation": latest_plan_validation,
        "latest_completion_validation": latest_completion_validation,
        "runtime_metadata": {
            "backend": runtime_metadata.get("backend") or current_backend,
            "model_family": runtime_metadata.get("model_family")
            or current_model_family,
            "policy_profile": runtime_metadata.get("policy_profile")
            or get_policy_profile(current_policy).name,
            "adaptation_profile": runtime_metadata.get("adaptation_profile")
            or get_adaptation_profile(current_adaptation).name,
            "derived_from_current_settings": not bool(runtime_metadata),
        },
        "validation_verdicts": validation_verdicts,
        "replay_source": {
            "requested_checkpoint_name": payload.get("_requested_checkpoint_name"),
            "resolved_checkpoint_name": payload.get("_resolved_checkpoint_name")
            or payload.get("checkpoint_name", checkpoint_name),
            "mode": payload.get("replay_mode") or "inspection",
        },
        "validation_history": validation_history[-10:],
        "plan_preview": plan[:5],
        "step_results_preview": step_results[-5:],
    }


async def load_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    runtime = create_agent_runtime(db, session_id)
    try:
        session_key = await runtime.resume_session(checkpoint_name=checkpoint_name)
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "success": True,
        "session_key": session_key,
        "message": f"Session resumed from checkpoint: {checkpoint_name}",
        "session_id": session_id,
        "mode": "resume",
        "replay_source": {
            "checkpoint_name": checkpoint_name,
            "mode": "resume",
        },
    }


async def replay_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    result = await load_session_checkpoint_payload(db, session_id, checkpoint_name)
    result["replay_requested"] = True
    result["mode"] = "replay"
    result["replay_source"] = {
        "checkpoint_name": checkpoint_name,
        "mode": "replay",
    }
    result["message"] = f"Replay started from checkpoint: {checkpoint_name}"
    return result


def delete_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    checkpoint_service = CheckpointService(db)
    deleted = checkpoint_service.delete_checkpoint(session_id, checkpoint_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    return {
        "success": True,
        "message": f"Checkpoint '{checkpoint_name}' deleted successfully",
        "session_id": session_id,
        "checkpoint_name": checkpoint_name,
    }


def cleanup_session_checkpoints_payload(
    db: Session, session_id: int, *, keep_latest: int = 3, max_age_hours: int = 24
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    checkpoint_service = CheckpointService(db)
    result = checkpoint_service.cleanup_old_checkpoints(
        session_id=session_id, keep_latest=keep_latest, max_age_hours=max_age_hours
    )
    return {
        "success": True,
        "deleted_count": result.get("deleted", 0),
        "kept_count": result.get("kept", 0),
        "error": result.get("error"),
    }


def cleanup_orphaned_checkpoints_payload(db: Session) -> Dict[str, Any]:
    checkpoint_service = CheckpointService(db)
    result = checkpoint_service.cleanup_orphaned_checkpoints()
    if result.get("error"):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup orphaned checkpoints: {result['error']}",
        )

    db.commit()
    return {
        "success": True,
        "deleted_files": result.get("deleted_files", 0),
        "deleted_dirs": result.get("deleted_dirs", 0),
        "orphaned_session_ids": result.get("orphaned_session_ids", []),
    }
