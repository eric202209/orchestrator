"""E2 lifecycle transition and generation-fence primitives.

The functions in this module are deliberately not wired into the legacy
failure/retry writers yet.  They provide the durable write contract that those
writers will adopt in the following Phase 36 slices.

Commit ownership belongs to the caller by default.  Passing ``commit=True``
is an explicit convenience for callers that own the whole transition.  No
function publishes to Celery or claims that a database commit is atomic with
an external broker operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.run_state.transitions import (
    mark_task_attempt_done,
    mark_task_attempt_cancelled,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
    reset_active_attempts_for_session_stop,
)
from app.services.orchestration.state.session_state import normalize_session_status
from app.services.tasks.execution import create_task_execution


MAX_ACTIVE_LOGICAL_EXECUTIONS_PER_SESSION = 1

VALID_CONTINUATION_KINDS = frozenset(
    {"celery_retry", "backend_capacity", "automatic_recovery"}
)
_AUTONOMOUS_SESSION_STATUSES = frozenset({"running", "recovering", "retry_pending"})
_STABLE_TERMINAL_STATUSES = frozenset(
    {"failed", "completed", "done", "stopped", "cancelled", "canceled"}
)
_REVOCATION_STATUSES = frozenset({"paused", "stopped", "cancelled", "canceled"})


class LifecycleTransitionError(ValueError):
    """A requested transition does not satisfy its durable preconditions."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ContinuationIdentity:
    """The durable identity a continuation delivery must present to claim.

    ``continuation_task_id`` is the task identity stored on Session.
    ``task_execution_id`` binds the delivery to the exact pending attempt; it
    is required for a strict claim.  The Session ``instance_id`` binds both to
    the logical generation that created the continuation.
    """

    session_id: int
    instance_id: str
    continuation_task_id: int
    continuation_kind: str
    task_execution_id: int | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class TransitionResult:
    """Deterministic result for admission and continuation claims."""

    accepted: bool
    reason: str
    session_id: int | None = None
    task_id: int | None = None
    task_execution_id: int | None = None
    continuation_identity: ContinuationIdentity | None = None

    def __bool__(self) -> bool:
        return self.accepted


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(timezone.utc)


def _commit(db: DbSession, commit: bool) -> None:
    if commit:
        db.commit()


def _session_id(session: SessionModel) -> int:
    value = getattr(session, "id", None)
    if not isinstance(value, int):
        raise LifecycleTransitionError("session_identity_missing")
    return value


def _ensure_instance_id(session: SessionModel) -> str:
    current = getattr(session, "instance_id", None)
    if isinstance(current, str) and current.strip():
        return current
    session.instance_id = str(uuid.uuid4())
    return session.instance_id


def _rotate_instance_id(session: SessionModel) -> str:
    previous = getattr(session, "instance_id", None)
    next_value = str(uuid.uuid4())
    while previous and next_value == previous:
        next_value = str(uuid.uuid4())
    session.instance_id = next_value
    return next_value


def _validate_kind(kind: Any) -> str:
    if not isinstance(kind, str):
        raise LifecycleTransitionError("continuation_kind_invalid")
    normalized = kind.strip()
    if normalized not in VALID_CONTINUATION_KINDS:
        raise LifecycleTransitionError("continuation_kind_invalid")
    return normalized


def _validate_retry_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleTransitionError("continuation_retry_count_invalid")
    return value


def _set_continuation(
    session: SessionModel,
    *,
    task_id: int,
    kind: str,
    retry_count: int,
    retry_eta: datetime | None,
    changed_at: datetime,
) -> None:
    if not isinstance(task_id, int) or task_id <= 0:
        raise LifecycleTransitionError("continuation_task_id_invalid")
    session.continuation_task_id = task_id
    session.continuation_kind = _validate_kind(kind)
    session.continuation_retry_count = _validate_retry_count(retry_count)
    session.continuation_retry_eta = retry_eta
    session.lifecycle_updated_at = changed_at


def _clear_continuation(session: SessionModel, *, changed_at: datetime) -> None:
    session.continuation_task_id = None
    session.continuation_kind = None
    session.continuation_retry_count = 0
    session.continuation_retry_eta = None
    session.lifecycle_updated_at = changed_at


def _latest_link(db: DbSession, *, session_id: int, task_id: int) -> SessionTask | None:
    return (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )


def _task_for_execution(
    db: DbSession, execution: TaskExecution
) -> tuple[Task | None, SessionTask | None]:
    task = db.query(Task).filter(Task.id == execution.task_id).first()
    link = _latest_link(db, session_id=execution.session_id, task_id=execution.task_id)
    return task, link


def _validate_execution_belongs(
    execution: TaskExecution,
    *,
    session_id: int,
    task_id: int | None = None,
) -> None:
    if execution.session_id != session_id:
        raise LifecycleTransitionError("task_execution_session_mismatch")
    if task_id is not None and execution.task_id != task_id:
        raise LifecycleTransitionError("task_execution_task_mismatch")


def _active_executions(db: DbSession, session_id: int) -> list[TaskExecution]:
    return (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .order_by(TaskExecution.id.asc())
        .all()
    )


def _active_session_links(db: DbSession, session_id: int) -> list[SessionTask]:
    return (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .order_by(SessionTask.id.asc())
        .all()
    )


def _active_linked_tasks(db: DbSession, session_id: int) -> list[Task]:
    return (
        db.query(Task)
        .join(SessionTask, SessionTask.task_id == Task.id)
        .filter(
            SessionTask.session_id == session_id,
            Task.status == TaskStatus.RUNNING,
        )
        .order_by(Task.id.asc())
        .all()
    )


def _cancel_other_active_executions(
    db: DbSession,
    *,
    session_id: int,
    keep_execution_id: int | None,
    completed_at: datetime,
    reason: str | None,
) -> None:
    """Fence obsolete queued/running attempts during logical finalization."""

    for execution in _active_executions(db, session_id):
        if keep_execution_id is not None and execution.id == keep_execution_id:
            continue
        task, link = _task_for_execution(db, execution)
        mark_task_attempt_cancelled(
            task=task,
            session_task_link=link,
            task_execution=execution,
            completed_at=completed_at,
            error_message=reason,
        )


def _result_rejected(
    *,
    session_id: int | None,
    reason: str,
    task_id: int | None = None,
    task_execution_id: int | None = None,
) -> TransitionResult:
    return TransitionResult(
        accepted=False,
        reason=reason,
        session_id=session_id,
        task_id=task_id,
        task_execution_id=task_execution_id,
    )


def admit_autonomous_execution(
    db: DbSession,
    session: SessionModel,
    *,
    task: Task,
    expected_instance_id: str | None = None,
    expected_task_execution_id: int | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> TransitionResult:
    """Atomically admit one fresh autonomous execution for a Session.

    Preconditions: the Session is ``pending`` (or an idempotent active claim
    is explicitly identified by its execution id in a future caller).  The
    conditional ``pending -> running`` UPDATE is the lock boundary: databases
    that honor row locks serialize it, while SQLite deterministically reports
    the losing conditional update after the winning transaction commits.

    Rows changed: Session, one SessionTask link, one PENDING TaskExecution,
    and the Task status.  The helper does not publish work.  The caller owns
    commit/rollback unless ``commit=True``.
    """

    session_id = _session_id(session)
    if task is None or not isinstance(getattr(task, "id", None), int):
        return _result_rejected(session_id=session_id, reason="task_identity_missing")
    expected = expected_instance_id
    current_instance = getattr(session, "instance_id", None)
    if expected is not None and expected != current_instance:
        return _result_rejected(
            session_id=session_id,
            task_id=task.id,
            reason="session_instance_changed",
        )

    status = normalize_session_status(getattr(session, "status", None))
    if status in _AUTONOMOUS_SESSION_STATUSES:
        if expected_task_execution_id is not None:
            existing = (
                db.query(TaskExecution)
                .filter(
                    TaskExecution.id == expected_task_execution_id,
                    TaskExecution.session_id == session_id,
                    TaskExecution.task_id == task.id,
                    TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
                )
                .first()
            )
            if existing is not None:
                return TransitionResult(
                    accepted=True,
                    reason="already_admitted",
                    session_id=session_id,
                    task_id=task.id,
                    task_execution_id=existing.id,
                )
        return _result_rejected(
            session_id=session_id,
            task_id=task.id,
            reason="autonomous_execution_already_active",
        )
    if status != "pending":
        return _result_rejected(
            session_id=session_id,
            task_id=task.id,
            reason="session_not_admissible",
        )

    # Preserve caller-owned transaction composition: a preceding explicit
    # generation transition may have changed the in-memory Session fence.
    db.flush()
    changed_at = _now(changed_at)
    values = {
        SessionModel.status: "running",
        SessionModel.is_active: True,
        SessionModel.lifecycle_updated_at: changed_at,
    }
    filters = [
        SessionModel.id == session_id,
        SessionModel.status == "pending",
    ]
    if expected is not None:
        filters.append(SessionModel.instance_id == expected)
    updated = (
        db.query(SessionModel)
        .filter(*filters)
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        return _result_rejected(
            session_id=session_id,
            task_id=task.id,
            reason="autonomous_admission_race_lost",
        )

    # Keep the caller's identity-map instance coherent with the conditional
    # UPDATE.  This also makes the postcondition observable before commit.
    session.status = "running"
    session.is_active = True
    session.lifecycle_updated_at = changed_at
    instance_id = _ensure_instance_id(session)

    active = _active_executions(db, session_id)
    if (
        active
        or _active_session_links(db, session_id)
        or _active_linked_tasks(db, session_id)
    ):
        # A pending Session with only historical active-looking residue is not
        # a supported writer state.  Do not leave the reservation active when
        # that ambiguity is encountered; the caller may inspect and retry.
        db.query(SessionModel).filter(
            SessionModel.id == session_id,
            SessionModel.status == "running",
        ).update(
            {
                SessionModel.status: "pending",
                SessionModel.is_active: False,
                SessionModel.lifecycle_updated_at: changed_at,
            },
            synchronize_session=False,
        )
        session.status = "pending"
        session.is_active = False
        return _result_rejected(
            session_id=session_id,
            task_id=task.id,
            reason="active_execution_residue_ambiguous",
        )

    link = _latest_link(db, session_id=session_id, task_id=task.id)
    if link is None:
        link = SessionTask(
            session_id=session_id, task_id=task.id, status=TaskStatus.PENDING
        )
        db.add(link)
        db.flush()
    else:
        mark_task_attempt_pending(task=task, session_task_link=link)

    task.status = TaskStatus.PENDING
    execution = create_task_execution(
        db,
        session_id=session_id,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    identity = ContinuationIdentity(
        session_id=session_id,
        instance_id=instance_id,
        continuation_task_id=task.id,
        continuation_kind="automatic_recovery",
        task_execution_id=execution.id,
        retry_count=0,
    )
    _commit(db, commit)
    return TransitionResult(
        accepted=True,
        reason="admitted",
        session_id=session_id,
        task_id=task.id,
        task_execution_id=execution.id,
        continuation_identity=identity,
    )


def enter_recovering(
    db: DbSession,
    session: SessionModel,
    *,
    task_execution: TaskExecution | None = None,
    task_id: int | None = None,
    continuation_kind: str = "automatic_recovery",
    retry_count: int = 0,
    retry_eta: datetime | None = None,
    failure_reason: str | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> ContinuationIdentity:
    """Record a failed attempt with an autonomous continuation in flight.

    The attempt is marked FAILED, while Session becomes ``recovering`` and
    retains its generation.  No final logical failure is published.
    """

    session_id = _session_id(session)
    if task_execution is None:
        if task_id is None:
            raise LifecycleTransitionError("task_execution_missing")
        task_execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.task_id == task_id,
            )
            .order_by(TaskExecution.id.desc())
            .first()
        )
        if task_execution is None:
            raise LifecycleTransitionError("task_execution_missing")
    _validate_execution_belongs(task_execution, session_id=session_id)
    status = normalize_session_status(getattr(session, "status", None))
    if status in _STABLE_TERMINAL_STATUSES or status == "paused":
        raise LifecycleTransitionError("session_generation_not_recoverable")
    if task_execution.status in {TaskStatus.DONE, TaskStatus.CANCELLED}:
        raise LifecycleTransitionError("attempt_not_recoverable")

    changed_at = _now(changed_at)
    task, link = _task_for_execution(db, task_execution)
    mark_task_attempt_failed(
        task=task,
        session_task_link=link,
        task_execution=task_execution,
        error_message=failure_reason,
        completed_at=changed_at,
    )
    if failure_reason and not task_execution.failure_category:
        task_execution.failure_category = failure_reason
    _ensure_instance_id(session)
    _set_continuation(
        session,
        task_id=task_execution.task_id,
        kind=continuation_kind,
        retry_count=retry_count,
        retry_eta=retry_eta,
        changed_at=changed_at,
    )
    session.status = "recovering"
    session.is_active = True
    _commit(db, commit)
    return ContinuationIdentity(
        session_id=session_id,
        instance_id=session.instance_id,
        continuation_task_id=task_execution.task_id,
        continuation_kind=session.continuation_kind,
        task_execution_id=task_execution.id,
        retry_count=session.continuation_retry_count,
    )


def schedule_continuation(
    db: DbSession,
    session: SessionModel,
    *,
    task_id: int | None = None,
    continuation_task_id: int | None = None,
    task_execution: TaskExecution | None = None,
    continuation_kind: str | None = None,
    retry_count: int | None = None,
    retry_eta: datetime | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> ContinuationIdentity:
    """Durably schedule a pending continuation without touching a broker.

    Preconditions: Session is ``recovering`` or already ``retry_pending``;
    the task belongs to the Session and any supplied execution is PENDING.
    Rows mutated: a pending TaskExecution, its SessionTask/Task facts, and
    Session continuation metadata/status.  The current instance is required
    to remain unchanged.  The caller owns commit/rollback unless requested;
    invalid identity or attempt state raises ``LifecycleTransitionError``.
    """

    session_id = _session_id(session)
    status = normalize_session_status(getattr(session, "status", None))
    if status not in {"recovering", "retry_pending"}:
        raise LifecycleTransitionError("session_not_recovering")

    if (
        task_id is not None
        and continuation_task_id is not None
        and task_id != continuation_task_id
    ):
        raise LifecycleTransitionError("continuation_task_id_mismatch")
    requested_task_id = task_id if task_id is not None else continuation_task_id
    marker_task_id = getattr(session, "continuation_task_id", None)
    resolved_task_id = requested_task_id or marker_task_id
    if not isinstance(resolved_task_id, int) or resolved_task_id <= 0:
        raise LifecycleTransitionError("continuation_task_id_missing")
    if marker_task_id is not None and marker_task_id != resolved_task_id:
        raise LifecycleTransitionError("continuation_task_id_mismatch")

    kind = continuation_kind or getattr(session, "continuation_kind", None)
    if not kind:
        kind = "automatic_recovery"
    kind = _validate_kind(kind)
    current_count = getattr(session, "continuation_retry_count", 0) or 0
    count = current_count if retry_count is None else retry_count
    count = _validate_retry_count(count)
    changed_at = _now(changed_at)

    task = db.query(Task).filter(Task.id == resolved_task_id).first()
    if task is None:
        raise LifecycleTransitionError("continuation_task_missing")

    if task_execution is not None:
        _validate_execution_belongs(
            task_execution, session_id=session_id, task_id=resolved_task_id
        )
        if task_execution.status != TaskStatus.PENDING:
            raise LifecycleTransitionError("continuation_attempt_not_pending")
    else:
        task_execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.task_id == resolved_task_id,
                TaskExecution.status == TaskStatus.PENDING,
            )
            .order_by(TaskExecution.id.desc())
            .first()
        )
        if task_execution is None:
            task_execution = create_task_execution(
                db,
                session_id=session_id,
                task_id=resolved_task_id,
                status=TaskStatus.PENDING,
            )

    link = _latest_link(db, session_id=session_id, task_id=resolved_task_id)
    if link is None:
        link = SessionTask(
            session_id=session_id,
            task_id=resolved_task_id,
            status=TaskStatus.PENDING,
        )
        db.add(link)
        db.flush()
    mark_task_attempt_pending(
        task=task,
        session_task_link=link,
        task_execution=task_execution,
    )
    _ensure_instance_id(session)
    _set_continuation(
        session,
        task_id=resolved_task_id,
        kind=kind,
        retry_count=count,
        retry_eta=retry_eta,
        changed_at=changed_at,
    )
    session.status = "retry_pending"
    session.is_active = True
    _commit(db, commit)
    return ContinuationIdentity(
        session_id=session_id,
        instance_id=session.instance_id,
        continuation_task_id=resolved_task_id,
        continuation_kind=kind,
        task_execution_id=task_execution.id,
        retry_count=count,
    )


def claim_continuation(
    db: DbSession,
    identity: ContinuationIdentity,
    *,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> TransitionResult:
    """Atomically claim a pending continuation for its exact generation.

    Preconditions: the supplied session/instance/task/kind/retry-count and
    exact TaskExecution identity match durable ``retry_pending`` state, the
    attempt is PENDING, and no other session execution/link/task is active.
    The conditional Session UPDATE is the fence and admission boundary; the
    same transaction then changes Session to running and the attempt facts to
    RUNNING.  No commit or external publication is implicit.  A stale,
    duplicate, malformed, or contended claim returns a rejected result and
    mutates no lifecycle state.
    """

    if not isinstance(identity, ContinuationIdentity):
        return _result_rejected(session_id=None, reason="continuation_identity_invalid")
    try:
        kind = _validate_kind(identity.continuation_kind)
        count = _validate_retry_count(identity.retry_count)
    except LifecycleTransitionError as exc:
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason=exc.reason,
        )
    if not identity.instance_id or identity.task_execution_id is None:
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="continuation_identity_incomplete",
        )

    # Earlier E2 transitions may intentionally be uncommitted in the same
    # caller-owned transaction.  Flush them before the conditional fence so
    # the claim predicate observes the durable pending marker, while keeping
    # commit ownership with the caller.
    db.flush()

    session = (
        db.query(SessionModel).filter(SessionModel.id == identity.session_id).first()
    )
    if session is None:
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="session_missing",
        )
    execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.id == identity.task_execution_id,
            TaskExecution.session_id == identity.session_id,
            TaskExecution.task_id == identity.continuation_task_id,
        )
        .first()
    )
    if execution is None or execution.status != TaskStatus.PENDING:
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="continuation_attempt_not_pending",
        )
    active = _active_executions(db, identity.session_id)
    if any(row.id != execution.id for row in active):
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="autonomous_execution_already_active",
        )
    if _active_session_links(db, identity.session_id) or _active_linked_tasks(
        db, identity.session_id
    ):
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="autonomous_execution_already_active",
        )

    changed_at = _now(changed_at)
    updated = (
        db.query(SessionModel)
        .filter(
            SessionModel.id == identity.session_id,
            SessionModel.status == "retry_pending",
            SessionModel.instance_id == identity.instance_id,
            SessionModel.continuation_task_id == identity.continuation_task_id,
            SessionModel.continuation_kind == kind,
            SessionModel.continuation_retry_count == count,
        )
        .update(
            {
                SessionModel.status: "running",
                SessionModel.is_active: True,
                SessionModel.continuation_task_id: None,
                SessionModel.continuation_kind: None,
                SessionModel.continuation_retry_count: 0,
                SessionModel.continuation_retry_eta: None,
                SessionModel.lifecycle_updated_at: changed_at,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        return _result_rejected(
            session_id=identity.session_id,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            reason="stale_or_duplicate_continuation",
        )

    task, link = _task_for_execution(db, execution)
    mark_task_attempt_running(
        task=task,
        session_task_link=link,
        task_execution=execution,
        started_at=changed_at,
    )
    session.status = "running"
    session.is_active = True
    _clear_continuation(session, changed_at=changed_at)
    _commit(db, commit)
    return TransitionResult(
        accepted=True,
        reason="claimed",
        session_id=identity.session_id,
        task_id=identity.continuation_task_id,
        task_execution_id=identity.task_execution_id,
    )


def revoke_autonomous_continuation(
    db: DbSession,
    session: SessionModel,
    *,
    resulting_status: str = "paused",
    reason: str | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> str:
    """Revoke autonomous work and rotate the Session generation.

    Pause remains nonterminal and quiescent; stop/cancel become stable
    operator outcomes.  Existing active attempts are normalized before the
    marker is cleared.  The old ``mark_session_paused`` failure-path helper is
    intentionally not changed by this primitive.  Rows mutated are the
    current active attempt facts, Session status/continuation metadata,
    timestamps, and (when an autonomous generation was present) instance_id.
    The caller owns commit/rollback unless requested.
    """

    session_id = _session_id(session)
    previous_status = normalize_session_status(getattr(session, "status", None))
    had_marker = any(
        getattr(session, field, None) not in (None, 0, "")
        for field in (
            "continuation_task_id",
            "continuation_kind",
            "continuation_retry_count",
            "continuation_retry_eta",
        )
    )
    result = normalize_session_status(resulting_status)
    if result not in _REVOCATION_STATUSES:
        raise LifecycleTransitionError("revocation_status_invalid")
    changed_at = _now(changed_at)
    reset_active_attempts_for_session_stop(
        db,
        session_id=session_id,
        next_status=TaskStatus.PENDING,
        terminalize=result in {"stopped", "cancelled", "canceled"},
        stop_reason=reason,
    )
    _clear_continuation(session, changed_at=changed_at)
    session.status = result
    session.is_active = False
    if result == "paused":
        session.paused_at = changed_at
    else:
        session.stopped_at = changed_at
    if previous_status in _AUTONOMOUS_SESSION_STATUSES or had_marker:
        _rotate_instance_id(session)
    session.lifecycle_updated_at = changed_at
    _commit(db, commit)
    return session.instance_id


def finalize_logical_failure(
    db: DbSession,
    session: SessionModel,
    *,
    task_execution: TaskExecution | None = None,
    failure_reason: str | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> str:
    """Finalize a failed logical generation and fence its old deliveries.

    The optional current attempt is marked FAILED; obsolete active attempts
    are cancelled, continuation metadata is cleared, Session becomes failed,
    and the instance is rotated.  No broker/event publication is performed;
    commit ownership remains with the caller unless requested.
    """

    session_id = _session_id(session)
    changed_at = _now(changed_at)
    if task_execution is not None:
        _validate_execution_belongs(task_execution, session_id=session_id)
        task, link = _task_for_execution(db, task_execution)
        if task_execution.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}:
            mark_task_attempt_failed(
                task=task,
                session_task_link=link,
                task_execution=task_execution,
                error_message=failure_reason,
                completed_at=changed_at,
            )
        if failure_reason and not task_execution.failure_category:
            task_execution.failure_category = failure_reason
    _cancel_other_active_executions(
        db,
        session_id=session_id,
        keep_execution_id=task_execution.id if task_execution is not None else None,
        completed_at=changed_at,
        reason=failure_reason or "logical_failure_finalized",
    )
    _clear_continuation(session, changed_at=changed_at)
    session.status = "failed"
    session.is_active = False
    session.stopped_at = changed_at
    _rotate_instance_id(session)
    session.lifecycle_updated_at = changed_at
    _commit(db, commit)
    return session.instance_id


def finalize_logical_success(
    db: DbSession,
    session: SessionModel,
    *,
    task_execution: TaskExecution | None = None,
    commit: bool = False,
    changed_at: datetime | None = None,
) -> str:
    """Finalize successful logical work and fence its old deliveries.

    The optional current attempt is marked DONE; obsolete active attempts are
    cancelled, continuation metadata is cleared, Session becomes completed,
    and the instance is rotated.  No broker/event publication is performed;
    commit ownership remains with the caller unless requested.
    """

    session_id = _session_id(session)
    changed_at = _now(changed_at)
    if task_execution is not None:
        _validate_execution_belongs(task_execution, session_id=session_id)
        task, link = _task_for_execution(db, task_execution)
        mark_task_attempt_done(
            task=task,
            session_task_link=link,
            task_execution=task_execution,
            completed_at=changed_at,
        )
    _cancel_other_active_executions(
        db,
        session_id=session_id,
        keep_execution_id=task_execution.id if task_execution is not None else None,
        completed_at=changed_at,
        reason="logical_success_finalized",
    )
    _clear_continuation(session, changed_at=changed_at)
    session.status = "completed"
    session.is_active = False
    session.stopped_at = changed_at
    _rotate_instance_id(session)
    session.lifecycle_updated_at = changed_at
    _commit(db, commit)
    return session.instance_id


def begin_new_generation(
    db: DbSession,
    session: SessionModel,
    *,
    resulting_status: str = "pending",
    commit: bool = False,
    changed_at: datetime | None = None,
) -> str:
    """Create an explicit operator-owned generation without broker work.

    Existing active attempt residue is cancelled, continuation metadata is
    cleared, the instance is rotated, and Session is set to the requested
    ``pending`` or ``running`` status.  The caller owns commit/rollback unless
    requested.
    """

    result = normalize_session_status(resulting_status)
    if result not in {"pending", "running"}:
        raise LifecycleTransitionError("new_generation_status_invalid")
    changed_at = _now(changed_at)
    _cancel_other_active_executions(
        db,
        session_id=_session_id(session),
        keep_execution_id=None,
        completed_at=changed_at,
        reason="new_generation_started",
    )
    _clear_continuation(session, changed_at=changed_at)
    session.status = result
    session.is_active = result == "running"
    _rotate_instance_id(session)
    session.lifecycle_updated_at = changed_at
    _commit(db, commit)
    return session.instance_id


# A descriptive alias for callers that use the operator vocabulary.
start_new_generation = begin_new_generation


__all__ = [
    "MAX_ACTIVE_LOGICAL_EXECUTIONS_PER_SESSION",
    "VALID_CONTINUATION_KINDS",
    "LifecycleTransitionError",
    "ContinuationIdentity",
    "TransitionResult",
    "admit_autonomous_execution",
    "enter_recovering",
    "schedule_continuation",
    "claim_continuation",
    "revoke_autonomous_continuation",
    "finalize_logical_failure",
    "finalize_logical_success",
    "begin_new_generation",
    "start_new_generation",
]
