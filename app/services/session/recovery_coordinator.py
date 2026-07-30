"""Canonical mutation authority for legacy stale-execution recovery.

Ownership evaluation remains observational and fail-safe in
``orphan_ownership``.  This module owns the separately locked, aggregate
mutation for an eligible legacy execution.  Startup and claim reconciliation
use the inspection helpers below and never mutate the execution graph.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Any, Callable

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    LogEntry,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.backend_concurrency import (
    make_redis_client,
    release_backend_slot,
)
from app.services.orchestration.run_state import (
    EXECUTION_PROGRESS_METADATA_KEY,
    mark_task_attempt_cancelled,
    mark_task_attempt_pending,
)
from app.services.orchestration.state.session_state import mark_session_stopped
from app.services.session.orphan_ownership import (
    OwnershipEvaluation,
    evaluate_execution_ownership,
)

logger = logging.getLogger(__name__)

RECOVERY_SOURCE_BOOT = "BOOT_RECOVERY"
RECOVERY_SOURCE_PERIODIC = "PERIODIC_SWEEP"
RECOVERY_SOURCE_CLAIM = "CLAIM_RECONCILIATION"
RECOVERY_SOURCES = frozenset(
    {RECOVERY_SOURCE_BOOT, RECOVERY_SOURCE_PERIODIC, RECOVERY_SOURCE_CLAIM}
)

RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
RECOVERY_SKIPPED = "RECOVERY_SKIPPED"
RECOVERY_REQUESTED = "RECOVERY_REQUESTED"
RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
RECOVERY_LOCK_UNAVAILABLE = "RECOVERY_LOCK_UNAVAILABLE"


@dataclass(frozen=True)
class RecoveryGraph:
    session: SessionModel
    task: Task
    session_task: SessionTask | None
    execution: TaskExecution


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _status(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _graph_state(graph: RecoveryGraph | None) -> dict[str, Any]:
    if graph is None:
        return {}
    lease = getattr(graph.execution, "runtime_lease", None)
    return {
        "session": {
            "id": graph.session.id,
            "status": _status(graph.session.status),
            "is_active": bool(graph.session.is_active),
        },
        "task": {
            "id": graph.task.id,
            "status": _status(graph.task.status),
            "started_at": _iso(graph.task.started_at),
            "completed_at": _iso(graph.task.completed_at),
        },
        "session_task": (
            {
                "id": graph.session_task.id,
                "status": _status(graph.session_task.status),
                "started_at": _iso(graph.session_task.started_at),
                "completed_at": _iso(graph.session_task.completed_at),
            }
            if graph.session_task is not None
            else None
        ),
        "execution": {
            "id": graph.execution.id,
            "status": _status(graph.execution.status),
            "runtime_lease_id": graph.execution.runtime_lease_id,
            "completed_at": _iso(graph.execution.completed_at),
        },
        "runtime_lease": (
            {
                "id": lease.id,
                "status": _status(lease.lease_status),
                "closed_at": _iso(getattr(lease, "closed_at", None)),
            }
            if lease is not None
            else None
        ),
    }


def _latest_session_task(
    db: Session, *, session_id: int, task_id: int, lock: bool = False
) -> SessionTask | None:
    query = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id, SessionTask.task_id == task_id)
        .order_by(SessionTask.id.desc())
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _load_graph(db: Session, execution_id: int, *, lock: bool) -> RecoveryGraph | None:
    query = db.query(TaskExecution).filter(TaskExecution.id == int(execution_id))
    if lock:
        query = query.with_for_update()
    execution = query.first()
    if execution is None:
        return None

    session_query = db.query(SessionModel).filter(
        SessionModel.id == execution.session_id
    )
    task_query = db.query(Task).filter(Task.id == execution.task_id)
    if lock:
        session_query = session_query.with_for_update()
        task_query = task_query.with_for_update()
    session = session_query.first()
    task = task_query.first()
    if session is None or task is None:
        return None
    session_task = _latest_session_task(
        db, session_id=session.id, task_id=task.id, lock=lock
    )
    return RecoveryGraph(
        session=session,
        task=task,
        session_task=session_task,
        execution=execution,
    )


def _log_counts_as_execution_progress(log: LogEntry) -> bool:
    """Return whether a structured log explicitly proves execution progress."""

    if not log.log_metadata:
        return False
    try:
        metadata = json.loads(log.log_metadata)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get(EXECUTION_PROGRESS_METADATA_KEY) is True
    )


def _latest_explicit_execution_progress_at(
    db: Session, *, session_id: int, task_id: int
) -> datetime | None:
    """Find only explicitly progress-bearing logs for one session/task scope."""

    candidates = [
        log.created_at
        for log in (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session_id,
                LogEntry.task_id == task_id,
                LogEntry.created_at.isnot(None),
            )
            .all()
        )
        if _log_counts_as_execution_progress(log)
    ]
    return max(candidates, key=_utc) if candidates else None


def _progress_at_for_scope(
    db: Session,
    *,
    session: SessionModel,
    task: Task,
    session_task: SessionTask | None,
    execution: TaskExecution | None,
) -> datetime | None:
    """Resolve progress from authoritative facts, then deterministic starts.

    Recovery and maintenance LogEntry rows are intentionally excluded.  The
    only log fallback is an explicit ``counts_as_execution_progress`` marker;
    otherwise the execution heartbeat/start and graph start timestamps provide
    stable historical behavior without manufacturing freshness from generic
    ``updated_at`` or inspection evidence.
    """

    explicit_log_at = _latest_explicit_execution_progress_at(
        db, session_id=session.id, task_id=task.id
    )
    authoritative = [
        getattr(execution, "heartbeat_at", None) if execution is not None else None,
        explicit_log_at,
    ]
    authoritative_candidates = [value for value in authoritative if value is not None]
    if authoritative_candidates:
        return max(authoritative_candidates, key=_utc)

    # Starts are a fallback chain, not a max across mutable graph timestamps.
    # This prevents a newer session creation/update timestamp from masking the
    # original execution start when no genuine progress has been recorded.
    return next(
        (
            value
            for value in (
                execution.started_at if execution is not None else None,
                session_task.started_at if session_task is not None else None,
                task.started_at,
                session.started_at,
                session.created_at,
            )
            if value is not None
        ),
        None,
    )


def _last_progress_at(db: Session, graph: RecoveryGraph) -> datetime | None:
    """Return the latest genuine execution progress, never latest evidence."""

    return _progress_at_for_scope(
        db,
        session=graph.session,
        task=graph.task,
        session_task=graph.session_task,
        execution=graph.execution,
    )


def _ownership_id(execution_id: int, evaluation: OwnershipEvaluation) -> str:
    return f"ownership:{execution_id}:{evaluation.observed_at.isoformat()}"


def _release_backend_capacity(graph: RecoveryGraph) -> str:
    """Release the legacy session slot exactly once when one was recorded."""

    backend_id = getattr(graph.execution, "backend_id", None)
    if not backend_id:
        return "not_owned"
    redis_client = make_redis_client()
    if not release_backend_slot(redis_client, backend_id, graph.session.id):
        raise RuntimeError(
            f"backend slot release failed for session {graph.session.id}"
        )
    return "released"


def _event(
    db: Session,
    *,
    event_type: str,
    source: str,
    graph: RecoveryGraph | None,
    metadata: dict[str, Any],
) -> None:
    payload = {
        "schema_version": "recovery-lifecycle/1.0",
        "event_type": event_type,
        "source": source,
        "execution_id": graph.execution.id if graph else metadata.get("execution_id"),
        "session_id": graph.session.id if graph else metadata.get("session_id"),
        "task_id": graph.task.id if graph else metadata.get("task_id"),
        **metadata,
        EXECUTION_PROGRESS_METADATA_KEY: False,
    }
    db.add(
        LogEntry(
            session_id=payload.get("session_id"),
            task_id=payload.get("task_id"),
            task_execution_id=payload.get("execution_id"),
            session_instance_id=(graph.session.instance_id if graph else None),
            level=(
                "WARN"
                if event_type in {RECOVERY_BLOCKED, RECOVERY_LOCK_UNAVAILABLE}
                else "INFO"
            ),
            message=f"[{event_type}] stale execution recovery",
            log_metadata=json.dumps(payload, sort_keys=True, default=str),
        )
    )


def _result(
    *,
    outcome: str,
    source: str,
    execution_id: int,
    ownership: OwnershipEvaluation | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    result = {
        "execution_id": execution_id,
        "source": source,
        "outcome": outcome,
        "lock_result": metadata.pop("lock_result", "not_acquired"),
        "ownership_evaluation_id": (
            _ownership_id(execution_id, ownership) if ownership else None
        ),
    }
    if ownership is not None:
        result["ownership"] = ownership.to_dict()
        # Preserve the flat G1 maintenance decision interface while adding the
        # richer G2R coordinator envelope around it.
        result["ownership_classification"] = ownership.ownership_classification
        result["recovery_eligible"] = ownership.recovery_eligible
        result["signals"] = list(ownership.signals)
        result["decision_reason"] = ownership.decision_reason
    result.update(metadata)
    return result


def inspect_running_claim(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    execution_id: int | None,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Observe a duplicate running claim and request periodic adjudication.

    This seam intentionally never changes task, link, session, or execution
    state.  It exists so a redelivered worker cannot cancel the original
    execution merely because its task row is already ``running``.
    """

    observed_at = _utc(now)
    execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
            TaskExecution.status == TaskStatus.RUNNING,
        )
        .order_by(TaskExecution.id.desc())
        .first()
    )
    if execution is None:
        result = _result(
            outcome="no_running_execution",
            source=RECOVERY_SOURCE_CLAIM,
            execution_id=int(execution_id or 0),
            session_id=session_id,
            task_id=task_id,
        )
        _event(
            db,
            event_type=RECOVERY_SKIPPED,
            source=RECOVERY_SOURCE_CLAIM,
            graph=None,
            metadata=result,
        )
        db.commit()
        return result

    graph = _load_graph(db, execution.id, lock=False)
    if graph is None:
        return _result(
            outcome="graph_not_found",
            source=RECOVERY_SOURCE_CLAIM,
            execution_id=execution.id,
        )
    ownership = evaluate_execution_ownership(
        graph.execution,
        runtime_lease=getattr(graph.execution, "runtime_lease", None),
        now=observed_at,
        stale_after_seconds=stale_after_seconds,
    )
    progress_at = _last_progress_at(db, graph)
    age_seconds = (
        (observed_at - _utc(progress_at)).total_seconds() if progress_at else None
    )
    eligible = bool(
        ownership.recovery_eligible
        and age_seconds is not None
        and age_seconds >= stale_after_seconds
    )
    result = _result(
        outcome="recovery_requested" if eligible else "duplicate_delivery_ignored",
        source=RECOVERY_SOURCE_CLAIM,
        execution_id=graph.execution.id,
        ownership=ownership,
        lock_result="not_requested",
        recovery_eligible=eligible,
        progress_at=_iso(progress_at),
        age_seconds=age_seconds,
        selected_action="periodic_sweep" if eligible else "preserve_original_execution",
        pre_mutation_graph=_graph_state(graph),
    )
    _event(
        db,
        event_type=RECOVERY_REQUESTED if eligible else RECOVERY_SKIPPED,
        source=RECOVERY_SOURCE_CLAIM,
        graph=graph,
        metadata=result,
    )
    db.commit()
    return result


def inspect_startup_running_executions(
    db: Session,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Observe boot candidates without owning execution recovery mutation."""

    observed_at = _utc(now)
    rows = (
        db.query(TaskExecution)
        .filter(TaskExecution.status == TaskStatus.RUNNING)
        .order_by(TaskExecution.id.asc())
        .all()
    )
    results: list[dict[str, Any]] = []
    for execution in rows:
        graph = _load_graph(db, execution.id, lock=False)
        if graph is None:
            continue
        ownership = evaluate_execution_ownership(
            graph.execution,
            runtime_lease=getattr(graph.execution, "runtime_lease", None),
            now=observed_at,
            stale_after_seconds=stale_after_seconds,
        )
        progress_at = _last_progress_at(db, graph)
        age_seconds = (
            (observed_at - _utc(progress_at)).total_seconds() if progress_at else None
        )
        eligible = bool(
            ownership.recovery_eligible
            and age_seconds is not None
            and age_seconds >= stale_after_seconds
        )
        result = _result(
            outcome="recovery_requested" if eligible else "startup_observed",
            source=RECOVERY_SOURCE_BOOT,
            execution_id=execution.id,
            ownership=ownership,
            lock_result="not_requested",
            recovery_eligible=eligible,
            progress_at=_iso(progress_at),
            age_seconds=age_seconds,
            selected_action="periodic_sweep" if eligible else "preserve_state",
            pre_mutation_graph=_graph_state(graph),
        )
        _event(
            db,
            event_type=RECOVERY_REQUESTED if eligible else RECOVERY_SKIPPED,
            source=RECOVERY_SOURCE_BOOT,
            graph=graph,
            metadata=result,
        )
        results.append(result)
    if results:
        db.commit()
    return results


def recover_running_residue(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    session_task_id: int,
    stale_after_seconds: int,
    now: datetime | None = None,
    knowledge_recorder: Callable[..., bool] | None = None,
) -> dict[str, Any]:
    """Recover a stale running task/link that has no valid execution row.

    This is a distinct residue class, not execution-orphan adjudication.  It
    is still owned by the periodic recovery coordinator so boot and claim
    paths cannot reset the graph merely to make a task claimable.
    """

    observed_at = _utc(now)
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id)
        .with_for_update()
        .first()
    )
    task = db.query(Task).filter(Task.id == task_id).with_for_update().first()
    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.id == session_task_id)
        .with_for_update()
        .first()
    )
    if session is None or task is None or session_task is None:
        return _result(
            outcome="already_reconciled",
            source=RECOVERY_SOURCE_PERIODIC,
            execution_id=0,
            lock_result="acquired",
            idempotency="residue_missing",
            session_id=session_id,
            task_id=task_id,
        )

    before = {
        "session": {
            "id": session.id,
            "status": _status(session.status),
            "is_active": bool(session.is_active),
        },
        "task": {"id": task.id, "status": _status(task.status)},
        "session_task": {
            "id": session_task.id,
            "status": _status(session_task.status),
        },
        "execution": None,
    }
    progress_at = _progress_at_for_scope(
        db,
        session=session,
        task=task,
        session_task=session_task,
        execution=None,
    )
    age_seconds = (
        (observed_at - _utc(progress_at)).total_seconds() if progress_at else None
    )
    if (
        age_seconds is None
        or age_seconds < stale_after_seconds
        or task.status != TaskStatus.RUNNING
        or session_task.status != TaskStatus.RUNNING
        or session.status not in {"running", "active"}
        or not session.is_active
    ):
        result = _result(
            outcome="residue_skipped",
            source=RECOVERY_SOURCE_PERIODIC,
            execution_id=0,
            lock_result="acquired",
            progress_at=_iso(progress_at),
            age_seconds=age_seconds,
            pre_mutation_graph=before,
            session_id=session_id,
            task_id=task_id,
        )
        _event(
            db,
            event_type=RECOVERY_SKIPPED,
            source=RECOVERY_SOURCE_PERIODIC,
            graph=None,
            metadata=result,
        )
        db.commit()
        return result

    completed_at = observed_at.astimezone(timezone.utc)
    stop_reason = (
        "no_progress_timeout"
        if progress_at is not None
        else "hard_time_limit_or_worker_killed"
    )
    mark_task_attempt_pending(
        task=task,
        session_task_link=session_task,
        reset_started_at=True,
        error_message="Recovered stale running task residue without an active execution.",
    )
    mark_session_stopped(session, stopped_at=completed_at)
    session.last_alert_level = "warn"
    session.last_alert_message = (
        "Recovered stale running task residue without an active execution."
    )
    session.last_alert_at = completed_at
    after = {
        "session": {
            "id": session.id,
            "status": _status(session.status),
            "is_active": bool(session.is_active),
        },
        "task": {"id": task.id, "status": _status(task.status)},
        "session_task": {
            "id": session_task.id,
            "status": _status(session_task.status),
        },
        "execution": None,
    }
    result = _result(
        outcome="recovered",
        source=RECOVERY_SOURCE_PERIODIC,
        execution_id=0,
        lock_result="acquired",
        stop_reason=stop_reason,
        selected_action="reset_task_residue_stop_session",
        mutation_result="committed",
        progress_at=_iso(progress_at),
        age_seconds=age_seconds,
        pre_mutation_graph=before,
        post_mutation_graph=after,
        session_id=session_id,
        task_id=task_id,
    )
    _event(
        db,
        event_type=RECOVERY_COMPLETED,
        source=RECOVERY_SOURCE_PERIODIC,
        graph=None,
        metadata=result,
    )
    db.commit()
    result["knowledge_recorded"] = (
        bool(
            knowledge_recorder(
                db,
                session_id=session_id,
                task_id=task_id,
                stop_reason=stop_reason,
            )
        )
        if knowledge_recorder is not None
        else False
    )
    return result


def recover_stale_execution(
    db: Session,
    execution_id: int,
    *,
    stale_after_seconds: int,
    source: str = RECOVERY_SOURCE_PERIODIC,
    now: datetime | None = None,
    knowledge_recorder: Callable[..., bool] | None = None,
) -> dict[str, Any]:
    """Recheck and atomically recover one eligible stale legacy execution."""

    if source != RECOVERY_SOURCE_PERIODIC:
        raise ValueError("stale execution mutation is owned by PERIODIC_SWEEP")

    observed_at = _utc(now)
    graph: RecoveryGraph | None = None
    try:
        graph = _load_graph(db, int(execution_id), lock=True)
    except (OperationalError, SQLAlchemyError) as exc:
        db.rollback()
        result = _result(
            outcome="lock_unavailable",
            source=source,
            execution_id=int(execution_id),
            lock_result="unavailable",
            error_category=exc.__class__.__name__,
        )
        try:
            _event(
                db,
                event_type=RECOVERY_LOCK_UNAVAILABLE,
                source=source,
                graph=None,
                metadata=result,
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
        return result

    if graph is None:
        result = _result(
            outcome="already_recovered",
            source=source,
            execution_id=int(execution_id),
            lock_result="acquired",
            idempotency="target_missing_or_deleted",
        )
        return result

    before = _graph_state(graph)
    if graph.execution.status != TaskStatus.RUNNING:
        result = _result(
            outcome="already_recovered",
            source=source,
            execution_id=graph.execution.id,
            lock_result="acquired",
            idempotency="execution_already_terminal",
            pre_mutation_graph=before,
        )
        _event(
            db, event_type=RECOVERY_SKIPPED, source=source, graph=graph, metadata=result
        )
        db.commit()
        return result

    ownership = evaluate_execution_ownership(
        graph.execution,
        runtime_lease=getattr(graph.execution, "runtime_lease", None),
        now=observed_at,
        stale_after_seconds=stale_after_seconds,
    )
    progress_at = _last_progress_at(db, graph)
    age_seconds = (
        (observed_at - _utc(progress_at)).total_seconds() if progress_at else None
    )
    common = {
        "lock_result": "acquired",
        "recovery_eligible": ownership.recovery_eligible,
        "progress_at": _iso(progress_at),
        "age_seconds": age_seconds,
        "pre_mutation_graph": before,
    }
    if (
        not ownership.recovery_eligible
        or age_seconds is None
        or age_seconds < stale_after_seconds
    ):
        result = _result(
            outcome="skipped_live_or_ambiguous",
            source=source,
            execution_id=graph.execution.id,
            ownership=ownership,
            **common,
        )
        _event(
            db, event_type=RECOVERY_SKIPPED, source=source, graph=graph, metadata=result
        )
        db.commit()
        return result

    running_links = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == graph.session.id,
            SessionTask.task_id == graph.task.id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .all()
    )
    if (
        graph.task.status != TaskStatus.RUNNING
        or graph.session.status not in {"running", "active"}
        or not graph.session.is_active
        or graph.session_task is None
        or graph.session_task.status != TaskStatus.RUNNING
        or len(running_links) != 1
    ):
        result = _result(
            outcome="blocked_contradictory_graph",
            source=source,
            execution_id=graph.execution.id,
            ownership=ownership,
            error_category="RecoveryGraphInvariantError",
            selected_action="operator_review",
            **common,
        )
        _event(
            db, event_type=RECOVERY_BLOCKED, source=source, graph=graph, metadata=result
        )
        db.commit()
        return result

    completed_at = observed_at.astimezone(timezone.utc)
    stop_reason = (
        "no_progress_timeout"
        if progress_at
        and graph.session_task
        and progress_at != graph.session_task.started_at
        else "hard_time_limit_or_worker_killed"
    )
    try:
        backend_slot_reconciliation = _release_backend_capacity(graph)
        mark_task_attempt_cancelled(
            task=None,
            session_task_link=None,
            task_execution=graph.execution,
            completed_at=completed_at,
        )
        mark_task_attempt_pending(
            task=graph.task,
            session_task_link=graph.session_task,
            reset_started_at=True,
            error_message="Recovered stale running session after runtime stopped making progress.",
        )
        mark_session_stopped(graph.session, stopped_at=completed_at)
        graph.session.last_alert_level = "warn"
        graph.session.last_alert_message = (
            "Recovered stale running session after runtime stopped making progress."
        )
        graph.session.last_alert_at = completed_at
        lease = getattr(graph.execution, "runtime_lease", None)
        lease_reconciliation = "none"
        if (
            lease is not None
            and _status(getattr(lease, "lease_status", None)) == "active"
        ):
            lease.lease_status = "expired"
            lease.released_at = completed_at
            lease.release_reason = "stale_execution_recovery"
            lease.closed_at = completed_at
            lease.closure_reason = "stale_execution_recovery"
            lease_reconciliation = "expired"
        result = _result(
            outcome="recovered",
            source=source,
            execution_id=graph.execution.id,
            ownership=ownership,
            stop_reason=stop_reason,
            selected_action="cancel_execution_reset_task_stop_session",
            lease_reconciliation=lease_reconciliation,
            mutation_result="committed",
            backend_slot_reconciliation=backend_slot_reconciliation,
            post_mutation_graph=_graph_state(graph),
            **common,
        )
        _event(
            db,
            event_type=RECOVERY_COMPLETED,
            source=source,
            graph=graph,
            metadata=result,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        result = _result(
            outcome="mutation_failed",
            source=source,
            execution_id=graph.execution.id,
            ownership=ownership,
            error_category=exc.__class__.__name__,
            mutation_result="rolled_back",
            **common,
        )
        try:
            _event(
                db,
                event_type=RECOVERY_BLOCKED,
                source=source,
                graph=graph,
                metadata=result,
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
        return result

    refreshed = _load_graph(db, graph.execution.id, lock=False)
    result["post_mutation_graph"] = _graph_state(refreshed)
    if knowledge_recorder is not None:
        result["knowledge_recorded"] = bool(
            knowledge_recorder(
                db,
                session_id=graph.session.id,
                task_id=graph.task.id,
                stop_reason=stop_reason,
            )
        )
    else:
        result["knowledge_recorded"] = False
    return result
