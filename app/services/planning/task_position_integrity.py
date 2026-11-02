"""Shared checks for effective Task positions and SQLite write reservation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy import event, inspect as sqlalchemy_inspect, text
from sqlalchemy.exc import NoInspectionAvailable, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session


_RESERVATION_TRANSACTION_KEY = "_task_position_write_reservation_transaction"


@dataclass(frozen=True)
class TaskPositionSnapshot:
    """Immutable conflict-relevant fields captured before or after a mutation."""

    task_identity: tuple[str, Any, Any]
    database_identity: tuple[str, Any, Any] | None
    project_id: Any
    plan_id: Any
    plan_position: Any
    source_task: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PositionConflict:
    """A pair of Task-like records that share an effective position."""

    left: Any
    right: Any
    project_id: Any
    position: Any


class TaskPositionIntegrityError(RuntimeError):
    """Base error for failures enforcing Task position integrity."""

    code = "TASK_POSITION_INTEGRITY_ERROR"
    retryable = False


class TaskPositionConflictError(TaskPositionIntegrityError):
    """Raised when a mutation introduces one or more conflicting Task pairs."""

    code = "TASK_POSITION_CONFLICT"

    def __init__(self, conflicts: Sequence[PositionConflict]):
        self.conflicts = tuple(conflicts)
        super().__init__(f"{len(self.conflicts)} new Task position conflict(s)")


class UnsupportedTaskPositionDialectError(TaskPositionIntegrityError):
    """Raised when position integrity is requested outside SQLite."""

    code = "TASK_POSITION_UNSUPPORTED_DIALECT"


class TaskPositionLockAcquisitionError(TaskPositionIntegrityError):
    """Raised when SQLite cannot grant the required write reservation."""

    code = "TASK_POSITION_LOCK_UNAVAILABLE"


class RetryableTaskPositionLockError(TaskPositionLockAcquisitionError):
    """Recognized SQLite writer contention; the caller may retry the unit."""

    code = "TASK_POSITION_WRITE_BUSY"
    retryable = True


def resolve_effective_positions(candidates: Iterable[Any]) -> tuple[Any, ...]:
    """Resolve PlanCommit positions using its existing one-based fallback.

    The fallback is evaluated over the candidates passed to PlanCommit, so a
    filtered list falls back to its new one-based sequence. ``0`` and a missing
    or ``None`` value fall back; negative values remain explicit, matching the
    existing ``item.plan_position or index`` persistence behavior.
    """

    return tuple(
        getattr(candidate, "plan_position", None) or index
        for index, candidate in enumerate(candidates, start=1)
    )


def task_positions_conflict(left: Any, right: Any) -> bool:
    """Apply the shared same-project, same-position, overlapping-Plan rule."""

    if _same_task(left, right):
        return False

    left_project_id = getattr(left, "project_id", None)
    right_project_id = getattr(right, "project_id", None)
    left_position = getattr(left, "plan_position", None)
    right_position = getattr(right, "plan_position", None)

    if (
        left_project_id is None
        or left_project_id != right_project_id
        or left_position is None
        or left_position != right_position
    ):
        return False

    left_plan_id = getattr(left, "plan_id", None)
    right_plan_id = getattr(right, "plan_id", None)
    return (
        left_plan_id is None or right_plan_id is None or left_plan_id == right_plan_id
    )


def find_position_conflicts(tasks: Iterable[Any]) -> tuple[PositionConflict, ...]:
    """Return every conflicting pair in the supplied Task snapshot."""

    records = tuple(tasks)
    conflicts: list[PositionConflict] = []
    for left_index, left in enumerate(records):
        for right in records[left_index + 1 :]:
            if task_positions_conflict(left, right):
                conflicts.append(
                    PositionConflict(
                        left=left,
                        right=right,
                        project_id=getattr(left, "project_id", None),
                        position=getattr(left, "plan_position", None),
                    )
                )
    return tuple(conflicts)


def snapshot_task_positions(tasks: Iterable[Any]) -> tuple[TaskPositionSnapshot, ...]:
    """Copy conflict-relevant state so in-place ORM edits retain a true before."""

    return tuple(
        TaskPositionSnapshot(
            task_identity=_task_identity(task),
            database_identity=_task_database_identity(task),
            project_id=getattr(task, "project_id", None),
            plan_id=getattr(task, "plan_id", None),
            plan_position=getattr(task, "plan_position", None),
            source_task=task,
        )
        for task in tasks
    )


def find_new_position_conflicts(
    before: Iterable[Any], after: Iterable[Any]
) -> tuple[PositionConflict, ...]:
    """Return only conflict pairs absent from the pre-mutation snapshot.

    Capture ``before`` with :func:`snapshot_task_positions` before mutating ORM
    objects, then capture ``after`` in the same guarded Session transaction.
    Each snapshot retains its source object, bridging a pending Task across
    flush and preventing Python object-id reuse. Persisted Tasks can match
    across reloads by database ID unless the source object was deleted during
    the comparison window.
    """

    old_pairs: set[frozenset[tuple[str, Any, Any]]] = set()
    for conflict in find_position_conflicts(before):
        old_pairs.update(_conflict_pair_keys(conflict.left, conflict.right))
    return tuple(
        conflict
        for conflict in find_position_conflicts(after)
        if not (_conflict_pair_keys(conflict.left, conflict.right) & old_pairs)
    )


def check_no_new_position_conflicts(
    before: Iterable[Any], after: Iterable[Any]
) -> None:
    """Raise a typed conflict error only for newly conflicting Task pairs."""

    conflicts = find_new_position_conflicts(before, after)
    if conflicts:
        raise TaskPositionConflictError(conflicts)


def acquire_sqlite_write_reservation(session: Session) -> None:
    """Acquire and retain SQLite's writer reservation for this Session txn.

    Call before conflict-relevant reads or writes. ``BEGIN IMMEDIATE`` reserves
    SQLite's single writer slot across processes and connections. The Session
    remains transaction owner; its commit or rollback releases the reservation.
    """

    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "sqlite":
        raise UnsupportedTaskPositionDialectError(
            f"Task position integrity requires SQLite; got {dialect_name!r}"
        )

    current_transaction = session.get_transaction()
    if (
        current_transaction is not None
        and session.info.get(_RESERVATION_TRANSACTION_KEY) is current_transaction
    ):
        return

    try:
        session.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        if _is_sqlite_lock_contention(exc):
            raise RetryableTaskPositionLockError(
                "SQLite Task-position writer is busy; retry the transaction"
            ) from exc
        raise TaskPositionLockAcquisitionError(
            "Unable to acquire SQLite Task-position write reservation"
        ) from exc
    except SQLAlchemyError as exc:
        raise TaskPositionLockAcquisitionError(
            "Unable to acquire SQLite Task-position write reservation"
        ) from exc

    acquired_transaction = session.get_transaction()
    if acquired_transaction is None:
        raise TaskPositionLockAcquisitionError(
            "SQLite write reservation did not create a Session transaction"
        )
    if not event.contains(
        session,
        "after_transaction_end",
        _clear_task_position_reservation_marker,
    ):
        event.listen(
            session,
            "after_transaction_end",
            _clear_task_position_reservation_marker,
        )
    session.info[_RESERVATION_TRANSACTION_KEY] = acquired_transaction


def _task_identity(task: Any) -> tuple[str, Any, Any]:
    snapshot_identity = getattr(task, "task_identity", None)
    if snapshot_identity is not None:
        return snapshot_identity
    return ("object-id", int, id(task))


def _task_database_identity(task: Any) -> tuple[str, Any, Any] | None:
    snapshot_identity = getattr(task, "database_identity", None)
    if snapshot_identity is not None:
        return snapshot_identity
    for attribute in ("id", "task_id"):
        task_id = getattr(task, attribute, None)
        if task_id is not None:
            return ("database-id", type(task_id), task_id)
    return None


def _task_identity_tokens(task: Any) -> frozenset[tuple[str, Any, Any]]:
    tokens = {_task_identity(task)}
    database_identity = _task_database_identity(task)
    if database_identity is not None and not _source_task_was_deleted(task):
        tokens.add(database_identity)
    return frozenset(tokens)


def _source_task_was_deleted(task: Any) -> bool:
    source_task = getattr(task, "source_task", None)
    if source_task is None:
        source_task = task
    try:
        return bool(sqlalchemy_inspect(source_task).was_deleted)
    except NoInspectionAvailable:
        return False


def _same_task(left: Any, right: Any) -> bool:
    return bool(_task_identity_tokens(left) & _task_identity_tokens(right))


def _conflict_pair_keys(left: Any, right: Any) -> set[frozenset[tuple[str, Any, Any]]]:
    return {
        frozenset((left_identity, right_identity))
        for left_identity in _task_identity_tokens(left)
        for right_identity in _task_identity_tokens(right)
    }


def _clear_task_position_reservation_marker(session: Session, transaction: Any) -> None:
    """Clear the marker only when its owning Session transaction ends."""

    if session.info.get(_RESERVATION_TRANSACTION_KEY) is transaction:
        session.info.pop(_RESERVATION_TRANSACTION_KEY, None)


def _is_sqlite_lock_contention(exc: OperationalError) -> bool:
    original = exc.orig
    error_code = getattr(original, "sqlite_errorcode", None)
    if error_code is not None:
        base_code = error_code & 0xFF
        if base_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True

    message = str(original).lower()
    return any(
        fragment in message
        for fragment in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "database is busy",
        )
    )
