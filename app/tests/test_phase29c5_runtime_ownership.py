"""Focused Phase 29C-5 worker receipt and runtime ownership tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskRuntimeLease,
    ExecutionTaskTransition,
    TaskExecution,
)
from app.services.execution.execution_task_dispatch_service import (
    DISPATCH_STATUS_PENDING,
    ExecutionTaskDispatchService,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    AcquireRuntimeOwnershipCommand,
    ExecutionRuntimeOwnershipError,
    ExecutionTaskRuntimeOwnershipService,
    HeartbeatRuntimeOwnershipCommand,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.tasks import worker as worker_tasks

from test_phase29c2_execution_eligibility import _build_context
from test_phase29c3_scheduler_claim import _ready_root
from test_phase29c4_dispatch_intent_attempt import _created


def _submitted(db, task):
    dispatch_service, claim, created = _created(
        db,
        task,
        publisher=lambda broker_id, payload, task_name: type(
            "BrokerResult", (), {"id": broker_id}
        )(),
    )
    dispatch_service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_PENDING,
        f"submit-{created.intent.id}",
    )
    return dispatch_service, claim, created


def _ownership_command(task, created, *, key="runtime-start-1", **overrides):
    command = AcquireRuntimeOwnershipCommand(
        dispatch_intent_id=created.intent.id,
        execution_task_attempt_id=created.attempt.id,
        execution_task_id=task.id,
        broker_task_id=created.broker_task_id,
        worker_id="celery@worker-a",
        worker_hostname="worker-a",
        worker_pid=1234,
        worker_process_start_identity="process-start-1",
        worker_instance_id="worker-instance-1",
        ownership_idempotency_key=key,
    )
    return command.__class__(**{**command.__dict__, **overrides})


def _start(db, task, created, **overrides):
    command = _ownership_command(task, created, **overrides)
    result = ExecutionTaskRuntimeOwnershipService(db).acquire(command)
    db.commit()
    return result, command


def test_submitted_attempt_acquires_runtime_ownership_and_starts_once(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    dispatch_service, _, created = _created(
        db_session,
        task,
        publisher=lambda broker_id, payload, task_name: type(
            "BrokerResult", (), {"id": broker_id}
        )(),
    )
    dispatch_service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_PENDING,
        "submit-for-runtime-start",
    )

    result = ExecutionTaskRuntimeOwnershipService(db_session).acquire(
        AcquireRuntimeOwnershipCommand(
            dispatch_intent_id=created.intent.id,
            execution_task_attempt_id=created.attempt.id,
            execution_task_id=task.id,
            broker_task_id=created.broker_task_id,
            worker_id="celery@worker-a",
            worker_hostname="worker-a",
            worker_pid=1234,
            worker_process_start_identity="process-start-1",
            worker_instance_id="worker-instance-1",
            ownership_idempotency_key="runtime-start-1",
        )
    )
    db_session.commit()

    assert result.replayed is False
    assert result.lease.ownership_fencing_token == 1
    assert result.transition.to_state == "running"
    assert task.status == "running"
    assert task.state_version == 2
    assert created.attempt.attempt_status == "running"
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 1
    assert db_session.query(TaskExecution).count() == 0
    assert db_session.query(ExecutionTaskAttempt).one().started_at is not None


def test_worker_receipt_validates_payload_and_starts_without_runtime_side_effects(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)

    result = ExecutionTaskRuntimeOwnershipService(db_session).receive_worker_dispatch(
        dict(created.intent.worker_command_payload),
        created.broker_task_id,
        worker_id="celery@worker-a",
        worker_hostname="worker-a",
        worker_pid=4321,
        worker_process_start_identity="process-start-receipt",
        worker_instance_id="worker-instance-receipt",
    )
    db_session.commit()

    assert result.lease.worker_instance_id == "worker-instance-receipt"
    assert result.lease.worker_hostname == "worker-a"
    assert result.lease.worker_pid == 4321
    assert result.lease.worker_process_start_identity == "process-start-receipt"
    assert result.lease.runtime_start_evidence["status"] == "RUNTIME_OWNERSHIP_ACQUIRED"
    assert db_session.query(TaskExecution).count() == 0


def test_celery_worker_entry_returns_acquired_then_replayed(monkeypatch, db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    monkeypatch.setattr(worker_tasks, "get_db_session", lambda: db_session)
    payload = dict(created.intent.worker_command_payload)
    first = worker_tasks.receive_execution_task_dispatch.__wrapped__(payload)
    replay = worker_tasks.receive_execution_task_dispatch.__wrapped__(payload)

    assert first["status"] == "RUNTIME_OWNERSHIP_ACQUIRED"
    assert replay["status"] == "RUNTIME_OWNERSHIP_REPLAYED"
    assert first["runtime_lease_id"] == replay["runtime_lease_id"]
    assert worker_tasks.receive_execution_task_dispatch.acks_late is True
    assert worker_tasks.receive_execution_task_dispatch.reject_on_worker_lost is True


def test_exact_replay_returns_original_lease_without_second_transition_or_fence(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    first, command = _start(db_session, task, created)
    transition_count = db_session.query(ExecutionTaskTransition).count()

    replay = ExecutionTaskRuntimeOwnershipService(db_session).acquire(command)
    db_session.commit()

    assert replay.replayed is True
    assert replay.lease.id == first.lease.id
    assert replay.lease.ownership_fencing_token == 1
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 1
    assert db_session.query(ExecutionTaskTransition).count() == transition_count


def test_competing_worker_is_rejected_before_lease_expiry(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    _start(db_session, task, created)

    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(
                task,
                created,
                key="runtime-start-worker-b",
                worker_id="celery@worker-b",
                worker_hostname="worker-b",
                worker_pid=5678,
                worker_process_start_identity="process-start-2",
                worker_instance_id="worker-instance-2",
            )
        )
    assert exc.value.code == "runtime_ownership_conflict"
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 1


def test_same_key_with_different_worker_is_an_idempotency_conflict(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    _start(db_session, task, created)

    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(
                task,
                created,
                worker_instance_id="worker-instance-other",
            )
        )
    assert exc.value.code == "runtime_ownership_idempotency_conflict"


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ("unknown_intent", "dispatch_intent_not_found"),
        ("unknown_attempt", "runtime_attempt_not_found"),
        ("wrong_broker", "broker_task_id_mismatch"),
        ("not_submitted", "dispatch_intent_not_submitted"),
        ("attempt_not_submitted", "runtime_attempt_not_submitted"),
        ("inactive_plan", "execution_plan_inactive"),
        ("not_ready", "task_not_ready"),
    ],
)
def test_worker_start_rejects_invalid_authority_without_runtime_owner(
    db_session, mutation, expected_code
):
    context = _build_context(db_session)
    task = _ready_root(context)
    dispatch_service, _, created = _created(
        db_session,
        task,
        publisher=lambda broker_id, payload, task_name: type(
            "BrokerResult", (), {"id": broker_id}
        )(),
    )
    if mutation not in {"not_submitted", "unknown_intent", "unknown_attempt"}:
        dispatch_service.submit_dispatch_intent(
            created.intent.id, DISPATCH_STATUS_PENDING, f"submit-{mutation}"
        )
    if mutation == "attempt_not_submitted":
        dispatch_service.submit_dispatch_intent(
            created.intent.id, DISPATCH_STATUS_PENDING, "submit-attempt"
        )
        created.attempt.attempt_status = "created"
    elif mutation == "inactive_plan":
        context["execution_plan"].status = "superseded"
    elif mutation == "not_ready":
        task.status = "blocked"

    command = _ownership_command(task, created)
    if mutation == "unknown_intent":
        command = _ownership_command(task, created, dispatch_intent_id=999999)
    elif mutation == "unknown_attempt":
        command = _ownership_command(task, created, execution_task_attempt_id=999999)
    elif mutation == "wrong_broker":
        command = _ownership_command(task, created, broker_task_id="wrong-broker")

    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(command)
    assert exc.value.code == expected_code
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0
    assert db_session.query(ExecutionTaskTransition).count() == 1


def test_tampered_worker_payload_is_rejected_before_start(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    payload = dict(created.intent.worker_command_payload)
    payload["execution_task_id"] = task.id + 1

    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).receive_worker_dispatch(
            payload,
            created.broker_task_id,
            worker_id="celery@worker-a",
            worker_hostname="worker-a",
            worker_pid=1234,
            worker_process_start_identity="process-start-1",
            worker_instance_id="worker-instance-1",
        )
    assert exc.value.code == "worker_payload_mismatch"
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0


def test_cancelled_intent_and_attempt_are_not_started(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    dispatch_service, _, created = _created(
        db_session,
        task,
        publisher=lambda broker_id, payload, task_name: type(
            "BrokerResult", (), {"id": broker_id}
        )(),
    )
    task.status = "blocked"
    cancelled = dispatch_service.submit_dispatch_intent(
        created.intent.id, DISPATCH_STATUS_PENDING, "cancel-before-runtime"
    )
    assert cancelled.error_code == "task_no_longer_ready"
    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(task, created)
        )
    assert exc.value.code == "dispatch_intent_cancelled"
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0


def test_stale_task_version_is_rejected_after_valid_lifecycle_changes(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state="ready",
            expected_state_version=task.state_version,
            to_state="blocked",
            reason_code="dependency_blocked",
            actor_type="test",
            actor_id="phase29c5",
            idempotency_key="phase29c5-block",
        )
    )
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state="blocked",
            expected_state_version=task.state_version,
            to_state="ready",
            reason_code="dependencies_satisfied",
            actor_type="test",
            actor_id="phase29c5",
            idempotency_key="phase29c5-ready-again",
        )
    )

    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(task, created)
        )
    assert exc.value.code == "task_version_stale"


def test_transition_failure_rolls_back_lease_attempt_and_task_start(
    db_session, monkeypatch
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)

    def fail_transition(*args, **kwargs):
        raise ExecutionTaskTransitionError("transition_state_stale", "simulated")

    monkeypatch.setattr(ExecutionTaskTransitionService, "transition", fail_transition)
    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(task, created)
        )
    assert exc.value.code == "runtime_start_transition_failed"
    db_session.rollback()
    db_session.expire_all()
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0
    assert db_session.query(ExecutionTaskTransition).count() == 1
    assert db_session.get(ExecutionTask, task.id).status == "ready"
    assert db_session.get(ExecutionTaskAttempt, created.attempt.id).attempt_status == (
        "submitted"
    )


def test_lease_insert_failure_is_bounded_and_leaves_authority_unchanged(
    db_session, monkeypatch
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    original_flush = db_session.flush
    calls = [0]

    def fail_first_flush(*args, **kwargs):
        if calls[0] == 0:
            calls[0] += 1
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("runtime lease", {}, Exception("duplicate"))
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fail_first_flush)
    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(
            _ownership_command(task, created)
        )
    assert exc.value.code == "runtime_ownership_conflict"
    db_session.expire_all()
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0
    assert db_session.get(ExecutionTask, task.id).status == "ready"
    assert db_session.get(ExecutionTaskAttempt, created.attempt.id).attempt_status == (
        "submitted"
    )


def test_attempt_update_failure_is_bounded_and_rolls_back_transaction(
    db_session, monkeypatch
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    command = _ownership_command(task, created)
    original_execute = db_session.execute
    calls = [0]

    def fail_attempt_update(statement, *args, **kwargs):
        if calls[0] == 0 and "UPDATE execution_task_attempts" in str(statement):
            calls[0] += 1
            from sqlalchemy.exc import OperationalError

            raise OperationalError("attempt update", {}, Exception("locked"))
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", fail_attempt_update)
    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        ExecutionTaskRuntimeOwnershipService(db_session).acquire(command)
    assert exc.value.code == "runtime_start_transition_failed"
    db_session.rollback()
    db_session.expire_all()
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0
    assert db_session.query(ExecutionTaskTransition).count() == 1
    assert db_session.get(ExecutionTask, task.id).status == "ready"


def test_heartbeat_is_fenced_bounded_and_rejects_stale_owner(db_session):
    clock = [datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)]
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    service = ExecutionTaskRuntimeOwnershipService(db_session, now=lambda: clock[0])
    first = service.acquire(_ownership_command(task, created, key="heartbeat-start"))
    db_session.commit()

    clock[0] += timedelta(seconds=5)
    heartbeat = service.heartbeat(
        HeartbeatRuntimeOwnershipCommand(
            runtime_lease_id=first.lease.id,
            worker_instance_id="worker-instance-1",
            fencing_token=1,
            lease_seconds=30,
        )
    )
    db_session.commit()
    assert heartbeat.last_heartbeat_at == clock[0]
    assert heartbeat.lease_expires_at == clock[0] + timedelta(seconds=30)

    with pytest.raises(ExecutionRuntimeOwnershipError) as owner_error:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=first.lease.id,
                worker_instance_id="worker-instance-2",
                fencing_token=1,
            )
        )
    assert owner_error.value.code == "runtime_ownership_owner_mismatch"
    with pytest.raises(ExecutionRuntimeOwnershipError) as fence_error:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=first.lease.id,
                worker_instance_id="worker-instance-1",
                fencing_token=2,
            )
        )
    assert fence_error.value.code == "runtime_ownership_fence_stale"

    clock[0] += timedelta(seconds=31)
    with pytest.raises(ExecutionRuntimeOwnershipError) as expired_error:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=first.lease.id,
                worker_instance_id="worker-instance-1",
                fencing_token=1,
            )
        )
    assert expired_error.value.code == "runtime_ownership_expired"


def test_integrity_verifiers_report_clean_chain_and_tampering(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    result, _ = _start(db_session, task, created)
    service = ExecutionTaskRuntimeOwnershipService(db_session)

    assert service.verify_runtime_ownership_integrity(result.lease.id).verified
    assert service.verify_execution_task_runtime_integrity(task.id).verified
    assert service.verify_execution_plan_runtime_integrity(
        context["execution_plan"].id
    ).verified

    result.lease.worker_instance_id = ""
    tampered = service.verify_runtime_ownership_integrity(result.lease.id)
    assert tampered.verified is False
    assert "worker_instance_identity_missing" in tampered.issues

    result.lease.worker_instance_id = "worker-instance-1"
    event = db_session.get(
        ExecutionTaskTransition, result.lease.lifecycle_transition_id
    )
    event.runtime_lease_id = 999999
    reference_tampered = service.verify_runtime_ownership_integrity(result.lease.id)
    assert reference_tampered.verified is False
    assert "lifecycle_transition_reference_mismatch" in reference_tampered.issues


def test_running_attempt_without_owner_is_reported_without_repair(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    result, _ = _start(db_session, task, created)
    db_session.delete(result.lease)
    db_session.flush()

    integrity = ExecutionTaskRuntimeOwnershipService(
        db_session
    ).verify_execution_task_runtime_integrity(task.id)
    assert integrity.verified is False
    assert "running_attempt_without_active_owner" in integrity.issues
    assert "running_task_without_active_owner" in integrity.issues


def test_expiry_detection_preserves_ownership_history(db_session):
    clock = [datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)]
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    service = ExecutionTaskRuntimeOwnershipService(db_session, now=lambda: clock[0])
    result = service.acquire(_ownership_command(task, created, key="expiry-start"))
    db_session.commit()
    clock[0] = result.lease.lease_expires_at + timedelta(seconds=1)

    stale = service.expire_runtime_ownership(result.lease.id)
    db_session.commit()
    assert stale.verified is True
    lease = db_session.get(ExecutionTaskRuntimeLease, result.lease.id)
    assert lease.lease_status == "expired"
    assert lease.release_reason == "lease_expired"
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 1


def test_concurrent_workers_create_one_owner_and_one_running_transition(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase29c5-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SessionLocal = sessionmaker(autoflush=False, bind=engine)
    seed = SessionLocal()
    try:
        Base.metadata.create_all(engine)
        context = _build_context(seed)
        task = _ready_root(context)
        _, _, created = _submitted(seed, task)
        seed.commit()
        task_id = task.id
        attempt_id = created.attempt.id
        intent_id = created.intent.id
        broker_task_id = created.broker_task_id
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def acquire_worker(worker_number):
            session = SessionLocal()
            try:
                barrier.wait(timeout=5)
                result = ExecutionTaskRuntimeOwnershipService(session).acquire(
                    AcquireRuntimeOwnershipCommand(
                        dispatch_intent_id=intent_id,
                        execution_task_attempt_id=attempt_id,
                        execution_task_id=task_id,
                        broker_task_id=broker_task_id,
                        worker_id=f"celery@worker-{worker_number}",
                        worker_hostname=f"worker-{worker_number}",
                        worker_pid=1000 + worker_number,
                        worker_process_start_identity=f"start-{worker_number}",
                        worker_instance_id=f"instance-{worker_number}",
                        ownership_idempotency_key=f"race-runtime-{worker_number}",
                    )
                )
                session.commit()
                outcome = ("acquired", result.lease.id)
            except ExecutionRuntimeOwnershipError as exc:
                session.rollback()
                outcome = (exc.code, None)
            finally:
                with lock:
                    outcomes.append(outcome)
                session.close()

        threads = [
            threading.Thread(target=acquire_worker, args=(number,)) for number in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(outcome[0] == "acquired" for outcome in outcomes) == 1
        assert len(outcomes) == 2

        check = SessionLocal()
        try:
            assert check.query(ExecutionTaskRuntimeLease).count() == 1
            assert (
                check.query(ExecutionTaskTransition)
                .filter(
                    ExecutionTaskTransition.execution_task_id == task_id,
                    ExecutionTaskTransition.to_state == "running",
                )
                .count()
                == 1
            )
        finally:
            check.close()
    finally:
        seed.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_runtime_migration_replays_from_phase29c4_and_preserves_old_attempts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c5-migration.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE execution_task_runtime_leases"))
            connection.execute(text("DROP TABLE execution_task_attempts"))
        # C5 starts from the committed C4 schema; C6B is now the final
        # additive migration and must not be applied before the fixture row.
        run_schema_migrations(engine, MIGRATIONS[:-2])
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO execution_task_attempts (
                        id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at
                    ) VALUES (1, 1, 1, 1, 1, 'legacy-attempt',
                        'legacy-broker', 'submitted', '2026-07-22T12:00:00+00:00')
                    """
                )
            )
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        attempt_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_attempts")
        }
        lease_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_runtime_leases")
        }
        lease_indexes = {
            index["name"]
            for index in inspect(engine).get_indexes("execution_task_runtime_leases")
        }
        start_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_runtime_starts")
        }
        outcome_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_attempt_outcomes")
        }
        start_indexes = {
            index["name"]
            for index in inspect(engine).get_indexes("execution_task_runtime_starts")
        }
        outcome_indexes = {
            index["name"]
            for index in inspect(engine).get_indexes("execution_task_attempt_outcomes")
        }
        assert "started_at" in attempt_columns
        assert "candidate_completed" in str(
            inspect(engine).get_check_constraints("execution_task_attempts")
        )
        assert {
            "worker_instance_id",
            "ownership_fencing_token",
            "lease_expires_at",
            "runtime_start_evidence",
            "progress_state",
            "progress_sequence",
            "closed_outcome_id",
        } <= lease_columns
        assert "uq_execution_task_runtime_lease_active" in lease_indexes
        assert {
            "execution_start_idempotency_key",
            "deterministic_start_command_id",
            "canonical_start_command_hash",
            "configuration_hash",
        } <= start_columns
        assert {
            "outcome_idempotency_key",
            "deterministic_outcome_command_id",
            "canonical_outcome_command_hash",
            "lifecycle_transition_id",
            "lease_closure_hash",
        } <= outcome_columns
        assert "ix_execution_task_runtime_starts_plan_task" in start_indexes
        assert "ix_execution_task_attempt_outcomes_plan_status" in outcome_indexes
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT attempt_status, started_at FROM execution_task_attempts "
                    "WHERE id = 1"
                )
            ).one() == ("submitted", None)
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM execution_task_runtime_leases")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM execution_task_runtime_starts")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM execution_task_attempt_outcomes")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM schema_migrations "
                        "WHERE version = '036_execution_task_runtime_ownership'"
                    )
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_execution_plan_deletion_cascades_runtime_history_downward_only(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    result, _ = _start(db_session, task, created)
    plan_id = context["execution_plan"].id

    db_session.delete(context["execution_plan"])
    db_session.flush()

    assert (
        db_session.query(ExecutionTaskRuntimeLease)
        .filter_by(execution_plan_id=plan_id)
        .count()
        == 0
    )
    assert (
        db_session.query(ExecutionTaskAttempt)
        .filter_by(execution_plan_id=plan_id)
        .count()
        == 0
    )
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(execution_plan_id=plan_id)
        .count()
        == 0
    )
    assert db_session.get(ExecutionTaskRuntimeLease, result.lease.id) is None
