"""Deterministic file-backed coverage for TaskExecution dispatch recovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import (
    MIGRATIONS,
    _migration_060_task_execution_dispatch,
    run_schema_migrations,
)
from app.models import (
    Base,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    WorkflowBudget,
)
from app.services.execution import workflow_lifecycle as workflow
from app.services.execution import workflow_budget as r3a
from app.services.execution.workflow_budget import BudgetOutcome
from app.services.orchestration.lifecycle.transitions import (
    ContinuationIdentity,
    enter_recovering,
    resolve_continuation_identity,
    schedule_continuation,
)
from app.services.orchestration.lifecycle import continuation_recovery as e8
from app.services.orchestration.run_state.transitions import mark_task_attempt_done
from app.tasks.worker_support.dispatch import _claim_queued_task_for_worker


MIGRATION_060 = next(
    migration
    for migration in MIGRATIONS
    if migration.version == "060_task_execution_dispatch_contract"
)
MIGRATION_059 = next(
    migration
    for migration in MIGRATIONS
    if migration.version == "059_workflow_provider_request_budget"
)


def _engine(path: Path, timeout: float = 30.0):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        cursor.close()

    return engine


def _factory(engine):
    return sessionmaker(autoflush=False, expire_on_commit=False, bind=engine)


@pytest.fixture
def dispatch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", False)
    path = tmp_path / "dispatch.sqlite3"
    engine = _engine(path)
    Base.metadata.create_all(engine)
    run_schema_migrations(engine, (MIGRATION_059, MIGRATION_060))
    factory = _factory(engine)
    db = factory()
    try:
        yield path, engine, factory, db
    finally:
        db.close()
        engine.dispose()


def _graph(db, *, session_status="running"):
    project = Project(name=f"dispatch-{uuid.uuid4()}")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"dispatch-session-{uuid.uuid4()}",
        status=session_status,
        is_active=True,
        instance_id=str(uuid.uuid4()),
    )
    task = Task(
        project_id=project.id,
        title="Durable dispatch test",
        description="Execute the durable dispatch fixture.",
        status=TaskStatus.PENDING,
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.PENDING
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db.add_all([link, execution])
    db.commit()
    return project, session, task, link, execution


def _configure(db, session, task, execution, *, kind="ROOT", parent_id=None):
    return workflow.configure_task_execution_dispatch(
        db,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind=kind,
        parent_task_execution_id=parent_id,
        prompt=task.description,
        timeout_seconds=1800,
        context={"planner_contract": {"schema": "bounded-v1"}},
    )


def _claim_in_subprocess(
    database_path,
    session_id,
    task_id,
    execution_id,
    generation,
    celery_id,
    result_queue,
):
    settings.WORKFLOW_BUDGET_ENFORCEMENT_ENABLED = False
    engine = _engine(Path(database_path))
    db = _factory(engine)()
    try:
        session = db.query(SessionModel).filter_by(id=session_id).first()
        task = db.query(Task).filter_by(id=task_id).first()
        link = (
            db.query(SessionTask)
            .filter_by(session_id=session_id, task_id=task_id)
            .order_by(SessionTask.id.desc())
            .first()
        )
        result = _claim_queued_task_for_worker(
            db=db,
            session=session,
            task=task,
            session_task_link=link,
            expected_session_instance_id=generation,
            task_execution_id=execution_id,
            celery_task_id=celery_id,
        )
        result_queue.put((result[0], result[1]))
    except BaseException as exc:  # propagate child failures through the queue
        result_queue.put((False, f"child_error:{exc.__class__.__name__}:{exc}"))
        raise
    finally:
        db.close()
        engine.dispose()


def _migration_in_subprocess(database_path, result_queue):
    engine = _engine(Path(database_path))
    try:
        run_schema_migrations(engine, (MIGRATION_060,))
        result_queue.put("ok")
    except BaseException as exc:
        result_queue.put(f"error:{exc.__class__.__name__}:{exc}")
        raise
    finally:
        engine.dispose()


def _enable_budget(monkeypatch):
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_ID", "dispatch-test")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_VERSION", "1")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_REQUESTS", 3)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_DURATION_SECONDS", 1800)


def test_migration_060_preserves_historical_rows_and_guards_identity(tmp_path):
    path = tmp_path / "pre-dispatch.sqlite3"
    engine = _engine(path)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE task_executions (id INTEGER PRIMARY KEY, session_id INTEGER, "
            "task_id INTEGER, attempt_number INTEGER, status VARCHAR(32))"
        )
        connection.exec_driver_sql("INSERT INTO sessions(id) VALUES (1)")
        connection.exec_driver_sql(
            "INSERT INTO task_executions(id,session_id,task_id,attempt_number,status) "
            "VALUES (1,1,5,1,'pending')"
        )

    _migration_060_task_execution_dispatch(engine)
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id,session_id,task_id,attempt_number,status,dispatch_state "
            "FROM task_executions WHERE id=1"
        ).one()
        assert tuple(row) == (1, 1, 5, 1, "pending", None)
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO task_executions(id,session_id,task_id,attempt_number,status,dispatch_state) "
                "VALUES (2,1,5,2,'pending','READY')"
            )


def test_t1_rollback_leaves_no_partial_attempt_or_dispatch_intent(dispatch_db):
    _, _, factory, db = dispatch_db
    _, session, task, _, _ = _graph(db)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.PENDING,
    )
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.PENDING
    )
    db.add_all([execution, link])
    db.flush()
    execution_id = execution.id
    assert _configure(db, session, task, execution)
    db.rollback()  # Process death before T1 commit.

    observer = factory()
    try:
        assert observer.query(TaskExecution).filter_by(id=execution_id).count() == 0
        assert (
            observer.query(SessionTask)
            .filter_by(session_id=session.id, task_id=task.id)
            .count()
            == 1
        )
    finally:
        observer.close()


def test_dispatch_schema_integrity_and_identity_immutability(dispatch_db):
    _, engine, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()

    with engine.connect() as connection:
        indexes = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list(task_executions)"
            ).fetchall()
        }
        assert "uq_task_executions_dispatch_celery_id" in indexes
        assert "ix_task_executions_dispatch_reconcile" in indexes
        fks = connection.exec_driver_sql(
            "PRAGMA foreign_key_list(task_executions)"
        ).fetchall()
        assert any(
            row[3] == "dispatch_parent_task_execution_id"
            and str(row[6]).upper() == "RESTRICT"
            for row in fks
        )
        assert not any(
            row[3] == "continuation_task_execution_id"
            for row in connection.exec_driver_sql(
                "PRAGMA foreign_key_list(sessions)"
            ).fetchall()
        )

    duplicate = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.PENDING,
    )
    db.add(duplicate)
    db.flush()
    assert _configure(db, session, task, duplicate)
    duplicate.dispatch_celery_task_id = execution.dispatch_celery_task_id
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    original_invocation = deepcopy(execution.dispatch_invocation)
    execution.dispatch_invocation = {**original_invocation, "timeout_seconds": 5}
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "UPDATE task_executions SET dispatch_state='NOT_A_STATE' WHERE id=?",
                (execution.id,),
            )

    session.continuation_task_id = task.id
    session.continuation_task_execution_id = execution.id
    db.commit()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "UPDATE sessions SET continuation_task_execution_id=? WHERE id=?",
                (execution.id + 1000, session.id),
            )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "DELETE FROM task_executions WHERE id=?", (execution.id,)
            )


def test_concurrent_060_migration_is_idempotent(tmp_path):
    path = tmp_path / "parallel-migration.sqlite3"
    engine = _engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_migration_in_subprocess,
            args=(str(path), result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert [result_queue.get(timeout=5) for _ in processes] == ["ok", "ok"]

    check_engine = _engine(path)
    try:
        with check_engine.connect() as connection:
            applied = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=?",
                (MIGRATION_060.version,),
            ).scalar_one()
        assert applied == 1
    finally:
        check_engine.dispose()


def test_interrupted_060_migration_rolls_back_and_can_resume(tmp_path):
    path = tmp_path / "interrupted-migration.sqlite3"
    engine = _engine(path)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE task_executions (id INTEGER PRIMARY KEY, session_id INTEGER, "
            "task_id INTEGER, attempt_number INTEGER, status VARCHAR(32))"
        )
    failed = {"once": False}

    def fail_during_migration(
        connection, cursor, statement, parameters, context, executemany
    ):
        if (
            not failed["once"]
            and "tr_task_execution_dispatch_identity_immutable" in statement
        ):
            failed["once"] = True
            raise RuntimeError("injected migration interruption")

    event.listen(engine, "before_cursor_execute", fail_during_migration)
    with pytest.raises(RuntimeError, match="injected migration interruption"):
        _migration_060_task_execution_dispatch(engine)
    event.remove(engine, "before_cursor_execute", fail_during_migration)
    with engine.connect() as connection:
        assert "dispatch_state" not in {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(task_executions)"
            ).fetchall()
        }
    _migration_060_task_execution_dispatch(engine)
    with engine.connect() as connection:
        assert "dispatch_state" in {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(task_executions)"
            ).fetchall()
        }
    engine.dispose()


def test_r3a_expected_generation_refuses_stale_root_without_replacement(
    dispatch_db, monkeypatch
):
    _, engine, _, db = dispatch_db
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_ID", "dispatch-test")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_POLICY_VERSION", "1")
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_REQUESTS", 3)
    monkeypatch.setattr(settings, "WORKFLOW_BUDGET_MAX_DURATION_SECONDS", 1800)
    _, session, _, _, execution = _graph(db)
    expected = session.instance_id
    session.instance_id = str(uuid.uuid4())
    db.commit()

    result = workflow.bind_new_root(
        db, execution.id, expected_session_instance_id=expected
    )
    assert result is BudgetOutcome.IDENTITY_CONFLICT
    assert db.query(WorkflowBudget).count() == 0

    session.instance_id = expected
    db.commit()
    created = workflow.bind_new_root(
        db, execution.id, expected_session_instance_id=expected
    )
    assert created is BudgetOutcome.CREATED
    session.instance_id = str(uuid.uuid4())
    db.commit()
    refused = workflow.bind_new_root(
        db, execution.id, expected_session_instance_id=expected
    )
    assert refused is BudgetOutcome.IDENTITY_CONFLICT
    assert db.query(WorkflowBudget).count() == 1


def test_uncertain_publication_reuses_exact_id_and_duplicate_claim_is_fenced(
    dispatch_db,
):
    _, _, factory, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    db.commit()  # T1; simulated process death before T2

    observed = []

    def accepted_but_ack_lost(*, kwargs, task_id):
        observed.append((kwargs, task_id))
        raise ConnectionError("simulated lost publisher acknowledgment")

    first = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=accepted_but_ack_lost
    )
    assert first["state"] == "publish_error"
    assert observed[0][1] == stable_id
    execution = db.query(TaskExecution).filter_by(id=execution.id).one()
    execution.dispatch_next_attempt_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()

    def accepted(*, kwargs, task_id):
        observed.append((kwargs, task_id))

    second = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=accepted
    )
    assert second["state"] == "published_unclaimed"
    assert observed[1][1] == stable_id
    assert observed[0][0] == observed[1][0]

    claim = _claim_queued_task_for_worker(
        db=db,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=session.instance_id,
        task_execution_id=execution.id,
        celery_task_id=stable_id,
    )
    assert claim[0] is True
    duplicate_db = factory()
    try:
        duplicate_session = (
            duplicate_db.query(SessionModel).filter_by(id=session.id).one()
        )
        duplicate_task = duplicate_db.query(Task).filter_by(id=task.id).one()
        duplicate_link = duplicate_db.query(SessionTask).filter_by(id=link.id).one()
        duplicate = _claim_queued_task_for_worker(
            db=duplicate_db,
            session=duplicate_session,
            task=duplicate_task,
            session_task_link=duplicate_link,
            expected_session_instance_id=session.instance_id,
            task_execution_id=execution.id,
            celery_task_id=stable_id,
        )
        assert duplicate[0] is False
        assert duplicate[1] == "task_not_claimable:running"
    finally:
        duplicate_db.close()
    db.expire_all()
    claimed = db.query(TaskExecution).filter_by(id=execution.id).one()
    assert claimed.status is TaskStatus.RUNNING
    assert claimed.dispatch_state == "CLAIMED"
    assert db.query(Task).filter_by(id=task.id).one().status is TaskStatus.RUNNING


def test_two_processes_admit_only_one_exact_worker_claim(dispatch_db):
    path, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    execution.dispatch_state = "READY"
    db.commit()

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_subprocess,
            args=(
                str(path),
                session.id,
                task.id,
                execution.id,
                session.instance_id,
                execution.dispatch_celery_task_id,
                result_queue,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [result_queue.get(timeout=5) for _ in processes]
    assert sum(accepted for accepted, _ in outcomes) == 1


def test_t1_crash_reconciliation_and_expired_lease_recover_same_attempt(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    db.commit()  # T1 durable; no binding or broker publication occurred

    # Simulate a publisher dying after T4 but before its broker call.
    execution.dispatch_state = "READY"
    execution.dispatch_lease_owner = "dead-publisher:old"
    execution.dispatch_lease_fence = 1
    execution.dispatch_lease_expires_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()
    sent = []
    result = workflow.reconcile_task_execution_dispatches(
        db,
        task_execution_id=execution.id,
        publisher=lambda *, kwargs, task_id: sent.append((kwargs, task_id)),
    )
    assert result["published_count"] == 1
    assert sent[0][1] == stable_id
    assert db.query(TaskExecution).filter_by(id=execution.id).count() == 1


def test_r3a_uncertain_commit_and_crash_before_ready_reuse_original_root(
    dispatch_db, monkeypatch
):
    _, _, _, db = dispatch_db
    _enable_budget(monkeypatch)
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    original_bind = workflow.bind_new_root
    interrupted = {"once": False}

    def commit_then_lose_reply(*args, **kwargs):
        result = original_bind(*args, **kwargs)
        if not interrupted["once"]:
            interrupted["once"] = True
            raise ConnectionError("R3A commit reply lost")
        return result

    monkeypatch.setattr(workflow, "bind_new_root", commit_then_lose_reply)
    first = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=lambda **_: None
    )
    assert first["state"] == "not_ready"
    assert execution.dispatch_state == "PENDING_BINDING"
    roots = db.query(WorkflowBudget).filter_by(root_task_execution_id=execution.id)
    assert roots.count() == 1
    original_root = roots.one()
    original_uuid = original_root.workflow_uuid
    original_deadline = original_root.deadline_at
    reserved = r3a.reserve_provider_request(
        db,
        workflow_uuid=original_uuid,
        idempotency_key="dispatch-recovery-reservation",
        stage="planning",
        runtime_role="planning",
        task_execution_id=execution.id,
    )
    assert reserved.outcome is BudgetOutcome.RESERVED
    db.expire_all()
    original_counter = (
        db.query(WorkflowBudget)
        .filter_by(root_task_execution_id=execution.id)
        .one()
        .reserved_requests
    )

    monkeypatch.setattr(workflow, "bind_new_root", original_bind)
    execution.dispatch_next_attempt_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()
    second = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=lambda **_: None
    )
    assert second["state"] == "published_unclaimed"
    assert roots.count() == 1
    reconciled = roots.one()
    assert reconciled.workflow_uuid == original_uuid
    assert reconciled.deadline_at == original_deadline
    assert reconciled.reserved_requests == original_counter


def test_crash_after_binding_before_ready_retries_idempotently(
    dispatch_db, monkeypatch
):
    _, _, _, db = dispatch_db
    _enable_budget(monkeypatch)
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    original_validate = workflow.validate_existing_dispatch_binding
    failed_once = {"value": False}

    def fail_after_binding(*args, **kwargs):
        if not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("crash after binding, before READY")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        workflow, "validate_existing_dispatch_binding", fail_after_binding
    )
    first = workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=lambda **_: None
    )
    assert first["state"] == "not_ready"
    assert execution.dispatch_state == "PENDING_BINDING"
    assert (
        db.query(WorkflowBudget).filter_by(root_task_execution_id=execution.id).count()
        == 1
    )

    monkeypatch.setattr(
        workflow, "validate_existing_dispatch_binding", original_validate
    )
    execution.dispatch_next_attempt_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()
    sent = []
    second = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: sent.append((kwargs, task_id)),
    )
    assert second["state"] == "published_unclaimed"
    assert len(sent) == 1
    assert sent[0][1] == execution.dispatch_celery_task_id
    assert (
        db.query(WorkflowBudget).filter_by(root_task_execution_id=execution.id).count()
        == 1
    )


def test_ready_before_lease_rejection_uses_bounded_backoff_and_same_id(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    execution.dispatch_state = "READY"
    db.commit()  # Restart after T3, before T4.

    rejected = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda **_: (_ for _ in ()).throw(ConnectionError("broker rejected")),
    )
    assert rejected["state"] == "publish_error"
    assert execution.dispatch_state == "READY"
    assert execution.dispatch_publish_attempts == 1
    assert execution.dispatch_lease_owner is None
    assert execution.dispatch_next_attempt_at.replace(
        tzinfo=timezone.utc
    ) > datetime.now(timezone.utc)
    assert (
        workflow.publish_task_execution_dispatch(
            db, execution.id, publisher=lambda **_: None
        )["state"]
        == "not_due"
    )

    execution.dispatch_next_attempt_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()
    replay = []
    result = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: replay.append((kwargs, task_id)),
    )
    assert result["state"] == "published_unclaimed"
    assert replay[0][1] == stable_id


def test_claim_during_publish_fences_stale_publisher_writeback(dispatch_db):
    _, _, factory, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    execution.dispatch_state = "READY"
    db.commit()

    def publish_and_claim(*, kwargs, task_id):
        assert task_id == stable_id
        worker_db = factory()
        try:
            worker_session = (
                worker_db.query(SessionModel).filter_by(id=session.id).one()
            )
            worker_task = worker_db.query(Task).filter_by(id=task.id).one()
            worker_link = worker_db.query(SessionTask).filter_by(id=link.id).one()
            claimed = _claim_queued_task_for_worker(
                db=worker_db,
                session=worker_session,
                task=worker_task,
                session_task_link=worker_link,
                expected_session_instance_id=session.instance_id,
                task_execution_id=execution.id,
                celery_task_id=task_id,
            )
            assert claimed[0]
        finally:
            worker_db.close()

    workflow.publish_task_execution_dispatch(
        db, execution.id, publisher=publish_and_claim
    )
    db.expire_all()
    claimed = db.query(TaskExecution).filter_by(id=execution.id).one()
    assert claimed.dispatch_state == "CLAIMED"
    assert claimed.dispatch_lease_owner is None
    assert claimed.dispatch_lease_expires_at is None


def test_claimed_attempt_after_worker_crash_is_not_republished(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    execution.dispatch_state = "READY"
    db.commit()
    claim = _claim_queued_task_for_worker(
        db=db,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=session.instance_id,
        task_execution_id=execution.id,
        celery_task_id=stable_id,
    )
    assert claim[0]
    sent = []
    result = workflow.reconcile_task_execution_dispatches(
        db,
        task_execution_id=execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["inspected_count"] == 0
    assert not sent
    db.expire_all()
    assert (
        db.query(TaskExecution).filter_by(id=execution.id).one().dispatch_state
        == "CLAIMED"
    )


@pytest.mark.parametrize("session_status", ["paused", "stopped"])
def test_lifecycle_revocation_prevents_pending_dispatch(dispatch_db, session_status):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    session.status = session_status
    db.commit()
    sent = []
    result = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["state"] == "refused"
    assert not sent
    assert execution.dispatch_state == "REFUSED"


@pytest.mark.parametrize("invalidity", ["expired", "closed"])
def test_expired_or_closed_workflow_refuses_publication(
    dispatch_db, monkeypatch, invalidity
):
    _, _, _, db = dispatch_db
    _enable_budget(monkeypatch)
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    assert (
        workflow.bind_new_root(
            db,
            execution.id,
            expected_session_instance_id=session.instance_id,
        )
        is BudgetOutcome.CREATED
    )
    budget = (
        db.query(WorkflowBudget).filter_by(root_task_execution_id=execution.id).one()
    )
    if invalidity == "expired":
        deadline = budget.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        beyond_deadline = deadline + timedelta(seconds=1)
        monkeypatch.setattr(workflow, "trusted_utc_now", lambda: beyond_deadline)
        monkeypatch.setattr(r3a, "trusted_utc_now", lambda: beyond_deadline)
    else:
        assert (
            r3a.close_workflow(
                db, budget.workflow_uuid, require_quiescent=False
            ).outcome
            is BudgetOutcome.CLOSED
        )
    if invalidity == "expired":
        db.commit()
    sent = []
    result = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["state"] == "refused"
    assert not sent
    assert execution.dispatch_state == "REFUSED"


def test_terminal_attempt_is_never_redispatched(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    mark_task_attempt_done(
        task=task,
        session_task_link=link,
        task_execution=execution,
    )
    db.commit()
    sent = []
    result = workflow.reconcile_task_execution_dispatches(
        db,
        task_execution_id=execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["inspected_count"] == 0
    assert not sent
    assert execution.dispatch_state == "TERMINAL"
    assert execution.status is TaskStatus.DONE


def test_same_te_e3_retry_reopens_only_after_durable_continuation_marker(
    dispatch_db,
):
    _, _, _, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    stable_id = execution.dispatch_celery_task_id
    db.commit()
    execution.status = TaskStatus.RUNNING
    execution.dispatch_state = "CLAIMED"
    task.status = TaskStatus.RUNNING
    link.status = TaskStatus.RUNNING
    db.commit()

    enter_recovering(
        db,
        session,
        task_execution=execution,
        continuation_kind="celery_retry",
        retry_count=1,
    )
    assert execution.status is TaskStatus.FAILED
    assert execution.dispatch_state == "TERMINAL"
    identity = schedule_continuation(
        db,
        session,
        task_execution=execution,
        continuation_kind="celery_retry",
        retry_count=1,
    )
    db.commit()
    assert execution.status is TaskStatus.PENDING
    assert execution.dispatch_state == "READY"
    assert session.continuation_task_execution_id == execution.id

    published = []
    result = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: published.append((kwargs, task_id)),
    )
    assert result["state"] == "published_unclaimed"
    assert len(published) == 1
    assert published[0][1] == stable_id
    assert published[0][0]["continuation_retry_count"] == 1

    claim_identity = ContinuationIdentity(
        session_id=identity.session_id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
        celery_task_id=stable_id,
    )
    from app.services.orchestration.lifecycle.transitions import claim_continuation

    claimed = claim_continuation(db, claim_identity, commit=True)
    assert claimed.accepted
    db.refresh(execution)
    assert execution.status is TaskStatus.RUNNING
    assert execution.dispatch_state == "CLAIMED"
    assert (
        db.query(TaskExecution)
        .filter_by(session_id=session.id, task_id=task.id)
        .count()
        == 1
    )


def test_explicit_recovery_parent_is_persisted_and_validated(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, parent = _graph(db)
    parent.status = TaskStatus.FAILED
    child = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.PENDING,
    )
    db.add(child)
    db.commit()
    assert _configure(db, session, task, child, kind="RECOVERY", parent_id=parent.id)
    session.status = "recovering"
    schedule_continuation(
        db,
        session,
        task_execution=child,
        continuation_task_id=task.id,
        continuation_kind="automatic_recovery",
        retry_count=1,
    )
    db.commit()
    assert child.dispatch_parent_task_execution_id == parent.id
    _, reason = workflow._dispatch_replay_arguments(db, child)
    assert reason is None


def test_e8_routes_managed_continuation_through_same_te_dispatch(
    dispatch_db, monkeypatch
):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db, session_status="recovering")
    assert _configure(db, session, task, execution)
    execution.dispatch_state = "READY"
    identity = schedule_continuation(
        db,
        session,
        task_execution=execution,
        continuation_task_id=task.id,
        continuation_kind="celery_retry",
        retry_count=2,
        retry_eta=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db.commit()
    observed = []

    def publish_exact(db, task_execution_id, *, publisher=None):
        assert task_execution_id == execution.id
        assert callable(publisher)
        observed.append(execution.dispatch_celery_task_id)
        return {"state": "published_unclaimed"}

    monkeypatch.setattr(workflow, "publish_task_execution_dispatch", publish_exact)
    result = e8.reconcile_stranded_continuations(
        db,
        now=datetime.now(timezone.utc),
        grace_seconds=0,
        use_project_lock=False,
    )
    assert result["counts"][e8.REPUBLISHED] == 1
    assert observed == [execution.dispatch_celery_task_id]
    assert result["republished_session_ids"] == [session.id]
    assert identity.task_execution_id == execution.id
    assert db.query(TaskExecution).filter_by(session_id=session.id).count() == 1


def test_worker_rejects_delivery_revoked_after_publication(dispatch_db):
    _, _, factory, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    execution.dispatch_state = "READY"
    stable_id = execution.dispatch_celery_task_id
    db.commit()
    publisher_db = factory()
    try:
        current = publisher_db.query(SessionModel).filter_by(id=session.id).one()
        current.status = "stopped"
        publisher_db.commit()
    finally:
        publisher_db.close()

    rejected = _claim_queued_task_for_worker(
        db=db,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=session.instance_id,
        task_execution_id=execution.id,
        celery_task_id=stable_id,
    )
    assert rejected[0] is False
    assert rejected[1] in {
        "session_not_runnable:stopped",
        "session_generation_or_state_changed",
    }
    db.expire_all()
    assert (
        db.query(TaskExecution).filter_by(id=execution.id).one().status
        is TaskStatus.PENDING
    )


def test_worker_rejects_stale_generation_delivery(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    execution.dispatch_state = "READY"
    expected = session.instance_id
    db.commit()
    session.instance_id = str(uuid.uuid4())
    db.commit()
    rejected = _claim_queued_task_for_worker(
        db=db,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=expected,
        task_execution_id=execution.id,
        celery_task_id=execution.dispatch_celery_task_id,
    )
    assert rejected[0] is False
    assert rejected[1] == "session_instance_changed"
    db.expire_all()
    assert (
        db.query(TaskExecution).filter_by(id=execution.id).one().status
        is TaskStatus.PENDING
    )


def test_mutable_task_prompt_reference_is_refused(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    task.description += " changed after T1"
    db.commit()
    sent = []
    result = workflow.publish_task_execution_dispatch(
        db,
        execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["state"] == "refused"
    assert not sent
    assert execution.dispatch_refusal_reason == "task_prompt_reference_changed"


def test_concurrent_reconcilers_take_one_lease_and_publish_once(dispatch_db):
    _, _, factory, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    sent = []
    sent_lock = threading.Lock()

    def run_reconciler(_):
        local = factory()
        try:

            def publisher(*, kwargs, task_id):
                with sent_lock:
                    sent.append(task_id)

            return workflow.reconcile_task_execution_dispatches(
                local, task_execution_id=execution.id, publisher=publisher
            )
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_reconciler, range(2)))
    assert sent == [execution.dispatch_celery_task_id]


def test_exact_continuation_marker_beats_newer_pending_attempt(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    identity = schedule_continuation(
        db,
        session,
        task_execution=execution,
        continuation_task_id=task.id,
        continuation_kind="backend_capacity",
        retry_count=1,
    )
    db.commit()
    newer = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.PENDING,
    )
    db.add(newer)
    db.commit()

    resolved = resolve_continuation_identity(db, session)
    assert resolved is not None
    assert resolved.task_execution_id == identity.task_execution_id == execution.id
    assert session.continuation_task_execution_id == execution.id


def test_unreplayable_prompt_and_parentless_recovery_are_not_managed(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, _, execution = _graph(db)
    assert not workflow.configure_task_execution_dispatch(
        db,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind="ROOT",
        prompt="an ad-hoc prompt that is not stored on Task",
        timeout_seconds=1800,
    )
    assert execution.dispatch_state is None
    assert not _configure(db, session, task, execution, kind="RECOVERY")
    assert execution.dispatch_state is None


def test_terminal_and_stale_generation_intents_are_never_republished(dispatch_db):
    _, _, _, db = dispatch_db
    _, session, task, link, execution = _graph(db)
    assert _configure(db, session, task, execution)
    db.commit()
    session.instance_id = str(uuid.uuid4())
    execution.dispatch_next_attempt_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db.commit()
    sent = []
    result = workflow.reconcile_task_execution_dispatches(
        db,
        task_execution_id=execution.id,
        publisher=lambda *, kwargs, task_id: sent.append(task_id),
    )
    assert result["refused_count"] == 1
    assert not sent
    assert execution.dispatch_state == "REFUSED"
