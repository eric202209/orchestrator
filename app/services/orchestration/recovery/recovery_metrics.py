"""Phase 13B-S4: Recovery metrics aggregation from orchestration event logs.

Reads EXECUTION_RECOVERY_* events from an event log and produces a structured
metrics dict suitable for reporting and validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.events.event_types import EventType

_RECOVERY_EVENT_TYPES = {
    EventType.EXECUTION_RECOVERY_ATTEMPTED,
    EventType.EXECUTION_RECOVERY_SUCCEEDED,
    EventType.EXECUTION_RECOVERY_FAILED,
    EventType.EXECUTION_RECOVERY_SKIPPED,
}


def collect_recovery_metrics(
    project_dir: Any,
    session_id: int,
    task_id: int,
) -> Dict[str, Any]:
    """Read event log for one run and return recovery metrics dict."""
    from app.services.orchestration.state.persistence import read_orchestration_events

    events = read_orchestration_events(
        project_dir, session_id=session_id, task_id=task_id
    )
    return _tally(events)


def collect_recovery_ops_metrics(
    db: Any,
    *,
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    model: Optional[str] = None,
    day: Optional[str] = None,
    limit_sessions: Optional[int] = None,
) -> Dict[str, Any]:
    """Aggregate recovery metrics across sessions/tasks for ops reporting.

    This is read-only and derives all counts from the existing event journal.
    Aggregation dimensions:
    - project
    - session
    - model
    - day
    """
    from app.models import Project, Session as SessionModel, Task
    from app.services.session.session_runtime_service import (
        resolve_event_log_project_dir,
    )
    from app.services.orchestration.state.persistence import read_orchestration_events

    query = db.query(SessionModel).filter(SessionModel.deleted_at.is_(None))
    if project_id is not None:
        query = query.filter(SessionModel.project_id == project_id)
    if session_id is not None:
        query = query.filter(SessionModel.id == session_id)
    sessions = query.order_by(SessionModel.created_at.asc()).all()
    if limit_sessions is not None:
        sessions = sessions[: max(0, int(limit_sessions))]

    aggregate = {
        "recovery_attempted_count": 0,
        "recovery_succeeded_count": 0,
        "recovery_failed_count": 0,
        "recovery_skipped_count": 0,
        "recovery_budget_exhausted_count": 0,
        "recovery_false_success_count": 0,
        "recovery_by_scope": {},
        "recovery_by_failure_class": {},
        "by_project": {},
        "by_session": {},
        "by_model": {},
        "by_day": {},
    }

    def _bucket(target: Dict[str, Any], key: str) -> Dict[str, Any]:
        return target.setdefault(
            key,
            {
                "recovery_attempted_count": 0,
                "recovery_succeeded_count": 0,
                "recovery_failed_count": 0,
                "recovery_skipped_count": 0,
                "recovery_budget_exhausted_count": 0,
                "recovery_false_success_count": 0,
                "recovered_success_rate": 0.0,
                "recovery_by_scope": {},
                "recovery_by_failure_class": {},
            },
        )

    for sess in sessions:
        if project_id is not None and sess.project_id != project_id:
            continue
        project = (
            db.query(Project)
            .filter(Project.id == sess.project_id, Project.deleted_at.is_(None))
            .first()
        )
        project_key = f"{sess.project_id}:{getattr(project, 'name', 'unknown')}"
        model_key = (
            str(getattr(sess, "model_lane_label", None) or "")
            or str(
                (getattr(sess, "model_lane_metadata", {}) or {}).get("model_family")
                or ""
            )
            or "unknown"
        )
        if model is not None and model_key != model:
            continue
        day_key = None
        created_at = getattr(sess, "created_at", None)
        if created_at is not None:
            day_key = created_at.date().isoformat()
        if day is not None and day_key != day:
            continue

        tasks = db.query(Task).filter(Task.project_id == sess.project_id).all()
        for task in tasks:
            event_project_dir = resolve_event_log_project_dir(db, sess, task.id)
            if not event_project_dir:
                continue
            events = read_orchestration_events(event_project_dir, sess.id, task.id)
            task_metrics = _tally(events)

            for metric_key in (
                "recovery_attempted_count",
                "recovery_succeeded_count",
                "recovery_failed_count",
                "recovery_skipped_count",
                "recovery_budget_exhausted_count",
                "recovery_false_success_count",
            ):
                aggregate[metric_key] += task_metrics.get(metric_key, 0)
                project_bucket = _bucket(aggregate["by_project"], project_key)
                session_bucket = _bucket(aggregate["by_session"], str(sess.id))
                model_bucket = _bucket(aggregate["by_model"], model_key)
                day_bucket = _bucket(aggregate["by_day"], day_key or "unknown")
                for bucket in (
                    project_bucket,
                    session_bucket,
                    model_bucket,
                    day_bucket,
                ):
                    bucket[metric_key] += task_metrics.get(metric_key, 0)

            for scope, count in task_metrics.get("recovery_by_scope", {}).items():
                aggregate["recovery_by_scope"][scope] = (
                    aggregate["recovery_by_scope"].get(scope, 0) + count
                )
                for bucket in (
                    _bucket(aggregate["by_project"], project_key),
                    _bucket(aggregate["by_session"], str(sess.id)),
                    _bucket(aggregate["by_model"], model_key),
                    _bucket(aggregate["by_day"], day_key or "unknown"),
                ):
                    bucket["recovery_by_scope"][scope] = (
                        bucket["recovery_by_scope"].get(scope, 0) + count
                    )

            for fc, count in task_metrics.get("recovery_by_failure_class", {}).items():
                aggregate["recovery_by_failure_class"][fc] = (
                    aggregate["recovery_by_failure_class"].get(fc, 0) + count
                )
                for bucket in (
                    _bucket(aggregate["by_project"], project_key),
                    _bucket(aggregate["by_session"], str(sess.id)),
                    _bucket(aggregate["by_model"], model_key),
                    _bucket(aggregate["by_day"], day_key or "unknown"),
                ):
                    bucket["recovery_by_failure_class"][fc] = (
                        bucket["recovery_by_failure_class"].get(fc, 0) + count
                    )

    def _finalize(bucket: Dict[str, Any]) -> None:
        total_terminal = (
            bucket["recovery_succeeded_count"]
            + bucket["recovery_failed_count"]
            + bucket["recovery_skipped_count"]
        )
        bucket["recovered_success_rate"] = (
            round(bucket["recovery_succeeded_count"] / total_terminal, 3)
            if total_terminal > 0
            else 0.0
        )
        bucket["total_terminal_outcomes"] = total_terminal

    _finalize(aggregate)
    for section in ("by_project", "by_session", "by_model", "by_day"):
        for bucket in aggregate[section].values():
            _finalize(bucket)
    return aggregate


def aggregate_metrics(all_metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge metrics from multiple runs into a single aggregate dict."""
    totals: Dict[str, Any] = {
        "recovery_attempted_count": 0,
        "recovery_succeeded_count": 0,
        "recovery_failed_count": 0,
        "recovery_skipped_count": 0,
        "recovery_budget_exhausted_count": 0,
        "recovery_false_success_count": 0,
        "recovery_by_scope": {},
        "recovery_by_failure_class": {},
    }
    for m in all_metrics:
        totals["recovery_attempted_count"] += m.get("recovery_attempted_count", 0)
        totals["recovery_succeeded_count"] += m.get("recovery_succeeded_count", 0)
        totals["recovery_failed_count"] += m.get("recovery_failed_count", 0)
        totals["recovery_skipped_count"] += m.get("recovery_skipped_count", 0)
        totals["recovery_budget_exhausted_count"] += m.get(
            "recovery_budget_exhausted_count", 0
        )
        totals["recovery_false_success_count"] += m.get(
            "recovery_false_success_count", 0
        )
        for scope, count in m.get("recovery_by_scope", {}).items():
            totals["recovery_by_scope"][scope] = (
                totals["recovery_by_scope"].get(scope, 0) + count
            )
        for fc, count in m.get("recovery_by_failure_class", {}).items():
            totals["recovery_by_failure_class"][fc] = (
                totals["recovery_by_failure_class"].get(fc, 0) + count
            )

    total_terminal = (
        totals["recovery_succeeded_count"]
        + totals["recovery_failed_count"]
        + totals["recovery_skipped_count"]
    )
    totals["recovered_success_rate"] = (
        round(totals["recovery_succeeded_count"] / total_terminal, 3)
        if total_terminal > 0
        else 0.0
    )
    totals["total_terminal_outcomes"] = total_terminal
    return totals


def _tally(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "recovery_attempted_count": 0,
        "recovery_succeeded_count": 0,
        "recovery_failed_count": 0,
        "recovery_skipped_count": 0,
        "recovery_budget_exhausted_count": 0,
        "recovery_false_success_count": 0,
        "recovery_by_scope": {},
        "recovery_by_failure_class": {},
    }
    for event in events:
        et = event.get("event_type", "")
        details = event.get("details", {})
        scope = details.get("scope", "unknown")
        fc = details.get("failure_class", "unknown")

        if et == EventType.EXECUTION_RECOVERY_ATTEMPTED:
            metrics["recovery_attempted_count"] += 1
            metrics["recovery_by_scope"][scope] = (
                metrics["recovery_by_scope"].get(scope, 0) + 1
            )
            metrics["recovery_by_failure_class"][fc] = (
                metrics["recovery_by_failure_class"].get(fc, 0) + 1
            )
        elif et == EventType.EXECUTION_RECOVERY_SUCCEEDED:
            metrics["recovery_succeeded_count"] += 1
        elif et == EventType.EXECUTION_RECOVERY_FAILED:
            metrics["recovery_failed_count"] += 1
            if details.get("budget_exhausted"):
                metrics["recovery_budget_exhausted_count"] += 1
        elif et == EventType.EXECUTION_RECOVERY_SKIPPED:
            metrics["recovery_skipped_count"] += 1
            metrics["recovery_by_scope"][scope] = (
                metrics["recovery_by_scope"].get(scope, 0) + 1
            )
            metrics["recovery_by_failure_class"][fc] = (
                metrics["recovery_by_failure_class"].get(fc, 0) + 1
            )

    total_terminal = (
        metrics["recovery_succeeded_count"]
        + metrics["recovery_failed_count"]
        + metrics["recovery_skipped_count"]
    )
    metrics["recovered_success_rate"] = (
        round(metrics["recovery_succeeded_count"] / total_terminal, 3)
        if total_terminal > 0
        else 0.0
    )
    metrics["total_terminal_outcomes"] = total_terminal
    return metrics
