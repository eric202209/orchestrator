"""Durable structured lifecycle evidence for periodic maintenance tasks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import LogEntry


ORPHAN_SWEEP_TASK_NAME = "app.tasks.maintenance.sweep_orphaned_running_sessions"
ORPHAN_SWEEP_SCHEDULE_ID = "recover-orphaned-running-sessions"
MAINTENANCE_FRESHNESS_SECONDS = 2100

MAINTENANCE_DISPATCHED = "MAINTENANCE_DISPATCHED"
MAINTENANCE_RECEIVED = "MAINTENANCE_RECEIVED"
MAINTENANCE_STARTED = "MAINTENANCE_STARTED"
MAINTENANCE_COMPLETED = "MAINTENANCE_COMPLETED"
MAINTENANCE_FAILED = "MAINTENANCE_FAILED"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def record_maintenance_event(
    db: Session,
    *,
    event_type: str,
    invocation_id: str,
    observed_at: datetime | None = None,
    task_name: str = ORPHAN_SWEEP_TASK_NAME,
    schedule_identity: str = ORPHAN_SWEEP_SCHEDULE_ID,
    dispatch_source: str | None = None,
    scheduled_at: datetime | None = None,
    worker_identity: str | None = None,
    counts: dict[str, int] | None = None,
    error_category: str | None = None,
    error_timestamp: datetime | None = None,
    duration_seconds: float | None = None,
    decision_evidence: list[dict[str, Any]] | None = None,
) -> LogEntry:
    observed = _utc(observed_at)
    metadata: dict[str, Any] = {
        "event_type": event_type,
        "task_name": task_name,
        "schedule_identity": schedule_identity,
        "invocation_id": str(invocation_id),
        "observed_at": observed.isoformat(),
    }
    if dispatch_source:
        metadata["dispatch_source"] = dispatch_source
    if scheduled_at is not None:
        metadata["scheduled_at"] = _utc(scheduled_at).isoformat()
    if worker_identity:
        metadata["worker_identity"] = worker_identity
    if counts is not None:
        metadata["result_counts"] = {key: int(value) for key, value in counts.items()}
    if error_category:
        metadata["error_category"] = str(error_category)[:128]
    if error_timestamp is not None:
        metadata["error_timestamp"] = _utc(error_timestamp).isoformat()
    if duration_seconds is not None:
        metadata["duration_seconds"] = round(float(duration_seconds), 3)
    if decision_evidence is not None:
        metadata["decision_evidence"] = decision_evidence

    row = LogEntry(
        level="ERROR" if event_type == MAINTENANCE_FAILED else "INFO",
        message=f"[{event_type}] orphan sweep",
        log_metadata=json.dumps(metadata, sort_keys=True),
        created_at=observed,
    )
    db.add(row)
    return row


def build_sweep_result_counts(
    decisions: list[dict[str, Any]], *, recovered_count: int
) -> dict[str, int]:
    terminal = sum(
        1
        for decision in decisions
        if decision.get("ownership_classification") == "TERMINAL_EXECUTION"
    )
    ambiguous = sum(
        1
        for decision in decisions
        if decision.get("ownership_classification") == "AMBIGUOUS_FAIL_SAFE"
    )
    errors = sum(1 for decision in decisions if decision.get("error_category"))
    live = sum(
        1
        for decision in decisions
        if not decision.get("recovery_eligible")
        and decision.get("ownership_classification")
        not in {"TERMINAL_EXECUTION", "AMBIGUOUS_FAIL_SAFE"}
        and not decision.get("error_category")
    )
    return {
        "inspected_execution_count": len(decisions),
        "recovery_eligible_count": sum(
            1 for decision in decisions if decision.get("recovery_eligible")
        ),
        "recovered_count": int(recovered_count),
        "skipped_live_count": live,
        "skipped_terminal_count": terminal,
        "ambiguous_fail_safe_count": ambiguous,
        "error_count": errors,
    }


def _parse_metadata(row: LogEntry) -> dict[str, Any] | None:
    try:
        value = json.loads(row.log_metadata or "")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _event_time(row: LogEntry, metadata: dict[str, Any]) -> datetime:
    raw = metadata.get("observed_at")
    try:
        return _utc(datetime.fromisoformat(str(raw)))
    except (TypeError, ValueError):
        return _utc(row.created_at)


def _iso(row: LogEntry | None, metadata: dict[str, Any] | None) -> str | None:
    if row is None or metadata is None:
        return None
    return _event_time(row, metadata).isoformat()


def _beat_configuration() -> dict[str, Any]:
    try:
        from app.celery_app import celery_app

        matching = [
            (identity, entry)
            for identity, entry in celery_app.conf.beat_schedule.items()
            if entry.get("task") == ORPHAN_SWEEP_TASK_NAME
        ]
        if len(matching) != 1:
            return {
                "configured": False,
                "configuration_status": "DISABLED" if not matching else "ERROR",
                "matching_entry_count": len(matching),
                "task_name": ORPHAN_SWEEP_TASK_NAME,
            }
        identity, entry = matching[0]
        schedule = entry.get("schedule")
        interval_seconds = (
            schedule.total_seconds() if hasattr(schedule, "total_seconds") else None
        )
        return {
            "configured": True,
            "configuration_status": "CONFIGURED",
            "matching_entry_count": 1,
            "schedule_identity": identity,
            "task_name": ORPHAN_SWEEP_TASK_NAME,
            "interval_seconds": interval_seconds,
        }
    except Exception as exc:  # pragma: no cover - defensive operator surface
        return {
            "configured": False,
            "configuration_status": "UNKNOWN",
            "task_name": ORPHAN_SWEEP_TASK_NAME,
            "error": str(exc)[:255],
        }


def maintenance_health(
    db: Session,
    *,
    now: datetime | None = None,
    freshness_seconds: int = MAINTENANCE_FRESHNESS_SECONDS,
) -> dict[str, Any]:
    """Return state derived only from durable maintenance events and config."""

    checked_at = _utc(now)
    beat = _beat_configuration()
    if not beat.get("configured"):
        return {
            "status": "degraded",
            "freshness_state": "DISABLED",
            "checked_at": checked_at.isoformat(),
            "beat": {**beat, "liveness_state": "DISABLED"},
            "last_orphan_sweep_dispatch": None,
            "last_orphan_sweep_received": None,
            "last_orphan_sweep_start": None,
            "last_orphan_sweep_completion": None,
            "last_successful_orphan_sweep": None,
            "last_failed_orphan_sweep": None,
            "last_sweep_result_counts": None,
            "last_error_category": None,
            "last_error_timestamp": None,
            "worker_received_without_completion": False,
        }

    try:
        rows = (
            db.query(LogEntry)
            .filter(LogEntry.message.like("[MAINTENANCE_%"))
            .order_by(LogEntry.created_at.desc(), LogEntry.id.desc())
            .limit(500)
            .all()
        )
    except Exception as exc:
        return {
            "status": "degraded",
            "freshness_state": "UNKNOWN",
            "checked_at": checked_at.isoformat(),
            "beat": {**beat, "liveness_state": "UNKNOWN"},
            "error": str(exc)[:255],
            "last_orphan_sweep_dispatch": None,
            "last_orphan_sweep_received": None,
            "last_orphan_sweep_start": None,
            "last_orphan_sweep_completion": None,
            "last_successful_orphan_sweep": None,
            "last_failed_orphan_sweep": None,
            "last_sweep_result_counts": None,
            "last_error_category": "maintenance_state_unavailable",
            "last_error_timestamp": checked_at.isoformat(),
            "worker_received_without_completion": False,
        }

    events: list[tuple[LogEntry, dict[str, Any], datetime]] = []
    for row in rows:
        metadata = _parse_metadata(row)
        if metadata and metadata.get("task_name") == ORPHAN_SWEEP_TASK_NAME:
            events.append((row, metadata, _event_time(row, metadata)))

    by_type: dict[str, tuple[LogEntry, dict[str, Any], datetime]] = {}
    for row, metadata, timestamp in events:
        event_type = str(metadata.get("event_type") or "")
        if event_type not in by_type:
            by_type[event_type] = (row, metadata, timestamp)

    dispatch = by_type.get(MAINTENANCE_DISPATCHED)
    received = by_type.get(MAINTENANCE_RECEIVED)
    started = by_type.get(MAINTENANCE_STARTED)
    completed = by_type.get(MAINTENANCE_COMPLETED)
    failed = by_type.get(MAINTENANCE_FAILED)
    successful_completion = completed
    last_dispatch_time = dispatch[2] if dispatch else None
    beat_age = (
        (checked_at - last_dispatch_time).total_seconds()
        if last_dispatch_time is not None
        else None
    )
    beat_liveness = (
        "NEVER_OBSERVED"
        if beat_age is None
        else "RECENT_DISPATCH" if beat_age <= freshness_seconds else "STALE"
    )

    terminal_timestamp = max(
        [item[2] for item in (completed, failed) if item is not None],
        default=None,
    )
    incomplete = bool(
        received and (terminal_timestamp is None or received[2] > terminal_timestamp)
    )
    if not events:
        freshness_state = "NEVER_OBSERVED"
    elif failed is not None and (completed is None or failed[2] >= completed[2]):
        freshness_state = "FAILED"
    elif incomplete:
        freshness_state = (
            "UNKNOWN"
            if (checked_at - received[2]).total_seconds() <= freshness_seconds
            else "STALE"
        )
    elif completed is None:
        freshness_state = "UNKNOWN"
    elif (checked_at - completed[2]).total_seconds() > freshness_seconds:
        freshness_state = "STALE"
    else:
        freshness_state = "HEALTHY"

    latest_counts = completed[1].get("result_counts") if completed else None
    if not isinstance(latest_counts, dict):
        latest_counts = None
    last_error = failed[1] if failed else {}
    return {
        "status": "ok" if freshness_state == "HEALTHY" else "degraded",
        "freshness_state": freshness_state,
        "checked_at": checked_at.isoformat(),
        "beat": {**beat, "liveness_state": beat_liveness},
        "last_orphan_sweep_dispatch": _iso(*dispatch[:2]) if dispatch else None,
        "last_orphan_sweep_received": _iso(*received[:2]) if received else None,
        "last_orphan_sweep_start": _iso(*started[:2]) if started else None,
        "last_orphan_sweep_completion": _iso(*completed[:2]) if completed else None,
        "last_successful_orphan_sweep": (
            _iso(*successful_completion[:2]) if successful_completion else None
        ),
        "last_failed_orphan_sweep": _iso(*failed[:2]) if failed else None,
        "last_sweep_result_counts": latest_counts,
        "last_error_category": last_error.get("error_category"),
        "last_error_timestamp": last_error.get("error_timestamp")
        or (_iso(*failed[:2]) if failed else None),
        "worker_received_without_completion": incomplete,
    }
