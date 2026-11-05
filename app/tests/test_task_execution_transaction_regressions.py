from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.services.workspace.control_state_paths import (
    project_control_state_root,
)
from app.services.workspace.system_settings import get_effective_runtime_root
from app.models import (
    LogEntry,
    Plan,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
    User,
)
from app.services.tasks.service import TASK_CHANGE_SET_LOG_MESSAGE
from app.services.orchestration.task_rules import run_virtual_merge_gate


def _file_backed_session_factory(path):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


def _create_running_attempt(db):
    project = Project(name="Terminal CAS Project")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="Terminal CAS Session",
        status="running",
        is_active=True,
    )
    task = Task(
        project_id=project.id,
        title="Terminal CAS Task",
        description="Exercise stale terminal completion.",
        status=TaskStatus.RUNNING,
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db.add_all([link, execution])
    db.commit()
    return task.id, link.id, execution.id


def test_stale_task_completion_does_not_overwrite_committed_cancellation(tmp_path):
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        mark_task_attempt_cancelled,
        mark_task_attempt_done,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-repro.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        setup_db.close()

        session_a = session_factory()
        stale_task = session_a.get(Task, task_id)
        stale_link = session_a.get(SessionTask, link_id)
        stale_execution = session_a.get(TaskExecution, execution_id)
        session_a.commit()  # retain loaded values while ending SQLite's read transaction

        session_b = session_factory()
        mark_task_attempt_cancelled(
            task=session_b.get(Task, task_id),
            session_task_link=session_b.get(SessionTask, link_id),
            task_execution=session_b.get(TaskExecution, execution_id),
            completed_at=datetime.now(timezone.utc),
        )
        session_b.commit()
        session_b.close()

        with pytest.raises(TaskCompletionRejected) as rejected:
            mark_task_attempt_done(
                task=stale_task,
                session_task_link=stale_link,
                task_execution=stale_execution,
                completed_at=datetime.now(timezone.utc),
            )
        assert rejected.value.reason == "task_completion_not_active"
        session_a.close()

        verify_db = session_factory()
        assert verify_db.get(Task, task_id).status == TaskStatus.CANCELLED
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.CANCELLED
        verify_db.close()
    finally:
        engine.dispose()


@pytest.mark.parametrize("stale_cas", ["reservation", "attempt"])
def test_task_completion_and_cancellation_competing_orders_are_serialized(
    tmp_path, stale_cas
):
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        TaskCompletionTransactionAborted,
        mark_task_attempt_cancelled,
        mark_task_attempt_done,
        reserve_task_completion,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-concurrency.sqlite3"
    )
    errors = []
    outcomes = {}
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        setup_db.close()

        loaded = threading.Barrier(2)
        cancellation_committed = threading.Barrier(2)

        def complete_stale_snapshot():
            db = session_factory()
            try:
                # A WAL reader can retain its old snapshot while session B
                # commits cancellation; SQLite then rejects the stale write.
                db.connection().exec_driver_sql("BEGIN")
                task = db.get(Task, task_id)
                link = db.get(SessionTask, link_id)
                execution = db.get(TaskExecution, execution_id)
                stale_session = db.query(SessionModel).one()
                stale_session.name = "discarded by required full rollback"
                loaded.wait(timeout=10)
                cancellation_committed.wait(timeout=10)
                try:
                    if stale_cas == "reservation":
                        reserve_task_completion(db, task_id)
                    mark_task_attempt_done(
                        task=task,
                        session_task_link=link,
                        task_execution=execution,
                    )
                    db.commit()
                    outcomes["stale_completion"] = "completed"
                except TaskCompletionRejected as exc:
                    outcomes["stale_completion"] = exc.reason
                    outcomes["stale_transaction_aborted"] = isinstance(
                        exc, TaskCompletionTransactionAborted
                    )
                    outcomes["stale_database_reason"] = getattr(
                        exc, "database_reason", None
                    )
                    outcomes["stale_session_in_transaction"] = db.in_transaction()
                    db.rollback()
                    outcomes["stale_session_after_explicit_rollback"] = (
                        db.in_transaction()
                    )
            except BaseException as exc:  # capture thread failures for pytest
                errors.append(exc)
            finally:
                db.close()

        def cancel_after_stale_read():
            db = session_factory()
            try:
                loaded.wait(timeout=10)
                mark_task_attempt_cancelled(
                    task=db.get(Task, task_id),
                    session_task_link=db.get(SessionTask, link_id),
                    task_execution=db.get(TaskExecution, execution_id),
                )
                db.commit()
                outcomes["cancellation"] = "committed"
            except BaseException as exc:
                errors.append(exc)
            finally:
                cancellation_committed.wait(timeout=10)
                db.close()

        completion_thread = threading.Thread(target=complete_stale_snapshot)
        cancellation_thread = threading.Thread(target=cancel_after_stale_read)
        completion_thread.start()
        cancellation_thread.start()
        completion_thread.join(timeout=15)
        cancellation_thread.join(timeout=15)
        assert not completion_thread.is_alive()
        assert not cancellation_thread.is_alive()
        assert not errors
        assert outcomes["cancellation"] == "committed"
        assert outcomes["stale_completion"] == "task_completion_transaction_aborted"
        assert outcomes["stale_transaction_aborted"] is True
        assert outcomes["stale_database_reason"] == "sqlite_stale_snapshot"
        assert outcomes["stale_session_in_transaction"] is True
        assert outcomes["stale_session_after_explicit_rollback"] is False

        verify_db = session_factory()
        assert verify_db.get(Task, task_id).status == TaskStatus.CANCELLED
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.CANCELLED
        assert verify_db.query(SessionModel).one().name == "Terminal CAS Session"
        verify_db.close()

        # In the other serial order completion commits first; a later operator
        # cancellation is still durable and remains the final terminal state.
        setup_db = session_factory()
        second_task_id, second_link_id, second_execution_id = _create_running_attempt(
            setup_db
        )
        setup_db.close()
        completion_committed = threading.Barrier(2)
        second_errors = []

        def complete_first():
            db = session_factory()
            try:
                task = db.get(Task, second_task_id)
                link = db.get(SessionTask, second_link_id)
                execution = db.get(TaskExecution, second_execution_id)
                mark_task_attempt_done(
                    task=task,
                    session_task_link=link,
                    task_execution=execution,
                )
                db.commit()
                completion_committed.wait(timeout=10)
            except BaseException as exc:
                second_errors.append(exc)
                completion_committed.abort()
            finally:
                db.close()

        def cancel_after_completion():
            db = session_factory()
            try:
                completion_committed.wait(timeout=10)
                mark_task_attempt_cancelled(
                    task=db.get(Task, second_task_id),
                    session_task_link=db.get(SessionTask, second_link_id),
                    task_execution=db.get(TaskExecution, second_execution_id),
                )
                db.commit()
            except BaseException as exc:
                second_errors.append(exc)
            finally:
                db.close()

        completion_thread = threading.Thread(target=complete_first)
        cancellation_thread = threading.Thread(target=cancel_after_completion)
        completion_thread.start()
        cancellation_thread.start()
        completion_thread.join(timeout=15)
        cancellation_thread.join(timeout=15)
        assert not completion_thread.is_alive()
        assert not cancellation_thread.is_alive()
        assert not second_errors
        verify_db = session_factory()
        assert verify_db.get(Task, second_task_id).status == TaskStatus.CANCELLED
        assert verify_db.get(SessionTask, second_link_id).status == TaskStatus.CANCELLED
        assert (
            verify_db.get(TaskExecution, second_execution_id).status
            == TaskStatus.CANCELLED
        )
        verify_db.close()
    finally:
        engine.dispose()


def test_related_terminal_cas_failure_rolls_back_task_completion(tmp_path):
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        mark_task_attempt_done,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-rollback.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        link = setup_db.get(SessionTask, link_id)
        link.status = TaskStatus.CANCELLED
        link.completed_at = datetime.now(timezone.utc)
        setup_db.commit()
        setup_db.close()

        db = session_factory()
        caller_change = db.query(SessionModel).one()
        caller_change.name = "caller-owned change survives"
        with pytest.raises(TaskCompletionRejected) as rejected:
            mark_task_attempt_done(
                task=db.get(Task, task_id),
                session_task_link=db.get(SessionTask, link_id),
                task_execution=db.get(TaskExecution, execution_id),
            )
        assert rejected.value.reason == "task_completion_not_active"
        assert db.in_transaction()
        assert caller_change.name == "caller-owned change survives"
        assert caller_change in db.dirty
        db.commit()

        caller_change.name = "caller explicitly rolls this back"
        with pytest.raises(TaskCompletionRejected):
            mark_task_attempt_done(
                task=db.get(Task, task_id),
                session_task_link=db.get(SessionTask, link_id),
                task_execution=db.get(TaskExecution, execution_id),
            )
        assert db.in_transaction()
        db.rollback()

        verify_db = session_factory()
        assert verify_db.get(SessionModel, caller_change.id).name == (
            "caller-owned change survives"
        )
        assert verify_db.get(Task, task_id).status == TaskStatus.RUNNING
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
        verify_db.close()
        db.close()
    finally:
        engine.dispose()


def test_ordinary_terminal_cas_rejection_preserves_caller_owned_changes(tmp_path):
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        mark_task_attempt_done,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-caller-transaction.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        link = setup_db.get(SessionTask, link_id)
        link.status = TaskStatus.CANCELLED
        setup_db.commit()
        setup_db.close()

        caller_db = session_factory()
        caller_change = caller_db.query(SessionModel).one()
        caller_change.name = "caller-owned change survives"

        with pytest.raises(TaskCompletionRejected) as rejected:
            mark_task_attempt_done(
                task=caller_db.get(Task, task_id),
                session_task_link=caller_db.get(SessionTask, link_id),
                task_execution=caller_db.get(TaskExecution, execution_id),
            )

        assert rejected.value.reason == "task_completion_not_active"
        assert caller_db.in_transaction()
        assert caller_change.name == "caller-owned change survives"
        assert caller_change in caller_db.dirty
        caller_db.commit()

        verify_db = session_factory()
        assert verify_db.get(SessionModel, caller_change.id).name == (
            "caller-owned change survives"
        )
        assert verify_db.get(Task, task_id).status == TaskStatus.RUNNING
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
        verify_db.close()
        caller_db.close()
    finally:
        engine.dispose()


def test_task_completion_reservation_rejection_preserves_caller_changes(tmp_path):
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        reserve_task_completion,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-reservation-caller-transaction.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        task = setup_db.get(Task, task_id)
        task.status = TaskStatus.CANCELLED
        setup_db.commit()
        setup_db.close()

        caller_db = session_factory()
        caller_change = caller_db.query(SessionModel).one()
        caller_change.name = "reservation caller change survives"
        with pytest.raises(TaskCompletionRejected) as rejected:
            reserve_task_completion(caller_db, task_id)

        assert rejected.value.reason == "task_completion_not_active"
        assert caller_db.in_transaction()
        assert caller_change.name == "reservation caller change survives"
        assert caller_change in caller_db.dirty
        caller_db.commit()

        verify_db = session_factory()
        assert verify_db.get(SessionModel, caller_change.id).name == (
            "reservation caller change survives"
        )
        assert verify_db.get(Task, task_id).status == TaskStatus.CANCELLED
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.RUNNING
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
        verify_db.close()
        caller_db.close()
    finally:
        engine.dispose()


def test_successful_task_completion_can_be_rolled_back_by_caller(tmp_path):
    from app.services.orchestration.run_state.transitions import mark_task_attempt_done

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-caller-rollback.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        setup_db.close()

        db = session_factory()
        task = db.get(Task, task_id)
        link = db.get(SessionTask, link_id)
        execution = db.get(TaskExecution, execution_id)
        caller_change = db.query(SessionModel).one()
        caller_change.name = "unflushed until caller decides"
        connection = db.connection()
        raw_connection = connection.connection.driver_connection
        assert not raw_connection.in_transaction

        mark_task_attempt_done(
            task=task,
            session_task_link=link,
            task_execution=execution,
        )

        assert raw_connection.in_transaction
        assert caller_change in db.dirty
        assert task.status == TaskStatus.DONE
        assert link.status == TaskStatus.DONE
        assert execution.status == TaskStatus.DONE

        observer = session_factory()
        assert observer.get(Task, task_id).status == TaskStatus.RUNNING
        assert observer.get(SessionTask, link_id).status == TaskStatus.RUNNING
        assert observer.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
        observer.close()

        db.rollback()
        assert not raw_connection.in_transaction
        assert task.status == TaskStatus.RUNNING
        assert link.status == TaskStatus.RUNNING
        assert execution.status == TaskStatus.RUNNING
        assert caller_change.name == "Terminal CAS Session"

        verify_db = session_factory()
        assert verify_db.get(Task, task_id).status == TaskStatus.RUNNING
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.RUNNING
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
        assert verify_db.get(SessionModel, caller_change.id).name == (
            "Terminal CAS Session"
        )
        verify_db.close()
        db.close()
    finally:
        engine.dispose()


def test_valid_running_task_completion_updates_all_terminal_rows(tmp_path):
    from app.services.orchestration.run_state.transitions import (
        mark_task_attempt_done,
        reserve_task_completion,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-valid.sqlite3"
    )
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        setup_db.close()

        db = session_factory()
        completed_at = datetime.now(timezone.utc)
        task = db.get(Task, task_id)
        link = db.get(SessionTask, link_id)
        execution = db.get(TaskExecution, execution_id)
        reserve_task_completion(db, task_id)
        assert task.status == TaskStatus.RUNNING
        mark_task_attempt_done(
            task=task,
            session_task_link=link,
            task_execution=execution,
            completed_at=completed_at,
        )
        verify_before_commit = session_factory()
        assert verify_before_commit.get(Task, task_id).status == TaskStatus.RUNNING
        verify_before_commit.close()
        db.commit()

        verify_db = session_factory()
        completed_task = verify_db.get(Task, task_id)
        completed_link = verify_db.get(SessionTask, link_id)
        completed_execution = verify_db.get(TaskExecution, execution_id)
        assert completed_task.status == TaskStatus.DONE
        assert completed_task.completed_at.replace(tzinfo=timezone.utc) == completed_at
        assert completed_link.status == TaskStatus.DONE
        assert completed_link.completed_at.replace(tzinfo=timezone.utc) == completed_at
        assert completed_execution.status == TaskStatus.DONE
        assert (
            completed_execution.completed_at.replace(tzinfo=timezone.utc)
            == completed_at
        )
        verify_db.close()
        db.close()
    finally:
        engine.dispose()


def test_rejected_completion_publishes_no_success_event_or_follow_on(
    tmp_path, monkeypatch
):
    from app.services.orchestration import lifecycle
    from app.services.orchestration.events.event_types import EventType
    from app.services.orchestration.lifecycle.completion import TaskCompletionFinalizer
    from app.services.orchestration.run_state.transitions import (
        TaskCompletionRejected,
        mark_task_attempt_cancelled,
        reserve_task_completion,
    )

    session_factory, engine = _file_backed_session_factory(
        tmp_path / "terminal-cas-publication.sqlite3"
    )
    event_calls = []
    dispatch_calls = []
    try:
        setup_db = session_factory()
        task_id, link_id, execution_id = _create_running_attempt(setup_db)
        setup_db.close()

        stale_db = session_factory()
        stale_task = stale_db.get(Task, task_id)
        stale_link = stale_db.get(SessionTask, link_id)
        stale_execution = stale_db.get(TaskExecution, execution_id)
        stale_db.commit()

        cancel_db = session_factory()
        mark_task_attempt_cancelled(
            task=cancel_db.get(Task, task_id),
            session_task_link=cancel_db.get(SessionTask, link_id),
            task_execution=cancel_db.get(TaskExecution, execution_id),
        )
        cancel_db.commit()
        cancel_db.close()

        publication_calls = []
        with pytest.raises(TaskCompletionRejected) as guard_rejected:
            reserve_task_completion(stale_db, task_id)
            publication_calls.append("promote")
        assert guard_rejected.value.reason == "task_completion_not_active"
        assert publication_calls == []

        monkeypatch.setattr(
            lifecycle.completion,
            "record_outcome_checkpoint",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            lifecycle.completion,
            "append_orchestration_event",
            lambda **kwargs: event_calls.append(kwargs.get("event_type")),
        )
        ctx = SimpleNamespace(
            db=stale_db,
            session=None,
            project=None,
            task=stale_task,
            session_task_link=stale_link,
            session_id=1,
            task_id=task_id,
            task_execution_id=execution_id,
            orchestration_state=SimpleNamespace(plan=[]),
            logger=None,
            execution_profile="standard",
            prompt="completion",
            timeout_seconds=30,
            runs_in_canonical_baseline=False,
            runtime_workspace_used=False,
        )
        finalizer = TaskCompletionFinalizer(db=stale_db, task_service=object())

        with pytest.raises(TaskCompletionRejected):
            finalizer.finalize_success(
                ctx=ctx,
                summary="completed",
                baseline_publish_result=None,
                completion_validation=object(),
                write_project_state_snapshot_fn=lambda *_args: None,
                write_progress_notes_fn=lambda **_kwargs: None,
                get_next_pending_project_task_fn=lambda *_args: object(),
                get_latest_session_task_link_fn=lambda *_args: None,
                execute_orchestration_task_delay_fn=lambda **kwargs: dispatch_calls.append(
                    kwargs
                ),
            )

        assert EventType.TASK_COMPLETED not in event_calls
        assert dispatch_calls == []
        verify_db = session_factory()
        assert verify_db.get(Task, task_id).status == TaskStatus.CANCELLED
        assert verify_db.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert verify_db.get(TaskExecution, execution_id).status == TaskStatus.CANCELLED
        verify_db.close()
        stale_db.close()
    finally:
        engine.dispose()


class _FakeAsyncResult:
    id = "celery-123"


def _stub_retry_dispatch(
    monkeypatch,
    captured_kwargs: dict | None = None,
    *,
    bypass_binding_admission: bool = True,
):
    del bypass_binding_admission
    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/retry-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-task-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    def _fake_apply_async(*args, kwargs=None, task_id=None, **_options):
        if kwargs is None:
            kwargs = args[1] if len(args) > 1 else {}
        if captured_kwargs is not None:
            captured_kwargs.clear()
            captured_kwargs.update(kwargs)
            captured_kwargs["_celery_task_id"] = task_id
        return _FakeAsyncResult()

    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "apply_async",
        _fake_apply_async,
    )


def test_sync_task_execution_uses_terminal_task_state_over_stale_running_link(
    db_session,
):
    from app.tasks.worker_support.execution_state import (
        _sync_task_execution_from_task_state,
    )

    project = Project(name="Terminal Sync Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Terminal Sync Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="Terminal sync task",
        description="fail after debug parse",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.flush()
    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db_session.add(session_task)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()

    _sync_task_execution_from_task_state(
        db_session,
        execution.id,
        task=task,
        session_task_link=session_task,
    )

    db_session.refresh(execution)
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at is not None


def test_task_retry_keeps_durable_attempt_recoverable_when_publish_fails(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dispatch Failure Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry me",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)
    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/rollback-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-me-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    def _raise_publish(*, kwargs, task_id, **_options):
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "apply_async",
        _raise_publish,
    )

    response = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")
    assert response.status_code == 200

    session = db_session.query(SessionModel).one()
    assert session.status == "running"
    assert session.is_active is True
    assert session.stopped_at is None
    assert db_session.query(SessionTask).count() == 1
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.status == TaskStatus.PENDING
    assert task_execution.dispatch_state == "READY"
    assert task_execution.dispatch_publish_attempts == 1
    assert task_execution.dispatch_last_error == "publisher_error:RuntimeError"
    assert task_execution.dispatch_next_attempt_at is not None
    db_session.refresh(task)
    assert task.status == TaskStatus.PENDING
    assert task.error_message is None


def test_task_retry_commits_records_before_worker_dispatch(
    authenticated_client, db_session, db_session_factory, monkeypatch
):
    project = Project(name="Dispatch Visibility Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry after commit",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)
    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/dispatch-visibility-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-after-commit",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    seen: dict[str, bool] = {}

    def _fake_delay(**kwargs):
        with db_session_factory() as fresh_db:
            seen["session_visible"] = (
                fresh_db.query(SessionModel)
                .filter(SessionModel.id == kwargs["session_id"])
                .first()
                is not None
            )
            seen["task_visible"] = (
                fresh_db.query(Task).filter(Task.id == kwargs["task_id"]).first()
                is not None
            )
            seen["task_execution_visible"] = (
                fresh_db.query(TaskExecution)
                .filter(TaskExecution.id == kwargs["task_execution_id"])
                .first()
                is not None
            )
        return _FakeAsyncResult()

    from app.tasks import worker as worker_module

    def _fake_apply_async(*args, kwargs=None, task_id=None, **_options):
        if kwargs is None:
            kwargs = args[1] if len(args) > 1 else {}
        _fake_delay(**kwargs)
        return _FakeAsyncResult()

    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "apply_async",
        _fake_apply_async,
    )

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert seen == {
        "session_visible": True,
        "task_visible": True,
        "task_execution_visible": True,
    }


def test_task_retry_dual_writes_pending_task_execution(
    authenticated_client, db_session, monkeypatch, isolated_workspace_root
):
    project = Project(name="Dual Write Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry with execution",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    task_execution = db_session.query(TaskExecution).one()
    assert payload["task_execution_id"] == task_execution.id
    assert captured_kwargs["task_execution_id"] == task_execution.id
    assert task_execution.session_id == payload["session_id"]
    assert task_execution.task_id == task.id
    assert task_execution.attempt_number == 1
    assert task_execution.status == TaskStatus.PENDING
    # Relocated: the retry TASK_QUEUED event is durable control state and is
    # written under the runtime root, keyed by Project.id.
    assert (
        project_control_state_root(get_effective_runtime_root(db_session), project.id)
        / "events"
        / f"session_{payload['session_id']}_task_{task.id}.jsonl"
    ).exists()
    assert not (
        isolated_workspace_root / "dual-write-project" / "retry-task-1" / ".agent"
    ).exists()


def test_task_retry_default_reuses_latest_project_session_without_duplicates(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Workflow Retry Project")
    db_session.add(project)
    db_session.commit()

    older_session = SessionModel(
        project_id=project.id,
        name="Older workflow",
        status="stopped",
        is_active=False,
    )
    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
        instance_id="workflow-instance",
    )
    old_isolated_session = SessionModel(
        project_id=project.id,
        name="Retry without duplicates session",
        status="stopped",
        is_active=False,
        instance_id="orchestrator-task-999-123",
    )
    task = Task(
        project_id=project.id,
        title="Retry without duplicates",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([older_session, workflow_session, old_isolated_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    first = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")
    second = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert first.status_code == 200
    assert second.status_code == 409
    assert "project_execution_serialization_conflict" in second.json()["detail"]
    assert first.json()["session_id"] == workflow_session.id
    assert db_session.query(SessionModel).count() == 3
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert captured_kwargs["session_id"] == workflow_session.id
    assert captured_kwargs["task_execution_id"] == first.json()["task_execution_id"]


def test_task_retry_uses_requested_session_when_valid(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Requested Session Project")
    db_session.add(project)
    db_session.commit()

    requested_session = SessionModel(
        project_id=project.id,
        name="Requested workflow",
        status="stopped",
        is_active=False,
    )
    other_session = SessionModel(
        project_id=project.id,
        name="Other workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry requested session",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([requested_session, other_session, task])
    db_session.commit()
    db_session.refresh(requested_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"session_id": requested_session.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == requested_session.id
    assert captured_kwargs["session_id"] == requested_session.id
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.session_id == requested_session.id


def test_task_retry_explicit_new_session_preserves_legacy_isolated_session_creation(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Explicit New Session Project")
    db_session.add(project)
    db_session.commit()

    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry isolated",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([workflow_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] != workflow_session.id
    assert db_session.query(SessionModel).count() == 2
    new_session = (
        db_session.query(SessionModel)
        .filter(SessionModel.id == payload["session_id"])
        .one()
    )
    assert new_session.name == "Retry isolated session"


def test_task_retry_does_not_require_persistent_openclaw_binding(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    project_workspace = tmp_path / "unbound-project"
    project_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps({"agents": {"list": []}}), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    project = Project(
        name="Unbound OpenClaw Project",
        workspace_path=str(project_workspace),
    )
    task = Task(
        project=project,
        title="Reject before runtime",
        description="must fail closed",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([project, task])
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch, bypass_binding_admission=False)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert db_session.query(TaskExecution).count() == 1
    assert db_session.query(SessionModel).count() == 1


def test_task_retry_reaches_dispatch_with_valid_openclaw_binding(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    project_workspace = tmp_path / "bound-project"
    project_workspace.mkdir()
    runtime_root = tmp_path.parent / f"{tmp_path.name}-orchestrator-runtime"
    runtime_root.mkdir()
    runner_workspace = runtime_root / "openclaw" / "runner"
    runner_workspace.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [{"id": "bound-agent", "workspace": str(runner_workspace)}]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "bound-agent")
    monkeypatch.setattr(
        "app.services.workspace.workspace_admission.get_effective_runtime_root",
        lambda _db: runtime_root,
    )

    project = Project(
        name="Bound OpenClaw Project",
        workspace_path=str(project_workspace),
    )
    task = Task(
        project=project,
        title="Reach dispatch",
        description="provider-free dispatch reachability",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([project, task])
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch, bypass_binding_admission=False)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert db_session.query(TaskExecution).count() == 1


def test_task_retry_new_session_isolates_historical_ordered_tasks(
    authenticated_client, db_session, monkeypatch
):
    """Explicit retry must not inherit unrelated unplanned project history."""
    project = Project(name="Historical Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Belongs to an earlier workflow record.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        title="Selected isolated task",
        description="Run in an explicitly isolated execution session.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add_all([historical_pending, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    _stub_retry_dispatch(monkeypatch)

    isolated_response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert isolated_response.status_code == 200
    payload = isolated_response.json()
    assert payload["execution_scope"] == "isolated_session"
    assert payload["isolated_session"] is True
    assert payload["task_id"] == selected_task.id


def test_task_retry_explicit_target_ignores_failed_unplanned_predecessor(
    authenticated_client, db_session, monkeypatch
):
    """An explicit retry does not turn legacy FIFO position into a dependency."""
    project = Project(name="Explicit Retry Order Only Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    failed_predecessor = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Its failure is retained history, not a prerequisite.",
        status=TaskStatus.FAILED,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        title="Explicitly selected task",
        description="Retry this exact task.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    db_session.add_all([failed_predecessor, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(f"/api/v1/tasks/{selected_task.id}/retry")

    assert response.status_code == 200
    assert response.json()["task_id"] == selected_task.id
    assert captured_kwargs["task_id"] == selected_task.id
    db_session.refresh(failed_predecessor)
    db_session.refresh(selected_task)
    assert failed_predecessor.status == TaskStatus.FAILED
    assert selected_task.status == TaskStatus.PENDING
    assert (
        db_session.query(TaskExecution)
        .filter(TaskExecution.task_id == failed_predecessor.id)
        .count()
        == 0
    )
    assert (
        db_session.query(TaskExecution)
        .filter(TaskExecution.task_id == selected_task.id)
        .count()
        == 1
    )


def test_task_retry_preserves_failed_predecessor_in_explicit_plan(
    authenticated_client, db_session
):
    """An explicit Plan predecessor remains a fail-closed retry blocker."""
    project = Project(name="Explicit Retry Dependency Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Dependency plan",
        source_brain="local",
        requirement="Preserve explicit predecessor completion.",
        markdown="1. prerequisite\n2. dependent task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    failed_predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Failed prerequisite",
        description="Must complete first.",
        status=TaskStatus.FAILED,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Dependent task",
        description="Retry only after the prerequisite completes.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    db_session.add_all([failed_predecessor, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    response = authenticated_client.post(f"/api/v1/tasks/{selected_task.id}/retry")

    assert response.status_code == 409
    assert "Failed prerequisite" in response.json()["detail"]
    assert db_session.query(TaskExecution).count() == 0
    db_session.refresh(failed_predecessor)
    assert failed_predecessor.status == TaskStatus.FAILED


def test_task_retry_new_session_keeps_plan_scoped_ordering(
    authenticated_client, db_session
):
    """Isolation must not bypass predecessors in the same explicit Plan."""
    project = Project(name="Plan Scoped Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Bounded plan",
        source_brain="local",
        requirement="Preserve ordered execution.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan selected task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add_all([predecessor, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_dogfood_admission_persists_queue_isolation_authority(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dogfood Admission Persistence Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    monkeypatch.setattr(
        "app.api.v1.endpoints.sessions.admit_dogfood_workspace",
        lambda *args, **kwargs: None,
    )

    response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": project.id,
            "name": "admitted bounded session",
            "execution_mode": "manual",
            "dogfood_admission": True,
        },
    )

    assert response.status_code == 201
    created = db_session.query(SessionModel).filter_by(id=response.json()["id"]).one()
    assert created.dogfood_admitted is True


def test_admitted_dogfood_session_isolates_legacy_project_queue(
    authenticated_client, db_session, monkeypatch
):
    """Dogfood admission isolates only the historical unplanned queue."""
    project = Project(name="Admitted Historical Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_planning = PlanningSession(
        project_id=project.id,
        title="Historical planning session",
        prompt="Preserve the original planning history.",
        status="completed",
        source_brain="local",
    )
    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Belongs to an earlier workflow record.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Failed in an earlier workflow record.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    historical_cancelled = Task(
        project_id=project.id,
        title="Historical cancelled task",
        description="Was cancelled in an earlier workflow record.",
        status=TaskStatus.CANCELLED,
        plan_position=3,
    )
    selected_task = Task(
        project_id=project.id,
        title="Selected dogfood task",
        description="Run in the admitted bounded execution session.",
        status=TaskStatus.PENDING,
        plan_position=4,
    )
    standard_session = SessionModel(
        project_id=project.id,
        name="Standard session",
        status="pending",
        execution_mode="manual",
        instance_id="standard-session-instance",
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted dogfood session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-dogfood-session-instance",
    )
    # This attribute is the persisted admission marker added by the fix.
    admitted_session.dogfood_admitted = True
    db_session.add_all(
        [
            historical_planning,
            historical_pending,
            historical_failed,
            historical_cancelled,
            selected_task,
            standard_session,
            admitted_session,
        ]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(standard_session)
    db_session.refresh(admitted_session)
    db_session.refresh(historical_planning)

    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.services.session.session_runtime_service.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/admitted-dogfood-project",
            "task_subfolder": None,
            "stored_task_subfolder": "selected-dogfood-task",
            "workspace_scope": "isolated_task_workspace",
        },
    )
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.append_orchestration_event",
        lambda **kwargs: {"event_id": "queued-event-1"},
    )
    monkeypatch.setattr(
        "app.services.session.session_runtime_service._maybe_compact_checkpoint_before_dispatch",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "delay",
        lambda **kwargs: _FakeAsyncResult(),
    )

    standard_response = authenticated_client.post(
        f"/api/v1/sessions/{standard_session.id}/tasks/{selected_task.id}/run"
    )
    assert standard_response.status_code == 409

    admitted_response = authenticated_client.post(
        f"/api/v1/sessions/{admitted_session.id}/tasks/{selected_task.id}/run"
    )

    assert admitted_response.status_code == 200
    assert admitted_response.json()["queued_task"]["task_id"] == selected_task.id
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert db_session.get(Task, historical_pending.id).status == TaskStatus.PENDING
    assert db_session.get(Task, historical_failed.id).status == TaskStatus.FAILED
    assert db_session.get(Task, historical_cancelled.id).status == TaskStatus.CANCELLED
    preserved_planning = db_session.get(PlanningSession, historical_planning.id)
    assert preserved_planning is not None
    assert preserved_planning.status == "completed"
    assert preserved_planning.prompt == "Preserve the original planning history."


def test_admitted_dogfood_session_keeps_plan_scoped_ordering(
    authenticated_client, db_session
):
    """Admission cannot bypass a predecessor in the same explicit Plan."""
    project = Project(name="Admitted Plan Scoped Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Bounded plan",
        source_brain="local",
        requirement="Preserve ordered execution.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan selected task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted plan session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-plan-session-instance",
    )
    admitted_session.dogfood_admitted = True
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    response = authenticated_client.post(
        f"/api/v1/sessions/{admitted_session.id}/tasks/{selected_task.id}/run"
    )

    assert response.status_code == 409
    assert "earlier ordered work" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_uses_admitted_session_queue_isolation(
    authenticated_client, db_session, monkeypatch
):
    """The R6 compatibility launch must honor the persisted session authority."""
    project = Project(name="R6 Compatibility Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="R6 compatibility task",
        description="Run through the compatibility endpoint.",
        status=TaskStatus.PENDING,
        plan_position=3,
        plan_id=None,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="R6 admitted session",
        status="pending",
        execution_mode="manual",
        instance_id="r6-admitted-session-instance",
        dogfood_admitted=True,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, admitted_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)
    db_session.expire_all()
    assert db_session.get(SessionModel, admitted_session.id).dogfood_admitted is True

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["session_id"] == admitted_session.id
    assert payload["task_id"] == selected_task.id
    assert db_session.get(Task, historical_pending.id).status == TaskStatus.PENDING
    assert db_session.get(Task, historical_failed.id).status == TaskStatus.FAILED
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert (
        db_session.query(LogEntry)
        .filter(LogEntry.task_execution_id == payload["task_execution_id"])
        .count()
        == 3
    )
    assert captured_kwargs["session_id"] == admitted_session.id
    assert captured_kwargs["task_id"] == selected_task.id
    assert captured_kwargs["task_execution_id"] == payload["task_execution_id"]


def test_compatibility_execute_e2_shape_reaches_planning_boundary_without_duplicates(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    """The exact compatibility route admits E2-shaped implementation work once."""
    project = Project(
        name="E2 Exact Route Project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="Add utc_now() helper and migrate one naive datetime consumer",
        description=(
            "Add app/time_utils.py with utc_now() returning an aware UTC datetime. "
            "Migrate app/services/workspace/context_service.py and add regression "
            "coverage in app/tests/test_utc_now_helper.py. Run pytest for the "
            "acceptance tests."
        ),
        status=TaskStatus.PENDING,
        plan_position=3,
        plan_id=None,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="E2 exact route admitted session",
        status="pending",
        execution_mode="manual",
        instance_id="e2-exact-route-session",
        dogfood_admitted=True,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, admitted_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 202
    payload = response.json()
    assert captured_kwargs["task_id"] == selected_task.id
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1

    # This is the worker's pre-planning boundary. The historical project
    # queue is not an explicit Plan predecessor for this implementation task.
    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            selected_task,
            "full_lifecycle",
            lambda root, **_kw: root / ".agent" / "state_manager.json",
        )
        is None
    )
    assert payload["task_execution_id"] == db_session.query(TaskExecution).one().id


def test_compatibility_execute_allows_explicit_unplanned_target(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Ordinary Compatibility Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="Ordinary compatibility task",
        description="Remain project-ordered.",
        status=TaskStatus.PENDING,
        plan_position=3,
    )
    ordinary_session = SessionModel(
        project_id=project.id,
        name="Ordinary session",
        status="pending",
        execution_mode="manual",
        instance_id="ordinary-session-instance",
        dogfood_admitted=False,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, ordinary_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(ordinary_session)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": ordinary_session.id},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] == selected_task.id
    assert db_session.get(Task, historical_pending.id).status == TaskStatus.PENDING
    assert db_session.get(Task, historical_failed.id).status == TaskStatus.FAILED
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1


def test_compatibility_execute_keeps_admitted_plan_predecessor_ordering(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Admitted Compatibility Plan Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Compatibility plan",
        source_brain="local",
        requirement="Preserve Plan ordering.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan compatibility task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted compatibility session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-compatibility-plan-session",
        dogfood_admitted=True,
    )
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_keeps_admitted_session_owned_ordering(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Admitted Session-Owned Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    predecessor = Task(
        project_id=project.id,
        title="Session predecessor",
        description="Must finish in the current session first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        title="Session-owned compatibility task",
        description="Must remain ordered within the admitted session.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted session-owned session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-session-owned-instance",
        dogfood_admitted=True,
    )
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)
    db_session.add(
        SessionTask(
            session_id=admitted_session.id,
            task_id=predecessor.id,
            status=TaskStatus.PENDING,
        )
    )
    db_session.commit()

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_does_not_duplicate_active_task_execution(
    authenticated_client, db_session
):
    project = Project(name="Active Compatibility Task Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already running compatibility task",
        description="Must not be duplicated.",
        status=TaskStatus.RUNNING,
        plan_position=1,
    )
    session = SessionModel(
        project_id=project.id,
        name="Active compatibility session",
        status="running",
        is_active=True,
        instance_id="active-compatibility-session-instance",
        dogfood_admitted=True,
    )
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
        )
    )
    db_session.add(
        TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
    )
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/execute",
        json={"session_id": session.id},
    )

    assert response.status_code == 409
    assert "already has an active canonical execution" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1


def test_task_retry_with_requested_changes_injects_operator_note_and_change_set(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    project = Project(name="Requested Changes Repair Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Repair requested",
        description="original task prompt",
        status=TaskStatus.DONE,
        workspace_status="changes_requested",
        promotion_note="Tighten the README and remove the extra file.",
        task_subfolder="task-repair-requested",
    )
    session = SessionModel(
        project_id=project.id,
        name="Previous run",
        status="stopped",
        is_active=False,
    )
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    db_session.add(
        TaskExecutionChangeSet(
            project_id=project.id,
            task_id=task.id,
            session_id=session.id,
            task_execution_id=execution.id,
            base_snapshot_key="requested-changes-snapshot",
            added_files=["extra.md"],
            modified_files=["README.md"],
            deleted_files=["old.md"],
            warning_flags=["deleted_files"],
            disposition="captured",
        )
    )
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            level="INFO",
            message=TASK_CHANGE_SET_LOG_MESSAGE,
            log_metadata=json.dumps(
                {
                    "schema": "openclaw.task_execution_change_set.v1",
                    "task_id": task.id,
                    "task_execution_id": execution.id,
                    "added_count": 1,
                    "modified_count": 1,
                    "deleted_count": 1,
                    "changed_count": 3,
                    "added_files": ["extra.md"],
                    "modified_files": ["README.md"],
                    "deleted_files": ["old.md"],
                    "warning_flags": ["deleted_files"],
                }
            ),
        )
    )
    db_session.commit()

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)
    monkeypatch.setattr(
        "app.services.tasks.service.TaskService.archive_task_workspace_for_repair_rerun",
        lambda *a, **kw: {"archived": False, "reason": "test"},
    )

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={
            "execution_scope": "new_session",
            "create_new_session": True,
            "retry_intent": "request_changes",
        },
    )

    assert response.status_code == 200, response.text
    assert captured_kwargs["prompt"] == task.description
    execution_id = captured_kwargs["task_execution_id"]
    retry_execution = db_session.get(TaskExecution, execution_id)
    assert retry_execution is not None
    assert retry_execution.dispatch_invocation["prompt_source"] == "description"
    assert (
        retry_execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"]
        == "request_changes"
    )

    from app.tests.test_pr20_t3a2_request_changes_boundary import (
        _run_worker_completion_seam,
        _worker_seam_failure_diagnostics,
    )

    worker_result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "requested-changes-worker",
        db_session_factory=db_session_factory,
        task=task,
        prompt=captured_kwargs["prompt"],
        context=captured_kwargs.get("context"),
        dispatch_kind="ROOT",
        persisted_task_execution_id=execution_id,
        expected_attempt_number=1,
        celery_task_id=captured_kwargs["_celery_task_id"],
    )
    diagnostics = _worker_seam_failure_diagnostics(worker_result)
    assert len(worker_result.worker_contexts) == 1, diagnostics
    assert (
        worker_result.worker_contexts[0].acceptance_authority_status
        == "verified_request_changes"
    )
    prompt = worker_result.worker_contexts[0].prompt
    assert "Operator requested changes" in prompt
    assert "Tighten the README" in prompt
    assert "added=1, modified=1, deleted=1" in prompt
    assert "README.md" in prompt
    assert "extra.md" in prompt
    assert "old.md" in prompt


def test_legacy_worker_alias_still_exists():
    from app.tasks import worker as worker_module

    assert (
        worker_module.execute_openclaw_task is worker_module.execute_orchestration_task
    )
