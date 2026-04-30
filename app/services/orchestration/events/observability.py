"""Derived observability payloads built from orchestration events and snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional


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


def _duration_ms(started_at: Any, finished_at: Any) -> Optional[int]:
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(finished_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def build_trace_export(
    *,
    session_id: int,
    task_id: int,
    events: Iterable[Dict[str, Any]],
    snapshots: Iterable[Dict[str, Any]],
    exporter_backend: str = "local_json",
    include_langfuse_handoff: bool = False,
) -> Dict[str, Any]:
    ordered_events = list(events)
    ordered_snapshots = list(snapshots)
    spans: List[Dict[str, Any]] = []
    open_phase_spans: Dict[str, Dict[str, Any]] = {}

    for event in ordered_events:
        event_type = str(event.get("event_type") or "")
        details = event.get("details") or {}
        timestamp = event.get("timestamp")

        if event_type == "phase_started":
            phase = str(details.get("phase") or "unknown")
            open_phase_spans[phase] = {
                "span_id": event.get("event_id") or f"phase:{phase}:{timestamp}",
                "kind": "phase",
                "name": phase,
                "started_at": timestamp,
                "finished_at": None,
                "status": "running",
                "duration_ms": None,
                "attributes": {
                    "phase": phase,
                    "event_count": 1,
                },
            }
            continue

        if event_type == "phase_finished":
            phase = str(details.get("phase") or "unknown")
            span = open_phase_spans.pop(phase, None)
            if span is None:
                span = {
                    "span_id": event.get("event_id") or f"phase:{phase}:{timestamp}",
                    "kind": "phase",
                    "name": phase,
                    "started_at": timestamp,
                    "attributes": {"phase": phase, "event_count": 0},
                }
            span["finished_at"] = timestamp
            span["status"] = str(details.get("status") or "completed")
            span["duration_ms"] = _duration_ms(span.get("started_at"), timestamp)
            span["attributes"]["event_count"] = (
                int(span["attributes"].get("event_count") or 0) + 1
            )
            spans.append(span)
            continue

        if event_type in {"step_finished", "retry_entered", "repair_applied"}:
            spans.append(
                {
                    "span_id": event.get("event_id") or f"{event_type}:{timestamp}",
                    "kind": "event",
                    "name": event_type,
                    "started_at": timestamp,
                    "finished_at": timestamp,
                    "status": str(details.get("status") or "completed"),
                    "duration_ms": 0,
                    "attributes": {
                        "step_number": details.get("step_number"),
                        "attempt": details.get("attempt"),
                        "reason": details.get("reason"),
                    },
                }
            )

        failure_envelope = details.get("failure_envelope")
        if isinstance(failure_envelope, dict):
            spans.append(
                {
                    "span_id": f"failure:{event.get('event_id')}",
                    "kind": "failure",
                    "name": str(
                        failure_envelope.get("phase") or event_type or "failure"
                    ),
                    "started_at": timestamp,
                    "finished_at": timestamp,
                    "status": "error",
                    "duration_ms": 0,
                    "attributes": {
                        "root_cause": failure_envelope.get("root_cause"),
                        "step_index": failure_envelope.get("step_index"),
                        "model_id": failure_envelope.get("model_id"),
                    },
                }
            )

    for phase, span in open_phase_spans.items():
        span["status"] = "open"
        spans.append(span)

    spans.sort(
        key=lambda item: (
            _parse_timestamp(item.get("started_at"))
            or datetime.min.replace(tzinfo=UTC),
            str(item.get("span_id") or ""),
        )
    )

    return {
        "schema_version": 1,
        "session_id": session_id,
        "task_id": task_id,
        "exporter_backend": exporter_backend,
        "langfuse_handoff_ready": include_langfuse_handoff,
        "span_count": len(spans),
        "snapshot_count": len(ordered_snapshots),
        "spans": spans,
    }


def build_execution_dag(
    *,
    session_id: int,
    task_id: int,
    events: Iterable[Dict[str, Any]],
    snapshots: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered_events = list(events)
    ordered_snapshots = list(snapshots)
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for event in ordered_events:
        event_id = str(event.get("event_id") or "")
        details = event.get("details") or {}
        nodes.append(
            {
                "id": event_id or f"event:{len(nodes)}",
                "type": "event",
                "label": str(event.get("event_type") or "event"),
                "status": str(details.get("status") or "recorded"),
                "timestamp": event.get("timestamp"),
                "step_number": details.get("step_number"),
            }
        )
        parent_event_id = event.get("parent_event_id")
        if parent_event_id:
            edges.append(
                {
                    "source": parent_event_id,
                    "target": event_id,
                    "kind": "parent",
                }
            )

    prior_snapshot_id: Optional[str] = None
    for index, snapshot in enumerate(ordered_snapshots):
        snapshot_id = f"snapshot:{index}"
        nodes.append(
            {
                "id": snapshot_id,
                "type": "checkpoint",
                "label": str(
                    snapshot.get("checkpoint_name")
                    or snapshot.get("trigger")
                    or "snapshot"
                ),
                "status": str(snapshot.get("status") or "recorded"),
                "timestamp": snapshot.get("timestamp"),
                "step_number": snapshot.get("current_step_index"),
            }
        )
        related_event_id = snapshot.get("related_event_id")
        if related_event_id:
            edges.append(
                {
                    "source": related_event_id,
                    "target": snapshot_id,
                    "kind": "checkpoint_lineage",
                }
            )
        if prior_snapshot_id is not None:
            edges.append(
                {
                    "source": prior_snapshot_id,
                    "target": snapshot_id,
                    "kind": "snapshot_sequence",
                }
            )
        prior_snapshot_id = snapshot_id

    return {
        "session_id": session_id,
        "task_id": task_id,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def build_focus_mode_payload(
    *,
    session: Any,
    current_task: Optional[Dict[str, Any]],
    events: Iterable[Dict[str, Any]],
    snapshots: Iterable[Dict[str, Any]],
    pending_interventions: Iterable[Dict[str, Any]],
    dispatch_watchdog: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered_events = list(events)
    ordered_snapshots = list(snapshots)
    latest_snapshot = ordered_snapshots[-1] if ordered_snapshots else {}
    previous_snapshot = ordered_snapshots[-2] if len(ordered_snapshots) >= 2 else {}
    latest_event = ordered_events[-1] if ordered_events else {}
    pending_cards = list(pending_interventions)

    state_delta = {
        "current_step_index": {
            "from": previous_snapshot.get("current_step_index"),
            "to": latest_snapshot.get("current_step_index"),
        },
        "files_touched_delta": sorted(
            set(latest_snapshot.get("files_touched") or [])
            - set(previous_snapshot.get("files_touched") or [])
        ),
        "validation_status": (
            (latest_snapshot.get("validation_verdicts") or [{}])[-1].get("status")
            if latest_snapshot.get("validation_verdicts")
            else None
        ),
    }

    visible_event_types = {
        "phase_started",
        "phase_finished",
        "step_finished",
        "retry_entered",
        "repair_generated",
        "repair_applied",
        "task_failed",
        "task_completed",
        "human_intervention_requested",
        "validation_result",
    }
    visible_events = [
        event
        for event in ordered_events
        if event.get("event_type") in visible_event_types
    ]

    return {
        "session_id": getattr(session, "id", None),
        "session_status": getattr(session, "status", None),
        "current_task": current_task,
        "phase": (latest_event.get("details") or {}).get("phase"),
        "latest_event": latest_event,
        "live_state_delta": state_delta,
        "active_approvals": pending_cards,
        "dispatch_watchdog": dispatch_watchdog,
        "timeline": {
            "total_event_count": len(ordered_events),
            "focus_event_count": len(visible_events),
            "suppressed_event_count": max(0, len(ordered_events) - len(visible_events)),
            "events": visible_events[-20:],
        },
    }


def build_mobile_interruption_cards(
    *,
    session: Any,
    dispatch_watchdog: Optional[Dict[str, Any]],
    pending_interventions: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    cards: List[Dict[str, Any]] = []

    for intervention in pending_interventions:
        cards.append(
            {
                "kind": "approval_needed",
                "priority": "high",
                "title": str(intervention.get("prompt") or "Approval needed")[:120],
                "session_id": getattr(session, "id", None),
                "task_id": intervention.get("task_id"),
                "intervention_id": intervention.get("id"),
                "action": "reply_to_intervention",
            }
        )

    latest_failure = (dispatch_watchdog or {}).get("latest_failure") or {}
    root_cause = str(latest_failure.get("root_cause") or "").strip()
    if root_cause:
        cards.append(
            {
                "kind": "retry_suggested",
                "priority": "medium",
                "title": f"Retry suggested: {root_cause.replace('_', ' ')}",
                "session_id": getattr(session, "id", None),
                "task_id": latest_failure.get("task_id"),
                "action": "review_failure",
                "details": latest_failure,
            }
        )

    if getattr(session, "is_active", False) and str(
        getattr(session, "status", "")
    ).lower() in {
        "running",
        "waiting_for_human",
    }:
        cards.append(
            {
                "kind": "emergency_stop",
                "priority": "critical",
                "title": "Emergency stop available",
                "session_id": getattr(session, "id", None),
                "action": "stop_session",
            }
        )

    if latest_failure:
        cards.append(
            {
                "kind": "failure_summary",
                "priority": "medium",
                "title": "Latest failure summary",
                "session_id": getattr(session, "id", None),
                "task_id": latest_failure.get("task_id"),
                "action": "open_failure_summary",
                "details": latest_failure,
            }
        )

    return {
        "session_id": getattr(session, "id", None),
        "card_count": len(cards),
        "cards": cards,
    }
