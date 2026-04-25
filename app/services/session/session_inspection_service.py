"""Session log, workspace, and checkpoint inspection helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LogEntry, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.agents.interfaces import AgentRuntimeError
from app.services.model_adaptation import get_adaptation_profile
from app.services.orchestration.policy import get_policy_profile
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.log_utils import deduplicate_logs
from app.services.orchestration.persistence import diff_orchestration_state_snapshots
from app.services.orchestration.persistence import read_orchestration_events
from app.services.orchestration.persistence import (
    read_session_fingerprint_index,
    write_session_fingerprint_index,
    _apply_counterfactual_overrides_to_checkpoint,
)
from app.services.workspace.overwrite_protection_service import (
    OverwriteProtectionError,
    OverwriteProtectionService,
)
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.system_settings import (
    get_effective_adaptation_profile,
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_policy_profile,
)
from app.services.session.session_runtime_service import (
    get_session_task_subfolder,
    revoke_session_celery_tasks,
)


def _get_session_or_404(db: Session, session_id: int) -> SessionModel:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _session_task_event_roots(
    db: Session,
    session: SessionModel,
) -> List[Dict[str, Any]]:
    from app.models import Project

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        return []

    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    links = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id)
        .order_by(SessionTask.id.asc())
        .all()
    )
    roots: List[Dict[str, Any]] = []
    seen_task_ids: set[int] = set()
    for link in links:
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if not task:
            continue
        roots.append(
            {
                "task_id": task.id,
                "task_title": task.title,
                "project_dir": project_root,
            }
        )
    return roots


def _build_session_divergence_fingerprint(
    db: Session,
    session: SessionModel,
) -> Dict[str, Any]:
    roots = _session_task_event_roots(db, session)
    events: List[Dict[str, Any]] = []
    for root in roots:
        events.extend(
            read_orchestration_events(
                root["project_dir"],
                session.id,
                root["task_id"],
            )
        )
    events.sort(key=lambda item: str(item.get("timestamp") or ""))

    retries = [event for event in events if event.get("event_type") == "retry_entered"]
    tool_failures = [
        event for event in events if event.get("event_type") == "tool_failed"
    ]
    validation_results = [
        event for event in events if event.get("event_type") == "validation_result"
    ]
    divergence_events = [
        event for event in events if event.get("event_type") == "divergence_detected"
    ]
    intent_gaps = [
        event
        for event in events
        if event.get("event_type") == "intent_outcome_mismatch"
    ]
    health_events = [
        event for event in events if event.get("event_type") == "health_score_updated"
    ]

    anomaly_tags: set[str] = set()
    divergence_reasons: List[str] = []
    for event in divergence_events:
        reason = str(((event.get("details") or {}).get("reason") or "")).strip()
        if reason:
            anomaly_tags.add(f"divergence:{reason}")
            divergence_reasons.append(reason)
    if tool_failures:
        anomaly_tags.add("tool_failed")
    if retries:
        anomaly_tags.add("retry_entered")
    if intent_gaps:
        anomaly_tags.add("intent_gap")

    validation_statuses = [
        str(((event.get("details") or {}).get("status") or "")).lower()
        for event in validation_results
    ]
    for status_value in validation_statuses:
        if status_value:
            anomaly_tags.add(f"validation:{status_value}")

    min_health_score = None
    for event in health_events:
        score = (event.get("details") or {}).get("score")
        if isinstance(score, int):
            min_health_score = (
                score if min_health_score is None else min(min_health_score, score)
            )

    return {
        "session_id": session.id,
        "session_name": session.name,
        "status": session.status,
        "created_at": (
            session.created_at.isoformat()
            if getattr(session, "created_at", None)
            else None
        ),
        "task_count": len(roots),
        "event_count": len(events),
        "retry_count": len(retries),
        "tool_failure_count": len(tool_failures),
        "intent_gap_count": len(intent_gaps),
        "divergence_count": len(divergence_events),
        "divergence_reasons": sorted(set(divergence_reasons)),
        "validation_statuses": validation_statuses[-10:],
        "min_health_score": min_health_score,
        "anomaly_tags": sorted(anomaly_tags),
    }


def get_session_divergence_compare_payload(
    db: Session,
    session_id: int,
    *,
    limit: int = 5,
) -> Dict[str, Any]:
    from app.models import Project

    session = _get_session_or_404(db, session_id)
    current = _build_session_divergence_fingerprint(db, session)
    current_tags = set(current.get("anomaly_tags", []))

    project = db.query(Project).filter(Project.id == session.project_id).first()
    workspace_path = project.workspace_path if project else None

    # Write current fingerprint to index so siblings can reference it later.
    if workspace_path:
        try:
            write_session_fingerprint_index(workspace_path, session_id, current)
        except Exception:
            pass

    siblings = (
        db.query(SessionModel)
        .filter(
            SessionModel.project_id == session.project_id,
            SessionModel.id != session.id,
            SessionModel.deleted_at.is_(None),
        )
        .order_by(SessionModel.created_at.desc())
        .limit(25)
        .all()
    )

    matches: List[Dict[str, Any]] = []
    for candidate in siblings:
        fingerprint = None
        if workspace_path:
            # Completed sessions: long TTL.  Running sessions: short TTL.
            max_age = 300 if candidate.status in {"running", "active"} else 3600
            try:
                fingerprint = read_session_fingerprint_index(
                    workspace_path, candidate.id, max_age_seconds=max_age
                )
            except Exception:
                fingerprint = None
        if fingerprint is None:
            fingerprint = _build_session_divergence_fingerprint(db, candidate)
            if workspace_path:
                try:
                    write_session_fingerprint_index(
                        workspace_path, candidate.id, fingerprint
                    )
                except Exception:
                    pass
        candidate_tags = set(fingerprint.get("anomaly_tags", []))
        union = current_tags | candidate_tags
        overlap_score = 0.0
        if union:
            overlap_score = len(current_tags & candidate_tags) / len(union)
        count_penalty = (
            abs(
                int(current.get("retry_count") or 0)
                - int(fingerprint.get("retry_count") or 0)
            )
            * 0.05
        )
        similarity_score = max(0.0, round(overlap_score - count_penalty, 3))
        matches.append(
            {
                **fingerprint,
                "similarity_score": similarity_score,
                "shared_tags": sorted(current_tags & candidate_tags),
            }
        )

    matches.sort(
        key=lambda item: (
            item.get("similarity_score", 0),
            item.get("divergence_count", 0),
            item.get("retry_count", 0),
        ),
        reverse=True,
    )

    return {
        "session_id": session_id,
        "project_id": session.project_id,
        "current": current,
        "matches": matches[: max(1, min(limit, 10))],
    }


def _prepare_session_for_replay(db: Session, session: SessionModel) -> None:
    """Allow checkpoint replay even if the session is still marked running."""

    if session.status in {"paused", "stopped"}:
        return

    if session.status not in {"running", "active"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot replay checkpoint while session status is '{session.status}'",
        )

    revoked_ids = revoke_session_celery_tasks(db, session.id, terminate=True)

    running_links = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .all()
    )
    seen_task_ids: set[int] = set()
    for link in running_links:
        link.status = TaskStatus.PENDING
        link.completed_at = None
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if task and task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.PENDING
            task.completed_at = None
            task.error_message = None

    session.status = "paused"
    session.is_active = True
    session.paused_at = datetime.now(UTC)
    db.add(
        LogEntry(
            session_id=session.id,
            level="INFO",
            message="Prepared running session for checkpoint replay",
            log_metadata=json.dumps(
                {
                    "event_type": "session_replay_prepared",
                    "revoked_task_ids": revoked_ids,
                }
            ),
        )
    )
    db.commit()


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
    resume_metadata = checkpoint_service._checkpoint_resume_metadata(payload)
    restore_fidelity = checkpoint_service._checkpoint_restore_fidelity(payload)
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
        "resume_readiness": resume_metadata,
        "restore_fidelity": restore_fidelity,
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
    from app.services.session.session_lifecycle_service import resume_session_lifecycle

    session = _get_session_or_404(db, session_id)
    _prepare_session_for_replay(db, session)

    result = await resume_session_lifecycle(
        db,
        session_id,
        checkpoint_name=checkpoint_name,
    )
    result["success"] = True
    result["session_key"] = None
    result["replay_requested"] = True
    result["mode"] = "replay"
    result["replay_source"] = {
        "checkpoint_name": checkpoint_name,
        "mode": "replay",
    }
    result["message"] = f"Replay started from checkpoint: {checkpoint_name}"
    return result


async def replay_session_checkpoint_counterfactual_payload(
    db: Session,
    session_id: int,
    checkpoint_name: str,
    *,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Replay a checkpoint with variable overrides for counterfactual root-cause analysis.

    Applies overrides to the loaded checkpoint (e.g. rewind step, swap policy) then
    saves a derived checkpoint and starts execution from it.  Records a
    COUNTERFACTUAL_REPLAY_STARTED event in the event journal.
    """
    import uuid as _uuid
    import json as _json
    from pathlib import Path as _Path
    from app.services.session.session_lifecycle_service import resume_session_lifecycle
    from app.services.orchestration.event_types import EventType
    from app.services.orchestration.persistence import append_orchestration_event

    overrides = overrides or {}
    session = _get_session_or_404(db, session_id)
    _prepare_session_for_replay(db, session)

    checkpoint_service = CheckpointService(db)
    try:
        payload = checkpoint_service.load_checkpoint(session_id, checkpoint_name)
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail=f"Checkpoint not found: {exc}"
        ) from exc

    modified, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        payload, overrides=overrides
    )

    counterfactual_name = f"counterfactual_{checkpoint_name}_{_uuid.uuid4().hex[:8]}"
    cp_path = _Path(
        checkpoint_service._get_checkpoint_path(session_id, counterfactual_name)
    )
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    modified["checkpoint_name"] = counterfactual_name
    modified["replay_mode"] = "counterfactual"
    modified["_requested_checkpoint_name"] = checkpoint_name
    modified["_resolved_checkpoint_name"] = counterfactual_name
    cp_path.write_text(_json.dumps(modified, default=str), encoding="utf-8")

    roots = _session_task_event_roots(db, session)
    if roots:
        task_root = roots[-1]
        try:
            append_orchestration_event(
                project_dir=task_root["project_dir"],
                session_id=session_id,
                task_id=task_root["task_id"],
                event_type=EventType.COUNTERFACTUAL_REPLAY_STARTED,
                details={
                    "source_checkpoint": checkpoint_name,
                    "counterfactual_checkpoint": counterfactual_name,
                    "applied_overrides": applied,
                    "deferred_overrides": deferred,
                },
            )
        except Exception:
            pass

    result = await resume_session_lifecycle(
        db,
        session_id,
        checkpoint_name=counterfactual_name,
    )
    result["success"] = True
    result["replay_requested"] = True
    result["mode"] = "counterfactual"
    result["replay_source"] = {
        "source_checkpoint": checkpoint_name,
        "counterfactual_checkpoint": counterfactual_name,
        "mode": "counterfactual",
        "applied_overrides": applied,
        "deferred_overrides": deferred,
    }
    result["message"] = (
        f"Counterfactual replay started from checkpoint: {checkpoint_name}"
    )
    return result


def get_session_state_diff_payload(
    db: Session,
    session_id: int,
    *,
    from_checkpoint: Optional[int] = None,
    to_checkpoint: Optional[int] = None,
    task_id: Optional[int] = None,
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)

    if task_id is None:
        latest_link = (
            db.query(SessionTask)
            .filter(SessionTask.session_id == session_id)
            .order_by(
                SessionTask.started_at.desc().nullslast(),
                SessionTask.completed_at.desc().nullslast(),
                SessionTask.id.desc(),
            )
            .first()
        )
        if not latest_link:
            raise HTTPException(status_code=404, detail="Session has no linked task")
        task_id = latest_link.task_id

    task = (
        db.query(Task)
        .filter(Task.id == task_id, Task.project_id == session.project_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found for session")

    from app.models import Project

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = resolve_project_workspace_path(project.workspace_path, project.name)
    try:
        return diff_orchestration_state_snapshots(
            project_dir,
            session_id,
            task_id,
            from_checkpoint=from_checkpoint,
            to_checkpoint=to_checkpoint,
        )
    except ValueError as exc:
        msg = str(exc)
        if "No orchestration state snapshots found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc


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
