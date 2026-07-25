"""Phase 30D — Program 1 certification: Phase 29 Execution Plan architecture.

Companion to `test_phase30d_interruption_resume_certification.py`, which
certifies interruption/resume for the CURRENTLY LIVE v1 Session/Task/
TaskExecution runtime (0 `execution_plans` rows exist in the live
`orchestrator.db` at the time of this certification -- the Phase 29
Execution Plan architecture has never been used by a real dogfood run).

This file exercises the not-yet-live Phase 29 Execution Plan / ExecutionTask
/ ExecutionTaskRuntimeLease / ExecutionTaskRecoveryService stack exactly as
committed, against the same in-memory `db_session` fixture every other
Phase 29C test file uses. No production code is modified.

Finding: a genuine worker/process interruption (real lease expiry, i.e. the
owning worker dies and never heartbeats again) has NO production-supported
path back into this architecture's recovery authority. See Scenario A. This
is reported as a defect, not silently routed around, per this phase's own
instructions. Scenarios B-E show the recovery authority itself (policy,
authorization, idempotency, replay, terminal states) behaves correctly on
every interruption class where a live actor CAN reach it (self-reported
worker loss, competing-worker rejection, dispatch-intent replay, operator
cancellation) -- the gap is specifically the bridge from "lease genuinely
expired with no report" to "recovery input creation," not the authority
logic itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskRecoveryAuthorization,
    ExecutionTaskRecoveryInput,
    ExecutionTaskRuntimeLease,
    ExecutionTaskTransition,
)
from app.services.execution.execution_task_dispatch_service import (
    ExecutionTaskDispatchIntent,
    ExecutionTaskDispatchService,
)
from app.services.execution.execution_task_recovery_service import (
    AuthorizeRecoveryCommand,
    CreateRecoveryInputCommand,
    ExecutionTaskRecoveryError,
    ExecutionTaskRecoveryService,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionRuntimeEvidenceError,
    ExecutionTaskRuntimeExecutionService,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    ExecutionRuntimeOwnershipError,
    ExecutionTaskRuntimeOwnershipService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
    TERMINAL_EXECUTION_TASK_STATES,
)

from test_phase29c2_execution_eligibility import _build_context
from test_phase29c3_scheduler_claim import _ready_root
from test_phase29c5_runtime_ownership import _ownership_command, _submitted
from test_phase29c6b_runtime_evidence import _record_command, _start_command
from test_phase29c8_recovery_boundary import _authorize, _recovery_input

from test_phase30d_interruption_resume_certification import _write_evidence


def test_scenario_a_undetected_lease_expiry_orphans_the_execution_task(db_session):
    """Reproduction, not a passing scenario: a real, undetected worker/process
    death leaves the task permanently orphaned in this architecture today."""
    clock = [datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)]
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    ownership_service = ExecutionTaskRuntimeOwnershipService(
        db_session, now=lambda: clock[0]
    )
    acquired = ownership_service.acquire(
        _ownership_command(task, created, key="ep-s-a-lease")
    )
    db_session.commit()
    lease_id = acquired.lease.id

    exec_service = ExecutionTaskRuntimeExecutionService(
        db_session, now=lambda: clock[0]
    )
    start = exec_service.mark_runtime_execution_started(
        _start_command(task, created, acquired, key="ep-s-a-start")
    )
    db_session.commit()

    interruption_timestamp = clock[0].isoformat()
    # The worker process is killed here: no further heartbeat, no outcome.
    clock[0] = acquired.lease.lease_expires_at + timedelta(seconds=600)

    expiry_result = ownership_service.expire_runtime_ownership(lease_id)
    db_session.commit()
    assert expiry_result.verified is True
    lease = db_session.get(ExecutionTaskRuntimeLease, lease_id)
    assert lease.lease_status == "expired"

    with pytest.raises(ExecutionRuntimeEvidenceError) as exc:
        exec_service.record_runtime_attempt_outcome(
            _record_command(
                task,
                created,
                acquired,
                start.start,
                status="attempt_failed",
                key="ep-s-a-outcome",
                failure_category="worker_lost",
                exception_type="WorkerLost",
            )
        )
    assert exc.value.code == "runtime_start_lease_expired"
    db_session.rollback()

    db_session.refresh(task)
    attempt = db_session.get(ExecutionTaskAttempt, created.attempt.id)
    assert task.status == "running"
    assert attempt.attempt_status == "running"
    assert task.status not in TERMINAL_EXECUTION_TASK_STATES
    assert (
        db_session.query(ExecutionTaskAttemptOutcome)
        .filter(ExecutionTaskAttemptOutcome.execution_task_attempt_id == attempt.id)
        .count()
        == 0
    )

    projection = exec_service.inspect_unresolved_runtime(task.id)
    assert projection.state == "ownership_expired_without_outcome"

    with pytest.raises(ExecutionTaskRecoveryError) as recovery_exc:
        ExecutionTaskRecoveryService(db_session).create_recovery_input(
            CreateRecoveryInputCommand(
                execution_task_id=task.id,
                failed_attempt_id=attempt.id,
                recovery_source="runtime_attempt_failed",
                expected_task_state="awaiting_recovery",
                expected_task_state_version=task.state_version,
                runtime_outcome_id=0,
                input_idempotency_key="ep-s-a-recovery-input",
            )
        )
    assert recovery_exc.value.code == "task_not_awaiting_recovery"

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "execution_identifier": {
            "execution_plan_id": context["execution_plan"].id,
            "execution_task_id": task.id,
            "execution_task_attempt_id": attempt.id,
            "runtime_lease_id": lease_id,
        },
        "interruption_type": "process_interruption",
        "interruption_point": "post_runtime_start_pre_outcome",
        "interruption_timestamp": interruption_timestamp,
        "recovery_timestamp": None,
        "recovery_authority": "none_reachable",
        "resumed_execution_state": None,
        "terminal_state": "none_reached_orphaned_running",
        "unresolved_runtime_projection": projection.state,
        "rejected_outcome_error_code": exc.value.code,
        "rejected_recovery_input_error_code": recovery_exc.value.code,
        "replay_result": "not_applicable_no_recovery_input_created",
        "outcome": "DEFECT_REPRODUCED_ORPHAN_EXECUTION",
    }
    _write_evidence(
        "execution-plan-architecture", "scenario_a_undetected_lease_expiry", evidence
    )


def test_scenario_b_self_reported_worker_loss_recovers_through_authority(db_session):
    """The recovery AUTHORITY works correctly once it is reachable at all --
    isolates the gap to the interruption-detection bridge, not the policy/
    authorization/idempotency logic itself."""
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    ownership = ExecutionTaskRuntimeOwnershipService(db_session)
    acquired = ownership.acquire(_ownership_command(task, created, key="ep-s-b-lease"))
    db_session.commit()
    exec_service = ExecutionTaskRuntimeExecutionService(db_session)
    start = exec_service.mark_runtime_execution_started(
        _start_command(task, created, acquired, key="ep-s-b-start")
    )
    db_session.commit()

    outcome = exec_service.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            acquired,
            start.start,
            status="attempt_failed",
            key="ep-s-b-outcome",
            failure_category="worker_lost",
            exception_type="WorkerLostSignal",
        )
    )
    db_session.commit()
    db_session.refresh(task)
    assert task.status == "awaiting_recovery"

    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome.outcome, key="ep-s-b-recovery-input"
    )

    before_inputs = db_session.query(ExecutionTaskRecoveryInput).count()
    replay_input = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=created.attempt.id,
            recovery_source="runtime_attempt_failed",
            expected_task_state_version=recovery_input.task_state_version_at_creation,
            runtime_outcome_id=outcome.outcome.id,
            input_idempotency_key="ep-s-b-recovery-input",
        )
    )
    after_inputs = db_session.query(ExecutionTaskRecoveryInput).count()
    assert replay_input.replayed is True
    assert replay_input.recovery_input.id == recovery_input.id
    assert before_inputs == after_inputs

    authorization = _authorize(db_session, recovery_input, key="ep-s-b-authorize")
    db_session.commit()
    assert authorization.authorization.authorization_status == "authorized"
    assert authorization.replacement_attempt is not None
    db_session.refresh(task)
    assert task.status == "ready"

    before_auth = db_session.query(ExecutionTaskRecoveryAuthorization).count()
    before_attempts = db_session.query(ExecutionTaskAttempt).count()
    replay_auth = ExecutionTaskRecoveryService(db_session).authorize_recovery(
        AuthorizeRecoveryCommand(
            recovery_input_id=recovery_input.id,
            expected_task_state_version=recovery_input.task_state_version_at_creation,
            authorization_idempotency_key="ep-s-b-authorize",
        )
    )
    after_auth = db_session.query(ExecutionTaskRecoveryAuthorization).count()
    after_attempts = db_session.query(ExecutionTaskAttempt).count()
    assert replay_auth.replayed is True
    assert before_auth == after_auth
    assert before_attempts == after_attempts

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "interruption_type": "worker_interruption_self_reported",
        "recovery_authority": "ExecutionTaskRecoveryService.authorize_recovery",
        "resumed_execution_state": task.status,
        "terminal_state": None,
        "duplicate_recovery_inputs_after_replay": after_inputs - before_inputs,
        "duplicate_authorizations_after_replay": after_auth - before_auth,
        "duplicate_attempts_after_replay": after_attempts - before_attempts,
        "replay_result": "match",
        "outcome": "RECOVERY_SUCCEEDED",
    }
    _write_evidence(
        "execution-plan-architecture", "scenario_b_authorized_retry", evidence
    )


def test_scenario_b2_recovery_budget_exhaustion_reaches_single_terminal_state(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)

    def _submitted_n(db, task, n):
        from test_phase29c4_dispatch_intent_attempt import _claimed, _created

        claim = _claimed(db, task, key=f"ep-s-b2-claim-{n}")
        service = ExecutionTaskDispatchService(
            db,
            publisher=lambda broker_id, payload, task_name: type(
                "BrokerResult", (), {"id": broker_id}
            )(),
        )
        from test_phase29c4_dispatch_intent_attempt import _intent_command

        result = service.create_dispatch_intent(
            _intent_command(db, task, claim, key=f"ep-s-b2-dispatch-{n}")
        )
        from app.services.execution.execution_task_dispatch_service import (
            DISPATCH_STATUS_PENDING,
        )

        service.submit_dispatch_intent(
            result.intent.id,
            DISPATCH_STATUS_PENDING,
            f"ep-s-b2-submit-{n}",
        )
        return service, claim, result

    def _one_failed_cycle(n):
        _, _, created = _submitted_n(db_session, task, n)
        ownership = ExecutionTaskRuntimeOwnershipService(db_session)
        acquired = ownership.acquire(
            _ownership_command(task, created, key=f"ep-s-b2-lease-{n}")
        )
        db_session.commit()
        exec_service = ExecutionTaskRuntimeExecutionService(db_session)
        start = exec_service.mark_runtime_execution_started(
            _start_command(task, created, acquired, key=f"ep-s-b2-start-{n}")
        )
        db_session.commit()
        outcome = exec_service.record_runtime_attempt_outcome(
            _record_command(
                task,
                created,
                acquired,
                start.start,
                status="attempt_failed",
                key=f"ep-s-b2-outcome-{n}",
                failure_category="worker_lost",
                exception_type="WorkerLostSignal",
            )
        )
        db_session.commit()
        db_session.refresh(task)
        svc, recovery_input = _recovery_input(
            db_session,
            task,
            created.attempt,
            outcome.outcome,
            key=f"ep-s-b2-recovery-input-{n}",
        )
        authorization = _authorize(
            db_session, recovery_input, key=f"ep-s-b2-authorize-{n}"
        )
        db_session.commit()
        db_session.refresh(task)
        return authorization

    first = _one_failed_cycle(1)
    assert first.authorization.authorization_status == "authorized"
    assert task.status == "ready"
    second = _one_failed_cycle(2)
    assert second.authorization.authorization_status == "authorized"
    assert task.status == "ready"
    third = _one_failed_cycle(3)
    assert third.authorization.authorization_status == "exhausted"
    db_session.refresh(task)

    assert task.status == "failed"
    assert task.status in TERMINAL_EXECUTION_TASK_STATES
    terminal_transitions = (
        db_session.query(ExecutionTaskTransition)
        .filter(
            ExecutionTaskTransition.execution_task_id == task.id,
            ExecutionTaskTransition.to_state == "failed",
        )
        .all()
    )
    assert len(terminal_transitions) == 1

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "interruption_type": "worker_interruption_self_reported_repeated",
        "recovery_authority": "ExecutionTaskRecoveryService.authorize_recovery",
        "terminal_state": task.status,
        "terminal_transition_count": len(terminal_transitions),
        "outcome": "SINGLE_VALID_TERMINAL_STATE",
    }
    _write_evidence(
        "execution-plan-architecture", "scenario_b2_budget_exhaustion", evidence
    )


def test_scenario_c_post_restart_worker_cannot_duplicate_ownership(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    service = ExecutionTaskRuntimeOwnershipService(db_session)
    original = service.acquire(
        _ownership_command(task, created, key="ep-s-c-original-lease")
    )
    db_session.commit()

    before_leases = db_session.query(ExecutionTaskRuntimeLease).count()
    with pytest.raises(ExecutionRuntimeOwnershipError) as exc:
        service.acquire(
            _ownership_command(
                task,
                created,
                key="ep-s-c-restarted-lease",
                worker_id="celery@worker-b-post-restart",
                worker_instance_id="worker-instance-2",
            )
        )
    after_leases = db_session.query(ExecutionTaskRuntimeLease).count()
    db_session.rollback()

    assert after_leases == before_leases
    original_lease = db_session.get(ExecutionTaskRuntimeLease, original.lease.id)
    assert original_lease.lease_status == "active"

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "interruption_type": "service_restart_competing_worker",
        "recovery_authority": "ExecutionTaskRuntimeOwnershipService.acquire"
        " (unique active-lease index)",
        "rejected_error_code": exc.value.code,
        "duplicate_leases_created": after_leases - before_leases,
        "outcome": "DUPLICATE_OWNERSHIP_REJECTED",
    }
    _write_evidence(
        "execution-plan-architecture", "scenario_c_competing_worker_rejected", evidence
    )


def test_scenario_d_scheduler_restart_replays_dispatch_intent_without_duplication(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    from test_phase29c4_dispatch_intent_attempt import _intent_command, _claimed

    claim = _claimed(db_session, task)
    dispatch_service = ExecutionTaskDispatchService(
        db_session,
        publisher=lambda broker_id, payload, task_name: type(
            "BrokerResult", (), {"id": broker_id}
        )(),
    )
    first = dispatch_service.create_dispatch_intent(
        _intent_command(db_session, task, claim, key="ep-s-d-dispatch-key")
    )
    before_intents = db_session.query(ExecutionTaskDispatchIntent).count()
    before_attempts = db_session.query(ExecutionTaskAttempt).count()
    replay = dispatch_service.create_dispatch_intent(
        _intent_command(db_session, task, claim, key="ep-s-d-dispatch-key")
    )
    after_intents = db_session.query(ExecutionTaskDispatchIntent).count()
    after_attempts = db_session.query(ExecutionTaskAttempt).count()

    assert replay.replayed is True
    assert replay.intent.id == first.intent.id
    assert replay.attempt.id == first.attempt.id
    assert after_intents == before_intents
    assert after_attempts == before_attempts

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "interruption_type": "scheduler_interruption",
        "recovery_authority": "ExecutionTaskDispatchService.create_dispatch_intent"
        " (idempotency key)",
        "resumed_execution_state": "ready",
        "duplicate_intents_after_replay": after_intents - before_intents,
        "duplicate_attempts_after_replay": after_attempts - before_attempts,
        "replay_result": "match",
        "outcome": "REPLAY_MATCHES_NO_DUPLICATION",
    }
    _write_evidence(
        "execution-plan-architecture", "scenario_d_dispatch_intent_replay", evidence
    )


def test_scenario_e_operator_cancels_task_awaiting_recovery(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    ownership = ExecutionTaskRuntimeOwnershipService(db_session)
    acquired = ownership.acquire(_ownership_command(task, created, key="ep-s-e-lease"))
    db_session.commit()
    exec_service = ExecutionTaskRuntimeExecutionService(db_session)
    start = exec_service.mark_runtime_execution_started(
        _start_command(task, created, acquired, key="ep-s-e-start")
    )
    db_session.commit()
    outcome = exec_service.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            acquired,
            start.start,
            status="attempt_failed",
            key="ep-s-e-outcome",
            failure_category="provider_timeout",
        )
    )
    db_session.commit()
    db_session.refresh(task)
    assert task.status == "awaiting_recovery"

    before_version = task.state_version
    ExecutionTaskTransitionService(db_session).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=context["execution_plan"].id,
            expected_from_state="awaiting_recovery",
            expected_state_version=before_version,
            to_state="cancelled",
            reason_code="operator_cancelled",
            reason_detail="operator interrupted the task while awaiting recovery",
            actor_type="operator",
            actor_id="operator-1",
            idempotency_key="ep-s-e-operator-cancel",
        )
    )
    db_session.commit()
    db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.status in TERMINAL_EXECUTION_TASK_STATES

    with pytest.raises(ExecutionTaskRecoveryError) as exc:
        ExecutionTaskRecoveryService(db_session).create_recovery_input(
            CreateRecoveryInputCommand(
                execution_task_id=task.id,
                failed_attempt_id=created.attempt.id,
                recovery_source="runtime_attempt_failed",
                expected_task_state="awaiting_recovery",
                expected_task_state_version=before_version,
                runtime_outcome_id=outcome.outcome.id,
                input_idempotency_key="ep-s-e-recovery-input",
            )
        )

    terminal_transitions = (
        db_session.query(ExecutionTaskTransition)
        .filter(
            ExecutionTaskTransition.execution_task_id == task.id,
            ExecutionTaskTransition.to_state == "cancelled",
        )
        .all()
    )
    assert len(terminal_transitions) == 1

    evidence = {
        "project": context["project"].id,
        "session": context["session"].id,
        "task": task.id,
        "interruption_type": "operator_interruption",
        "recovery_authority": "ExecutionTaskTransitionService.transition"
        " (operator actor)",
        "terminal_state": task.status,
        "terminal_transition_count": len(terminal_transitions),
        "post_cancellation_recovery_input_rejected_code": exc.value.code,
        "outcome": "OPERATOR_CANCELLATION_REACHES_SINGLE_TERMINAL_STATE",
    }
    _write_evidence(
        "execution-plan-architecture",
        "scenario_e_cancel_while_awaiting_recovery",
        evidence,
    )
