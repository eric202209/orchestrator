"""Focused Phase 29C-6B runtime start, heartbeat, and outcome tests."""

from __future__ import annotations

from datetime import timedelta
import hashlib

import pytest

from app.models import (
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskRuntimeLease,
    ExecutionTaskRuntimeStart,
    ExecutionTaskTransition,
    TaskExecution,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionRuntimeEvidenceError,
    ExecutionTaskRuntimeExecutionService,
    MarkRuntimeExecutionStartedCommand,
    RecordRuntimeAttemptOutcomeCommand,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    ExecutionRuntimeOwnershipError,
    HeartbeatRuntimeOwnershipCommand,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionService,
)
from app.services.execution.runtime_execution_adapter import (
    DeterministicExecutionRuntimeAdapter,
    RuntimeExecutionResult,
    RuntimeProgress,
)

from test_phase29c2_execution_eligibility import _build_context
from test_phase29c3_scheduler_claim import _ready_root
from test_phase29c4_dispatch_intent_attempt import _created
from test_phase29c5_runtime_ownership import _start, _submitted


CONFIGURATION_HASH = hashlib.sha256(b"phase29c6b-test-config").hexdigest()


def _owned(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    ownership, ownership_command = _start(db_session, task, created)
    return task, created, ownership, ownership_command


def _start_command(task, created, ownership, *, key="runtime-execution-start-1"):
    return MarkRuntimeExecutionStartedCommand(
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        dispatch_intent_id=created.intent.id,
        runtime_lease_id=ownership.lease.id,
        worker_instance_id=ownership.lease.worker_instance_id,
        ownership_fencing_token=ownership.lease.ownership_fencing_token,
        execution_start_idempotency_key=key,
        runtime_adapter_name="deterministic-test",
        execution_mode="test",
        configuration_hash=CONFIGURATION_HASH,
    )


def _record_command(task, created, ownership, start, *, status, key, **overrides):
    command = RecordRuntimeAttemptOutcomeCommand(
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        runtime_start_id=start.id,
        runtime_lease_id=ownership.lease.id,
        worker_instance_id=ownership.lease.worker_instance_id,
        ownership_fencing_token=ownership.lease.ownership_fencing_token,
        expected_task_state="running",
        expected_task_state_version=task.state_version,
        outcome_status=status,
        outcome_idempotency_key=key,
        output_reference=(
            "runtime://test-output" if status == "candidate_completed" else None
        ),
        **overrides,
    )
    return command


def test_valid_runtime_start_is_distinct_from_ownership_and_is_idempotent(db_session):
    task, created, ownership, _ = _owned(db_session)
    transition_count = db_session.query(ExecutionTaskTransition).count()
    service = ExecutionTaskRuntimeExecutionService(db_session)
    command = _start_command(task, created, ownership)

    first = service.mark_runtime_execution_started(command)
    db_session.commit()
    replay = service.mark_runtime_execution_started(command)
    db_session.commit()

    assert first.replayed is False
    assert replay.replayed is True
    assert first.start.id == replay.start.id
    assert first.start.started_at == replay.start.started_at
    assert first.start.runtime_lease_id == ownership.lease.id
    assert first.start.broker_task_id == created.attempt.broker_task_id
    assert db_session.query(ExecutionTaskRuntimeStart).count() == 1
    assert db_session.query(ExecutionTaskTransition).count() == transition_count
    assert task.status == "running"
    assert created.attempt.attempt_status == "running"
    assert ownership.lease.runtime_started_at != first.start.started_at


def test_runtime_start_rejects_conflicting_key_second_attempt_start_wrong_fence_and_worker(
    db_session,
):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    command = _start_command(task, created, ownership)
    service.mark_runtime_execution_started(command)
    db_session.commit()

    with pytest.raises(ExecutionRuntimeEvidenceError) as conflict:
        service.mark_runtime_execution_started(
            _start_command(task, created, ownership, key="another-start")
        )
    assert conflict.value.code == "runtime_start_already_exists"
    with pytest.raises(ExecutionRuntimeEvidenceError) as key_conflict:
        service.mark_runtime_execution_started(
            _start_command(
                task,
                created,
                ownership,
                key=command.execution_start_idempotency_key,
            ).__class__(
                **{
                    **_start_command(
                        task,
                        created,
                        ownership,
                        key=command.execution_start_idempotency_key,
                    ).__dict__,
                    "worker_instance_id": "stale-worker",
                }
            )
        )
    assert key_conflict.value.code == "runtime_start_idempotency_conflict"
    with pytest.raises(ExecutionRuntimeEvidenceError) as fence:
        service.mark_runtime_execution_started(
            _start_command(task, created, ownership, key="stale-fence").__class__(
                **{
                    **_start_command(
                        task, created, ownership, key="stale-fence"
                    ).__dict__,
                    "ownership_fencing_token": 99,
                }
            )
        )
    assert fence.value.code == "runtime_start_already_exists"


def test_progress_heartbeat_requires_start_and_is_bounded_and_fenced(db_session):
    task, created, ownership, ownership_command = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    with pytest.raises(ExecutionRuntimeOwnershipError) as progress_before_start:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=ownership.lease.id,
                worker_instance_id=ownership.lease.worker_instance_id,
                fencing_token=ownership.lease.ownership_fencing_token,
                progress_state="provider_stream_active",
                progress_sequence=1,
            )
        )
    assert progress_before_start.value.code == "runtime_start_not_found"

    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    heartbeat = service.heartbeat(
        HeartbeatRuntimeOwnershipCommand(
            runtime_lease_id=ownership.lease.id,
            worker_instance_id=ownership.lease.worker_instance_id,
            fencing_token=ownership.lease.ownership_fencing_token,
            progress_state="provider_stream_active",
            progress_sequence=1,
        )
    )
    db_session.commit()
    assert heartbeat.last_heartbeat_at is not None
    assert ownership.lease.progress_state == "provider_stream_active"
    assert ownership.lease.progress_sequence == 1
    with pytest.raises(ExecutionRuntimeOwnershipError) as arbitrary:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=ownership.lease.id,
                worker_instance_id=ownership.lease.worker_instance_id,
                fencing_token=ownership.lease.ownership_fencing_token,
                progress_state="provider-token-123",
                progress_sequence=2,
            )
        )
    assert arbitrary.value.code == "runtime_progress_state_invalid"
    with pytest.raises(ExecutionRuntimeOwnershipError) as stale:
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=ownership.lease.id,
                worker_instance_id=ownership.lease.worker_instance_id,
                fencing_token=ownership.lease.ownership_fencing_token,
                progress_state="provider_stream_active",
                progress_sequence=1,
            )
        )
    assert stale.value.code == "runtime_progress_sequence_stale"
    assert ownership_command.worker_instance_id == ownership.lease.worker_instance_id
    assert start.start.runtime_lease_id == ownership.lease.id


def test_candidate_outcome_is_atomic_waiting_validation_and_idempotent(db_session):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    command = _record_command(
        task,
        created,
        ownership,
        start.start,
        status="candidate_completed",
        key="candidate-outcome-1",
        output_hash=hashlib.sha256(b"candidate").hexdigest(),
    )
    result = service.record_runtime_attempt_outcome(command)
    db_session.commit()
    replay = service.record_runtime_attempt_outcome(command)
    db_session.commit()

    assert result.replayed is False
    assert replay.replayed is True
    assert result.outcome.id == replay.outcome.id
    assert task.status == "awaiting_validation"
    assert created.attempt.attempt_status == "candidate_completed"
    assert ownership.lease.lease_status == "completed"
    assert ownership.lease.closed_outcome_id == result.outcome.id
    assert result.transition.to_state == "awaiting_validation"
    assert result.transition.reason_code == "runtime_candidate_completed"
    assert db_session.query(ExecutionTaskAttemptOutcome).count() == 1
    assert db_session.query(ExecutionTaskTransition).count() == 3
    with pytest.raises(ExecutionRuntimeOwnershipError):
        service.heartbeat(
            HeartbeatRuntimeOwnershipCommand(
                runtime_lease_id=ownership.lease.id,
                worker_instance_id=ownership.lease.worker_instance_id,
                fencing_token=ownership.lease.ownership_fencing_token,
            )
        )


def test_failed_outcome_maps_to_waiting_recovery_without_retry_or_terminal_failure(
    db_session,
):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    command = _record_command(
        task,
        created,
        ownership,
        start.start,
        status="attempt_failed",
        key="failed-outcome-1",
        failure_category="provider_process_failure",
        failure_code="provider_result_missing",
        sanitized_detail="provider response was unavailable\nsecret-free",
        exception_type="ProviderProtocolError",
    )
    result = service.record_runtime_attempt_outcome(command)
    db_session.commit()

    assert task.status == "awaiting_recovery"
    assert created.attempt.attempt_status == "failed"
    assert result.outcome.failure_category == "provider_protocol_error"
    assert result.outcome.failure_code == "provider_result_missing"
    assert "\n" not in result.outcome.sanitized_failure_detail
    assert ownership.lease.lease_status == "completed"
    assert db_session.query(ExecutionTaskAttempt).count() == 1
    assert db_session.query(TaskExecution).count() == 0
    assert (
        db_session.query(ExecutionTaskTransition).filter_by(to_state="failed").count()
        == 0
    )


def test_outcome_transition_failure_rolls_back_outcome_attempt_task_and_lease(
    db_session, monkeypatch
):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()

    def fail_transition(*args, **kwargs):
        raise RuntimeError("injected lifecycle persistence failure")

    monkeypatch.setattr(ExecutionTaskTransitionService, "transition", fail_transition)
    with pytest.raises(ExecutionRuntimeEvidenceError) as failure:
        service.record_runtime_attempt_outcome(
            _record_command(
                task,
                created,
                ownership,
                start.start,
                status="candidate_completed",
                key="atomic-outcome-1",
            )
        )
    assert failure.value.code == "runtime_outcome_integrity_failure"
    db_session.rollback()
    db_session.expire_all()
    assert db_session.query(ExecutionTaskAttemptOutcome).count() == 0
    assert (
        db_session.get(ExecutionTaskAttempt, created.attempt.id).attempt_status
        == "running"
    )
    assert db_session.get(task.__class__, task.id).status == "running"
    assert (
        db_session.get(ExecutionTaskRuntimeLease, ownership.lease.id).lease_status
        == "active"
    )


def test_different_outcome_key_cannot_create_a_competing_lifecycle_result(db_session):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    first_command = _record_command(
        task,
        created,
        ownership,
        start.start,
        status="candidate_completed",
        key="single-outcome-1",
    )
    service.record_runtime_attempt_outcome(first_command)
    db_session.commit()
    with pytest.raises(ExecutionRuntimeEvidenceError) as competing:
        service.record_runtime_attempt_outcome(
            _record_command(
                task,
                created,
                ownership,
                start.start,
                status="attempt_failed",
                key="single-outcome-2",
                failure_category="runtime_exception",
            )
        )
    assert competing.value.code == "runtime_outcome_conflict"
    db_session.rollback()
    assert db_session.query(ExecutionTaskAttemptOutcome).count() == 1
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(reason_code="runtime_candidate_completed")
        .count()
        == 1
    )


def test_deterministic_adapter_executes_outside_transaction_and_returns_candidate(
    db_session,
):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    adapter = DeterministicExecutionRuntimeAdapter(
        result=RuntimeExecutionResult(
            completion_kind="candidate_completed",
            output_reference="runtime://bounded-output",
        ),
        progress=(RuntimeProgress("provider_request_active"),),
    )
    result = service.execute_owned_runtime_attempt(
        _start_command(task, created, ownership), adapter
    )

    assert result.rejected is False
    assert result.outcome is not None
    assert result.outcome.outcome.outcome_status == "candidate_completed"
    assert adapter.calls == 1
    assert adapter.last_command is not None
    assert task.status == "awaiting_validation"
    assert db_session.query(TaskExecution).count() == 0


def test_stale_owner_result_is_rejected_without_partial_authority_mutation(db_session):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(
        db_session,
        now=lambda: ownership.lease.acquired_at + timedelta(seconds=2),
    )
    start_command = _start_command(task, created, ownership)
    start = service.mark_runtime_execution_started(start_command)
    db_session.commit()
    lease = db_session.get(ExecutionTaskRuntimeLease, ownership.lease.id)
    lease.lease_expires_at = lease.acquired_at + timedelta(seconds=1)
    db_session.commit()
    adapter = DeterministicExecutionRuntimeAdapter()

    result = service.execute_owned_runtime_attempt(start_command, adapter)

    assert result.rejected is True
    assert result.error_code == "runtime_start_lease_expired"
    assert db_session.query(ExecutionTaskAttemptOutcome).count() == 0
    assert created.attempt.attempt_status == "running"
    assert task.status == "running"
    assert lease.lease_status == "active"


def test_integrity_projection_detects_output_and_lifecycle_tampering(db_session):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    result = service.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="candidate_completed",
            key="tamper-outcome-1",
            output_hash=hashlib.sha256(b"original").hexdigest(),
        )
    )
    db_session.commit()
    result.outcome.output_hash = hashlib.sha256(b"tampered").hexdigest()
    db_session.commit()

    integrity = service.verify_attempt_outcome_integrity(result.outcome.id)
    assert integrity.verified is False
    assert "output_hash_tampered" in integrity.issues
