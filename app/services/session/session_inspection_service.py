"""Session log, workspace, and checkpoint inspection helpers."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    InterventionRequest,
    LogEntry,
    PermissionRequest,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
)
from app.services.agents.interfaces import AgentRuntimeError
from app.services.model_adaptation import get_adaptation_profile
from app.services.orchestration.run_state import mark_task_attempt_pending
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.policy import get_policy_profile
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.log_utils import deduplicate_logs
from app.services.orchestration.state.persistence import (
    diff_orchestration_state_snapshots,
)
from app.services.orchestration.state.persistence import (
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
from app.services.orchestration.state.persistence import (
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
from .session_lookup import get_session_or_404

QUEUE_WATCHDOG_SLA_SECONDS = 30
_DIGEST_ENRICHMENT_CACHE: Dict[str, Dict[str, Any]] = {}

# ── Orchestration-state derivation tables ─────────────────────────────────────

_STATUS_TO_PHASE: Dict[str, Optional[str]] = {
    "pending": None,
    "running": "step_executing",
    "paused": "awaiting_input",
    "awaiting_input": "awaiting_input",
    "stopped": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "failed",
    "done": "done",
    "completed": "done",
}

_TERMINAL_PHASES: frozenset = frozenset({"done", "failed", "cancelled"})

_PHASE_TO_COORDINATOR: Dict[str, str] = {
    "step_executing": "ExecutionCoordinator",
    "awaiting_input": "ExecutionCoordinator",
}

_STATUS_TO_ALLOWED_ACTIONS: Dict[str, List[str]] = {
    "pending": ["view_logs", "view_timeline", "start_session"],
    "running": ["view_logs", "view_timeline", "pause_session", "stop_session"],
    "paused": ["view_logs", "view_timeline", "resume_session", "stop_session"],
    "awaiting_input": ["view_logs", "view_timeline", "submit_guidance", "stop_session"],
    "stopped": ["view_logs", "view_timeline", "resume_session", "start_session"],
    "cancelled": ["view_logs", "view_timeline", "resume_session", "start_session"],
    "canceled": ["view_logs", "view_timeline", "resume_session", "start_session"],
    "failed": [
        "view_logs",
        "view_timeline",
        "resume_session",
        "retry_task",
        "start_session",
    ],
    "done": ["view_logs", "view_timeline", "start_session"],
    "completed": ["view_logs", "view_timeline", "start_session"],
}


def derive_orchestration_state_block(
    db: Session,
    session: SessionModel,
    *,
    latest_task_execution: Optional[TaskExecution] = None,
) -> Dict[str, Any]:
    """Derive the operator-facing orchestration state block from session status.

    Returns a server-derived block with no DB columns written and no runtime
    behaviour changed.  All fields are derived from existing session state and
    task execution records.
    """
    status = str(session.status or "")
    current_phase = _STATUS_TO_PHASE.get(status)

    terminal_reason: Optional[str] = None
    if current_phase in _TERMINAL_PHASES:
        if latest_task_execution is not None:
            terminal_reason = (
                str(latest_task_execution.failure_category).strip() or None
                if latest_task_execution.failure_category
                else None
            )
        if terminal_reason is None:
            failed_exec = (
                db.query(TaskExecution)
                .filter(
                    TaskExecution.session_id == session.id,
                    TaskExecution.failure_category.isnot(None),
                )
                .order_by(
                    TaskExecution.completed_at.desc().nullslast(),
                    TaskExecution.id.desc(),
                )
                .first()
            )
            if failed_exec:
                terminal_reason = str(failed_exec.failure_category).strip() or None

    coordinator = _PHASE_TO_COORDINATOR.get(current_phase) if current_phase else None
    is_terminal = current_phase in _TERMINAL_PHASES if current_phase else False
    allowed_actions = list(
        _STATUS_TO_ALLOWED_ACTIONS.get(status, ["view_logs", "view_timeline"])
    )

    return {
        "current_phase": current_phase,
        "terminal_reason": terminal_reason,
        "coordinator": coordinator,
        "is_terminal": is_terminal,
        "allowed_actions": allowed_actions,
    }


def _latest_validation_evidence(
    db: Session, *, session_id: int, task_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    query = db.query(TaskCheckpoint).filter(
        TaskCheckpoint.session_id == session_id,
        TaskCheckpoint.checkpoint_type == "validation_task_completion",
    )
    if task_id is not None:
        query = query.filter(TaskCheckpoint.task_id == task_id)
    checkpoint = query.order_by(TaskCheckpoint.id.desc()).first()
    if not checkpoint or not checkpoint.state_snapshot:
        return None
    try:
        payload = json.loads(checkpoint.state_snapshot)
    except (TypeError, json.JSONDecodeError):
        return None
    details = payload.get("details") if isinstance(payload, dict) else None
    if not isinstance(details, dict):
        return None
    evidence = details.get("validation_evidence")
    return evidence if isinstance(evidence, dict) else None


def get_inspectable_session_or_404(db: Session, session_id: int) -> SessionModel:
    return get_session_or_404(db, session_id, include_deleted=True)


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


def _extract_model_lane_limitation(error_message: Any) -> Optional[str]:
    """Extract the model-lane limitation key from a task error message string."""
    text = str(error_message or "")
    m = re.search(r"model_lane_limitation=([^\s;,]+)", text)
    if m:
        return m.group(1)
    if (
        "repeated_stale_exact_patch_after_capsule" in text
        or "model_lane_repeated_stale_exact_patch" in text
    ):
        return "repeated_stale_exact_patch_after_capsule"
    return None


def _stronger_lane_available() -> bool:
    """Return True if a secondary stronger planning backend is configured."""
    secondary = str(settings.AGENT_SECONDARY_BACKEND or "").strip()
    if not secondary:
        return False
    try:
        descriptor = get_backend_descriptor(secondary)
    except UnsupportedAgentBackendError:
        return False
    return bool(
        descriptor.implemented
        and descriptor.available
        and descriptor.capabilities.supports_planning
    )


def _stronger_lane_summary() -> Dict[str, Any]:
    """Return configured secondary lane state without implying provider prestige."""
    secondary = str(settings.AGENT_SECONDARY_BACKEND or "").strip()
    if not secondary:
        return {
            "configured": False,
            "available": False,
            "backend": None,
            "label": "unavailable",
            "capability_traits": {
                "configured_available": False,
            },
            "reasons": ["AGENT_SECONDARY_BACKEND is not configured"],
        }
    try:
        descriptor = get_backend_descriptor(secondary)
    except UnsupportedAgentBackendError as exc:
        return {
            "configured": True,
            "available": False,
            "backend": secondary,
            "label": "unsupported",
            "capability_traits": {
                "configured_available": False,
            },
            "reasons": [str(exc)],
        }

    descriptor_payload = descriptor.to_dict()
    available = bool(
        descriptor.implemented
        and descriptor.available
        and descriptor.capabilities.supports_planning
    )
    reasons: List[str] = []
    if not descriptor.implemented:
        reasons.append("Backend is registered but not implemented")
    if not descriptor.capabilities.supports_planning:
        reasons.append("Backend does not support planning")
    reasons.extend(descriptor.health.errors)
    return {
        "configured": True,
        "available": available,
        "backend": descriptor.name,
        "label": descriptor.display_name,
        "capability_traits": descriptor_payload.get("lane_traits", {}),
        "reasons": reasons,
    }


def _find_stale_old_text_in_logs(db: Session, session_id: int) -> List[str]:
    """Scan planning phase-event log entries for stale_old_text emitted at model-lane stop.

    Returns the list (possibly empty) to include as operator evidence in rerun payload.
    Must not be injected into model prompts.
    """
    entries = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.level == "ERROR",
        )
        .order_by(LogEntry.created_at.desc())
        .limit(50)
        .all()
    )
    for entry in entries:
        meta = entry.log_metadata or {}
        if not isinstance(meta, dict):
            continue
        if meta.get("reason") != "planning_invalid_commands_after_repair":
            continue
        texts = meta.get("stale_old_text")
        if isinstance(texts, list) and texts:
            return texts
    return []


def _classify_test_scaffold_failure(error_message: Any) -> Optional[str]:
    text = str(error_message or "").lower()
    if not text:
        return None
    if (
        "repeated_stale_exact_patch_after_capsule" in text
        or "model_lane_repeated_stale_exact_patch" in text
    ):
        return "model_lane_repeated_stale_exact_patch"
    if (
        "patch_strategy_fallback_required" in text
        or "post_repair_stale_replace_fallback" in text
        or "exact-text patching is exhausted" in text
    ):
        return "patch_strategy_fallback_required"
    if (
        "test_assertion_preservation_failed" in text
        or "test_assertion_loss_ops_steps" in text
        or "fewer assertions" in text
    ):
        return "test_assertion_preservation_failed"
    if "test_deletion_ops_steps" in text or "test_preservation_violation" in text:
        return "test_preservation_violation"
    if "stale_replace_ops_steps" in text or "old text not found" in text:
        return "stale_replace_in_file_old_text"
    if "nameerror" in text or "importerror" in text or "no module named" in text:
        return "test_scaffold_import_error"
    if (
        "dict" in text
        and ("has no attribute" in text or "attributeerror" in text)
        and ("test" in text or "pytest" in text)
    ):
        return "test_scaffold_type_mismatch"
    if "unexpected keyword argument" in text and ("test" in text or "pytest" in text):
        return "test_scaffold_constructor_mismatch"
    return None


def get_session_dispatch_watchdog_payload(
    db: Session,
    session_id: int,
    *,
    sla_seconds: int = QUEUE_WATCHDOG_SLA_SECONDS,
) -> Dict[str, Any]:
    session = get_inspectable_session_or_404(db, session_id)
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


def get_session_reconciliation_audit_payload(
    db: Session,
    session_id: int,
) -> Dict[str, Any]:
    session = get_inspectable_session_or_404(db, session_id)
    active_task_executions = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status == TaskStatus.RUNNING,
        )
        .order_by(TaskExecution.started_at.desc().nullslast(), TaskExecution.id.desc())
        .all()
    )
    pending_tasks = (
        db.query(Task)
        .filter(
            Task.project_id == session.project_id, Task.status == TaskStatus.PENDING
        )
        .order_by(Task.plan_position.asc().nullslast(), Task.id.asc())
        .all()
    )
    intervention_requests = (
        db.query(InterventionRequest)
        .filter(
            InterventionRequest.session_id == session_id,
            InterventionRequest.status.in_(["pending", "awaiting_reply", "open"]),
        )
        .order_by(InterventionRequest.created_at.desc().nullslast())
        .all()
    )
    permission_requests = (
        db.query(PermissionRequest)
        .filter(
            PermissionRequest.session_id == session_id,
            PermissionRequest.status.in_(["pending", "requested", "awaiting_approval"]),
        )
        .order_by(PermissionRequest.created_at.desc().nullslast())
        .all()
    )
    reasons, category = _extract_stop_reasons(db, session)
    explicit_reason = None
    if session.status == "paused":
        if intervention_requests:
            explicit_reason = "waiting_intervention"
        elif permission_requests:
            explicit_reason = "waiting_permission"
        elif active_task_executions:
            explicit_reason = "waiting_operator"
        elif pending_tasks:
            explicit_reason = (
                "blocked_dependency" if category != "operator_paused" else None
            )

    scheduler_bug = bool(
        session.status == "paused" and pending_tasks and explicit_reason is None
    )

    return {
        "session_id": session.id,
        "session_status": session.status,
        "session_is_active": session.is_active,
        "project_id": session.project_id,
        "active_task_execution_count": len(active_task_executions),
        "pending_task_count": len(pending_tasks),
        "intervention_request_count": len(intervention_requests),
        "permission_request_count": len(permission_requests),
        "stop_category": category,
        "stop_reasons": reasons,
        "explicit_pause_reason": explicit_reason,
        "scheduler_bug": scheduler_bug,
        "active_task_executions": [
            {
                "task_execution_id": execution.id,
                "task_id": execution.task_id,
                "task_status": (
                    execution.status.value
                    if hasattr(execution.status, "value")
                    else str(execution.status)
                ),
                "started_at": (
                    execution.started_at.isoformat() if execution.started_at else None
                ),
                "failure_category": execution.failure_category,
            }
            for execution in active_task_executions
        ],
        "pending_tasks": [
            {
                "task_id": task.id,
                "title": task.title,
                "plan_position": task.plan_position,
                "status": (
                    task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status)
                ),
            }
            for task in pending_tasks[:20]
        ],
        "intervention_requests": [
            {
                "intervention_id": req.id,
                "task_id": req.task_id,
                "status": req.status,
                "intervention_type": req.intervention_type,
            }
            for req in intervention_requests[:10]
        ],
        "permission_requests": [
            {
                "permission_request_id": req.id,
                "task_id": req.task_id,
                "status": req.status,
                "reason": getattr(req, "reason", None),
            }
            for req in permission_requests[:10]
        ],
    }


def refresh_session_dispatch_watchdog_alert(
    db: Session,
    session_id: int,
    *,
    sla_seconds: int = QUEUE_WATCHDOG_SLA_SECONDS,
) -> Dict[str, Any]:
    session = get_inspectable_session_or_404(db, session_id)
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

    session = get_inspectable_session_or_404(db, session_id)
    current = _build_session_divergence_fingerprint(db, session)
    current_tags = set(current.get("anomaly_tags", []))

    project = db.query(Project).filter(Project.id == session.project_id).first()
    workspace_path = (
        str(resolve_project_workspace_path(project.workspace_path, project.name, db=db))
        if project and project.workspace_path
        else None
    )

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
        mark_task_attempt_pending(task=None, session_task_link=link)
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if task and task.status == TaskStatus.RUNNING:
            mark_task_attempt_pending(task=task, error_message=None)

    mark_session_paused(session, paused_at=datetime.now(UTC), is_active=True)
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
    session = get_inspectable_session_or_404(db, session_id)
    effective_limit = min(limit if limit else 100, 1000)

    logs_query = db.query(LogEntry).filter(LogEntry.session_id == session_id)
    if session.instance_id:
        logs_query = logs_query.filter(
            or_(
                LogEntry.session_instance_id == session.instance_id,
                LogEntry.session_instance_id.is_(None),
            )
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
    session = get_inspectable_session_or_404(db, session_id)
    effective_limit = min(limit if limit else 100, 1000)

    logs_query = db.query(LogEntry).filter(LogEntry.session_id == session_id)
    if session.instance_id:
        logs_query = logs_query.filter(
            or_(
                LogEntry.session_instance_id == session.instance_id,
                LogEntry.session_instance_id.is_(None),
            )
        )
    else:
        logs_query = logs_query.filter(LogEntry.session_instance_id.is_(None))
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
    get_inspectable_session_or_404(db, session_id)
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
    session = get_inspectable_session_or_404(db, session_id)
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
    session = get_inspectable_session_or_404(db, session_id)
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
    get_inspectable_session_or_404(db, session_id)
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
    get_inspectable_session_or_404(db, session_id)
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
    get_inspectable_session_or_404(db, session_id)
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
    current_backend = get_effective_agent_backend(settings.AGENT_BACKEND, db=db)
    current_model_family = get_effective_agent_model_family(settings.AGENT_MODEL, db=db)
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
    session = get_inspectable_session_or_404(db, session_id)
    root, events, snapshots = _latest_session_task_context(db, session)
    if root is None:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "task_id": None,
            "exporter_backend": settings.TRACE_EXPORTER_BACKEND,
            "langfuse_handoff_ready": bool(settings.LANGFUSE_ENABLED),
            "span_count": 0,
            "snapshot_count": 0,
            "spans": [],
        }
    return build_trace_export(
        session_id=session.id,
        task_id=root["task_id"],
        events=events,
        snapshots=snapshots,
        exporter_backend=settings.TRACE_EXPORTER_BACKEND,
        include_langfuse_handoff=bool(settings.LANGFUSE_ENABLED),
    )


def get_session_execution_dag_payload(db: Session, session_id: int) -> Dict[str, Any]:
    session = get_inspectable_session_or_404(db, session_id)
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
    session = get_inspectable_session_or_404(db, session_id)
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
    session = get_inspectable_session_or_404(db, session_id)
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
    get_inspectable_session_or_404(db, session_id)
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

    session = get_inspectable_session_or_404(db, session_id)
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
    from app.services.orchestration.state.persistence import append_orchestration_event

    overrides = overrides or {}
    session = get_inspectable_session_or_404(db, session_id)
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
    session = get_inspectable_session_or_404(db, session_id)

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
            return {
                "session_id": session_id,
                "task_id": task_id,
                "from_checkpoint": None,
                "to_checkpoint": None,
                "from_snapshot": None,
                "to_snapshot": None,
                "delta": None,
                "available": False,
            }
        raise HTTPException(status_code=400, detail=msg) from exc


def delete_session_checkpoint_payload(
    db: Session, session_id: int, checkpoint_name: str
) -> Dict[str, Any]:
    get_inspectable_session_or_404(db, session_id)
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
    get_inspectable_session_or_404(db, session_id)
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


_RECOVERY_STATUS_LABELS: Dict[str, str] = {
    "paused": "operator_paused",
    "awaiting_input": "awaiting_input",
    "stopped": "stopped",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

_RECOVERY_ACTION_MAP: Dict[str, List[Dict[str, Any]]] = {
    "repair_budget_exhausted": [
        {
            "label": "Retry last task",
            "action": "retry_task",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Resume from checkpoint",
            "action": "resume",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
    "validation_rejected": [
        {
            "label": "Retry task",
            "action": "retry_task",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
    "operator_paused": [
        {
            "label": "Resume from checkpoint",
            "action": "resume",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
    "backend_capacity_error": [
        {
            "label": "Retry last task",
            "action": "retry_task",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Resume from checkpoint",
            "action": "resume",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
    ],
    "checkpoint_conflict": [
        {
            "label": "Force resume",
            "action": "resume",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Inspect diff",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
    "model_lane_limitation": [
        {
            "label": "Review manually",
            "action": "diagnostics",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Retry with stronger planning lane",
            "action": "retry_stronger_lane",
            "variant": "secondary",
            "task_id": None,
            "requires_stronger_lane": True,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
    "awaiting_input": [
        {
            "label": "Submit guidance",
            "action": "submit_guidance",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
    ],
    "default": [
        {
            "label": "Resume from checkpoint",
            "action": "resume",
            "variant": "primary",
            "task_id": None,
        },
        {
            "label": "Open diagnostics",
            "action": "diagnostics",
            "variant": "secondary",
            "task_id": None,
        },
        {
            "label": "Rollback to baseline",
            "action": "rollback",
            "variant": "danger",
            "task_id": None,
        },
    ],
}


def _timeline_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _timeline_phase_for_log(entry: LogEntry, metadata: Dict[str, Any]) -> str:
    event_type = str(metadata.get("event_type") or "").lower()
    message = str(entry.message or "").lower()
    if "checkpoint" in event_type or "checkpoint" in message:
        return "checkpoint"
    if any(term in event_type or term in message for term in ("repair", "retry")):
        return "repair"
    if any(
        term in event_type or term in message
        for term in ("validation", "validator", "contract")
    ):
        return "validation"
    if any(term in event_type or term in message for term in ("planning", "plan_")):
        return "planning"
    if entry.level in {"ERROR", "WARNING"}:
        return "failure"
    return "execution"


def _timeline_kind_for_phase(phase: str, level: str = "INFO") -> str:
    if phase == "checkpoint":
        return "checkpoint"
    if phase == "repair":
        return "repair"
    if phase == "validation":
        return "warning" if level != "ERROR" else "failure"
    if phase == "failure" or level == "ERROR":
        return "failure"
    return "milestone"


def _append_timeline_event(
    events: List[Dict[str, Any]],
    *,
    at: Optional[str],
    phase: str,
    kind: str,
    title: str,
    detail: Optional[str] = None,
    task_id: Optional[int] = None,
    token_cost: Optional[int] = None,
    cause: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    events.append(
        {
            "id": f"{phase}-{kind}-{len(events) + 1}",
            "at": at,
            "phase": phase,
            "kind": kind,
            "title": title,
            "detail": detail,
            "task_id": task_id,
            "token_cost": token_cost,
            "cause": cause,
            "metadata": metadata or {},
        }
    )


def get_session_timeline_payload(
    db: Session,
    session_id: int,
) -> Dict[str, Any]:
    """Return an operator-readable narrative timeline derived from existing evidence."""
    from app.models import TaskExecution

    session = get_inspectable_session_or_404(db, session_id)
    events: List[Dict[str, Any]] = []

    _append_timeline_event(
        events,
        at=_timeline_iso(session.started_at or session.created_at),
        phase="session",
        kind="milestone",
        title="Session started" if session.started_at else "Session created",
        detail=session.name,
    )

    links = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id)
        .order_by(SessionTask.id.asc())
        .all()
    )
    tasks_by_id = {
        task.id: task
        for task in db.query(Task)
        .filter(Task.id.in_([link.task_id for link in links] or [-1]))
        .all()
    }

    for link in links:
        task = tasks_by_id.get(link.task_id)
        if not task:
            continue
        status_value = (
            link.status.value if hasattr(link.status, "value") else str(link.status)
        )
        task_status = (
            task.status.value if hasattr(task.status, "value") else str(task.status)
        )
        phase = "execution"
        kind = "milestone"
        if task_status == TaskStatus.DONE.value:
            title = "Task complete"
            kind = "success"
        elif task_status == TaskStatus.FAILED.value:
            title = "Task failed"
            phase = "failure"
            kind = "failure"
        elif task_status == TaskStatus.RUNNING.value:
            title = "Task running"
        else:
            title = "Task queued"
        _append_timeline_event(
            events,
            at=_timeline_iso(link.started_at or task.started_at or task.created_at),
            phase=phase,
            kind=kind,
            title=title,
            detail=task.title,
            task_id=task.id,
            metadata={"task_status": task_status, "session_task_status": status_value},
        )

    executions = (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id)
        .order_by(TaskExecution.id.asc())
        .all()
    )
    for execution in executions:
        status_value = (
            execution.status.value
            if hasattr(execution.status, "value")
            else str(execution.status or "")
        )
        if status_value not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            continue
        cause = str(execution.failure_category or "execution_failure")
        _append_timeline_event(
            events,
            at=_timeline_iso(
                execution.completed_at or execution.started_at or execution.created_at
            ),
            phase="failure",
            kind="failure",
            title="Execution failed",
            detail=f"Attempt {execution.attempt_number}",
            task_id=execution.task_id,
            cause=cause,
            metadata={"failure_category": cause, "backend_id": execution.backend_id},
        )

    log_entries = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.created_at.asc(), LogEntry.id.asc())
        .limit(200)
        .all()
    )
    for entry in log_entries:
        metadata: Dict[str, Any] = {}
        if entry.log_metadata:
            try:
                parsed = json.loads(entry.log_metadata)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                metadata = {}
        event_type = str(metadata.get("event_type") or "")
        if entry.level not in {"ERROR", "WARNING"} and not event_type:
            continue
        phase = _timeline_phase_for_log(entry, metadata)
        token_cost = (
            metadata.get("token_cost")
            or metadata.get("tokens")
            or metadata.get("estimated_tokens")
        )
        try:
            token_cost_int = int(token_cost) if token_cost is not None else None
        except (TypeError, ValueError):
            token_cost_int = None
        cause = metadata.get("reason") or metadata.get("failure_category")
        _append_timeline_event(
            events,
            at=_timeline_iso(entry.created_at),
            phase=phase,
            kind=_timeline_kind_for_phase(phase, entry.level),
            title=(
                event_type.replace("_", " ").title()
                if event_type
                else entry.level.title()
            ),
            detail=str(entry.message or "")[:240],
            task_id=entry.task_id,
            token_cost=token_cost_int,
            cause=str(cause) if cause else None,
            metadata={
                key: metadata[key]
                for key in (
                    "reason",
                    "validation_reasons",
                    "contract_violations",
                    "checkpoint_name",
                )
                if key in metadata
            },
        )

    checkpoint_service = CheckpointService(db)
    for checkpoint in checkpoint_service.list_checkpoints(session_id):
        _append_timeline_event(
            events,
            at=_timeline_iso(checkpoint.get("created_at")),
            phase="checkpoint",
            kind="checkpoint",
            title="Checkpoint saved",
            detail=str(
                checkpoint.get("checkpoint_name")
                or checkpoint.get("name")
                or "checkpoint"
            ),
            task_id=checkpoint.get("task_id"),
            metadata={
                "checkpoint_name": checkpoint.get("checkpoint_name")
                or checkpoint.get("name"),
                "resumable": checkpoint.get("resumable"),
                "progress_score": checkpoint.get("progress_score"),
            },
        )

    if session.status in {
        "paused",
        "stopped",
        "failed",
        "cancelled",
        "canceled",
        "awaiting_input",
    }:
        stop_reasons, stop_category = _extract_stop_reasons(db, session)
        _append_timeline_event(
            events,
            at=_timeline_iso(
                session.stopped_at or session.paused_at or session.updated_at
            ),
            phase="failure" if session.status in {"failed", "stopped"} else "session",
            kind="failure" if session.status in {"failed", "stopped"} else "warning",
            title=f"Session {session.status}",
            detail="; ".join(stop_reasons),
            cause=stop_category,
        )

    events.sort(key=lambda event: event.get("at") or "")
    phases: List[Dict[str, Any]] = []
    for phase_name in (
        "session",
        "planning",
        "execution",
        "validation",
        "repair",
        "checkpoint",
        "failure",
    ):
        phase_events = [event for event in events if event["phase"] == phase_name]
        if phase_events:
            phases.append(
                {
                    "phase": phase_name,
                    "title": phase_name.replace("_", " ").title(),
                    "event_count": len(phase_events),
                    "events": phase_events,
                }
            )

    return {
        "session_id": session_id,
        "session_status": session.status,
        "generated_at": datetime.now(UTC).isoformat(),
        "phases": phases,
        "event_count": len(events),
        "source_note": (
            "Narrative timeline derived from session status, task links, task executions, "
            "structured logs, and checkpoints."
        ),
    }


def _extract_stop_reasons(
    db: Session,
    session: SessionModel,
) -> tuple[List[str], str]:
    """Return (human_readable_reasons, stop_category)."""
    reasons: List[str] = []
    category = _RECOVERY_STATUS_LABELS.get(str(session.status or ""), "stopped")

    if session.last_alert_message:
        alert = str(session.last_alert_message)
        reasons.append(alert)
        lowered_alert = alert.lower()
        if "repair budget" in lowered_alert or "repair_budget" in lowered_alert:
            category = "repair_budget_exhausted"
        elif "validation" in lowered_alert or "validator" in lowered_alert:
            category = "validation_rejected"
        elif any(
            term in lowered_alert
            for term in (
                "capacity",
                "slot",
                "busy",
                "gateway unreachable",
                "overloaded",
            )
        ):
            category = "backend_capacity_error"
        elif any(
            term in lowered_alert
            for term in ("checkpoint conflict", "stale checkpoint", "branch state")
        ):
            category = "checkpoint_conflict"

    # Scan recent error logs for structured metadata
    recent_errors = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.level.in_(["ERROR", "WARNING"]),
            LogEntry.log_metadata.isnot(None),
        )
        .order_by(LogEntry.created_at.desc())
        .limit(40)
        .all()
    )

    for entry in recent_errors:
        try:
            meta = json.loads(entry.log_metadata or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        reason_val = meta.get("reason") or ""
        reason_text = str(reason_val).lower()
        if (
            meta.get("model_lane_limitation")
            or meta.get("failure_cause_bucket")
            == "model_lane_repeated_stale_exact_patch"
        ):
            if category not in {
                "repair_budget_exhausted",
                "backend_capacity_error",
                "checkpoint_conflict",
            }:
                category = "model_lane_limitation"
                limitation_key = str(
                    meta.get("model_lane_limitation")
                    or "repeated_stale_exact_patch_after_capsule"
                )
                reasons.append(
                    f"Planning stopped: the current model lane repeated stale patch text "
                    f"({limitation_key}) and could not be repaired by prompt guidance. "
                    "Source files were not changed. Manual review or a stronger planning lane is required."
                )
        elif "repair_budget" in reason_text or "repair_budget_exhausted" in reason_text:
            category = "repair_budget_exhausted"
            attempts = meta.get("repair_attempts") or meta.get("attempt_count") or ""
            reasons.append(f"Repair budget exhausted ({attempts or '?'} attempts used)")
        elif any(
            term in reason_text
            for term in ("backend_capacity", "capacity", "slot", "busy")
        ):
            category = "backend_capacity_error"
            reasons.append("Backend capacity unavailable or overloaded.")
        elif any(
            term in reason_text
            for term in ("checkpoint_conflict", "stale_checkpoint", "stale checkpoint")
        ):
            category = "checkpoint_conflict"
            reasons.append("Checkpoint conflict detected against current branch state.")

        validation_reasons = (
            meta.get("validation_reasons") or meta.get("contract_violations") or []
        )
        if isinstance(validation_reasons, list) and validation_reasons:
            if category not in {
                "repair_budget_exhausted",
                "backend_capacity_error",
                "checkpoint_conflict",
            }:
                category = "validation_rejected"
            for vr in validation_reasons[:3]:
                reasons.append(f"Validator rejected: {str(vr)[:120]}")

        if reasons:
            break

    if category not in {"backend_capacity_error", "checkpoint_conflict"}:
        recent_failure = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session.id,
                TaskExecution.failure_category.isnot(None),
            )
            .order_by(
                TaskExecution.completed_at.desc().nullslast(),
                TaskExecution.started_at.desc().nullslast(),
                TaskExecution.id.desc(),
            )
            .first()
        )
        failure_category = str(getattr(recent_failure, "failure_category", "") or "")
        if failure_category == "backend_capacity_limit":
            category = "backend_capacity_error"
            if not reasons:
                reasons.append("Backend capacity unavailable or overloaded.")

    if not reasons and session.status == "awaiting_input":
        reasons = ["Waiting for operator input before continuing."]
        category = "awaiting_input"
    elif not reasons and session.status == "paused":
        reasons = ["Session paused by operator."]
        category = "operator_paused"
    elif not reasons:
        reasons = [f"Session {session.status}."]

    return reasons, category


def get_session_recovery_context_payload(
    db: Session,
    session_id: int,
) -> Dict[str, Any]:
    from app.models import ConversationHistory, Project, TaskExecutionChangeSet

    session = get_inspectable_session_or_404(db, session_id)

    # --- task progress ---
    links = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id)
        .order_by(SessionTask.id.asc())
        .all()
    )
    seen_task_ids: set = set()
    task_progress: List[Dict[str, Any]] = []
    failed_task_id: Optional[int] = None

    for link in links:
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if not task:
            continue

        if hasattr(task.status, "value"):
            task_status_str = task.status.value
        else:
            task_status_str = str(task.status or "").lower()

        # repair attempts = count of FAILED executions for this task in this session
        repair_attempts = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.task_id == task.id,
                TaskExecution.status == TaskStatus.FAILED,
            )
            .count()
        )
        latest_execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.task_id == task.id,
            )
            .order_by(
                TaskExecution.completed_at.desc().nullslast(),
                TaskExecution.started_at.desc().nullslast(),
                TaskExecution.id.desc(),
            )
            .first()
        )

        # files changed = most recent changeset for this task
        changeset = (
            db.query(TaskExecutionChangeSet)
            .filter(
                TaskExecutionChangeSet.session_id == session_id,
                TaskExecutionChangeSet.task_id == task.id,
            )
            .order_by(TaskExecutionChangeSet.id.desc())
            .first()
        )
        files_changed: List[str] = []
        if changeset:
            added = list(changeset.added_files or [])
            modified = list(changeset.modified_files or [])
            files_changed = (added + modified)[:10]
        validation_evidence = _latest_validation_evidence(
            db, session_id=session_id, task_id=task.id
        )

        resolved_status: str
        if task_status_str == "done":
            resolved_status = "completed"
        elif task_status_str == "failed":
            resolved_status = "failed"
            if failed_task_id is None:
                failed_task_id = task.id
        elif task_status_str == "running":
            resolved_status = "running"
        else:
            resolved_status = "not_started"

        committed = resolved_status == "completed" and bool(files_changed or changeset)

        task_progress.append(
            {
                "task_id": task.id,
                "title": task.title,
                "status": resolved_status,
                "files_changed": files_changed,
                "repair_attempts": repair_attempts,
                "committed": committed,
                "validation_evidence": validation_evidence,
                "error_message": str(task.error_message or "")[:500] or None,
                "failure_category": (
                    str(latest_execution.failure_category or "") or None
                    if latest_execution
                    else None
                ),
                "failure_cause_bucket": _classify_test_scaffold_failure(
                    task.error_message
                ),
                "model_lane_limitation": _extract_model_lane_limitation(
                    task.error_message
                ),
            }
        )

    # --- checkpoints ---
    checkpoint_service = CheckpointService(db)
    checkpoints = checkpoint_service.list_checkpoints(session_id)
    latest_cp = checkpoints[-1] if checkpoints else None
    last_cp_id: Optional[str] = None
    last_cp_age_minutes: Optional[float] = None
    if latest_cp:
        last_cp_id = str(latest_cp.get("checkpoint_name") or "")
        cp_created = latest_cp.get("created_at")
        if cp_created:
            try:
                cp_dt = datetime.fromisoformat(str(cp_created).replace("Z", "+00:00"))
                if cp_dt.tzinfo is None:
                    cp_dt = cp_dt.replace(tzinfo=UTC)
                last_cp_age_minutes = round(
                    (datetime.now(UTC) - cp_dt).total_seconds() / 60, 1
                )
            except ValueError:
                pass

    # --- conversation history preserved? ---
    has_conversation = (
        db.query(ConversationHistory)
        .filter(ConversationHistory.session_id == session_id)
        .first()
        is not None
    )

    completed_count = sum(1 for t in task_progress if t["status"] == "completed")
    failed_count = sum(1 for t in task_progress if t["status"] == "failed")
    not_started_count = sum(1 for t in task_progress if t["status"] == "not_started")

    # --- stop reasons ---
    stop_reasons, stop_category = _extract_stop_reasons(db, session)

    # --- inject failed task_id into retry actions ---
    actions = [
        dict(a)
        for a in _RECOVERY_ACTION_MAP.get(
            stop_category, _RECOVERY_ACTION_MAP["default"]
        )
    ]
    for action in actions:
        if action["action"] in ("retry_task",) and failed_task_id is not None:
            action["task_id"] = failed_task_id

    # --- project branch ---
    project = db.query(Project).filter(Project.id == session.project_id).first()
    branch = getattr(project, "branch", None) if project else None

    # --- source note ---
    cp_note = f"checkpoint {last_cp_id}" if last_cp_id else "no checkpoint"
    source_note = (
        f"Recovery context assembled from {cp_note}, session stats, and last validation error"
        " — no raw log reading required."
    )
    latest_validation_evidence = _latest_validation_evidence(db, session_id=session_id)

    repair_churn_stopped = bool(getattr(session, "repair_churn_stopped", False))
    repair_churn_trigger = getattr(session, "repair_churn_trigger", None) or None

    lane_label = getattr(session, "model_lane_label", None) or None
    lane_metadata = getattr(session, "model_lane_metadata", None) or {}
    lane_capability_tier = (
        lane_metadata.get("capability_tier")
        if isinstance(lane_metadata, dict)
        else None
    )
    stronger_lane = _stronger_lane_available()
    stronger_lane_summary = _stronger_lane_summary()

    # Build rerun payload for model-lane limitation stops (operator evidence, not prompt injection)
    model_lane_rerun_payload: Optional[Dict[str, Any]] = None
    if stop_category == "model_lane_limitation" and failed_task_id is not None:
        failed_task_entry = next(
            (t for t in task_progress if t["task_id"] == failed_task_id), None
        )
        failed_task_obj = db.query(Task).filter(Task.id == failed_task_id).first()
        bad_old_text = _find_stale_old_text_in_logs(db, session_id)
        failure_reason = (
            failed_task_entry.get("error_message") if failed_task_entry else None
        )
        model_lane_rerun_payload = {
            "task_id": failed_task_id,
            "task_title": failed_task_obj.title if failed_task_obj else None,
            "task_intent": failed_task_obj.description if failed_task_obj else None,
            "model_lane_limitation": (
                failed_task_entry.get("model_lane_limitation")
                if failed_task_entry
                else None
            ),
            "failure_reason": failure_reason,
            "bad_old_text": bad_old_text,
            "capsule_available": True,
            "current_file_evidence_available": True,
            "source_mutation_confirmed_absent": (
                not bool(failed_task_entry.get("files_changed"))
                if failed_task_entry
                else True
            ),
            "stronger_lane_configured": stronger_lane,
            "stronger_lane": stronger_lane_summary,
            "note": (
                "This payload is operator evidence. "
                "bad_old_text is shown for manual review only and must not be injected into model prompts."
            ),
        }

    return {
        "session_id": session_id,
        "session_name": session.name,
        "session_status": session.status,
        "stop_reasons": stop_reasons,
        "stop_category": stop_category,
        "last_checkpoint_id": last_cp_id,
        "last_checkpoint_age_minutes": last_cp_age_minutes,
        "branch": branch,
        "tasks": task_progress,
        "tasks_total": len(task_progress),
        "tasks_completed": completed_count,
        "tasks_failed": failed_count,
        "tasks_not_started": not_started_count,
        "preserved": {
            "completed_tasks_checkpointed": completed_count > 0 and bool(checkpoints),
            "conversation_history_resumable": has_conversation,
            "failed_task_rolled_back": failed_count > 0,
            "remaining_plan_intact": not_started_count > 0,
        },
        "recommended_actions": actions,
        "validation_evidence": latest_validation_evidence,
        "repair_churn_stopped": repair_churn_stopped,
        "repair_churn_trigger": repair_churn_trigger,
        "source_note": source_note,
        "model_lane_label": lane_label,
        "model_lane_capability_tier": lane_capability_tier,
        "stronger_lane_available": stronger_lane,
        "stronger_lane": stronger_lane_summary,
        "model_lane_rerun_payload": model_lane_rerun_payload,
    }


def _session_digest_state_hash(
    db: Session,
    session_id: int,
    recovery: Dict[str, Any],
) -> str:
    latest_log = (
        db.query(LogEntry.id, LogEntry.created_at)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.id.desc())
        .first()
    )
    latest_execution = (
        db.query(TaskExecution.id, TaskExecution.updated_at, TaskExecution.completed_at)
        .filter(TaskExecution.session_id == session_id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    basis = {
        "session_id": session_id,
        "status": recovery.get("session_status"),
        "tasks": recovery.get("tasks"),
        "stop_reasons": recovery.get("stop_reasons"),
        "last_checkpoint_id": recovery.get("last_checkpoint_id"),
        "latest_log": (
            [latest_log.id, _timeline_iso(latest_log.created_at)]
            if latest_log
            else None
        ),
        "latest_execution": (
            [
                latest_execution.id,
                _timeline_iso(latest_execution.updated_at),
                _timeline_iso(latest_execution.completed_at),
            ]
            if latest_execution
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, default=str).encode()
    ).hexdigest()


def _generate_enriched_digest(
    db: Session,
    session_id: int,
    digest: Dict[str, Any],
    recovery: Dict[str, Any],
    state_hash: str,
) -> Dict[str, Any]:
    cached = _DIGEST_ENRICHMENT_CACHE.get(state_hash)
    if cached:
        return cached

    try:
        from app.services.agents.agent_runtime import invoke_runtime_prompt

        log_rows = (
            db.query(LogEntry)
            .filter(LogEntry.session_id == session_id)
            .order_by(LogEntry.created_at.desc(), LogEntry.id.desc())
            .limit(30)
            .all()
        )
        log_block = (
            "\n".join(
                f"[{row.level}] {str(row.message or '')[:220]}" for row in log_rows
            )
            or "(no logs)"
        )
        prompt = (
            "Write a concise operator digest for this execution session. "
            "Use exactly these headings: SUMMARY, WHAT CHANGED, WHY IT STOPPED, "
            "WHAT WAS PRESERVED, WHAT TO DO NEXT. Keep it under 300 words. "
            "Be specific and do not invent facts.\n\n"
            f"Structured digest:\n{json.dumps(digest, indent=2, default=str)}\n\n"
            f"Recovery context:\n{json.dumps(recovery, indent=2, default=str)}\n\n"
            f"Recent logs:\n{log_block}"
        )
        result = invoke_runtime_prompt(
            db,
            prompt,
            session_id=session_id,
            timeout_seconds=20,
            session_prefix="session_digest",
        )
        text = str(result.get("output") or result.get("content") or "").strip()
        if len(text) < 40:
            raise ValueError("digest enrichment returned empty output")
        enriched = {
            "enriched": True,
            "enriched_text": text[:3000],
            "enrichment_error": None,
            "state_hash": state_hash,
        }
    except Exception as exc:
        enriched = {
            "enriched": False,
            "enriched_text": None,
            "enrichment_error": str(exc)[:500],
            "state_hash": state_hash,
        }

    _DIGEST_ENRICHMENT_CACHE[state_hash] = enriched
    return enriched


def get_session_digest_payload(
    db: Session,
    session_id: int,
    *,
    enrich: bool = False,
) -> Dict[str, Any]:
    session = get_inspectable_session_or_404(db, session_id)

    recovery = get_session_recovery_context_payload(db, session_id)

    # Aggregate changed files across all completed tasks
    all_changed: List[str] = []
    for t in recovery["tasks"]:
        all_changed.extend(t.get("files_changed") or [])
    changed_files = sorted(set(all_changed))

    completed = recovery["tasks_completed"]
    failed = recovery["tasks_failed"]
    total = recovery["tasks_total"]
    stop_reasons = recovery["stop_reasons"]
    cp_id = recovery["last_checkpoint_id"]
    cp_age = recovery["last_checkpoint_age_minutes"]

    # Build summary sentence
    if completed > 0 and failed > 0:
        summary = f"Ran {total} planned tasks. Completed {completed}; {failed} failed."
    elif completed == total and total > 0:
        summary = f"All {total} tasks completed successfully."
    elif failed > 0:
        summary = f"Session stopped. {failed} task(s) failed."
    elif total == 0:
        summary = "No tasks were executed in this session."
    else:
        summary = f"{completed} of {total} tasks completed."

    stop_reason_text = stop_reasons[0] if stop_reasons else "Unknown stop reason."

    next_actions = [a["label"] for a in recovery["recommended_actions"][:3]]
    validation_evidence = recovery.get("validation_evidence") or {}
    integrity_findings = validation_evidence.get("integrity_findings") or []
    verification_insufficient = bool(
        validation_evidence.get("verification_insufficient")
    )
    repair_churn_stopped = bool(recovery.get("repair_churn_stopped"))
    repair_churn_trigger = recovery.get("repair_churn_trigger") or None
    if verification_insufficient:
        stop_reason_text = "Verification integrity is insufficient; review repair evidence before resuming."

    churn_findings: list[dict] = []
    if repair_churn_stopped:
        churn_findings = [
            {
                "finding": "repair_churn",
                "trigger": repair_churn_trigger,
                "message": (
                    f"Automatic repair stopped ({repair_churn_trigger}). "
                    "Operator review required before resuming."
                ),
            }
        ]

    payload = {
        "session_id": session_id,
        "session_name": session.name,
        "session_status": session.status,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "tasks_total": total,
        "tasks_completed": completed,
        "tasks_failed": failed,
        "changed_files": changed_files[:20],
        "why_stopped": stop_reason_text,
        "preserved": recovery["preserved"],
        "last_checkpoint_id": cp_id,
        "last_checkpoint_age_minutes": cp_age,
        "next_actions": next_actions,
        "validation_evidence": validation_evidence,
        "command_quality": validation_evidence.get("command_quality"),
        "integrity_findings": integrity_findings[:20],
        "verification_insufficient": verification_insufficient,
        "repair_churn_stopped": repair_churn_stopped,
        "repair_churn_trigger": repair_churn_trigger,
        "repair_churn_findings": churn_findings,
        "enriched": False,
        "enriched_text": None,
        "enrichment_error": None,
        "state_hash": None,
    }

    state_hash = _session_digest_state_hash(db, session_id, recovery)
    payload["state_hash"] = state_hash
    if enrich:
        payload.update(
            _generate_enriched_digest(db, session_id, payload, recovery, state_hash)
        )
    return payload


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
