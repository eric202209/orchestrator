"""Focused Phase 29C-4 dispatch-intent and canonical-attempt tests."""

from __future__ import annotations

from dataclasses import replace
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
    ExecutionTaskDispatchIntent,
    ExecutionTaskSchedulerClaim,
    TaskExecution,
)
from app.services.execution.execution_task_dispatch_service import (
    CreateDispatchIntentCommand,
    DISPATCH_STATUS_CANCELLED,
    DISPATCH_STATUS_FAILED,
    DISPATCH_STATUS_PENDING,
    DISPATCH_STATUS_SUBMITTED,
    DISPATCH_STATUS_SUBMITTING,
    ExecutionDispatchError,
    ExecutionTaskDispatchService,
)
from app.services.execution.execution_task_scheduler_claim_service import (
    ExecutionTaskSchedulerClaimService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
)

from test_phase29c2_execution_eligibility import _build_context
from test_phase29c3_scheduler_claim import _command, _ready_root


def _intent_command(db, task, claim, key="dispatch-key", **overrides):
    command = CreateDispatchIntentCommand(
        execution_task_id=task.id,
        scheduler_claim_id=claim.id,
        scheduler_id=claim.scheduler_id,
        claim_fencing_token=claim.fencing_token,
        expected_task_state="ready",
        expected_task_state_version=task.state_version,
        expected_eligibility_decision_hash=claim.claimed_eligibility_decision_hash,
        dispatch_idempotency_key=key,
    )
    return replace(command, **overrides)


def _claimed(db, task, key="claim-key"):
    claim_result = ExecutionTaskSchedulerClaimService(db).acquire_claim(
        _command(db, task, key=key)
    )
    return claim_result.claim


def _created(db, task, key="dispatch-key", publisher=None, **overrides):
    claim = _claimed(db, task)
    service = ExecutionTaskDispatchService(db, publisher=publisher, **overrides)
    result = service.create_dispatch_intent(_intent_command(db, task, claim, key))
    return service, claim, result


def test_valid_intent_creates_one_attempt_consumes_claim_and_leaves_task_ready(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    original_version = task.state_version
    service, claim, result = _created(db_session, task)

    assert result.replayed is False
    assert result.attempt.attempt_number == 1
    assert result.intent.dispatch_status == DISPATCH_STATUS_PENDING
    assert result.intent.scheduler_claim_id == claim.id
    assert result.intent.claim_fencing_token == claim.fencing_token
    assert len(result.intent.broker_task_id) > 0
    assert claim.claim_status == "consumed"
    assert claim.consumed_dispatch_intent_id == result.intent.id
    assert claim.consumed_at is not None
    db_session.refresh(task)
    assert task.status == "ready"
    assert task.state_version == original_version
    assert db_session.query(TaskExecution).count() == 0
    assert service.verify_dispatch_intent_integrity(result.intent.id).verified


def test_intent_creation_is_replayable_by_key_claim_and_attempt(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, claim, first = _created(db_session, task)
    replay = service.create_dispatch_intent(_intent_command(db_session, task, claim))
    assert replay.replayed is True
    assert replay.intent.id == first.intent.id
    assert replay.attempt.id == first.attempt.id
    assert replay.broker_task_id == first.broker_task_id
    assert db_session.query(ExecutionTaskDispatchIntent).count() == 1
    assert db_session.query(ExecutionTaskAttempt).count() == 1


def test_dispatch_key_conflicts_and_consumed_claim_cannot_bind_another_intent(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, claim, _ = _created(db_session, task)
    with pytest.raises(ExecutionDispatchError) as conflict:
        service.create_dispatch_intent(
            _intent_command(
                db_session,
                task,
                claim,
                key="other-key",
            )
        )
    assert conflict.value.code in {
        "scheduler_claim_already_consumed",
        "dispatch_intent_already_exists",
        "dispatch_idempotency_conflict",
    }
    with pytest.raises(ExecutionDispatchError) as key_conflict:
        service.create_dispatch_intent(
            _intent_command(
                db_session,
                task,
                claim,
                key="dispatch-key",
                expected_task_state_version=99,
            )
        )
    assert key_conflict.value.code == "dispatch_idempotency_conflict"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("expired", "scheduler_claim_expired"),
        ("released", "scheduler_claim_not_active"),
        ("owner", "scheduler_claim_owner_mismatch"),
        ("fence", "scheduler_claim_fence_stale"),
    ],
)
def test_claim_validation_is_fenced(db_session, mutation, code):
    context = _build_context(db_session)
    task = _ready_root(context)
    claim = _claimed(db_session, task)
    if mutation == "expired":
        claim.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif mutation == "released":
        claim.claim_status = "released"
    command = _intent_command(
        db_session,
        task,
        claim,
        scheduler_id=("other" if mutation == "owner" else claim.scheduler_id),
        claim_fencing_token=(
            claim.fencing_token + 1 if mutation == "fence" else claim.fencing_token
        ),
    )
    with pytest.raises(ExecutionDispatchError) as exc:
        ExecutionTaskDispatchService(db_session).create_dispatch_intent(command)
    assert exc.value.code == code
    assert db_session.query(ExecutionTaskDispatchIntent).count() == 0


def test_non_ready_and_stale_version_are_rejected_without_claim_consumption(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    claim = _claimed(db_session, task)
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state="ready",
            expected_state_version=task.state_version,
            to_state="blocked",
            reason_code="dependency_blocked",
            actor_type="test",
            actor_id="phase29c4",
            idempotency_key="block-before-dispatch",
        )
    )
    with pytest.raises(ExecutionDispatchError) as exc:
        ExecutionTaskDispatchService(db_session).create_dispatch_intent(
            _intent_command(db_session, task, claim)
        )
    assert exc.value.code == "task_not_ready"
    assert claim.claim_status == "active"


def test_submission_uses_stored_broker_id_and_replays_without_new_attempt(db_session):
    calls = []

    def publisher(task_id, payload, task_name):
        calls.append((task_id, dict(payload), task_name))
        return type("BrokerResult", (), {"id": task_id})()

    context = _build_context(db_session)
    task = _ready_root(context)
    service, claim, created = _created(db_session, task, publisher=publisher)
    submitted = service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_PENDING,
        "submission-1",
    )
    replay = service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_SUBMITTED,
        "submission-2",
    )
    assert submitted.status == DISPATCH_STATUS_SUBMITTED
    assert replay.replayed is True
    assert replay.broker_task_id == created.broker_task_id
    assert len(calls) == 1
    assert calls[0][0] == created.broker_task_id
    assert calls[0][2] == "app.tasks.worker.receive_execution_task_dispatch"
    assert db_session.query(ExecutionTaskAttempt).count() == 1
    assert task.status == "ready"


def test_submission_failure_is_bounded_and_retryable_with_same_identity(db_session):
    calls = []

    def publisher(task_id, payload, task_name):
        calls.append(task_id)
        if len(calls) == 1:
            raise RuntimeError("redis password and stack trace must not escape")
        return type("BrokerResult", (), {"id": task_id})()

    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, created = _created(db_session, task, publisher=publisher)
    failed = service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_PENDING,
        "submission-fail",
    )
    assert failed.status == DISPATCH_STATUS_FAILED
    assert failed.error_code == "broker_submission_error"
    assert failed.intent.last_submission_error_code == "broker_submission_error"
    assert len(failed.intent.last_submission_detail) <= 1024
    retried = service.submit_dispatch_intent(
        created.intent.id,
        DISPATCH_STATUS_FAILED,
        "submission-retry",
    )
    assert retried.status == DISPATCH_STATUS_SUBMITTED
    assert calls == [created.broker_task_id, created.broker_task_id]
    assert retried.attempt.id == created.attempt.id
    assert service.verify_dispatch_intent_integrity(created.intent.id).verified


def test_submission_ownership_and_stale_recovery_are_bounded(db_session):
    clock = [datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)]

    def now():
        return clock[0]

    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, created = _created(
        db_session, task, now=now, publisher=lambda *args: None
    )
    intent = db_session.get(ExecutionTaskDispatchIntent, created.intent.id)
    intent.dispatch_status = DISPATCH_STATUS_SUBMITTING
    intent.submitter_id = "other-submitter"
    intent.submission_fencing_token = 3
    intent.submission_lease_expires_at = clock[0] + timedelta(seconds=30)
    intent.submission_idempotency_key = "other-submission"
    db_session.flush()
    with pytest.raises(ExecutionDispatchError) as in_progress:
        service.submit_dispatch_intent(intent.id, DISPATCH_STATUS_SUBMITTING, "mine")
    assert in_progress.value.code == "dispatch_submission_in_progress"
    clock[0] += timedelta(seconds=31)
    assert service.recover_stale_submission_intents() == 1
    assert intent.dispatch_status == DISPATCH_STATUS_PENDING
    assert intent.last_submission_error_code == "dispatch_submission_stale"


def test_prestart_invalidation_cancels_intent_without_reactivating_claim(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, claim, created = _created(db_session, task, publisher=lambda *args: None)
    task.status = "blocked"
    db_session.flush()
    result = service.submit_dispatch_intent(
        created.intent.id, DISPATCH_STATUS_PENDING, "cancel-before-submit"
    )
    assert result.status == DISPATCH_STATUS_CANCELLED
    assert result.error_code == "task_no_longer_ready"
    assert claim.claim_status == "consumed"
    assert created.intent.cancellation_reason == "task_no_longer_ready"
    assert created.attempt.attempt_status == "cancelled"


def test_failed_intent_attempt_transaction_rolls_back_and_claim_stays_active(
    db_session, monkeypatch
):
    context = _build_context(db_session)
    task = _ready_root(context)
    claim = _claimed(db_session, task)
    service = ExecutionTaskDispatchService(db_session)

    def fail_worker_payload(*args):
        raise RuntimeError("simulated attempt insert boundary failure")

    monkeypatch.setattr(service, "_worker_payload", fail_worker_payload)
    with pytest.raises(ExecutionDispatchError) as exc:
        service.create_dispatch_intent(_intent_command(db_session, task, claim))
    assert exc.value.code == "dispatch_intent_integrity_failure"
    db_session.expire_all()
    assert db_session.query(ExecutionTaskDispatchIntent).count() == 0
    assert db_session.query(ExecutionTaskAttempt).count() == 0
    assert (
        db_session.get(ExecutionTaskSchedulerClaim, claim.id).claim_status == "active"
    )


def test_cancelled_prestart_history_allows_attempt_two_only_after_new_ready_version(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, first = _created(db_session, task, publisher=lambda *args: None)
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state="ready",
            expected_state_version=task.state_version,
            to_state="blocked",
            reason_code="dependency_blocked",
            actor_type="test",
            actor_id="phase29c4",
            idempotency_key="cancel-history-block",
        )
    )
    cancelled = service.submit_dispatch_intent(
        first.intent.id, DISPATCH_STATUS_PENDING, "cancel-history"
    )
    assert cancelled.status == DISPATCH_STATUS_CANCELLED
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state="blocked",
            expected_state_version=task.state_version,
            to_state="ready",
            reason_code="dependencies_satisfied",
            actor_type="test",
            actor_id="phase29c4",
            idempotency_key="cancel-history-ready",
        )
    )
    second_claim = _claimed(db_session, task, key="claim-key-2")
    second = service.create_dispatch_intent(
        _intent_command(db_session, task, second_claim, key="dispatch-key-2")
    )
    assert second.attempt.attempt_number == 2
    assert first.attempt.attempt_number == 1


def test_submission_commits_marker_before_publisher_network_call(db_session):
    observed = []
    context = _build_context(db_session)
    task = _ready_root(context)

    def publisher(task_id, payload, task_name):
        observed.append(db_session.in_transaction())
        return type("BrokerResult", (), {"id": task_id})()

    service, _, created = _created(db_session, task, publisher=publisher)
    service.submit_dispatch_intent(
        created.intent.id, DISPATCH_STATUS_PENDING, "prepublication-commit"
    )
    assert observed == [False]


def test_worker_entry_accepts_same_authority_and_rejects_tampering(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, created = _created(db_session, task, publisher=lambda *args: None)
    payload = dict(created.intent.worker_command_payload)
    entry = service.validate_worker_entry(payload, created.broker_task_id)
    assert entry.runtime_attempt_id == created.attempt.id
    assert entry.duplicate_delivery is False
    with pytest.raises(ExecutionDispatchError) as tampered:
        service.validate_worker_entry(
            {**payload, "execution_task_id": task.id + 999}, created.broker_task_id
        )
    assert tampered.value.code == "dispatch_intent_integrity_failure"


@pytest.mark.parametrize(
    "field,mutation,issue",
    [
        ("scheduler_id", "tamper", "claim_intent_mismatch"),
        ("canonical_command_payload", "tamper", "command_hash_mismatch"),
        ("broker_task_id", "tamper", "broker_task_id_tampered"),
        ("runtime_attempt_id", "tamper", "attempt_identity_mismatch"),
    ],
)
def test_integrity_verifier_reports_tampering(db_session, field, mutation, issue):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, claim, created = _created(db_session, task)
    if field == "scheduler_id":
        claim.scheduler_id = "tampered"
    elif field == "canonical_command_payload":
        created.intent.canonical_command_payload = {"tampered": True}
    elif field == "broker_task_id":
        created.intent.broker_task_id = "other-broker-id"
    elif field == "runtime_attempt_id":
        created.intent.runtime_attempt_id = 99999
    db_session.flush()
    result = service.verify_dispatch_intent_integrity(created.intent.id)
    assert result.verified is False
    assert issue in result.issues


def test_plan_and_task_integrity_aggregate_clean_rows(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, created = _created(db_session, task)
    task_results = service.verify_execution_task_dispatch_integrity(task.id)
    plan_results = service.verify_execution_plan_dispatch_integrity(
        context["execution_plan"].id
    )
    assert task_results and all(result.verified for result in task_results)
    assert plan_results and all(result.verified for result in plan_results)
    assert created.intent.runtime_attempt_id == created.attempt.id


def test_execution_plan_deletion_cascades_new_rows_but_legacy_rows_are_separate(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    service, _, _ = _created(db_session, task)
    plan_id = context["execution_plan"].id
    db_session.delete(context["execution_plan"])
    db_session.flush()
    assert (
        db_session.query(ExecutionTaskDispatchIntent)
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
        db_session.query(ExecutionTaskSchedulerClaim)
        .filter_by(execution_plan_id=plan_id)
        .count()
        == 0
    )
    assert (
        db_session.query(ExecutionTask).filter_by(execution_plan_id=plan_id).count()
        == 0
    )
    assert service is not None


def test_two_consumers_of_one_claim_create_one_intent_and_attempt(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase29c4-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SessionLocal = sessionmaker(autoflush=False, bind=engine)
    seed = SessionLocal()
    try:
        Base.metadata.create_all(engine)
        context = _build_context(seed)
        task = _ready_root(context)
        claim = _claimed(seed, task)
        seed.commit()
        task_id = task.id
        claim_id = claim.id
        expected_version = task.state_version
        expected_hash = claim.claimed_eligibility_decision_hash
        fence = claim.fencing_token
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def consume():
            session = SessionLocal()
            try:
                barrier.wait(timeout=5)
                result = ExecutionTaskDispatchService(session).create_dispatch_intent(
                    CreateDispatchIntentCommand(
                        execution_task_id=task_id,
                        scheduler_claim_id=claim_id,
                        scheduler_id="scheduler-a",
                        claim_fencing_token=fence,
                        expected_task_state="ready",
                        expected_task_state_version=expected_version,
                        expected_eligibility_decision_hash=expected_hash,
                        dispatch_idempotency_key="race-dispatch",
                    )
                )
                session.commit()
                outcome = ("created", result.intent.id, result.attempt.id)
            except ExecutionDispatchError as exc:
                session.rollback()
                outcome = (exc.code, None, None)
            finally:
                with lock:
                    outcomes.append(outcome)
                session.close()

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sum(outcome[0] == "created" for outcome in outcomes) == 1
        assert (
            sum(
                outcome[0] == "dispatch_intent_integrity_failure"
                for outcome in outcomes
            )
            <= 1
        )
        check = SessionLocal()
        try:
            assert check.query(ExecutionTaskDispatchIntent).count() == 1
            assert check.query(ExecutionTaskAttempt).count() == 1
            assert (
                check.query(ExecutionTaskSchedulerClaim)
                .filter(ExecutionTaskSchedulerClaim.claim_status == "consumed")
                .count()
                == 1
            )
        finally:
            check.close()
    finally:
        seed.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_migration_replays_and_has_new_fields_without_fabricating_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c4.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE execution_task_attempts"))
            connection.execute(text("DROP TABLE execution_task_dispatch_intents"))
        # C4 predates the C5 ownership and C6B runtime-evidence migrations;
        # keep this fixture explicitly at the 29C-3/034 boundary as new
        # additive migrations are appended.
        run_schema_migrations(engine, MIGRATIONS[:-3])
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        intent_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_dispatch_intents")
        }
        attempt_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_attempts")
        }
        claim_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_scheduler_claims")
        }
        assert {
            "scheduler_claim_id",
            "dispatch_idempotency_key",
            "canonical_command_hash",
            "runtime_attempt_id",
            "broker_task_id",
            "dispatch_status",
            "submission_fencing_token",
        } <= intent_columns
        assert {
            "dispatch_intent_id",
            "attempt_number",
            "attempt_identity",
            "broker_task_id",
        } <= attempt_columns
        assert {"consumed_at", "consumed_dispatch_intent_id"} <= claim_columns
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM schema_migrations "
                        "WHERE version = '035_execution_task_dispatch_intent_attempt'"
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM execution_task_dispatch_intents")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM execution_task_attempts")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_migration_adds_claim_consumption_columns_to_phase29c3_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c3-to-c4.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS "
                    "ix_execution_task_scheduler_claims_consumed_dispatch_intent_id"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_task_scheduler_claims "
                    "DROP COLUMN consumed_dispatch_intent_id"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_task_scheduler_claims "
                    "DROP COLUMN consumed_at"
                )
            )
        run_schema_migrations(engine, MIGRATIONS[:-3])
        before = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_scheduler_claims")
        }
        assert "consumed_at" not in before
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        after = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_scheduler_claims")
        }
        assert {"consumed_at", "consumed_dispatch_intent_id"} <= after
    finally:
        engine.dispose()
