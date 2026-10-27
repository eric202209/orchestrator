"""Provider-free temporary-SQLite coverage for the Phase 40E-4R3A budget foundation."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import inspect
import multiprocessing
import os
import re
from pathlib import Path
import socket
import sqlite3
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db_migrations import (
    MIGRATIONS,
    _verify_059_workflow_provider_request_budget,
    run_schema_migrations,
)
from app import db_migrations
from app.models import (
    Base,
    PlanningSession,
    Project,
    ProviderRequestReservation,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
    WorkflowBudget,
    WorkflowTaskExecutionBinding,
)
from app.services.execution import workflow_budget as wb
from app.services.execution.workflow_budget import (
    LEGACY_UNENFORCED,
    R2_CANDIDATE_POLICY,
    BindingKind,
    BudgetEnforcementDisabled,
    BudgetOutcome,
    EnforcementMode,
    InvalidBudgetPolicy,
    ReservationStatus,
    WorkflowBudgetPolicy,
    WorkflowKind,
    bind_task_execution_attempt,
    budget_enforcement_status,
    close_workflow,
    create_planning_session_workflow,
    create_task_execution_workflow,
    inspect_workflow_accounting,
    mark_dispatch_started,
    new_idempotency_key,
    reconcile_stale_reservations,
    record_reservation_outcome,
    reserve_provider_request,
    resolve_planning_session_workflow,
    resolve_task_execution_workflow,
)
from app.services.tasks.execution import (
    latest_execution_for_session_task,
    next_attempt_number,
)

MIGRATION_059 = next(m for m in MIGRATIONS if m.version == wb.MIGRATION_VERSION)
NEW_TABLES = (
    WorkflowBudget.__table__,
    WorkflowTaskExecutionBinding.__table__,
    ProviderRequestReservation.__table__,
)
POLICY = WorkflowBudgetPolicy(
    policy_id="r3a-test",
    policy_version="1",
    workflow_kind=WorkflowKind.TASK_EXECUTION,
    max_requests=3,
    max_duration_seconds=600,
    enforcement_mode=EnforcementMode.ENFORCED,
)
PLANNING_POLICY = WorkflowBudgetPolicy(
    policy_id="r3a-planning-test",
    policy_version="1",
    workflow_kind=WorkflowKind.PLANNING_SESSION,
    max_requests=2,
    max_duration_seconds=600,
    enforcement_mode=EnforcementMode.ENFORCED,
)


class _SimulatedCrash(BaseException):
    """Stands in for process death: not an Exception, so nothing handles it."""


def _engine(path: Path, *, timeout: float = 30.0):
    """Mirror the production SQLite connection policy on a temporary file."""

    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        cursor.close()

    return engine


def _session(engine):
    return sessionmaker(autoflush=False, bind=engine)()


@pytest.fixture
def r3a(tmp_path: Path):
    path = tmp_path / "r3a.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_059,))
    db = _session(engine)
    try:
        yield path, engine, db
    finally:
        db.close()
        engine.dispose()


def _graph(db, *, attempts: int = 1):
    project = Project(name=f"R3A Project {uuid.uuid4()}")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"R3A Session {uuid.uuid4()}",
        status="pending",
        instance_id=str(uuid.uuid4()),
    )
    task = Task(project_id=project.id, title="R3A fixture", status=TaskStatus.PENDING)
    db.add_all([session, task])
    db.flush()
    executions = [
        _attempt(db, session, task, n, commit=False) for n in range(1, attempts + 1)
    ]
    db.commit()
    return project, session, task, executions


def _attempt(db, session, task, number: int, *, commit: bool = True):
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=number,
        status=TaskStatus.PENDING,
    )
    db.add(execution)
    db.flush()
    if commit:
        db.commit()
    return execution


def _workflow(db, execution, policy=POLICY):
    result = create_task_execution_workflow(
        db, policy, root_task_execution_id=execution.id
    )
    assert result.outcome is BudgetOutcome.CREATED
    return result.workflow


def _reserve(db, workflow, execution, *, key=None, stage="planning", **extra):
    return reserve_provider_request(
        db,
        workflow_uuid=workflow.workflow_uuid,
        idempotency_key=key or new_idempotency_key(),
        stage=stage,
        runtime_role="planning",
        task_execution_id=execution.id if execution is not None else None,
        **extra,
    )


def _raw(path: Path, sql: str, params=()):
    """Read durable rows through an independent raw sqlite3 connection."""

    with sqlite3.connect(path) as raw:
        return raw.execute(sql, params).fetchall()


def _counter(path: Path, workflow_uuid: str) -> int:
    return _raw(
        path,
        "SELECT reserved_requests FROM workflow_budgets WHERE workflow_uuid = ?",
        (workflow_uuid,),
    )[0][0]


def _reservation_rows(path: Path, workflow_uuid: str):
    return _raw(
        path,
        "SELECT request_ordinal, status, outcome_code FROM provider_request_reservations "
        "WHERE workflow_uuid = ? ORDER BY request_ordinal",
        (workflow_uuid,),
    )


def _clock(monkeypatch, instant: datetime):
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: instant)


def _commit_error():
    return OperationalError("COMMIT", {}, sqlite3.OperationalError("disk I/O error"))


# Identity ---------------------------------------------------------------------


def test_workflow_identity_is_stable_after_reconstruction(r3a):
    path, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)

    other_engine = _engine(path)
    try:
        other = _session(other_engine)
        resolved = resolve_task_execution_workflow(other, execution.id)
        again = create_task_execution_workflow(
            other, POLICY, root_task_execution_id=execution.id
        )
        other.close()
    finally:
        other_engine.dispose()

    assert resolved == workflow
    assert again.outcome is BudgetOutcome.ALREADY_EXISTS
    assert again.workflow == workflow
    assert _raw(path, "SELECT COUNT(*) FROM workflow_budgets") == [(1,)]
    assert budget_enforcement_status(db, task_execution_id=execution.id) == "ENFORCED"


def test_retry_and_recovery_attempts_keep_the_same_budget_owner(r3a):
    path, _, db = r3a
    _, session, task, (first,) = _graph(db)
    workflow = _workflow(db, first)
    retry = _attempt(db, session, task, 2)
    recovery = _attempt(db, session, task, 3)

    bound_retry = bind_task_execution_attempt(
        db,
        task_execution_id=retry.id,
        parent_task_execution_id=first.id,
        binding_kind=BindingKind.RETRY,
    )
    bound_recovery = bind_task_execution_attempt(
        db,
        task_execution_id=recovery.id,
        parent_task_execution_id=retry.id,
        binding_kind=BindingKind.RECOVERY,
    )

    assert bound_retry.outcome is BudgetOutcome.BOUND
    assert bound_recovery.outcome is BudgetOutcome.BOUND
    for execution in (first, retry, recovery):
        assert _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED
        resolved = resolve_task_execution_workflow(db, execution.id)
        assert resolved.workflow_uuid == workflow.workflow_uuid
        assert resolved.deadline_at == workflow.deadline_at
    assert _counter(path, workflow.workflow_uuid) == 3
    # The cumulative budget is shared: the recovery attempt cannot get a fourth.
    assert (
        _reserve(db, workflow, recovery).outcome
        is BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED
    )
    replay = bind_task_execution_attempt(
        db,
        task_execution_id=retry.id,
        parent_task_execution_id=first.id,
        binding_kind=BindingKind.RETRY,
    )
    assert replay.outcome is BudgetOutcome.ALREADY_BOUND
    mismatch = bind_task_execution_attempt(
        db,
        task_execution_id=retry.id,
        parent_task_execution_id=first.id,
        binding_kind=BindingKind.RECOVERY,
    )
    assert mismatch.outcome is BudgetOutcome.IDENTITY_CONFLICT


def test_binding_rejects_attempts_outside_the_session_generation_and_task(r3a):
    _, _, db = r3a
    project, session, task, (first, second) = _graph(db, attempts=2)
    _workflow(db, second)
    other_task = Task(project_id=project.id, title="other", status=TaskStatus.PENDING)
    db.add(other_task)
    db.commit()
    other = _attempt(db, session, other_task, 5)
    later = _attempt(db, session, task, 3)

    def bind(child, parent):
        return bind_task_execution_attempt(
            db,
            task_execution_id=child.id,
            parent_task_execution_id=parent.id,
            binding_kind=BindingKind.RETRY,
        ).outcome

    # Earlier attempt number, a different Task, and a regenerated Session.
    assert bind(first, second) is BudgetOutcome.IDENTITY_CONFLICT
    assert bind(other, second) is BudgetOutcome.IDENTITY_CONFLICT
    session.instance_id = str(uuid.uuid4())
    db.commit()
    assert bind(later, second) is BudgetOutcome.IDENTITY_CONFLICT
    with pytest.raises(ValueError):
        bind_task_execution_attempt(
            db,
            task_execution_id=later.id,
            parent_task_execution_id=second.id,
            binding_kind=BindingKind.ROOT,
        )


def test_independent_workflows_remain_isolated(r3a):
    path, _, db = r3a
    _, _, _, (a,) = _graph(db)
    _, _, _, (b,) = _graph(db)
    wa, wbudget = _workflow(db, a), _workflow(db, b)
    assert wa.workflow_uuid != wbudget.workflow_uuid
    for _ in range(3):
        assert _reserve(db, wa, a).outcome is BudgetOutcome.RESERVED
    assert (
        _reserve(db, wa, a).outcome is BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED
    )
    assert _reserve(db, wbudget, b).outcome is BudgetOutcome.RESERVED
    # An attempt cannot spend another workflow's allowance.
    assert _reserve(db, wbudget, a).outcome is BudgetOutcome.IDENTITY_CONFLICT
    assert _counter(path, wbudget.workflow_uuid) == 1


def test_repeated_execution_never_inherits_a_closed_workflow(r3a):
    path, _, db = r3a
    _, session, task, (first,) = _graph(db)
    workflow = _workflow(db, first)
    retry = _attempt(db, session, task, 2)
    assert (
        bind_task_execution_attempt(
            db,
            task_execution_id=retry.id,
            parent_task_execution_id=first.id,
            binding_kind=BindingKind.RETRY,
        ).outcome
        is BudgetOutcome.BOUND
    )
    assert _reserve(db, workflow, first).outcome is BudgetOutcome.RESERVED
    assert close_workflow(db, workflow.workflow_uuid).outcome is BudgetOutcome.CLOSED
    assert (
        close_workflow(db, workflow.workflow_uuid).outcome
        is BudgetOutcome.ALREADY_CLOSED
    )
    assert _reserve(db, workflow, first).outcome is BudgetOutcome.WORKFLOW_CLOSED

    rerun = _attempt(db, session, task, 3)
    inherited = bind_task_execution_attempt(
        db,
        task_execution_id=rerun.id,
        parent_task_execution_id=retry.id,
        binding_kind=BindingKind.RETRY,
    )
    assert inherited.outcome is BudgetOutcome.WORKFLOW_CLOSED
    # An attempt already owned by a workflow can never root a second one.
    assert (
        create_task_execution_workflow(
            db, POLICY, root_task_execution_id=retry.id
        ).outcome
        is BudgetOutcome.IDENTITY_CONFLICT
    )
    fresh = _workflow(db, rerun)
    assert fresh.workflow_uuid != workflow.workflow_uuid
    assert fresh.reserved_requests == 0
    assert _counter(path, workflow.workflow_uuid) == 1


def test_legacy_attempts_stay_unbudgeted_and_queries_compatible(r3a):
    _, _, db = r3a
    _, session, task, (legacy,) = _graph(db)
    child = _attempt(db, session, task, 2)

    assert resolve_task_execution_workflow(db, legacy.id) is None
    assert (
        budget_enforcement_status(db, task_execution_id=legacy.id) == LEGACY_UNENFORCED
    )
    bound = bind_task_execution_attempt(
        db,
        task_execution_id=child.id,
        parent_task_execution_id=legacy.id,
        binding_kind=BindingKind.RETRY,
    )
    assert bound.outcome is BudgetOutcome.WORKFLOW_NOT_FOUND
    assert resolve_task_execution_workflow(db, child.id) is None

    _workflow(db, child)
    assert next_attempt_number(db, session.id, task.id) == 3
    assert latest_execution_for_session_task(db, session.id, task.id).id == child.id
    assert db.query(TaskExecution).filter_by(session_id=session.id).count() == 2
    assert db.get(SessionModel, session.id).task_executions[0].id == legacy.id


def test_duplicate_creation_is_idempotent_and_concurrency_safe(r3a):
    path, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    # Threads never touch the main Session's ORM instances.
    execution_id = execution.id
    claimants = 6
    barrier = threading.Barrier(claimants)
    outcomes = []

    def create():
        local = _session(engine)
        try:
            barrier.wait()
            outcomes.append(
                create_task_execution_workflow(
                    local, POLICY, root_task_execution_id=execution_id
                )
            )
        finally:
            local.close()

    threads = [threading.Thread(target=create) for _ in range(claimants)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    kinds = sorted(result.outcome.value for result in outcomes)
    assert kinds == ["ALREADY_EXISTS"] * (claimants - 1) + ["CREATED"]
    assert len({result.workflow.workflow_uuid for result in outcomes}) == 1
    assert _raw(path, "SELECT COUNT(*) FROM workflow_budgets") == [(1,)]
    changed = WorkflowBudgetPolicy(**{**POLICY.__dict__, "max_requests": 4})
    assert (
        create_task_execution_workflow(
            db, changed, root_task_execution_id=execution.id
        ).outcome
        is BudgetOutcome.IDENTITY_CONFLICT
    )


def test_planning_session_workflow_is_bound_to_its_generation(r3a):
    _, _, db = r3a
    project, _, _, (execution,) = _graph(db)
    planning = PlanningSession(project_id=project.id, title="plan", prompt="p")
    db.add(planning)
    db.commit()

    created = create_planning_session_workflow(
        db, PLANNING_POLICY, planning_session_id=planning.id
    )
    again = create_planning_session_workflow(
        db, PLANNING_POLICY, planning_session_id=planning.id
    )
    assert created.outcome is BudgetOutcome.CREATED
    assert again.outcome is BudgetOutcome.ALREADY_EXISTS
    assert again.workflow == created.workflow
    assert resolve_planning_session_workflow(db, planning.id) == created.workflow
    workflow = created.workflow
    assert _reserve(db, workflow, None).outcome is BudgetOutcome.RESERVED
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.IDENTITY_CONFLICT
    with pytest.raises(InvalidBudgetPolicy):
        create_planning_session_workflow(db, POLICY, planning_session_id=planning.id)

    # A different generation on the same integer id is a different workflow.
    db.query(PlanningSession).filter_by(id=planning.id).update(
        {"generation_id": str(uuid.uuid4())}
    )
    db.commit()
    assert resolve_planning_session_workflow(db, planning.id) is None


# Reservation ------------------------------------------------------------------


def test_budget_boundary_first_final_and_rejected_reservations(r3a):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)

    results = [_reserve(db, workflow, execution) for _ in range(3)]
    rejected = _reserve(db, workflow, execution)

    assert [r.outcome for r in results] == [BudgetOutcome.RESERVED] * 3
    assert [r.reservation.request_ordinal for r in results] == [1, 2, 3]
    assert len({r.reservation.reservation_uuid for r in results}) == 3
    assert rejected.outcome is BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED
    assert rejected.reservation is None and not rejected.reserved
    assert _counter(path, workflow.workflow_uuid) == 3
    assert [row[0] for row in _reservation_rows(path, workflow.workflow_uuid)] == [
        1,
        2,
        3,
    ]
    accounting = inspect_workflow_accounting(db, workflow.workflow_uuid)
    assert accounting.consistent and accounting.reservation_count == 3
    # The database itself refuses a duplicate ordinal or an unconsumed row.
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(path) as raw:
            raw.execute(
                "INSERT INTO provider_request_reservations (reservation_uuid, "
                "workflow_uuid, request_ordinal, idempotency_key, stage, "
                "runtime_role, task_execution_id, status, reserved_at) VALUES "
                "(?, ?, 3, 'k', 's', 'r', ?, 'RESERVED', ?)",
                (
                    str(uuid.uuid4()),
                    workflow.workflow_uuid,
                    execution.id,
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
                ),
            )


def test_reservation_survives_reconnection(r3a):
    path, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reserved = _reserve(db, workflow, execution, key="stable-key")
    db.close()
    engine.dispose()

    fresh_engine = _engine(path)
    try:
        fresh = _session(fresh_engine)
        replay = _reserve(fresh, workflow, execution, key="stable-key")
        accounting = inspect_workflow_accounting(fresh, workflow.workflow_uuid)
        fresh.close()
    finally:
        fresh_engine.dispose()
    assert replay.outcome is BudgetOutcome.ALREADY_RESERVED
    assert replay.reservation == reserved.reservation
    assert accounting.reservation_count == 1 and accounting.consistent


def test_idempotency_key_never_double_consumes(r3a):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    first = _reserve(db, workflow, execution, key="op-1")
    replay = _reserve(db, workflow, execution, key="op-1")
    conflicting = _reserve(db, workflow, execution, key="op-1", stage="execution")

    assert first.outcome is BudgetOutcome.RESERVED
    assert replay.outcome is BudgetOutcome.ALREADY_RESERVED
    assert replay.reservation.reservation_uuid == first.reservation.reservation_uuid
    assert conflicting.outcome is BudgetOutcome.IDEMPOTENCY_CONFLICT
    assert _counter(path, workflow.workflow_uuid) == 1

    # A provider retry is a new POST: it needs a new key and a new allowance.
    retry = _reserve(
        db,
        workflow,
        execution,
        retry_of_reservation_uuid=first.reservation.reservation_uuid,
    )
    assert retry.outcome is BudgetOutcome.RESERVED
    assert retry.reservation.request_ordinal == 2
    assert (
        retry.reservation.retry_of_reservation_uuid
        == first.reservation.reservation_uuid
    )
    assert _counter(path, workflow.workflow_uuid) == 2
    _reserve(db, workflow, execution)
    # Replay still answers after exhaustion, without consuming.
    assert _reserve(db, workflow, execution, key="op-1").reserved
    assert _counter(path, workflow.workflow_uuid) == 3


def test_retry_correlation_must_stay_inside_the_workflow(r3a):
    _, _, db = r3a
    _, _, _, (a,) = _graph(db)
    _, _, _, (b,) = _graph(db)
    wa, wb_ = _workflow(db, a), _workflow(db, b)
    foreign = _reserve(db, wb_, b)
    result = _reserve(
        db, wa, a, retry_of_reservation_uuid=foreign.reservation.reservation_uuid
    )
    assert result.outcome is BudgetOutcome.IDENTITY_CONFLICT
    assert inspect_workflow_accounting(db, wa.workflow_uuid).reservation_count == 0


@pytest.mark.parametrize("claimants", [2, 8])
def test_concurrent_final_slot_has_exactly_one_winner(r3a, claimants):
    path, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    _reserve(db, workflow, execution)
    _reserve(db, workflow, execution)
    # Threads never touch the main Session's ORM instances.
    execution_id = execution.id
    barrier = threading.Barrier(claimants)
    outcomes = []

    def contend():
        local = _session(engine)
        try:
            barrier.wait()
            outcomes.append(
                reserve_provider_request(
                    local,
                    workflow_uuid=workflow.workflow_uuid,
                    idempotency_key=new_idempotency_key(),
                    stage="planning",
                    runtime_role="planning",
                    task_execution_id=execution_id,
                ).outcome
            )
        finally:
            local.close()

    threads = [threading.Thread(target=contend) for _ in range(claimants)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(BudgetOutcome.RESERVED) == 1
    assert outcomes.count(BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED) == (
        claimants - 1
    )
    assert _counter(path, workflow.workflow_uuid) == 3
    # An independent raw connection observes the same count and ledger.
    assert len(_reservation_rows(path, workflow.workflow_uuid)) == 3


def _process_reserve(path: str, workflow_uuid: str, execution_id: int, barrier, queue):
    """Separate-process claimant with its own engine and connection pool."""

    engine = _engine(Path(path))
    db = _session(engine)
    try:
        barrier.wait()
        outcomes = []
        for _ in range(3):
            outcomes.append(
                reserve_provider_request(
                    db,
                    workflow_uuid=workflow_uuid,
                    idempotency_key=new_idempotency_key(),
                    stage="execution",
                    runtime_role="execution",
                    task_execution_id=execution_id,
                ).outcome.value
            )
        queue.put(outcomes)
    finally:
        db.close()
        engine.dispose()


def test_separate_processes_cannot_exceed_a_shared_budget(tmp_path):
    path = tmp_path / "processes.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_059,))
    db = _session(engine)
    policy = WorkflowBudgetPolicy(**{**POLICY.__dict__, "max_requests": 5})
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution, policy)
    db.close()
    engine.dispose()

    context = multiprocessing.get_context("spawn")
    processes = 4
    barrier = context.Barrier(processes)
    queue = context.Queue()
    workers = [
        context.Process(
            target=_process_reserve,
            args=(str(path), workflow.workflow_uuid, execution.id, barrier, queue),
        )
        for _ in range(processes)
    ]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=120) for _ in range(processes)]
    for worker in workers:
        worker.join(timeout=120)
        assert worker.exitcode == 0

    flat = [outcome for outcomes in results for outcome in outcomes]
    assert flat.count("RESERVED") == 5
    assert flat.count("PROVIDER_REQUEST_BUDGET_EXHAUSTED") == processes * 3 - 5
    assert _counter(path, workflow.workflow_uuid) == 5
    assert [row[0] for row in _reservation_rows(path, workflow.workflow_uuid)] == [
        1,
        2,
        3,
        4,
        5,
    ]


def test_transaction_failure_during_reservation_consumes_nothing(r3a, monkeypatch):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)

    def fail(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(wb, "_load_reservation", fail)
    with pytest.raises(OperationalError):
        _reserve(db, workflow, execution, key="op")
    monkeypatch.undo()
    assert _counter(path, workflow.workflow_uuid) == 0
    assert _reservation_rows(path, workflow.workflow_uuid) == []
    assert _reserve(db, workflow, execution, key="op").outcome is BudgetOutcome.RESERVED


def test_uncertain_commit_that_committed_is_reconciled_and_replay_is_free(
    r3a, monkeypatch
):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    real_commit = wb._commit

    def commit_then_fail(connection):
        real_commit(connection)
        raise _commit_error()

    monkeypatch.setattr(wb, "_commit", commit_then_fail)
    uncertain = _reserve(db, workflow, execution, key="op")
    monkeypatch.undo()
    assert uncertain.outcome is BudgetOutcome.RESERVED and uncertain.reconciled
    replay = _reserve(db, workflow, execution, key="op")
    assert replay.outcome is BudgetOutcome.ALREADY_RESERVED
    assert _counter(path, workflow.workflow_uuid) == 1


def test_uncertain_commit_that_rolled_back_is_retried_with_the_same_key(
    r3a, monkeypatch
):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)

    def fail_without_commit(connection):
        raise _commit_error()

    monkeypatch.setattr(wb, "_commit", fail_without_commit)
    uncertain = _reserve(db, workflow, execution, key="op")
    monkeypatch.undo()
    assert uncertain.outcome is BudgetOutcome.COMMIT_UNCERTAIN
    assert not uncertain.reserved
    assert _counter(path, workflow.workflow_uuid) == 0
    retried = _reserve(db, workflow, execution, key="op")
    assert retried.outcome is BudgetOutcome.RESERVED
    assert _counter(path, workflow.workflow_uuid) == 1


def test_locked_database_fails_closed_as_busy(r3a, tmp_path):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    short_engine = _engine(path, timeout=0.2)
    short = _session(short_engine)
    with sqlite3.connect(path, isolation_level=None) as holder:
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = _reserve(short, workflow, execution)
        finally:
            holder.execute("ROLLBACK")
    short.close()
    short_engine.dispose()
    assert result.outcome is BudgetOutcome.DATABASE_BUSY
    assert _counter(path, workflow.workflow_uuid) == 0


def test_service_refuses_unverified_or_unsupported_schema(tmp_path):
    memory = create_engine("sqlite://")
    Base.metadata.create_all(bind=memory)
    db = _session(memory)
    _, _, _, (execution,) = _graph(db)
    assert (
        create_task_execution_workflow(
            db, POLICY, root_task_execution_id=execution.id
        ).outcome
        is BudgetOutcome.UNSUPPORTED_DATABASE
    )
    db.close()

    path = tmp_path / "unmigrated.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    db = _session(engine)
    _, _, _, (execution,) = _graph(db)
    assert (
        create_task_execution_workflow(
            db, POLICY, root_task_execution_id=execution.id
        ).outcome
        is BudgetOutcome.SCHEMA_UNAVAILABLE
    )
    with pytest.raises(wb.WorkflowBudgetSchemaUnavailable):
        resolve_task_execution_workflow(db, execution.id)
    db.close()
    engine.dispose()


# Deadline ---------------------------------------------------------------------


def test_deadline_is_persisted_and_never_extended(r3a, monkeypatch):
    path, _, db = r3a
    _, session, task, (first,) = _graph(db)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    _clock(monkeypatch, start)
    workflow = _workflow(db, first)
    assert workflow.created_at == start
    assert workflow.deadline_at == start + timedelta(seconds=600)
    assert _raw(
        path,
        "SELECT created_at, deadline_at FROM workflow_budgets WHERE workflow_uuid = ?",
        (workflow.workflow_uuid,),
    ) == [("2030-01-01 12:00:00.000000", "2030-01-01 12:10:00.000000")]

    _clock(monkeypatch, start + timedelta(seconds=500))
    retry = _attempt(db, session, task, 2)
    bind_task_execution_attempt(
        db,
        task_execution_id=retry.id,
        parent_task_execution_id=first.id,
        binding_kind=BindingKind.RECOVERY,
    )
    again = create_task_execution_workflow(db, POLICY, root_task_execution_id=first.id)
    assert again.workflow.deadline_at == workflow.deadline_at
    assert (
        resolve_task_execution_workflow(db, retry.id).deadline_at
        == workflow.deadline_at
    )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with sqlite3.connect(path) as raw:
            raw.execute(
                "UPDATE workflow_budgets SET deadline_at = '2031-01-01 00:00:00.000000'"
            )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with sqlite3.connect(path) as raw:
            raw.execute(
                "UPDATE workflow_budgets SET reserved_requests = 0, max_requests = 99"
            )


@pytest.mark.parametrize("offset", [0, 1, 3600])
def test_expired_deadline_rejects_without_consuming(r3a, monkeypatch, offset):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    _clock(monkeypatch, start)
    workflow = _workflow(db, execution)
    _clock(monkeypatch, workflow.deadline_at + timedelta(seconds=offset))
    result = _reserve(db, workflow, execution)
    assert result.outcome is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    assert _counter(path, workflow.workflow_uuid) == 0
    assert _reservation_rows(path, workflow.workflow_uuid) == []


def test_reservation_just_before_deadline_succeeds(r3a, monkeypatch):
    _, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    _clock(monkeypatch, datetime(2030, 1, 1, tzinfo=timezone.utc))
    workflow = _workflow(db, execution)
    _clock(monkeypatch, workflow.deadline_at - timedelta(microseconds=1))
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED


def test_deadline_reached_while_holding_the_lock_rolls_back(r3a, monkeypatch):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    _clock(monkeypatch, datetime(2030, 1, 1, tzinfo=timezone.utc))
    workflow = _workflow(db, execution)
    instants = [workflow.deadline_at - timedelta(seconds=1), workflow.deadline_at]
    monkeypatch.setattr(wb, "_trusted_utc_now", lambda: instants.pop(0))
    result = _reserve(db, workflow, execution)
    assert result.outcome is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    assert _counter(path, workflow.workflow_uuid) == 0


def test_clock_is_sampled_only_after_the_write_lock_is_held(r3a, monkeypatch):
    _, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, *_):
        statements.append(statement)

    def now():
        assert "BEGIN IMMEDIATE" in statements
        return datetime.now(timezone.utc)

    monkeypatch.setattr(wb, "_trusted_utc_now", now)
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED
    event.remove(engine, "before_cursor_execute", _record)


def test_caller_cannot_supply_time_or_bypass_the_deadline(r3a, monkeypatch):
    path, _, db = r3a
    for function in (
        reserve_provider_request,
        mark_dispatch_started,
        record_reservation_outcome,
        reconcile_stale_reservations,
        create_task_execution_workflow,
        create_planning_session_workflow,
    ):
        names = set(inspect.signature(function).parameters)
        assert not names & {"now", "observed_at", "reserved_at", "deadline_at", "clock"}

    _, _, _, (execution,) = _graph(db)
    _clock(monkeypatch, datetime(2030, 1, 1, tzinfo=timezone.utc))
    workflow = _workflow(db, execution)
    # Even a raw writer that consumes the counter cannot insert at/after deadline.
    with pytest.raises(sqlite3.DatabaseError, match="not consumed"):
        with sqlite3.connect(path) as raw:
            raw.execute("PRAGMA foreign_keys=ON")
            raw.execute(
                "UPDATE workflow_budgets SET reserved_requests = 1 "
                "WHERE workflow_uuid = ?",
                (workflow.workflow_uuid,),
            )
            raw.execute(
                "INSERT INTO provider_request_reservations (reservation_uuid, "
                "workflow_uuid, request_ordinal, idempotency_key, stage, "
                "runtime_role, task_execution_id, status, reserved_at) VALUES "
                "(?, ?, 1, 'k', 's', 'r', ?, 'RESERVED', '2030-01-01 00:10:00.000000')",
                (str(uuid.uuid4()), workflow.workflow_uuid, execution.id),
            )
    assert _counter(path, workflow.workflow_uuid) == 0


def test_dispatch_start_after_deadline_is_refused(r3a, monkeypatch):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    _clock(monkeypatch, datetime(2030, 1, 1, tzinfo=timezone.utc))
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    _clock(monkeypatch, workflow.deadline_at - timedelta(seconds=30))
    other = _reserve(db, workflow, execution).reservation
    started = mark_dispatch_started(db, other.reservation_uuid)
    assert started.dispatch_permitted and started.remaining_seconds == 30.0

    _clock(monkeypatch, workflow.deadline_at)
    late = mark_dispatch_started(db, reservation.reservation_uuid)
    assert late.outcome is BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
    assert not late.dispatch_permitted
    assert _reservation_rows(path, workflow.workflow_uuid)[0][1] == "RESERVED"


# Lifecycle --------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal",
    [
        ReservationStatus.COMPLETED,
        ReservationStatus.FAILED,
        ReservationStatus.TIMED_OUT,
        ReservationStatus.CANCELLED,
        ReservationStatus.OUTCOME_UNKNOWN,
    ],
)
def test_valid_dispatch_lifecycle_and_idempotent_terminal(r3a, terminal):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation

    started = mark_dispatch_started(db, reservation.reservation_uuid)
    assert started.outcome is BudgetOutcome.TRANSITIONED and started.dispatch_permitted
    again = mark_dispatch_started(db, reservation.reservation_uuid)
    assert again.outcome is BudgetOutcome.ALREADY_IN_STATE
    assert not again.dispatch_permitted

    done = record_reservation_outcome(
        db, reservation.reservation_uuid, terminal, outcome_code="first"
    )
    assert done.outcome is BudgetOutcome.TRANSITIONED
    assert done.reservation.status == terminal.value
    assert done.reservation.completed_at is not None
    repeat = record_reservation_outcome(
        db, reservation.reservation_uuid, terminal, outcome_code="second"
    )
    assert repeat.outcome is BudgetOutcome.ALREADY_IN_STATE
    assert repeat.reservation.outcome_code == "first"
    assert _counter(path, workflow.workflow_uuid) == 1


def test_unstarted_reservation_may_only_be_cancelled(r3a):
    _, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    completed = record_reservation_outcome(
        db, reservation.reservation_uuid, ReservationStatus.COMPLETED
    )
    assert completed.outcome is BudgetOutcome.INVALID_TRANSITION
    cancelled = record_reservation_outcome(
        db, reservation.reservation_uuid, ReservationStatus.CANCELLED
    )
    assert cancelled.outcome is BudgetOutcome.TRANSITIONED
    assert cancelled.reservation.dispatch_started_at is None
    late = mark_dispatch_started(db, reservation.reservation_uuid)
    assert late.outcome is BudgetOutcome.TERMINAL_CONFLICT
    with pytest.raises(ValueError):
        record_reservation_outcome(
            db, reservation.reservation_uuid, ReservationStatus.DISPATCH_STARTED
        )
    assert (
        mark_dispatch_started(db, str(uuid.uuid4())).outcome
        is BudgetOutcome.RESERVATION_NOT_FOUND
    )


def test_terminal_outcomes_are_never_overwritten(r3a):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    mark_dispatch_started(db, reservation.reservation_uuid)
    record_reservation_outcome(
        db, reservation.reservation_uuid, ReservationStatus.COMPLETED
    )
    stale = record_reservation_outcome(
        db, reservation.reservation_uuid, ReservationStatus.FAILED
    )
    assert stale.outcome is BudgetOutcome.TERMINAL_CONFLICT
    assert stale.reservation.status == "COMPLETED"
    for sql in (
        "UPDATE provider_request_reservations SET status = 'FAILED'",
        "UPDATE provider_request_reservations SET status = 'DISPATCH_STARTED', "
        "completed_at = NULL",
        "UPDATE provider_request_reservations SET stage = 'other'",
        "DELETE FROM provider_request_reservations",
        "DELETE FROM workflow_budgets",
        "DELETE FROM workflow_task_execution_bindings",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            with sqlite3.connect(path) as raw:
                raw.execute(sql)
    assert _reservation_rows(path, workflow.workflow_uuid) == [(1, "COMPLETED", None)]
    with pytest.raises(RuntimeError):
        db.delete(db.get(ProviderRequestReservation, reservation.reservation_uuid))
        db.flush()
    db.rollback()


def test_concurrent_dispatch_start_permits_exactly_one_sender(r3a):
    _, engine, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    barrier = threading.Barrier(6)
    results = []

    def start():
        local = _session(engine)
        try:
            barrier.wait()
            results.append(mark_dispatch_started(local, reservation.reservation_uuid))
        finally:
            local.close()

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result.dispatch_permitted for result in results) == 1


def test_uncertain_dispatch_marker_never_permits_a_send(r3a, monkeypatch):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    real_commit = wb._commit

    def commit_then_fail(connection):
        real_commit(connection)
        raise _commit_error()

    monkeypatch.setattr(wb, "_commit", commit_then_fail)
    result = mark_dispatch_started(db, reservation.reservation_uuid)
    monkeypatch.undo()
    assert result.outcome is BudgetOutcome.COMMIT_UNCERTAIN
    assert not result.dispatch_permitted
    assert _reservation_rows(path, workflow.workflow_uuid)[0][1] == "DISPATCH_STARTED"


def test_stale_reconciliation_is_conservative_and_repeatable(r3a, monkeypatch):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    _clock(monkeypatch, start)
    workflow = _workflow(db, execution)
    unstarted = _reserve(db, workflow, execution).reservation
    dispatched = _reserve(db, workflow, execution).reservation
    mark_dispatch_started(db, dispatched.reservation_uuid)
    _clock(monkeypatch, start + timedelta(seconds=200))
    fresh = _reserve(db, workflow, execution).reservation

    _clock(monkeypatch, start + timedelta(seconds=250))
    report = reconcile_stale_reservations(db, stale_after_seconds=100)
    assert report.outcome is BudgetOutcome.RECONCILED
    assert report.cancelled_unstarted == (unstarted.reservation_uuid,)
    assert report.marked_outcome_unknown == (dispatched.reservation_uuid,)
    assert _reservation_rows(path, workflow.workflow_uuid) == [
        (1, "CANCELLED", wb.STALE_NOT_DISPATCHED),
        (2, "OUTCOME_UNKNOWN", wb.STALE_DISPATCH_AMBIGUOUS),
        (3, "RESERVED", None),
    ]
    # No refund: the ambiguous allowance stays consumed and the budget is full.
    assert _counter(path, workflow.workflow_uuid) == 3
    assert inspect_workflow_accounting(db, workflow.workflow_uuid).consistent
    assert (
        _reserve(db, workflow, execution).outcome
        is BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED
    )
    # A late Worker update cannot overwrite the reconciled outcome.
    assert (
        record_reservation_outcome(
            db, dispatched.reservation_uuid, ReservationStatus.COMPLETED
        ).outcome
        is BudgetOutcome.TERMINAL_CONFLICT
    )
    assert (
        mark_dispatch_started(db, unstarted.reservation_uuid).outcome
        is BudgetOutcome.TERMINAL_CONFLICT
    )
    repeat = reconcile_stale_reservations(db, stale_after_seconds=100)
    assert repeat.cancelled_unstarted == () and repeat.marked_outcome_unknown == ()
    assert _reservation_rows(path, workflow.workflow_uuid)[2] == (3, "RESERVED", None)
    with pytest.raises(ValueError):
        reconcile_stale_reservations(db, stale_after_seconds=0)


def test_budget_module_has_no_provider_or_network_path(r3a, monkeypatch):
    path, _, db = r3a
    source = Path(wb.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"httpx", "requests", "socket", "subprocess", "celery"}
    assert not any("agents" in name or "runtime" in name for name in imported)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)
    reservation = _reserve(db, workflow, execution).reservation
    mark_dispatch_started(db, reservation.reservation_uuid)
    reconcile_stale_reservations(db, stale_after_seconds=1)
    assert _counter(path, workflow.workflow_uuid) == 1

    # No application module outside the tests imports the service yet.
    app_root = Path(wb.__file__).resolve().parents[2]

    def imports_service(candidate: Path) -> bool:
        for node in ast.walk(ast.parse(candidate.read_text())):
            if isinstance(node, ast.Import) and any(
                alias.name == wb.__name__ for alias in node.names
            ):
                return True
            if isinstance(node, ast.ImportFrom) and (
                node.module == wb.__name__
                or (
                    node.module == "app.services.execution"
                    and any(alias.name == "workflow_budget" for alias in node.names)
                )
            ):
                return True
        return False

    callers = [
        str(candidate.relative_to(app_root))
        for candidate in app_root.rglob("*.py")
        if "tests" not in candidate.parts and imports_service(candidate)
    ]
    assert callers == []


# Policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_requests": -1},
        {"max_requests": 0},
        {"max_requests": True},
        {"max_requests": 1001},
        {"max_duration_seconds": 0},
        {"max_duration_seconds": -5},
        {"max_duration_seconds": 1.5},
        {"max_duration_seconds": 10**12},
        {"policy_id": ""},
        {"policy_id": None},
        {"policy_version": ""},
        {"workflow_kind": "TASK_EXECUTION"},
        {"workflow_kind": "DISCOVERY"},
        {"enforcement_mode": "ENFORCED"},
    ],
)
def test_invalid_policies_are_rejected(overrides):
    policy = WorkflowBudgetPolicy(**{**POLICY.__dict__, **overrides})
    with pytest.raises(InvalidBudgetPolicy):
        wb.validate_budget_policy(policy)


def test_disabled_policy_never_creates_or_reports_enforcement(r3a):
    path, _, db = r3a
    _, _, _, (execution,) = _graph(db)
    assert R2_CANDIDATE_POLICY.enforcement_mode is EnforcementMode.DISABLED
    assert (
        R2_CANDIDATE_POLICY.max_requests,
        R2_CANDIDATE_POLICY.max_duration_seconds,
    ) == (
        24,
        1800,
    )
    with pytest.raises(BudgetEnforcementDisabled):
        create_task_execution_workflow(
            db, R2_CANDIDATE_POLICY, root_task_execution_id=execution.id
        )
    assert _raw(path, "SELECT COUNT(*) FROM workflow_budgets") == [(0,)]
    assert (
        budget_enforcement_status(db, task_execution_id=execution.id)
        == LEGACY_UNENFORCED
    )
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(path) as raw:
            raw.execute("UPDATE workflow_budgets SET enforcement_mode = 'DISABLED'")
            raw.execute(
                "INSERT INTO workflow_budgets (workflow_uuid, workflow_kind, project_id, "
                "planning_session_id, planning_generation_id, policy_id, policy_version, "
                "enforcement_mode, max_requests, max_duration_seconds, reserved_requests, "
                "status, created_at, deadline_at) VALUES ('x', 'PLANNING_SESSION', 1, 1, "
                "'g', 'p', '1', 'DISABLED', 1, 1, 0, 'OPEN', "
                "'2030-01-01 00:00:00.000000', '2030-01-01 00:00:01.000000')"
            )


# Crash boundaries (real process death) ---------------------------------------


def _crash_child(path: str, workflow_uuid: str, execution_id: int, boundary: str):
    engine = _engine(Path(path))
    db = _session(engine)
    real_commit = wb._commit

    def reserve():
        return reserve_provider_request(
            db,
            workflow_uuid=workflow_uuid,
            idempotency_key="crash-op",
            stage="execution",
            runtime_role="execution",
            task_execution_id=execution_id,
        )

    if boundary == "before_reservation_commit":
        wb._commit = lambda connection: os._exit(17)
        reserve()
    elif boundary == "after_reservation_commit":
        wb._commit = lambda connection: (real_commit(connection), os._exit(17))
        reserve()
    elif boundary == "after_dispatch_marker":
        mark_dispatch_started(db, reserve().reservation.reservation_uuid)
        os._exit(17)
    elif boundary == "before_terminal_commit":
        reservation = reserve().reservation
        mark_dispatch_started(db, reservation.reservation_uuid)
        wb._commit = lambda connection: os._exit(17)
        record_reservation_outcome(
            db, reservation.reservation_uuid, ReservationStatus.COMPLETED
        )
    os._exit(3)


@pytest.mark.parametrize(
    ("boundary", "durable", "reconciled"),
    [
        ("before_reservation_commit", [], []),
        ("after_reservation_commit", ["RESERVED"], ["CANCELLED"]),
        ("after_dispatch_marker", ["DISPATCH_STARTED"], ["OUTCOME_UNKNOWN"]),
        ("before_terminal_commit", ["DISPATCH_STARTED"], ["OUTCOME_UNKNOWN"]),
    ],
)
def test_process_loss_never_creates_budget_capacity(
    tmp_path, monkeypatch, boundary, durable, reconciled
):
    path = tmp_path / "crash.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_059,))
    db = _session(engine)
    _, _, _, (execution,) = _graph(db)
    workflow = _workflow(db, execution)

    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=_crash_child,
        args=(str(path), workflow.workflow_uuid, execution.id, boundary),
    )
    child.start()
    child.join(timeout=120)
    assert child.exitcode == 17

    assert [
        row[1] for row in _reservation_rows(path, workflow.workflow_uuid)
    ] == durable
    assert _counter(path, workflow.workflow_uuid) == len(durable)
    _clock(monkeypatch, datetime.now(timezone.utc) + timedelta(seconds=120))
    reconcile_stale_reservations(db, stale_after_seconds=60)
    assert [
        row[1] for row in _reservation_rows(path, workflow.workflow_uuid)
    ] == reconciled
    monkeypatch.undo()

    # Replaying the crashed operation's key never consumes a second allowance.
    replay = reserve_provider_request(
        db,
        workflow_uuid=workflow.workflow_uuid,
        idempotency_key="crash-op",
        stage="execution",
        runtime_role="execution",
        task_execution_id=execution.id,
    )
    assert replay.reserved
    granted = 0
    while _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED:
        granted += 1
    assert granted == POLICY.max_requests - 1
    accounting = inspect_workflow_accounting(db, workflow.workflow_uuid)
    assert accounting.reservation_count == POLICY.max_requests and accounting.consistent
    db.close()
    engine.dispose()


# Migration --------------------------------------------------------------------


def test_existing_schema_upgrades_and_preserves_records(tmp_path):
    path = tmp_path / "legacy.db"
    engine = _engine(path)
    legacy_tables = [t for t in Base.metadata.sorted_tables if t not in NEW_TABLES]
    Base.metadata.create_all(bind=engine, tables=legacy_tables)
    run_schema_migrations(
        engine, tuple(m for m in MIGRATIONS if m is not MIGRATION_059)
    )
    db = _session(engine)
    _, _, _, (execution,) = _graph(db)
    execution_id = execution.id
    before = _raw(path, "SELECT * FROM task_executions") + _raw(
        path, "SELECT * FROM sessions"
    )
    db.close()
    assert not {"workflow_budgets"} & set(
        row[0] for row in _raw(path, "SELECT name FROM sqlite_master")
    )

    run_schema_migrations(engine)
    db = _session(engine)
    assert (
        _raw(path, "SELECT * FROM task_executions")
        + _raw(path, "SELECT * FROM sessions")
        == before
    )
    assert _raw(
        path,
        "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
        (MIGRATION_059.version,),
    ) == [(1,)]
    _verify_059_workflow_provider_request_budget(engine)
    assert resolve_task_execution_workflow(db, execution_id) is None
    execution = db.get(TaskExecution, execution_id)
    workflow = _workflow(db, execution)
    assert _reserve(db, workflow, execution).outcome is BudgetOutcome.RESERVED
    db.close()
    engine.dispose()


def test_fresh_initialization_and_replay_are_stable(tmp_path):
    path = tmp_path / "fresh.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    schema = _raw(path, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    run_schema_migrations(engine)
    assert (
        _raw(path, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        == schema
    )
    assert _raw(
        path,
        "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
        (MIGRATION_059.version,),
    ) == [(1,)]
    engine.dispose()


def test_failed_upgrade_rolls_back_without_recording(tmp_path, monkeypatch):
    path = tmp_path / "failed.db"
    engine = _engine(path)
    legacy_tables = [t for t in Base.metadata.sorted_tables if t not in NEW_TABLES]
    Base.metadata.create_all(bind=engine, tables=legacy_tables)
    real = db_migrations._059_trigger_statements

    def broken():
        return {**real(), "trg_broken": "CREATE TRIGGER trg_broken BEFORE NOPE"}

    monkeypatch.setattr(db_migrations, "_059_trigger_statements", broken)
    with pytest.raises(Exception):
        run_schema_migrations(engine, (MIGRATION_059,))
    names = {row[0] for row in _raw(path, "SELECT name FROM sqlite_master")}
    assert not names & {t.name for t in NEW_TABLES}
    assert not any(name.startswith("trg_workflow") for name in names)
    assert _raw(path, "SELECT COUNT(*) FROM schema_migrations") == [(0,)]

    monkeypatch.undo()
    run_schema_migrations(engine, (MIGRATION_059,))
    _verify_059_workflow_provider_request_budget(engine)
    engine.dispose()


def test_replay_rejects_a_weakened_schema(r3a):
    path, engine, _ = r3a
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TRIGGER trg_provider_reservation_no_delete")
    with pytest.raises(RuntimeError, match="059"):
        run_schema_migrations(engine, (MIGRATION_059,))
    assert wb._schema_outcome(engine) is BudgetOutcome.SCHEMA_UNAVAILABLE


def test_guards_reference_only_r3a_tables_so_partial_schemas_stay_alterable(tmp_path):
    for sql in db_migrations._059_trigger_statements().values():
        referenced = set(re.findall(r"\b(?:FROM|JOIN|ON)\s+(\w+)", sql, re.I))
        assert referenced <= {t.name for t in NEW_TABLES}, referenced

    # A partial historical schema without the core tables still upgrades, and
    # SQLite's whole-schema re-validation on ALTER TABLE keeps working.
    engine = _engine(tmp_path / "partial.db")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, x TEXT)"
        )
    run_schema_migrations(engine, (MIGRATION_059,))
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE tasks DROP COLUMN x")
    engine.dispose()
