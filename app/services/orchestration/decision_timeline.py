"""Read-only session decision timeline projection.

This module normalizes existing orchestration evidence into a session-level
view model. It must not emit events, mutate runtime state, or create database
records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
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
)

from .events.event_types import EventType
from .persistence import read_orchestration_events

KNOWN_PHASES = ("planning", "validation", "execution", "failure", "completion")
DEFAULT_TIMELINE_LIMIT = 300
MAX_TIMELINE_LIMIT = 300
FAILURE_LIKE_EVENT_TYPES = frozenset(
    {
        EventType.TOOL_FAILED,
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
    task_ids = _discover_session_task_ids(db, session_id)
    knowledge_by_task_phase = _load_knowledge_usage_by_task_phase(db, session_id)
    events: List[Dict[str, Any]] = []

    if project and project.workspace_path:
        for task_id in task_ids:
            for raw_event in read_orchestration_events(
                project.workspace_path,
                session_id,
                task_id,
            ):
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


def _bounded_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = DEFAULT_TIMELINE_LIMIT
    if parsed <= 0:
        return DEFAULT_TIMELINE_LIMIT
    return min(parsed, MAX_TIMELINE_LIMIT)


def _discover_session_task_ids(db: Session, session_id: int) -> List[int]:
    task_ids = [
        row[0]
        for row in db.query(SessionTask.task_id)
        .filter(SessionTask.session_id == session_id)
        .all()
        if row[0] is not None
    ]
    if not task_ids:
        task_ids = [
            row[0]
            for row in db.query(LogEntry.task_id)
            .filter(LogEntry.session_id == session_id, LogEntry.task_id.isnot(None))
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
    if event_type in {
        EventType.TASK_FAILED,
        EventType.TOOL_FAILED,
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
        "queue_latency_seconds",
        "queue_age_seconds",
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
            parsed = datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _serialize_dt(value: Optional[datetime]) -> str:
    if value is None:
        return _now_iso()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
