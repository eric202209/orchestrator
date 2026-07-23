"""Focused Phase 29C-6A lifecycle truthfulness amendment tests."""

from __future__ import annotations

import pytest

from app.models import (
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskDispatchIntent,
)
from app.services.execution.execution_task_transition_service import (
    ALLOWED_EXECUTION_TASK_TRANSITIONS,
    EXECUTION_TASK_STATES,
    TERMINAL_EXECUTION_TASK_STATES,
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionIntegrityError,
    ExecutionTaskTransitionService,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_operator_projection import (
    project_execution_task_state,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    ExecutionTaskRuntimeOwnershipService,
)
from app.services.execution.execution_task_scheduler_claim_service import (
    AcquireSchedulerClaimCommand,
    ExecutionReadyTaskSelectionService,
    ExecutionTaskSchedulerClaimService,
    ExecutionSchedulerClaimError,
)

from test_phase29c3_scheduler_claim import _command as claim_command
from test_phase29c3_scheduler_claim import _ready_root
from test_phase29c4_dispatch_intent_attempt import _created
from test_phase29c5_runtime_ownership import _start, _submitted

from test_phase29c2_execution_eligibility import _build_context


def _transition(db, task: ExecutionTask, to_state: str, key: str, reason: str):
    return ExecutionTaskTransitionService(db).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state=task.status,
            expected_state_version=int(task.state_version),
            to_state=to_state,
            reason_code=reason,
            actor_type="test",
            actor_id="phase29c6a",
            idempotency_key=key,
        )
    )


def _running(context):
    db = context["db"]
    task = context["tasks"][0]
    _transition(db, task, "ready", "ready-1", "dependencies_satisfied")
    _transition(db, task, "running", "running-1", "execution_started")
    return task


def test_truthful_states_are_known_nonterminal_and_not_scheduler_or_success_states():
    assert {"awaiting_validation", "awaiting_recovery"} <= EXECUTION_TASK_STATES
    assert (
        not {"awaiting_validation", "awaiting_recovery"}
        & TERMINAL_EXECUTION_TASK_STATES
    )
    assert "awaiting_validation" not in {"ready", "running", "succeeded"}
    assert "awaiting_recovery" not in {"ready", "running", "succeeded"}


@pytest.mark.parametrize(
    ("to_state", "reason"),
    (
        ("awaiting_validation", "runtime_candidate_completed"),
        ("awaiting_recovery", "runtime_attempt_failed"),
    ),
)
def test_running_can_enter_truthful_nonterminal_state(db_session, to_state, reason):
    context = _build_context(db_session)
    task = _running(context)

    result = _transition(db_session, task, to_state, f"{to_state}-1", reason)

    assert result.from_state == "running"
    assert result.to_state == to_state
    assert task.status == to_state
    assert task.state_version == 3


def test_new_transition_edges_are_present():
    assert {
        "succeeded",
        "awaiting_recovery",
        "paused",
        "cancelled",
    } <= ALLOWED_EXECUTION_TASK_TRANSITIONS["awaiting_validation"]
    assert {
        "ready",
        "failed",
        "paused",
        "cancelled",
    } <= ALLOWED_EXECUTION_TASK_TRANSITIONS["awaiting_recovery"]


@pytest.mark.parametrize(
    ("state", "reason"),
    (
        ("awaiting_validation", "predecessor_awaiting_validation"),
        ("awaiting_recovery", "predecessor_awaiting_recovery"),
    ),
)
def test_nonterminal_predecessor_blocks_dependent_with_bounded_reason(
    db_session, state, reason
):
    context = _build_context(db_session)
    predecessor = _running(context)
    _transition(
        db_session,
        predecessor,
        state,
        f"predecessor-{state}",
        (
            "runtime_candidate_completed"
            if state == "awaiting_validation"
            else "runtime_attempt_failed"
        ),
    )

    decision = ExecutionEligibilityService(db_session).evaluate_task(
        context["tasks"][1].id
    )

    assert decision.eligible is False
    assert decision.reason_code == reason
    dependency = decision.dependency_results[0]
    assert dependency.predecessor_state == state
    assert dependency.predecessor_state_version == predecessor.state_version
    assert dependency.reason_code == reason
    assert dependency.result == "waiting"


def test_only_succeeded_predecessor_satisfies_dependency(db_session):
    context = _build_context(db_session)
    predecessor = _running(context)
    _transition(
        db_session,
        predecessor,
        "succeeded",
        "predecessor-succeeded",
        "execution_succeeded",
    )

    decision = ExecutionEligibilityService(db_session).evaluate_task(
        context["tasks"][1].id
    )

    assert decision.dependency_results[0].result == "satisfied"
    assert decision.dependency_results[0].reason_code is None


def test_predecessor_state_and_version_are_bound_into_deterministic_hashes(db_session):
    context = _build_context(db_session)
    predecessor = _running(context)
    _transition(
        db_session,
        predecessor,
        "awaiting_validation",
        "predecessor-awaiting-validation",
        "runtime_candidate_completed",
    )
    service = ExecutionEligibilityService(db_session)
    first = service.evaluate_task(context["tasks"][1].id)
    second = service.evaluate_task(context["tasks"][1].id)

    assert first.decision_hash == second.decision_hash
    assert first.dependency_results[0].predecessor_state_version == 3

    _transition(
        db_session,
        predecessor,
        "paused",
        "predecessor-paused",
        "operator_paused",
    )
    changed = service.evaluate_task(context["tasks"][1].id)
    assert changed.decision_hash != first.decision_hash
    assert changed.dependency_results[0].predecessor_state_version == 4


@pytest.mark.parametrize(
    ("state", "actions"),
    (
        ("awaiting_validation", ("validate", "pause", "cancel")),
        ("awaiting_recovery", ("evaluate_recovery", "pause", "cancel")),
    ),
)
def test_operator_projection_is_nonterminal_and_has_bounded_future_actions(
    state, actions
):
    projection = project_execution_task_state(
        state, execution_task_id=7, state_version=3
    )

    assert projection.current_state == state
    assert projection.is_terminal is False
    assert projection.is_successful is False
    assert projection.satisfies_dependencies is False
    assert projection.allowed_actions == actions
    assert "retry" not in projection.allowed_actions
    assert "dispatch" not in projection.allowed_actions
    assert "claim" not in projection.allowed_actions
    assert projection.to_dict()["current_state"] == state


def test_succeeded_is_the_only_successful_dependency_satisfying_projection():
    succeeded = project_execution_task_state("succeeded")
    recovery = project_execution_task_state("awaiting_recovery")

    assert succeeded.is_successful is True
    assert succeeded.satisfies_dependencies is True
    assert recovery.is_successful is False
    assert recovery.satisfies_dependencies is False


@pytest.mark.parametrize("state", ("awaiting_validation", "awaiting_recovery"))
def test_new_states_are_not_ready_candidates_or_claimable(db_session, state):
    context = _build_context(db_session)
    task = _running(context)
    _transition(
        db_session,
        task,
        state,
        f"claim-isolation-{state}",
        (
            "runtime_candidate_completed"
            if state == "awaiting_validation"
            else "runtime_attempt_failed"
        ),
    )

    candidates = ExecutionReadyTaskSelectionService(db_session).list_ready_candidates()
    assert candidates.task_ids == ()

    command = AcquireSchedulerClaimCommand(
        execution_task_id=task.id,
        expected_task_state="ready",
        expected_task_state_version=int(task.state_version),
        expected_eligibility_decision_hash="0" * 64,
        expected_graph_hash="0" * 64,
        expected_predecessor_fence_hash="0" * 64,
        scheduler_id="phase29c6a",
        idempotency_key=f"direct-claim-{state}",
    )
    with pytest.raises(ExecutionSchedulerClaimError) as exc_info:
        ExecutionTaskSchedulerClaimService(db_session).acquire_claim(command)
    assert exc_info.value.code == "task_not_ready"
    assert task.status == state


def test_lifecycle_amendment_does_not_reactivate_consumed_claim_or_create_runtime_rows(
    db_session,
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, claim, created = _created(db_session, task)
    claim_status = claim.claim_status
    intent_count = db_session.query(ExecutionTaskDispatchIntent).count()
    attempt_count = db_session.query(ExecutionTaskAttempt).count()

    _transition(db_session, task, "running", "historical-running", "execution_started")
    _transition(
        db_session,
        task,
        "awaiting_recovery",
        "historical-awaiting-recovery",
        "runtime_attempt_failed",
    )

    db_session.refresh(claim)
    assert claim.claim_status == claim_status == "consumed"
    assert claim.consumed_dispatch_intent_id == created.intent.id
    assert db_session.query(ExecutionTaskDispatchIntent).count() == intent_count
    assert db_session.query(ExecutionTaskAttempt).count() == attempt_count


def test_integrity_rejects_amended_terminal_edge_without_authority_reason(db_session):
    context = _build_context(db_session)
    task = _running(context)
    _transition(
        db_session,
        task,
        "awaiting_validation",
        "integrity-awaiting-validation",
        "runtime_candidate_completed",
    )
    event = task.transitions[-1]
    event.reason_code = "system_reconciliation"
    db_session.commit()

    with pytest.raises(ExecutionTaskTransitionIntegrityError):
        ExecutionTaskTransitionService(db_session).verify_task_lifecycle_integrity(
            task.id
        )


@pytest.mark.parametrize(
    ("source", "target", "reason"),
    (
        ("awaiting_validation", "succeeded", "validation_accepted"),
        ("awaiting_validation", "awaiting_recovery", "validation_rejected"),
        ("awaiting_validation", "paused", "operator_paused"),
        ("awaiting_validation", "cancelled", "operator_cancelled"),
        ("awaiting_recovery", "ready", "recovery_retry_authorized"),
        ("awaiting_recovery", "failed", "recovery_exhausted"),
        ("awaiting_recovery", "paused", "operator_paused"),
        ("awaiting_recovery", "cancelled", "operator_cancelled"),
    ),
)
def test_amended_transition_edges_are_structurally_exercisable(
    db_session, source, target, reason
):
    context = _build_context(db_session)
    task = _running(context)
    first_state = (
        "awaiting_validation"
        if source == "awaiting_validation"
        else "awaiting_recovery"
    )
    _transition(
        db_session,
        task,
        first_state,
        f"edge-source-{source}",
        (
            "runtime_candidate_completed"
            if first_state == "awaiting_validation"
            else "runtime_attempt_failed"
        ),
    )

    result = _transition(db_session, task, target, f"edge-{source}-{target}", reason)

    assert result.from_state == source
    assert result.to_state == target


@pytest.mark.parametrize(
    ("source", "target"),
    (
        ("awaiting_validation", "ready"),
        ("awaiting_validation", "running"),
        ("awaiting_validation", "failed"),
        ("awaiting_recovery", "running"),
        ("awaiting_recovery", "succeeded"),
        ("awaiting_recovery", "awaiting_validation"),
    ),
)
def test_unauthorized_amended_edges_are_rejected(db_session, source, target):
    context = _build_context(db_session)
    task = _running(context)
    _transition(
        db_session,
        task,
        source,
        f"unauthorized-source-{source}",
        (
            "runtime_candidate_completed"
            if source == "awaiting_validation"
            else "runtime_attempt_failed"
        ),
    )

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        _transition(
            db_session,
            task,
            target,
            f"unauthorized-{source}-{target}",
            "system_reconciliation",
        )
    assert getattr(exc_info.value, "code", None) == "transition_not_allowed"


@pytest.mark.parametrize(
    ("target", "reason", "code"),
    (
        (
            "succeeded",
            "runtime_candidate_completed",
            "terminal_acceptance_actor_required",
        ),
        ("failed", "runtime_attempt_failed", "terminal_recovery_actor_required"),
    ),
)
def test_runtime_outcome_reasons_cannot_directly_terminalize_for_worker(
    db_session, target, reason, code
):
    context = _build_context(db_session)
    task = _running(context)
    command = ExecutionTaskTransitionCommand(
        execution_task_id=task.id,
        execution_plan_id=task.execution_plan_id,
        expected_from_state="running",
        expected_state_version=int(task.state_version),
        to_state=target,
        reason_code=reason,
        actor_type="worker",
        actor_id="worker-1",
        idempotency_key=f"worker-terminal-{target}",
    )

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(db_session).transition(command)
    assert getattr(exc_info.value, "code", None) == code
    assert task.status == "running"


@pytest.mark.parametrize("target", ("succeeded", "failed"))
def test_runtime_worker_cannot_claim_terminal_authority_even_with_terminal_reason(
    db_session, target
):
    context = _build_context(db_session)
    task = _running(context)
    command = ExecutionTaskTransitionCommand(
        execution_task_id=task.id,
        execution_plan_id=task.execution_plan_id,
        expected_from_state="running",
        expected_state_version=int(task.state_version),
        to_state=target,
        reason_code=(
            "validation_accepted" if target == "succeeded" else "recovery_exhausted"
        ),
        actor_type="worker",
        actor_id="worker-1",
        idempotency_key=f"worker-authority-{target}",
    )

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(db_session).transition(command)
    assert exc_info.value.code.endswith("_actor_required")

    waiting_state = (
        "awaiting_validation" if target == "succeeded" else "awaiting_recovery"
    )
    _transition(
        db_session,
        task,
        waiting_state,
        f"worker-waiting-{target}",
        (
            "runtime_candidate_completed"
            if waiting_state == "awaiting_validation"
            else "runtime_attempt_failed"
        ),
    )
    waiting_command = ExecutionTaskTransitionCommand(
        execution_task_id=task.id,
        execution_plan_id=task.execution_plan_id,
        expected_from_state=waiting_state,
        expected_state_version=int(task.state_version),
        to_state=target,
        reason_code=(
            "validation_accepted" if target == "succeeded" else "recovery_exhausted"
        ),
        actor_type="worker",
        actor_id="worker-1",
        idempotency_key=f"worker-waiting-authority-{target}",
    )
    with pytest.raises(ExecutionTaskTransitionError) as waiting_error:
        ExecutionTaskTransitionService(db_session).transition(waiting_command)
    assert waiting_error.value.code.endswith("_actor_required")


def test_new_state_transition_uses_fencing_version_and_idempotent_replay(db_session):
    context = _build_context(db_session)
    task = _running(context)
    command = ExecutionTaskTransitionCommand(
        execution_task_id=task.id,
        execution_plan_id=task.execution_plan_id,
        expected_from_state="running",
        expected_state_version=2,
        to_state="awaiting_validation",
        reason_code="runtime_candidate_completed",
        actor_type="worker",
        actor_id="worker-1",
        idempotency_key="new-state-replay",
    )
    service = ExecutionTaskTransitionService(db_session)
    first = service.transition(command)
    db_session.commit()
    second = service.transition(command)

    assert first.event_hash == second.event_hash
    assert second.replayed is True
    assert task.status == "awaiting_validation"
    assert task.state_version == 3

    conflicting = ExecutionTaskTransitionCommand(
        **{**command.__dict__, "to_state": "awaiting_recovery"}
    )
    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(conflicting)
    assert getattr(exc_info.value, "code", None) == "transition_idempotency_conflict"


def test_integrity_reports_active_scheduler_claim_for_new_state(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    claim_service = ExecutionTaskSchedulerClaimService(db_session)
    claim = claim_service.acquire_claim(claim_command(db_session, task)).claim
    _transition(db_session, task, "running", "claim-running", "execution_started")
    _transition(
        db_session,
        task,
        "awaiting_validation",
        "claim-awaiting-validation",
        "runtime_candidate_completed",
    )

    integrity = claim_service.verify_scheduler_claim_integrity(task.id)

    assert "active_claim_for_non_ready_task" in integrity.issues
    assert claim.claim_status == "active"


def test_integrity_reports_pending_dispatch_for_new_state(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    dispatch_service, _, created = _created(db_session, task)
    _transition(db_session, task, "running", "dispatch-running", "execution_started")
    _transition(
        db_session,
        task,
        "awaiting_recovery",
        "dispatch-awaiting-recovery",
        "runtime_attempt_failed",
    )

    integrity = dispatch_service.verify_dispatch_intent_integrity(created.intent.id)

    assert "active_dispatch_for_non_ready_task" in integrity.issues


def test_integrity_reports_active_runtime_owner_for_new_state(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    runtime_result, _ = _start(db_session, task, created)
    _transition(
        db_session,
        task,
        "awaiting_validation",
        "runtime-awaiting-validation",
        "runtime_candidate_completed",
    )

    integrity = ExecutionTaskRuntimeOwnershipService(
        db_session
    ).verify_runtime_ownership_integrity(runtime_result.lease.id)

    assert "active_owner_on_non_running_task" in integrity.issues
