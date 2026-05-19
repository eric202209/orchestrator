"""Read-only session decision timeline projection.

This module normalizes existing orchestration evidence into a session-level
view model. It must not emit events, mutate runtime state, or create database
records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    InterventionRequest,
    KnowledgeItem,
    KnowledgeUsageLog,
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.task_service import TASK_CHANGE_SET_LOG_MESSAGE

from ..events.event_types import EventType
from ..state.persistence import read_orchestration_events

KNOWN_PHASES = ("planning", "validation", "execution", "failure", "completion")
DEFAULT_TIMELINE_LIMIT = 300
MAX_TIMELINE_LIMIT = 300
FAILURE_LIKE_EVENT_TYPES = frozenset(
    {
        EventType.TOOL_FAILED,
        EventType.DEBUG_FEEDBACK_CAPTURED,
        EventType.REPAIR_REJECTED,
        EventType.COMPLETION_EVIDENCE_FAILED,
        EventType.WAITING_FOR_INPUT,
    }
)


def get_session_decision_timeline_payload(
    db: Session,
    session_id: int,
    *,
    phase: Optional[str] = None,
    limit: int = DEFAULT_TIMELINE_LIMIT,
) -> Dict[str, Any]:
    """Return a normalized, read-only decision timeline for a session."""

    if phase is not None and phase not in (*KNOWN_PHASES, "system"):
        raise HTTPException(status_code=400, detail=f"Unknown phase '{phase}'")

    bounded_limit = _bounded_limit(limit)
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    project = db.query(Project).filter(Project.id == session.project_id).first()
    task_ids = _discover_session_task_ids(db, session)
    knowledge_by_task_phase = _load_knowledge_usage_by_task_phase(db, session_id)
    events: List[Dict[str, Any]] = []

    if project and project.workspace_path:
        tasks_by_id = {
            task.id: task for task in db.query(Task).filter(Task.id.in_(task_ids)).all()
        }
        for task_id in task_ids:
            raw_events = _read_task_orchestration_events(
                db=db,
                project=project,
                task=tasks_by_id.get(task_id),
                session_id=session_id,
                task_id=task_id,
            )
            for raw_event in raw_events:
                normalized = _normalize_orchestration_event(
                    raw_event,
                    session_id=session_id,
                    fallback_task_id=task_id,
                    knowledge_by_task_phase=knowledge_by_task_phase,
                )
                if normalized:
                    events.append(normalized)

    events.extend(
        _build_intervention_events(
            db,
            session_id=session_id,
            knowledge_by_task_phase=knowledge_by_task_phase,
        )
    )
    events.extend(_build_workspace_change_set_events(db, session_id=session_id))
    events.extend(
        _build_terminal_failure_events(
            db,
            session_id=session_id,
            existing_events=events,
        )
    )
    events.sort(key=_timeline_sort_key)
    _apply_causal_links(events)

    if phase is not None:
        events = [event for event in events if event["phase"] == phase]

    total_before_limit = len(events)
    events = events[:bounded_limit]
    counts = {known_phase: 0 for known_phase in KNOWN_PHASES}
    counts.update(Counter(event["phase"] for event in events))

    return {
        "session_id": session_id,
        "events": events,
        "counts": counts,
        "truncated": total_before_limit > len(events),
        "limit": bounded_limit,
    }


def _read_task_orchestration_events(
    *,
    db: Session,
    project: Project,
    task: Optional[Task],
    session_id: int,
    task_id: int,
) -> List[Dict[str, Any]]:
    for project_dir in event_project_dir_candidates(db=db, project=project, task=task):
        events = read_orchestration_events(project_dir, session_id, task_id)
        if events:
            return events
    return []


def event_project_dir_candidates(
    *,
    db: Session,
    project: Project,
    task: Optional[Task],
) -> List[Path]:
    candidates: List[Path] = []

    def add_candidate(value: Any) -> None:
        if not value:
            return
        candidate = Path(value).expanduser().resolve()
        if candidate not in candidates:
            candidates.append(candidate)

    add_candidate(
        resolve_project_workspace_path(project.workspace_path, project.name, db=db)
    )

    raw_workspace = str(project.workspace_path or "").strip()
    if raw_workspace and Path(raw_workspace).is_absolute():
        add_candidate(raw_workspace)

    task_subfolder = str(getattr(task, "task_subfolder", "") or "").strip()
    if task_subfolder:
        for candidate in list(candidates):
            add_candidate(candidate / task_subfolder)

    return candidates


def _bounded_limit(limit: int) -> int:
    parsed = limit
    if parsed <= 0:
        return DEFAULT_TIMELINE_LIMIT
    return min(parsed, MAX_TIMELINE_LIMIT)


def _discover_session_task_ids(db: Session, session: SessionModel) -> List[int]:
    task_ids = [
        row[0]
        for row in db.query(SessionTask.task_id)
        .filter(SessionTask.session_id == session.id)
        .all()
        if row[0] is not None
    ]
    if not task_ids:
        task_ids = [
            row[0]
            for row in db.query(LogEntry.task_id)
            .join(Task, Task.id == LogEntry.task_id)
            .filter(
                LogEntry.session_id == session.id,
                LogEntry.task_id.isnot(None),
                Task.project_id == session.project_id,
            )
            .distinct()
            .all()
            if row[0] is not None
        ]
    return sorted(set(int(task_id) for task_id in task_ids))


def _load_knowledge_usage_by_task_phase(
    db: Session, session_id: int
) -> Dict[tuple[Optional[int], str], List[Dict[str, Any]]]:
    rows = (
        db.query(KnowledgeUsageLog, KnowledgeItem)
        .join(KnowledgeItem, KnowledgeUsageLog.knowledge_item_id == KnowledgeItem.id)
        .filter(KnowledgeUsageLog.session_id == session_id)
        .order_by(KnowledgeUsageLog.created_at.asc(), KnowledgeUsageLog.rank.asc())
        .all()
    )
    grouped: Dict[tuple[Optional[int], str], List[Dict[str, Any]]] = defaultdict(list)
    for usage, item in rows:
        payload = {
            "usage_log_id": usage.id,
            "knowledge_item_id": usage.knowledge_item_id,
            "title": item.title,
            "knowledge_type": item.knowledge_type,
            "retrieval_reason": usage.retrieval_reason,
            "used_in_prompt": usage.used_in_prompt,
            "confidence": usage.confidence,
            "association": "knowledge used during this phase",
            "causal": False,
        }
        grouped[(usage.task_id, usage.trigger_phase)].append(payload)
    return grouped


def _normalize_orchestration_event(
    event: Dict[str, Any],
    *,
    session_id: int,
    fallback_task_id: int,
    knowledge_by_task_phase: Dict[tuple[Optional[int], str], List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    event_type = str(event.get("event_type") or "").strip()
    if not event_type:
        return None

    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    task_id = _coerce_optional_int(event.get("task_id")) or fallback_task_id
    phase = _phase_for_event(event_type, details)
    decision_type = _decision_type_for_event(event_type, phase)
    status = _status_for_event(event_type, details)
    severity = _severity_for_event(event_type, status, details)
    title = _title_for_event(event_type, details)
    summary = _summary_for_event(event_type, details, phase, task_id)
    knowledge_used = _knowledge_for_event(
        knowledge_by_task_phase,
        task_id=task_id,
        phase=phase,
    )

    normalized_details = _bounded_details(details)
    if knowledge_used:
        normalized_details["knowledge_association_label"] = (
            "knowledge used during this phase"
        )
        normalized_details["knowledge_used_during_phase"] = knowledge_used

    return {
        "id": str(event.get("event_id") or _derived_event_id(event)),
        "session_id": session_id,
        "task_id": task_id,
        "timestamp": event.get("timestamp") or _now_iso(),
        "phase": phase,
        "event_type": event_type,
        "decision_type": decision_type,
        "title": title,
        "summary": summary,
        "status": status,
        "severity": severity,
        "source": "orchestration_event",
        "parent_event_id": event.get("parent_event_id"),
        "related_event_ids": [],
        "knowledge_usage_ids": [item["usage_log_id"] for item in knowledge_used],
        "intervention_id": None,
        "details": normalized_details,
    }


def _build_intervention_events(
    db: Session,
    *,
    session_id: int,
    knowledge_by_task_phase: Dict[tuple[Optional[int], str], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows = (
        db.query(InterventionRequest)
        .filter(InterventionRequest.session_id == session_id)
        .order_by(InterventionRequest.created_at.asc(), InterventionRequest.id.asc())
        .all()
    )
    events: List[Dict[str, Any]] = []
    for req in rows:
        task_id = req.task_id
        phase = "failure"
        knowledge_used = _knowledge_for_event(
            knowledge_by_task_phase,
            task_id=task_id,
            phase=phase,
        )
        details: Dict[str, Any] = {
            "intervention_type": req.intervention_type,
            "initiated_by": req.initiated_by,
            "status": req.status,
        }
        if knowledge_used:
            details["knowledge_association_label"] = "knowledge used during this phase"
            details["knowledge_used_during_phase"] = knowledge_used

        events.append(
            {
                "id": f"intervention-{req.id}",
                "session_id": session_id,
                "task_id": task_id,
                "timestamp": _serialize_dt(req.created_at),
                "phase": phase,
                "event_type": "human_intervention_requested",
                "decision_type": "intervention",
                "title": "Human Intervention Requested",
                "summary": _intervention_summary(req),
                "status": req.status,
                "severity": "warning" if req.status == "pending" else "info",
                "source": "intervention_request",
                "parent_event_id": None,
                "related_event_ids": [],
                "knowledge_usage_ids": [
                    item["usage_log_id"] for item in knowledge_used
                ],
                "intervention_id": req.id,
                "details": details,
            }
        )
    return events


def _build_workspace_change_set_events(
    db: Session,
    *,
    session_id: int,
) -> List[Dict[str, Any]]:
    rows = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.message == TASK_CHANGE_SET_LOG_MESSAGE,
        )
        .order_by(LogEntry.created_at.asc(), LogEntry.id.asc())
        .all()
    )
    events: List[Dict[str, Any]] = []
    for entry in rows:
        try:
            metadata = json.loads(entry.log_metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

        changed_count = _coerce_optional_int(metadata.get("changed_count")) or 0
        warning_flags = metadata.get("warning_flags")
        if not isinstance(warning_flags, list):
            warning_flags = []
        added_files = metadata.get("added_files")
        modified_files = metadata.get("modified_files")
        deleted_files = metadata.get("deleted_files")
        details = {
            "task_execution_id": entry.task_execution_id
            or _coerce_optional_int(metadata.get("task_execution_id")),
            "task_id": entry.task_id or _coerce_optional_int(metadata.get("task_id")),
            "log_id": entry.id,
            "changed_count": changed_count,
            "added_files": added_files if isinstance(added_files, list) else [],
            "modified_files": (
                modified_files if isinstance(modified_files, list) else []
            ),
            "deleted_files": deleted_files if isinstance(deleted_files, list) else [],
            "warning_flags": warning_flags,
        }

        events.append(
            {
                "id": f"workspace-change-set-log-{entry.id}",
                "session_id": session_id,
                "task_id": details["task_id"],
                "timestamp": _serialize_dt(entry.created_at),
                "phase": "completion",
                "event_type": "workspace_change_set_captured",
                "decision_type": "workspace_governance",
                "title": "Workspace Change Set Captured",
                "summary": (
                    f"Captured {changed_count} changed file"
                    f"{'' if changed_count == 1 else 's'} for review."
                ),
                "status": "captured",
                "severity": "warning" if warning_flags else "info",
                "source": "workspace_change_set_log",
                "parent_event_id": None,
                "related_event_ids": [],
                "knowledge_usage_ids": [],
                "intervention_id": None,
                "details": _bounded_details(details),
            }
        )
    return events


_TERMINAL_REASON_POLICIES: Dict[str, Dict[str, str]] = {
    "planning_validation_failed_after_repair": {
        "title": "Planning Failed After Repair",
        "summary_prefix": "Planning terminalized after repair",
        "no_further_repair_reason": (
            "Brittle-command planning failures remain terminal after the bounded "
            "repair pass unless future evidence proves an orchestration semantic gap."
        ),
        "operator_next_action": (
            "Revise the task or model prompt to avoid oversized, heredoc-heavy, "
            "or malformed commands; do not loosen the validator for this failure class."
        ),
    },
    "planning_invalid_commands_after_repair": {
        "title": "Planning Failed After Repair",
        "summary_prefix": "Planning terminalized after repair",
        "no_further_repair_reason": (
            "Invalid-command planning failures remain terminal after the bounded "
            "repair pass unless future evidence proves an orchestration semantic gap."
        ),
        "operator_next_action": (
            "Revise the task or model prompt to produce short runnable shell "
            "commands; do not loosen the validator for this failure class."
        ),
    },
    "planning_context_overflow": {
        "title": "Planning Timed Out Or Exceeded Context",
        "summary_prefix": "Planning terminalized before execution",
        "no_further_repair_reason": (
            "No repair pass is available because planning did not produce a "
            "validated plan to repair."
        ),
        "operator_next_action": (
            "Retry with a smaller task or reduced project context, or investigate "
            "model/runtime availability if this repeats across simple workloads."
        ),
    },
    "planning timeout or context overflow": {
        "title": "Planning Timed Out Or Exceeded Context",
        "summary_prefix": "Planning terminalized before execution",
        "no_further_repair_reason": (
            "No repair pass is available because planning did not produce a "
            "validated plan to repair."
        ),
        "operator_next_action": (
            "Retry with a smaller task or reduced project context, or investigate "
            "model/runtime availability if this repeats across simple workloads."
        ),
    },
    "planning_openclaw_lock_contention": {
        "title": "OpenClaw Session Lock Contention",
        "summary_prefix": "Planning terminalized because OpenClaw session storage was locked",
        "no_further_repair_reason": (
            "No repair pass is available because OpenClaw could not acquire its "
            "local session lock before producing a usable plan."
        ),
        "operator_next_action": (
            "Retry after concurrent OpenClaw work drains, or reduce local planning "
            "concurrency if this repeats."
        ),
    },
    "repair_output_contract_violation": {
        "title": "Repair Output Contract Violation",
        "summary_prefix": "Planning repair terminalized",
        "no_further_repair_reason": (
            "Repair output contract violations terminalize to preserve the "
            "strict JSON-only planner boundary."
        ),
        "operator_next_action": (
            "Retry only if model output reliability has improved; keep the repair "
            "contract strict and inspect the repair output format."
        ),
    },
    "repair output contract violation": {
        "title": "Repair Output Contract Violation",
        "summary_prefix": "Planning repair terminalized",
        "no_further_repair_reason": (
            "Repair output contract violations terminalize to preserve the "
            "strict JSON-only planner boundary."
        ),
        "operator_next_action": (
            "Retry only if model output reliability has improved; keep the repair "
            "contract strict and inspect the repair output format."
        ),
    },
    "op_contract_violation": {
        "title": "Operation Contract Violation",
        "summary_prefix": "Planning terminalized after a structured operation contract violation",
        "no_further_repair_reason": (
            "Operation contract violations are terminal because a structured file "
            "operation did not match the executor-owned schema."
        ),
        "operator_next_action": (
            "Inspect the malformed op shape, update contract normalization or "
            "repair handling if the intent is unambiguous, then rerun."
        ),
    },
    "workspace isolation violation": {
        "title": "Workspace Isolation Violation",
        "summary_prefix": "Execution terminalized after a workspace isolation violation",
        "no_further_repair_reason": (
            "Workspace isolation violations are terminal because the attempted "
            "operation crossed the project boundary."
        ),
        "operator_next_action": (
            "Inspect the blocked path or command, keep execution inside the "
            "project workspace, then rerun only after the task instructions are clear."
        ),
    },
    "workspace_isolation_violation": {
        "title": "Workspace Isolation Violation",
        "summary_prefix": "Execution terminalized after a workspace isolation violation",
        "no_further_repair_reason": (
            "Workspace isolation violations are terminal because the attempted "
            "operation crossed the project boundary."
        ),
        "operator_next_action": (
            "Inspect the blocked path or command, keep execution inside the "
            "project workspace, then rerun only after the task instructions are clear."
        ),
    },
}

_TERMINAL_FAILURE_DIAGNOSTIC_KEYS = (
    "reason",
    "contract_violation_type",
    "validation_reasons",
    "contract_violations",
    "semantic_violation_codes",
    "brittle_command_subcodes",
    "brittle_command_step_details",
    "brittle_command_step_command_lengths",
    "shadow_warnings",
    "max_command_length",
    "command_total_chars",
    "heredoc_command_count",
    "weak_verification_steps",
    "missing_verification_steps",
    "snapshot_path",
    "target_path",
)


def _build_terminal_failure_events(
    db: Session,
    *,
    session_id: int,
    existing_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    latest_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        )
        .order_by(
            TaskExecution.completed_at.desc().nullslast(),
            TaskExecution.id.desc(),
        )
        .first()
    )
    if not latest_execution:
        return []
    if _has_journal_terminal_event(existing_events, latest_execution):
        return []

    rows = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.task_execution_id == latest_execution.id,
            LogEntry.log_metadata.isnot(None),
        )
        .order_by(LogEntry.created_at.desc(), LogEntry.id.desc())
        .limit(120)
        .all()
    )

    for entry in rows:
        try:
            metadata = json.loads(entry.log_metadata or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        reason = str(metadata.get("reason") or "").strip()
        policy = _terminal_reason_policy(reason)
        if not policy:
            continue

        details = {
            key: metadata[key]
            for key in _TERMINAL_FAILURE_DIAGNOSTIC_KEYS
            if key in metadata and metadata[key] not in (None, [], {})
        }
        details.update(
            {
                "task_execution_id": latest_execution.id,
                "task_id": latest_execution.task_id,
                "log_id": entry.id,
                "terminal": True,
                "repair_attempted": _terminal_reason_implies_repair_attempted(reason),
                "targeted_second_repair_attempted": False,
                "no_further_repair_reason": policy["no_further_repair_reason"],
                "operator_next_action": policy["operator_next_action"],
            }
        )

        return [
            {
                "id": f"terminal-failure-log-{entry.id}",
                "session_id": session_id,
                "task_id": latest_execution.task_id,
                "timestamp": _serialize_dt(entry.created_at),
                "phase": "failure",
                "event_type": EventType.TASK_FAILED,
                "decision_type": "failure",
                "title": policy["title"],
                "summary": _terminal_failure_summary(details, policy=policy),
                "status": _terminal_status(latest_execution),
                "severity": "error",
                "source": "terminal_log_metadata",
                "parent_event_id": None,
                "related_event_ids": [],
                "knowledge_usage_ids": [],
                "intervention_id": None,
                "details": _bounded_details(details),
            }
        ]

    if latest_execution.status == TaskStatus.CANCELLED:
        details = {
            "reason": "forced-stop or cancellation",
            "task_execution_id": latest_execution.id,
            "task_id": latest_execution.task_id,
            "terminal": True,
            "repair_attempted": False,
            "targeted_second_repair_attempted": False,
            "no_further_repair_reason": (
                "Execution was cancelled or force-stopped, so no further repair "
                "was attempted."
            ),
            "operator_next_action": (
                "Inspect the last execution step, then rerun when the runtime is "
                "healthy or split the work if it repeatedly stalls."
            ),
        }
        return [
            {
                "id": f"terminal-cancelled-execution-{latest_execution.id}",
                "session_id": session_id,
                "task_id": latest_execution.task_id,
                "timestamp": _serialize_dt(latest_execution.completed_at),
                "phase": "failure",
                "event_type": EventType.TASK_FAILED,
                "decision_type": "failure",
                "title": "Execution Cancelled",
                "summary": "Execution terminalized after cancellation or forced stop.",
                "status": "cancelled",
                "severity": "warning",
                "source": "terminal_log_metadata",
                "parent_event_id": None,
                "related_event_ids": [],
                "knowledge_usage_ids": [],
                "intervention_id": None,
                "details": _bounded_details(details),
            }
        ]

    return []


def _terminal_reason_policy(reason: str) -> Optional[Dict[str, str]]:
    return _TERMINAL_REASON_POLICIES.get(reason)


def _terminal_reason_implies_repair_attempted(reason: str) -> bool:
    return reason in {
        "planning_validation_failed_after_repair",
        "planning_invalid_commands_after_repair",
        "repair_output_contract_violation",
        "repair output contract violation",
    }


def _terminal_status(execution: TaskExecution) -> str:
    if execution.status == TaskStatus.CANCELLED:
        return "cancelled"
    return "failed"


def _has_journal_terminal_event(
    existing_events: List[Dict[str, Any]],
    execution: TaskExecution,
) -> bool:
    for event in existing_events:
        if event.get("event_type") != EventType.TASK_FAILED:
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        event_execution_id = _coerce_optional_int(details.get("task_execution_id"))
        if event_execution_id is not None:
            if event_execution_id == execution.id:
                return True
            continue
        if _coerce_optional_int(event.get("task_id")) == execution.task_id:
            return True
    return False


def _terminal_failure_summary(
    details: Dict[str, Any],
    *,
    policy: Dict[str, str],
) -> str:
    reason = str(details.get("reason") or "planning failure")
    subcodes = details.get("brittle_command_subcodes")
    if isinstance(subcodes, list) and subcodes:
        return (
            f"{policy['summary_prefix']}: {reason} "
            f"({', '.join(map(str, subcodes[:3]))})."
        )
    return f"{policy['summary_prefix']}: {reason}."


def _knowledge_for_event(
    knowledge_by_task_phase: Dict[tuple[Optional[int], str], List[Dict[str, Any]]],
    *,
    task_id: Optional[int],
    phase: str,
) -> List[Dict[str, Any]]:
    exact = knowledge_by_task_phase.get((task_id, phase), [])
    session_phase = knowledge_by_task_phase.get((None, phase), [])
    return [*exact, *session_phase]


def _apply_causal_links(events: List[Dict[str, Any]]) -> None:
    """Infer bounded causal links between already-normalized timeline events."""

    latest_retry_by_task: Dict[Optional[int], str] = {}
    latest_failure_like_by_task: Dict[Optional[int], str] = {}
    latest_validation_issue_by_task: Dict[Optional[int], str] = {}
    latest_repair_by_task: Dict[Optional[int], str] = {}

    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        event_type = str(event.get("event_type") or "")
        task_id = _coerce_optional_int(event.get("task_id"))
        parent_event_id = event.get("parent_event_id")

        if parent_event_id:
            _add_causal_link(
                event,
                relation="explicit_parent",
                target_event_id=str(parent_event_id),
                inferred=False,
                confidence="exact",
            )

        if event_type == EventType.RETRY_ENTERED:
            previous_retry = latest_retry_by_task.get(task_id)
            if previous_retry:
                _add_causal_link(
                    event,
                    relation="previous_retry",
                    target_event_id=previous_retry,
                    inferred=True,
                    confidence="high",
                )
            latest_failure = latest_failure_like_by_task.get(task_id)
            if latest_failure:
                _add_causal_link(
                    event,
                    relation="retry_after_failure",
                    target_event_id=latest_failure,
                    inferred=True,
                    confidence="high",
                )
            latest_retry_by_task[task_id] = event_id

        if event_type in {
            EventType.REPAIR_GENERATED,
            EventType.REPAIR_APPLIED,
            EventType.REPAIR_REJECTED,
        }:
            validation_issue = latest_validation_issue_by_task.get(task_id)
            if validation_issue:
                _add_causal_link(
                    event,
                    relation="repair_for_validation",
                    target_event_id=validation_issue,
                    inferred=True,
                    confidence="medium",
                )
            latest_repair_by_task[task_id] = event_id

        if event_type == EventType.VALIDATION_RESULT:
            latest_repair = latest_repair_by_task.get(task_id)
            if latest_repair:
                _add_causal_link(
                    event,
                    relation="validation_after_repair",
                    target_event_id=latest_repair,
                    inferred=True,
                    confidence="medium",
                )
            if _is_problem_status(event):
                latest_validation_issue_by_task[task_id] = event_id

        if event_type == EventType.TASK_FAILED:
            latest_failure = latest_failure_like_by_task.get(task_id)
            if latest_failure:
                _add_causal_link(
                    event,
                    relation="task_failed_because",
                    target_event_id=latest_failure,
                    inferred=True,
                    confidence="medium",
                )

        if (
            event_type == EventType.HUMAN_INTERVENTION_REQUESTED
            or event.get("source") == "intervention_request"
        ):
            latest_failure = latest_failure_like_by_task.get(task_id)
            if latest_failure:
                _add_causal_link(
                    event,
                    relation="intervention_after_failure",
                    target_event_id=latest_failure,
                    inferred=True,
                    confidence="medium",
                )

        if _is_failure_like_event(event):
            latest_failure_like_by_task[task_id] = event_id


def _add_causal_link(
    event: Dict[str, Any],
    *,
    relation: str,
    target_event_id: str,
    inferred: bool,
    confidence: str,
) -> None:
    if not target_event_id or target_event_id == event.get("id"):
        return

    related_event_ids = event.setdefault("related_event_ids", [])
    if target_event_id not in related_event_ids:
        related_event_ids.append(target_event_id)
        del related_event_ids[10:]

    details = event.setdefault("details", {})
    if not isinstance(details, dict):
        details = {}
        event["details"] = details

    links = details.setdefault("causal_links", [])
    if not isinstance(links, list):
        links = []
        details["causal_links"] = links
    if any(
        isinstance(link, dict)
        and link.get("relation") == relation
        and link.get("event_id") == target_event_id
        for link in links
    ):
        return
    links.append(
        {
            "relation": relation,
            "event_id": target_event_id,
            "inferred": inferred,
            "confidence": confidence,
        }
    )
    del links[8:]


def _is_problem_status(event: Dict[str, Any]) -> bool:
    status = str(event.get("status") or "").lower()
    return status in {"failed", "rejected", "repair_required", "error"}


def _is_failure_like_event(event: Dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    if event_type in FAILURE_LIKE_EVENT_TYPES:
        return True
    if event_type == EventType.STEP_FINISHED and _is_problem_status(event):
        return True
    if event_type == EventType.VALIDATION_RESULT and _is_problem_status(event):
        return True
    return False


def _phase_for_event(event_type: str, details: Dict[str, Any]) -> str:
    detail_phase = str(details.get("phase") or "").strip()
    if detail_phase in (*KNOWN_PHASES, "system"):
        return detail_phase
    if event_type == EventType.REASONING_ARTIFACT_GENERATED:
        return "planning"
    if event_type == EventType.VALIDATION_RESULT:
        return "validation"
    if event_type in {
        EventType.TASK_FAILED,
        EventType.WAITING_FOR_INPUT,
        EventType.HUMAN_INTERVENTION_REQUESTED,
        EventType.HUMAN_INTERVENTION_REPLIED,
    }:
        return "failure"
    if event_type in {
        EventType.DEBUG_FEEDBACK_CAPTURED,
        EventType.DEBUG_REPAIR_ATTEMPTED,
        EventType.REPAIR_GENERATED,
        EventType.REPAIR_APPLIED,
        EventType.REPAIR_REJECTED,
        EventType.EVALUATOR_RESULT,
        EventType.COMPLETION_EVIDENCE_FAILED,
        EventType.TASK_COMPLETED,
    }:
        return "completion"
    if event_type in {
        EventType.TASK_STARTED,
        EventType.TASK_QUEUED,
        EventType.TASK_CLAIMED,
        EventType.TASK_DISPATCH_REJECTED,
        EventType.STEP_STARTED,
        EventType.STEP_FINISHED,
        EventType.TOOL_FAILED,
        EventType.RETRY_ENTERED,
        EventType.PLAN_REVISED,
        EventType.CHECKPOINT_SAVED,
        EventType.CHECKPOINT_LOADED,
        EventType.CHECKPOINT_REDIRECTED,
        EventType.TOOL_INVOKED,
    }:
        return "execution"
    return "system"


def _decision_type_for_event(event_type: str, phase: str) -> str:
    if event_type in {EventType.PHASE_STARTED, EventType.PHASE_FINISHED}:
        return "phase"
    if event_type == EventType.VALIDATION_RESULT:
        return "validation"
    if event_type == EventType.RETRY_ENTERED:
        return "retry"
    if event_type == EventType.DEBUG_REPAIR_ATTEMPTED:
        return "retry"
    if event_type in {
        EventType.TASK_FAILED,
        EventType.TOOL_FAILED,
        EventType.DEBUG_FEEDBACK_CAPTURED,
        EventType.WAITING_FOR_INPUT,
    }:
        return "failure"
    if event_type in {
        EventType.HUMAN_INTERVENTION_REQUESTED,
        EventType.HUMAN_INTERVENTION_REPLIED,
    }:
        return "intervention"
    if phase == "completion":
        return "completion"
    if event_type in {EventType.TASK_STARTED, EventType.TASK_COMPLETED}:
        return "task"
    if event_type == EventType.REASONING_ARTIFACT_GENERATED:
        return "planning"
    return phase if phase in KNOWN_PHASES else "system"


def _status_for_event(event_type: str, details: Dict[str, Any]) -> str:
    status = str(details.get("status") or "").strip()
    if status:
        return status
    if event_type.endswith("_started"):
        return "started"
    if event_type.endswith("_finished"):
        return "finished"
    if event_type in {EventType.TASK_FAILED, EventType.TOOL_FAILED}:
        return "failed"
    if event_type == EventType.TASK_COMPLETED:
        return "completed"
    if event_type == EventType.RETRY_ENTERED:
        return "started"
    return "recorded"


def _severity_for_event(event_type: str, status: str, details: Dict[str, Any]) -> str:
    status_lower = status.lower()
    if event_type in {
        EventType.TASK_FAILED,
        EventType.TOOL_FAILED,
        EventType.DEBUG_FEEDBACK_CAPTURED,
        EventType.REPAIR_REJECTED,
        EventType.COMPLETION_EVIDENCE_FAILED,
    }:
        return "error"
    if status_lower in {"failed", "rejected", "repair_required"}:
        return "error"
    if event_type in {
        EventType.RETRY_ENTERED,
        EventType.WAITING_FOR_INPUT,
        EventType.HUMAN_INTERVENTION_REQUESTED,
    }:
        return "warning"
    if status_lower in {"warning", "pending"}:
        return "warning"
    return "info"


def _title_for_event(event_type: str, details: Dict[str, Any]) -> str:
    if event_type == EventType.PHASE_STARTED:
        return f"{_humanize(details.get('phase') or 'Phase')} Started"
    if event_type == EventType.PHASE_FINISHED:
        return f"{_humanize(details.get('phase') or 'Phase')} Finished"
    return _humanize(event_type)


def _summary_for_event(
    event_type: str, details: Dict[str, Any], phase: str, task_id: Optional[int]
) -> str:
    message = _first_text(details.get("message"))
    if message:
        return message
    status = _first_text(details.get("status"))
    if event_type == EventType.VALIDATION_RESULT:
        stage = _humanize(details.get("stage") or "validation")
        status_suffix = f" with status {status}" if status else ""
        return f"{stage} validation recorded{status_suffix}."
    if event_type == EventType.RETRY_ENTERED:
        return f"Retry cycle started for task {task_id}."
    if event_type == EventType.DEBUG_FEEDBACK_CAPTURED:
        failure_class = _first_text(details.get("debug_failure_class"))
        eligible = details.get("eligible_for_debug_repair")
        suffix = "eligible" if eligible else "terminal"
        return f"Debug feedback captured for task {task_id}: {failure_class or 'unknown'} ({suffix})."
    if event_type == EventType.DEBUG_REPAIR_ATTEMPTED:
        failure_class = _first_text(details.get("debug_failure_class"))
        return f"Bounded debug repair attempted for task {task_id}: {failure_class or 'unknown'}."
    if event_type == EventType.TASK_FAILED:
        return f"Task {task_id} failed."
    if event_type == EventType.TASK_COMPLETED:
        return f"Task {task_id} completed."
    return f"{_humanize(event_type)} recorded during {phase}."


def _bounded_details(details: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "phase",
        "stage",
        "status",
        "profile",
        "step_index",
        "step_number",
        "step_total",
        "reasons",
        "confidence",
        "message",
        "tool_name",
        "checkpoint_name",
        "reason",
        "failure_envelope",
        "debug_feedback_captured",
        "debug_feedback_envelope",
        "debug_failure_class",
        "debug_repair_attempted",
        "debug_repair_terminal_reason",
        "debug_repair_used",
        "debug_repair_step_count",
        "debug_repair_validator_reasons",
        "eligible_for_debug_repair",
        "allowed",
        "allowed_reason",
        "queue_latency_seconds",
        "queue_age_seconds",
        "task_execution_id",
        "task_id",
        "log_id",
        "terminal",
        "repair_attempted",
        "targeted_second_repair_attempted",
        "no_further_repair_reason",
        "operator_next_action",
        "validation_reasons",
        "contract_violations",
        "contract_violation_type",
        "semantic_violation_codes",
        "brittle_command_subcodes",
        "brittle_command_step_details",
        "brittle_command_step_command_lengths",
        "shadow_warnings",
        "max_command_length",
        "command_total_chars",
        "heredoc_command_count",
        "weak_verification_steps",
        "missing_verification_steps",
        "snapshot_path",
        "target_path",
        "changed_count",
        "added_files",
        "modified_files",
        "deleted_files",
        "warning_flags",
    }
    bounded: Dict[str, Any] = {}
    for key in allowed:
        if key not in details:
            continue
        value = details[key]
        if isinstance(value, list):
            bounded[key] = value[:10]
        elif isinstance(value, dict):
            bounded[key] = {
                str(k): v
                for k, v in list(value.items())[:10]
                if isinstance(v, (str, int, float, bool, type(None), list))
            }
        elif isinstance(value, (str, int, float, bool)) or value is None:
            bounded[key] = value
    return bounded


def _timeline_sort_key(event: Dict[str, Any]) -> tuple[datetime, str]:
    timestamp = event.get("timestamp")
    parsed = _parse_dt(timestamp)
    return parsed, str(event.get("id") or "")


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _serialize_dt(value: Optional[datetime]) -> str:
    if value is None:
        return _now_iso()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _derived_event_id(event: Dict[str, Any]) -> str:
    return "-".join(
        str(part)
        for part in (
            event.get("session_id", "session"),
            event.get("task_id", "task"),
            event.get("timestamp", "time"),
            event.get("event_type", "event"),
        )
    )


def _humanize(value: Any) -> str:
    return str(value or "").replace("_", " ").strip().title() or "Event"


def _first_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Iterable) and not isinstance(value, (dict, bytes, str)):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return None


def _intervention_summary(req: InterventionRequest) -> str:
    actor = req.initiated_by or "system"
    return (
        f"{_humanize(actor)} requested {req.intervention_type} intervention"
        f" with status {req.status}."
    )
