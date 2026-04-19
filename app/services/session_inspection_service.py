"""Session log, workspace, and checkpoint inspection helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel
from app.services.checkpoint_service import CheckpointService
from app.services.log_utils import deduplicate_logs
from app.services.openclaw_service import OpenClawSessionError, OpenClawSessionService
from app.services.overwrite_protection_service import (
    OverwriteProtectionError,
    OverwriteProtectionService,
)
from app.services.session_runtime_service import get_session_task_subfolder


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
    openclaw_service = OpenClawSessionService(db, session_id)
    try:
        await openclaw_service.pause_session()
    except OpenClawSessionError as exc:
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
    return {
        "session_id": session_id,
        "total_count": len(checkpoints),
        "checkpoints": checkpoints,
    }


async def load_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    _get_session_or_404(db, session_id)
    openclaw_service = OpenClawSessionService(db, session_id)
    try:
        session_key = await openclaw_service.resume_session(
            checkpoint_name=checkpoint_name
        )
    except OpenClawSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "success": True,
        "session_key": session_key,
        "message": f"Session resumed from checkpoint: {checkpoint_name}",
        "session_id": session_id,
    }


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
