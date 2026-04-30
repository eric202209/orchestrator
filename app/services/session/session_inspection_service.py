"""Session log, workspace, and checkpoint inspection helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    InterventionRequest,
    LogEntry,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
)
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.agents.interfaces import AgentRuntimeError
from app.services.model_adaptation import get_adaptation_profile
from app.services.orchestration.policy import get_policy_profile
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.log_utils import deduplicate_logs
from app.services.orchestration.persistence import diff_orchestration_state_snapshots
from app.services.orchestration.persistence import (
    append_orchestration_event,
    read_orchestration_events,
    read_orchestration_state_snapshots,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.observability import (
    build_execution_dag,
    build_focus_mode_payload,
    build_mobile_interruption_cards,
    build_trace_export,
)
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

QUEUE_WATCHDOG_SLA_SECONDS = 30


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
        project_dir = _pick_event_project_dir(
            session_id=session.id,
            task_id=task.id,
            candidates=_event_project_dir_candidates(
                session,
                project,
                task_subfolder=getattr(task, "task_subfolder", None),
            ),
        )
        roots.append(
            {
                "task_id": task.id,
                "task_title": task.title,
                "project_dir": project_dir,
            }
        )
    return roots


def _event_project_dir_candidates(
    session: SessionModel, project: Any, *, task_subfolder: Optional[str] = None
) -> List[Path]:
    candidates: List[Path] = []
    resolved = resolve_project_workspace_path(project.workspace_path, project.name)
    candidates.append(resolved)
    raw_workspace = str(project.workspace_path or "").strip()
    if raw_workspace:
        raw_path = Path(raw_workspace).expanduser().resolve()
        if raw_path not in candidates:
            candidates.append(raw_path)
    if task_subfolder:
        extra: List[Path] = []
        for candidate in candidates:
            nested = (candidate / task_subfolder).resolve()
            if nested not in candidates and nested not in extra:
                extra.append(nested)
        candidates.extend(extra)
    return candidates


def _pick_event_project_dir(
    *,
    session_id: int,
    task_id: int,
    candidates: List[Path],
    prefer_snapshots: bool = False,
) -> Path:
    if not candidates:
        raise ValueError("No project-dir candidates provided")
    for candidate in candidates:
        if prefer_snapshots:
            snapshots = read_orchestration_state_snapshots(
                candidate, session_id, task_id
            )
            if snapshots:
                return candidate
        else:
            events = read_orchestration_events(candidate, session_id, task_id)
            if events:
                return candidate
    return candidates[0]


def _parse_timestamp(raw_value: Any) -> Optional[datetime]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _extract_failure_summary(
    event: Dict[str, Any],
    details: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    envelope = details.get("failure_envelope")
    if not isinstance(envelope, dict):
        return None

    stderr_text = str(envelope.get("stderr") or "").strip()
    output_payload = envelope.get("output")
    output_text = ""
    if isinstance(output_payload, dict):
        output_text = str(
            output_payload.get("error_message")
            or output_payload.get("verification_output")
            or output_payload.get("output")
            or ""
        ).strip()

    return {
        "schema_version": int(envelope.get("schema_version") or 1),
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "timestamp": event.get("timestamp"),
        "phase": envelope.get("phase"),
        "step_index": envelope.get("step_index"),
        "model_id": envelope.get("model_id"),
        "root_cause": envelope.get("root_cause"),
        "stderr_preview": stderr_text[:240] or None,
        "output_preview": output_text[:240] or None,
    }


def get_session_dispatch_watchdog_payload(
    db: Session,
    session_id: int,
    *,
    sla_seconds: int = QUEUE_WATCHDOG_SLA_SECONDS,
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    roots = _session_task_event_roots(db, session)
    now = datetime.now(UTC)
    task_summaries: List[Dict[str, Any]] = []
    stale_tasks: List[Dict[str, Any]] = []
    failure_history: List[Dict[str, Any]] = []

    for root in roots:
        events = read_orchestration_events(
            root["project_dir"],
            session.id,
            root["task_id"],
        )
        queue_event = None
        claim_event = None
        reject_event = None
        latest_failure_summary = None
        for event in reversed(events):
            event_type = str(event.get("event_type") or "")
            details = event.get("details") or {}
            failure_summary = _extract_failure_summary(event, details)
            if failure_summary:
                failure_summary["task_id"] = root["task_id"]
                failure_summary["task_title"] = root["task_title"]
                failure_history.append(failure_summary)
                if latest_failure_summary is None:
                    latest_failure_summary = failure_summary
            if queue_event is None and event_type == "task_queued":
                queue_event = event
            elif claim_event is None and event_type == "task_claimed":
                claim_event = event
            elif reject_event is None and event_type == "task_dispatch_rejected":
                reject_event = event
            if queue_event and claim_event and reject_event and latest_failure_summary:
                break

        queue_at = _parse_timestamp((queue_event or {}).get("timestamp"))
        claim_at = _parse_timestamp((claim_event or {}).get("timestamp"))
        reject_at = _parse_timestamp((reject_event or {}).get("timestamp"))
        latest_terminal = max(
            [item for item in [claim_at, reject_at] if item is not None],
            default=None,
        )
        queue_age_seconds = (
            round((now - queue_at).total_seconds(), 3) if queue_at is not None else None
        )
        is_stale = bool(
            queue_at is not None
            and queue_age_seconds is not None
            and queue_age_seconds > sla_seconds
            and (latest_terminal is None or latest_terminal < queue_at)
        )
        if claim_at and queue_at and claim_at >= queue_at:
            dispatch_state = "claimed"
        elif reject_at and queue_at and reject_at >= queue_at:
            dispatch_state = "rejected"
        elif queue_at:
            dispatch_state = "queued"
        else:
            dispatch_state = "unknown"

        summary = {
            "task_id": root["task_id"],
            "task_title": root["task_title"],
            "project_dir": root["project_dir"],
            "dispatch_state": dispatch_state,
            "queued_at": queue_at.isoformat() if queue_at else None,
            "claimed_at": claim_at.isoformat() if claim_at else None,
            "rejected_at": reject_at.isoformat() if reject_at else None,
            "queue_age_seconds": queue_age_seconds,
            "queue_latency_seconds": ((claim_event or {}).get("details", {}) or {}).get(
                "queue_latency_seconds"
            ),
            "queued_event_id": (queue_event or {}).get("event_id"),
            "claim_event_id": (claim_event or {}).get("event_id"),
            "reject_event_id": (reject_event or {}).get("event_id"),
            "stale": is_stale,
            "failure_root_cause": (
                latest_failure_summary.get("root_cause")
                if isinstance(latest_failure_summary, dict)
                else None
            ),
            "latest_failure": latest_failure_summary,
        }
        task_summaries.append(summary)
        if is_stale:
            stale_tasks.append(summary)

    sorted_failures = sorted(
        failure_history,
        key=lambda item: (
            _parse_timestamp(item.get("timestamp")) or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )

    return {
        "session_id": session.id,
        "sla_seconds": sla_seconds,
        "stale_task_count": len(stale_tasks),
        "has_stale_dispatches": bool(stale_tasks),
        "latest_failure": sorted_failures[0] if sorted_failures else None,
        "failure_history_preview": sorted_failures[:5],
        "tasks": task_summaries,
        "stale_tasks": stale_tasks,
    }


def refresh_session_dispatch_watchdog_alert(
    db: Session,
    session_id: int,
    *,
    sla_seconds: int = QUEUE_WATCHDOG_SLA_SECONDS,
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    watchdog = get_session_dispatch_watchdog_payload(
        db, session_id, sla_seconds=sla_seconds
    )
    if watchdog.get("has_stale_dispatches"):
        stale_tasks = list(watchdog.get("stale_tasks") or [])
        stale = stale_tasks[0] if stale_tasks else {}
        task_title = str(stale.get("task_title") or f"task {stale.get('task_id')}")
        age_value = stale.get("queue_age_seconds")
        age_text = (
            f"{float(age_value):.1f}s"
            if isinstance(age_value, (int, float))
            else "over SLA"
        )
        session.last_alert_level = "warning"
        session.last_alert_message = (
            f"Queued dispatch appears stalled for {task_title} ({age_text} in queue)."
        )[:2000]
        session.last_alert_at = datetime.now(UTC)
        for item in stale_tasks:
            project_dir = item.get("project_dir")
            if not project_dir:
                continue
            events = read_orchestration_events(
                project_dir,
                session.id,
                int(item.get("task_id") or 0),
            )
            latest_stale_event = next(
                (
                    event
                    for event in reversed(events)
                    if event.get("event_type") == EventType.TASK_QUEUE_STALE
                ),
                None,
            )
            queued_event_id = item.get("queued_event_id")
            if (
                latest_stale_event
                and ((latest_stale_event.get("details") or {}).get("queued_event_id"))
                == queued_event_id
            ):
                continue
            append_orchestration_event(
                project_dir=project_dir,
                session_id=session.id,
                task_id=int(item.get("task_id") or 0),
                event_type=EventType.TASK_QUEUE_STALE,
                details={
                    "task_title": item.get("task_title"),
                    "queued_event_id": queued_event_id,
                    "queue_age_seconds": item.get("queue_age_seconds"),
                    "sla_seconds": sla_seconds,
                    "failure_root_cause": item.get("failure_root_cause"),
                },
            )
    elif getattr(
        session, "last_alert_level", None
    ) == "warning" and "Queued dispatch appears stalled" in str(
        getattr(session, "last_alert_message", "") or ""
    ):
        session.last_alert_level = None
        session.last_alert_message = None
        session.last_alert_at = None
    db.commit()
    db.refresh(session)
    return watchdog


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
    dispatch_watchdog = get_session_dispatch_watchdog_payload(db, session_id)
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
        "reasoning_artifact": orchestration_state.get("reasoning_artifact"),
        "replay_source": {
            "requested_checkpoint_name": payload.get("_requested_checkpoint_name"),
            "resolved_checkpoint_name": payload.get("_resolved_checkpoint_name")
            or payload.get("checkpoint_name", checkpoint_name),
            "mode": payload.get("replay_mode") or "inspection",
        },
        "resume_readiness": resume_metadata,
        "restore_fidelity": restore_fidelity,
        "dispatch_watchdog": dispatch_watchdog,
        "latest_failure": dispatch_watchdog.get("latest_failure"),
        "failure_history_preview": dispatch_watchdog.get("failure_history_preview", []),
        "validation_history": validation_history[-10:],
        "plan_preview": plan[:5],
        "step_results_preview": step_results[-5:],
    }


def _latest_session_task_context(
    db: Session, session: SessionModel
) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    roots = _session_task_event_roots(db, session)
    if not roots:
        return None, [], []
    root = roots[-1]
    events = read_orchestration_events(root["project_dir"], session.id, root["task_id"])
    snapshots = read_orchestration_state_snapshots(
        root["project_dir"], session.id, root["task_id"]
    )
    return root, events, snapshots


def get_session_trace_export_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    root, events, snapshots = _latest_session_task_context(db, session)
    if root is None:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "task_id": None,
            "exporter_backend": settings.ORCHESTRATOR_TRACE_EXPORTER_BACKEND,
            "langfuse_handoff_ready": bool(settings.ORCHESTRATOR_LANGFUSE_ENABLED),
            "span_count": 0,
            "snapshot_count": 0,
            "spans": [],
        }
    return build_trace_export(
        session_id=session.id,
        task_id=root["task_id"],
        events=events,
        snapshots=snapshots,
        exporter_backend=settings.ORCHESTRATOR_TRACE_EXPORTER_BACKEND,
        include_langfuse_handoff=bool(settings.ORCHESTRATOR_LANGFUSE_ENABLED),
    )


def get_session_execution_dag_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    root, events, snapshots = _latest_session_task_context(db, session)
    if root is None:
        return {
            "session_id": session_id,
            "task_id": None,
            "node_count": 0,
            "edge_count": 0,
            "nodes": [],
            "edges": [],
        }
    return build_execution_dag(
        session_id=session.id,
        task_id=root["task_id"],
        events=events,
        snapshots=snapshots,
    )


def get_session_focus_mode_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    root, events, snapshots = _latest_session_task_context(db, session)
    pending_interventions = [
        {
            "id": item.id,
            "task_id": item.task_id,
            "prompt": item.prompt,
            "intervention_type": item.intervention_type,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in (
            db.query(InterventionRequest)
            .filter(
                InterventionRequest.session_id == session_id,
                InterventionRequest.status == "pending",
            )
            .order_by(InterventionRequest.created_at.desc())
            .all()
        )
    ]
    current_task = root if root else None
    return build_focus_mode_payload(
        session=session,
        current_task=current_task,
        events=events,
        snapshots=snapshots,
        pending_interventions=pending_interventions,
        dispatch_watchdog=get_session_dispatch_watchdog_payload(db, session_id),
    )


def get_session_mobile_interruptions_payload(
    db: Session, session_id: int
) -> Dict[str, Any]:
    session = _get_session_or_404(db, session_id)
    pending_interventions = [
        {
            "id": item.id,
            "task_id": item.task_id,
            "prompt": item.prompt,
            "status": item.status,
        }
        for item in (
            db.query(InterventionRequest)
            .filter(
                InterventionRequest.session_id == session_id,
                InterventionRequest.status == "pending",
            )
            .order_by(InterventionRequest.created_at.desc())
            .all()
        )
    ]
    return build_mobile_interruption_cards(
        session=session,
        dispatch_watchdog=get_session_dispatch_watchdog_payload(db, session_id),
        pending_interventions=pending_interventions,
    )


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
    from app.services.orchestration.events.event_types import EventType
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

    project_dir = _pick_event_project_dir(
        session_id=session_id,
        task_id=task_id,
        candidates=_event_project_dir_candidates(
            session, project, task_subfolder=getattr(task, "task_subfolder", None)
        ),
        prefer_snapshots=True,
    )
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
