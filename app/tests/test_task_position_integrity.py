from __future__ import annotations

import gc
import weakref
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models import Task, TaskStatus

from app.services.planning.task_position_integrity import (
    RetryableTaskPositionLockError,
    TaskPositionConflictError,
    TaskPositionLockAcquisitionError,
    UnsupportedTaskPositionDialectError,
    _clear_task_position_reservation_marker,
    acquire_sqlite_write_reservation,
    check_no_new_position_conflicts,
    find_new_position_conflicts,
    find_position_conflicts,
    resolve_effective_positions,
    snapshot_task_positions,
    task_positions_conflict,
)

_RESERVATION_KEY = "_task_position_write_reservation_transaction"


def _task(task_id, *, project_id=1, plan_id=10, position=1):
    return SimpleNamespace(
        id=task_id,
        project_id=project_id,
        plan_id=plan_id,
        plan_position=position,
    )


def _file_sqlite_sessions(tmp_path, name="positions.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 0.01},
    )
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
    assert journal_mode.lower() == "wal"
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory


def _orm_file_sqlite_sessions(tmp_path, name="orm-positions.db"):
    engine, factory = _file_sqlite_sessions(tmp_path, name)
    Task.__table__.create(engine)
    return engine, factory


def _orm_task(*, project_id=1, plan_id=10, position=1, title="position test"):
    return Task(
        project_id=project_id,
        plan_id=plan_id,
        plan_position=position,
        title=title,
        status=TaskStatus.PENDING,
    )


def test_effective_position_fallback_preserves_existing_truthiness_and_filtering():
    candidates = [
        SimpleNamespace(plan_position=8),
        SimpleNamespace(plan_position=0),
        SimpleNamespace(),
        SimpleNamespace(plan_position=-4),
    ]
    assert resolve_effective_positions(candidates) == (8, 2, 3, -4)

    filtered = [candidates[1], candidates[2]]
    assert resolve_effective_positions(filtered) == (1, 2)


def test_duplicate_candidate_positions_are_reported():
    positions = resolve_effective_positions(
        [SimpleNamespace(plan_position=2), SimpleNamespace(plan_position=2)]
    )
    candidates = [
        _task("candidate-a", position=positions[0]),
        _task("candidate-b", position=positions[1]),
    ]

    conflicts = find_position_conflicts(candidates)
    assert len(conflicts) == 1
    with pytest.raises(TaskPositionConflictError) as exc_info:
        check_no_new_position_conflicts([], candidates)
    assert exc_info.value.code == "TASK_POSITION_CONFLICT"
    assert len(exc_info.value.conflicts) == 1


def test_same_plan_equal_non_null_positions_conflict():
    assert task_positions_conflict(_task(1), _task(2))
    repeated = _task(3)
    assert not task_positions_conflict(repeated, repeated)


@pytest.mark.parametrize("unassigned_first", [True, False])
def test_unassigned_and_planned_tasks_conflict_at_equal_position(unassigned_first):
    plans = (None, 10) if unassigned_first else (10, None)
    assert task_positions_conflict(
        _task(1, plan_id=plans[0], position=4),
        _task(2, plan_id=plans[1], position=4),
    )


def test_equal_positions_in_separate_plans_are_legal():
    assert not task_positions_conflict(
        _task(1, plan_id=10, position=4), _task(2, plan_id=11, position=4)
    )
    assert not task_positions_conflict(
        _task(1, project_id=1, position=4), _task(2, project_id=2, position=4)
    )


def test_leading_gaps_and_explicit_two_one_order_are_preserved():
    assert resolve_effective_positions(
        [SimpleNamespace(plan_position=3), SimpleNamespace(plan_position=7)]
    ) == (3, 7)
    assert resolve_effective_positions(
        [SimpleNamespace(plan_position=2), SimpleNamespace(plan_position=1)]
    ) == (2, 1)
    assert find_position_conflicts([_task(1, position=3), _task(2, position=7)]) == ()


def test_legacy_conflicts_are_excluded_from_the_before_after_delta():
    tasks = [
        _task(1, position=5),
        _task(2, position=5),
        _task(3, position=8),
        _task(4, position=9),
    ]
    before = snapshot_task_positions(tasks)
    tasks[3].plan_position = 8
    after = snapshot_task_positions(tasks)

    new_conflicts = find_new_position_conflicts(before, after)
    assert len(new_conflicts) == 1
    assert {
        new_conflicts[0].left.database_identity[2],
        new_conflicts[0].right.database_identity[2],
    } == {3, 4}

    with pytest.raises(TaskPositionConflictError) as exc_info:
        check_no_new_position_conflicts(before, after)
    assert {
        exc_info.value.conflicts[0].left.database_identity[2],
        exc_info.value.conflicts[0].right.database_identity[2],
    } == {
        3,
        4,
    }
    unchanged_legacy = snapshot_task_positions(
        [_task(1, position=5), _task(2, position=5)]
    )
    check_no_new_position_conflicts(before[:2], unchanged_legacy)


def test_pending_orm_identity_and_unchanged_conflict_survive_flush(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "pending-flush.db")
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=1)
        second = _orm_task(project_id=1, plan_id=10, position=1)
        session.add_all([first, second])
        assert first.id is None and second.id is None

        before = snapshot_task_positions([first, second])
        before_identity = before[0].task_identity
        session.flush()
        after = snapshot_task_positions([first, second])

        assert first.id is not None and second.id is not None
        assert before_identity == after[0].task_identity
        assert find_new_position_conflicts(before, after) == ()
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_new_orm_conflict_after_flush_is_rejected(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "new-conflict.db")
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=1)
        second = _orm_task(project_id=1, plan_id=10, position=2)
        session.add_all([first, second])
        session.flush()
        before = snapshot_task_positions([first, second])

        second.plan_position = 1
        session.flush()
        after = snapshot_task_positions([first, second])

        with pytest.raises(TaskPositionConflictError):
            check_no_new_position_conflicts(before, after)
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_deleted_task_database_id_reuse_is_a_new_conflict(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "rowid-reuse.db")
    Task.metadata.create_all(engine)
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=1)
        deleted = _orm_task(project_id=1, plan_id=10, position=1)
        session.add_all([first, deleted])
        session.flush()
        deleted_id = deleted.id
        before = snapshot_task_positions([first, deleted])

        session.delete(deleted)
        session.flush()
        replacement = _orm_task(project_id=1, plan_id=10, position=1)
        session.add(replacement)
        session.flush()

        assert replacement.id == deleted_id
        after = snapshot_task_positions([first, replacement])
        assert len(find_new_position_conflicts(before, after)) == 1
        with pytest.raises(TaskPositionConflictError):
            check_no_new_position_conflicts(before, after)
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_pending_snapshot_retains_source_to_prevent_object_id_reuse(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "object-id-reuse.db")
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=1)
        discarded = _orm_task(project_id=1, plan_id=10, position=1)
        session.add_all([first, discarded])
        before = snapshot_task_positions([first, discarded])
        discarded_reference = weakref.ref(discarded)
        discarded_identity = before[1].task_identity

        session.expunge(discarded)
        del discarded
        gc.collect()

        assert discarded_reference() is before[1].source_task
        assert "source_task" not in repr(before[1])
        assert before[1] == replace(before[1], source_task=None)
        replacement = _orm_task(project_id=1, plan_id=10, position=1)
        session.add(replacement)
        after = snapshot_task_positions([first, replacement])

        assert before[1].source_task is not replacement
        assert after[1].task_identity != discarded_identity
        assert len(find_new_position_conflicts(before, after)) == 1
        with pytest.raises(TaskPositionConflictError):
            check_no_new_position_conflicts(before, after)
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_distinct_pending_orm_tasks_have_distinct_identities(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "distinct-pending.db")
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=3)
        second = _orm_task(project_id=1, plan_id=10, position=3)
        session.add_all([first, second])
        before = snapshot_task_positions([first, second])

        assert first.id is None and second.id is None
        assert before[0].task_identity != before[1].task_identity
        assert task_positions_conflict(first, second)
        assert len(find_position_conflicts(before)) == 1
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_persisted_orm_identity_matches_after_reload(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "reload.db")
    try:
        with factory() as writer:
            first = _orm_task(project_id=1, plan_id=10, position=5)
            second = _orm_task(project_id=1, plan_id=10, position=5)
            writer.add_all([first, second])
            writer.commit()

        with factory() as before_session:
            before_objects = before_session.query(Task).order_by(Task.id).all()
            before = snapshot_task_positions(before_objects)
        with factory() as after_session:
            after_objects = after_session.query(Task).order_by(Task.id).all()
            after = snapshot_task_positions(after_objects)

        assert before_objects[0] is not after_objects[0]
        assert [task.id for task in before_objects] == [
            task.id for task in after_objects
        ]
        assert find_new_position_conflicts(before, after) == ()
    finally:
        engine.dispose()


def test_orm_legacy_pair_removal_and_expansion_delta(tmp_path):
    engine, factory = _orm_file_sqlite_sessions(tmp_path, "legacy-delta.db")
    session = factory()
    try:
        first = _orm_task(project_id=1, plan_id=10, position=1)
        second = _orm_task(project_id=1, plan_id=10, position=1)
        third = _orm_task(project_id=1, plan_id=10, position=4)
        session.add_all([first, second, third])
        session.flush()
        before = snapshot_task_positions([first, second, third])

        second.plan_position = 2
        session.flush()
        check_no_new_position_conflicts(
            before, snapshot_task_positions([first, second, third])
        )

        second.plan_position = 1
        session.flush()
        before_expansion = snapshot_task_positions([first, second, third])
        session.add(_orm_task(project_id=1, plan_id=10, position=1))
        session.flush()
        after_expansion = snapshot_task_positions(
            session.query(Task).order_by(Task.id).all()
        )
        new_conflicts = find_new_position_conflicts(before_expansion, after_expansion)

        assert len(new_conflicts) == 2
        with pytest.raises(TaskPositionConflictError):
            check_no_new_position_conflicts(before_expansion, after_expansion)
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.mark.parametrize("resolution", ["commit", "rollback"])
def test_sqlite_reservation_is_idempotent_and_released_by_session_end(
    tmp_path, resolution
):
    engine, factory = _file_sqlite_sessions(tmp_path, f"release-{resolution}.db")
    owner = factory()
    contender = factory()
    try:
        acquire_sqlite_write_reservation(owner)
        assert owner.info.get(_RESERVATION_KEY) is owner.get_transaction()
        acquire_sqlite_write_reservation(owner)
        assert owner.info.get(_RESERVATION_KEY) is owner.get_transaction()

        with pytest.raises(RetryableTaskPositionLockError) as exc_info:
            acquire_sqlite_write_reservation(contender)
        assert exc_info.value.code == "TASK_POSITION_WRITE_BUSY"
        assert exc_info.value.retryable is True
        assert _RESERVATION_KEY not in contender.info
        contender.rollback()

        getattr(owner, resolution)()
        assert _RESERVATION_KEY not in owner.info
        acquire_sqlite_write_reservation(contender)
        assert contender.info.get(_RESERVATION_KEY) is contender.get_transaction()
        contender.rollback()
        assert _RESERVATION_KEY not in contender.info
    finally:
        owner.close()
        contender.close()
        engine.dispose()


@pytest.mark.parametrize("resolution", ["commit", "rollback"])
def test_same_session_can_reserve_again_after_transaction_end(tmp_path, resolution):
    engine, factory = _file_sqlite_sessions(tmp_path, f"reacquire-{resolution}.db")
    session = factory()
    try:
        acquire_sqlite_write_reservation(session)
        first_transaction = session.get_transaction()
        getattr(session, resolution)()
        assert _RESERVATION_KEY not in session.info

        acquire_sqlite_write_reservation(session)
        second_transaction = session.get_transaction()
        assert second_transaction is not first_transaction
        assert session.info.get(_RESERVATION_KEY) is second_transaction
        assert session.connection().connection.driver_connection.in_transaction
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_cleanup_hook_is_session_scoped_and_installed_once(tmp_path):
    engine, factory = _file_sqlite_sessions(tmp_path, "session-hook.db")
    owner = factory()
    unrelated = factory()
    try:
        assert not event.contains(
            owner, "after_transaction_end", _clear_task_position_reservation_marker
        )
        acquire_sqlite_write_reservation(owner)
        assert event.contains(
            owner, "after_transaction_end", _clear_task_position_reservation_marker
        )
        assert not event.contains(
            unrelated,
            "after_transaction_end",
            _clear_task_position_reservation_marker,
        )
        listener_count = owner.dispatch.after_transaction_end.listeners.count(
            _clear_task_position_reservation_marker
        )
        acquire_sqlite_write_reservation(owner)
        assert (
            owner.dispatch.after_transaction_end.listeners.count(
                _clear_task_position_reservation_marker
            )
            == listener_count
            == 1
        )
        owner.rollback()
        assert _RESERVATION_KEY not in owner.info
    finally:
        owner.close()
        unrelated.close()
        engine.dispose()


def test_reservation_marker_clears_after_exception_and_rollback(tmp_path):
    engine, factory = _file_sqlite_sessions(tmp_path, "exception-rollback.db")
    session = factory()
    try:
        acquire_sqlite_write_reservation(session)
        assert _RESERVATION_KEY in session.info

        with pytest.raises(RuntimeError, match="operation failed"):
            try:
                raise RuntimeError("operation failed")
            except RuntimeError:
                session.rollback()
                raise

        assert _RESERVATION_KEY not in session.info
    finally:
        session.close()
        engine.dispose()


def test_non_lock_operational_error_does_not_leave_reservation_marker(tmp_path):
    engine, factory = _file_sqlite_sessions(tmp_path, "prior-dml.db")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE prior_writes (id INTEGER PRIMARY KEY)")
    session = factory()
    try:
        session.execute(text("INSERT INTO prior_writes DEFAULT VALUES"))
        with pytest.raises(TaskPositionLockAcquisitionError) as exc_info:
            acquire_sqlite_write_reservation(session)
        assert exc_info.value.retryable is False
        assert _RESERVATION_KEY not in session.info
        assert "cannot start a transaction" not in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_cross_connection_same_position_contention_serializes_validation(tmp_path):
    engine, factory = _file_sqlite_sessions(tmp_path, "contention.db")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE task_positions "
            "(id INTEGER PRIMARY KEY, project_id INTEGER, plan_id INTEGER, position INTEGER)"
        )

    first = factory()
    second = factory()
    try:
        acquire_sqlite_write_reservation(first)
        first_connection = first.connection().connection.driver_connection
        second_connection = second.connection().connection.driver_connection
        assert first_connection is not second_connection

        candidate = _task(2, position=6)
        assert find_position_conflicts([candidate]) == ()
        first.execute(
            text(
                "INSERT INTO task_positions (id, project_id, plan_id, position) "
                "VALUES (:id, :project_id, :plan_id, :position)"
            ),
            {"id": 1, "project_id": 1, "plan_id": 10, "position": 6},
        )

        with pytest.raises(RetryableTaskPositionLockError):
            acquire_sqlite_write_reservation(second)
        second.rollback()

        first.commit()
        acquire_sqlite_write_reservation(second)
        stored = second.execute(
            text("SELECT id, project_id, plan_id, position FROM task_positions")
        ).one()
        persisted = _task(
            stored.id,
            project_id=stored.project_id,
            plan_id=stored.plan_id,
            position=stored.position,
        )
        assert task_positions_conflict(persisted, candidate)
        second.rollback()
    finally:
        first.close()
        second.close()
        engine.dispose()


def test_unsupported_dialect_fails_closed_before_execution():
    class NonSqliteSession:
        info = {}

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def get_transaction(self):
            return None

        def execute(self, statement):  # pragma: no cover - must not be called
            raise AssertionError(f"unexpected execution: {statement}")

    with pytest.raises(UnsupportedTaskPositionDialectError) as exc_info:
        session = NonSqliteSession()
        acquire_sqlite_write_reservation(session)
    assert exc_info.value.code == "TASK_POSITION_UNSUPPORTED_DIALECT"
    assert _RESERVATION_KEY not in session.info
