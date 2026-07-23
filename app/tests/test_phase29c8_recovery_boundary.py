"""Focused Phase 29C-8 recovery and replacement-attempt tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    ExecutionTaskAttempt,
    ExecutionTaskRecoveryAuthorization,
    ExecutionTaskTransition,
)
from app.services.execution.execution_task_recovery_service import (
    AuthorizeRecoveryCommand,
    CreateRecoveryInputCommand,
    ExecutionTaskRecoveryError,
    ExecutionTaskRecoveryService,
    RecoveryPolicySpecification,
    RecoveryStrategyRegistry,
    RecoveryStrategySpecification,
    evaluate_recovery_policy,
)
from app.services.execution.execution_task_dispatch_service import (
    ExecutionTaskDispatchService,
)

from test_phase29c4_dispatch_intent_attempt import _intent_command, _claimed
from test_phase29c6b_runtime_evidence import (
    _owned,
    _record_command,
    _start_command,
)


def _failed_runtime(
    db_session, *, failure_category="provider_timeout", failure_code=None
):
    task, created, ownership, _ = _owned(db_session)
    from app.services.execution.execution_task_runtime_execution_service import (
        ExecutionTaskRuntimeExecutionService,
    )

    service = ExecutionTaskRuntimeExecutionService(db_session)
    start = service.mark_runtime_execution_started(
        _start_command(task, created, ownership)
    )
    db_session.commit()
    outcome = service.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="attempt_failed",
            key="phase29c8-failure",
            failure_category=failure_category,
            failure_code=failure_code,
            exception_type="ProviderTimeout",
        )
    )
    db_session.commit()
    return task, created, outcome.outcome


def _recovery_input(db_session, task, attempt, outcome, *, key="recovery-input-1"):
    service = ExecutionTaskRecoveryService(db_session)
    result = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=attempt.id,
            recovery_source="runtime_attempt_failed",
            expected_task_state_version=task.state_version,
            runtime_outcome_id=outcome.id,
            input_idempotency_key=key,
        )
    )
    db_session.commit()
    return service, result.recovery_input


def _authorize(db_session, recovery_input, *, key="recovery-auth-1", **kwargs):
    return ExecutionTaskRecoveryService(db_session).authorize_recovery(
        AuthorizeRecoveryCommand(
            recovery_input_id=recovery_input.id,
            expected_task_state_version=recovery_input.task_state_version_at_creation,
            authorization_idempotency_key=key,
            **kwargs,
        )
    )


def test_policy_is_deterministic_and_unknown_fails_closed():
    recovery_input = SimpleNamespace(
        recovery_source="runtime_attempt_failed",
        failure_category="provider_timeout",
        failure_code=None,
        attempt_generation=1,
    )
    policy = RecoveryPolicySpecification()
    first = evaluate_recovery_policy(recovery_input, policy)
    second = evaluate_recovery_policy(recovery_input, policy)
    assert first == second
    assert first.status == "retry_authorized"

    unknown = SimpleNamespace(
        recovery_source="runtime_attempt_failed",
        failure_category="unclassified",
        failure_code=None,
        attempt_generation=1,
    )
    assert evaluate_recovery_policy(unknown, policy).status == "policy_blocked"


def test_strategy_registry_requires_versioned_unique_supported_entries():
    registry = RecoveryStrategyRegistry()
    registry.register(RecoveryStrategySpecification("same_input_retry", 1))
    with pytest.raises(ExecutionTaskRecoveryError) as duplicate:
        registry.register(RecoveryStrategySpecification("same_input_retry", 1))
    assert duplicate.value.code == "recovery_strategy_duplicate"
    with pytest.raises(ExecutionTaskRecoveryError) as unversioned:
        registry.resolve("same_input_retry", 0)
    assert unversioned.value.code == "recovery_strategy_unsupported"
    with pytest.raises(ExecutionTaskRecoveryError) as unsupported:
        registry.resolve("sibling_candidate", 1)
    assert unsupported.value.code == "recovery_strategy_unsupported"


def test_runtime_failure_creates_immutable_input_and_authorized_pre_dispatch_replacement(
    db_session,
):
    task, created, outcome = _failed_runtime(db_session)
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome
    )
    original_hash = recovery_input.canonical_input_hash

    result = _authorize(db_session, recovery_input)
    db_session.commit()
    db_session.refresh(task)
    replacement = result.replacement_attempt

    assert result.authorization.authorization_status == "authorized"
    assert replacement is not None
    assert replacement.attempt_number == 2
    assert replacement.predecessor_attempt_id == created.attempt.id
    assert replacement.recovery_authorization_id == result.authorization.id
    assert replacement.dispatch_intent_id is None
    assert replacement.broker_task_id is None
    assert replacement.runtime_start is None
    assert replacement.runtime_outcome is None
    assert task.status == "ready"
    assert result.transition.reason_code == "recovery_retry_authorized"
    assert db_session.query(ExecutionTaskAttempt).count() == 2
    assert (
        db_session.get(ExecutionTaskAttempt, created.attempt.id).attempt_status
        == "failed"
    )
    assert (
        db_session.get(ExecutionTaskAttempt, created.attempt.id).runtime_outcome.id
        == outcome.id
    )
    assert recovery_input.canonical_input_hash == original_hash
    assert service.verify_recovery_input_integrity(recovery_input.id).verified
    assert service.verify_recovery_authorization_integrity(
        result.authorization.id
    ).verified
    assert service.verify_replacement_attempt_integrity(replacement.id).verified


def test_recovery_input_and_authorization_replay_are_stable_and_conflicts_are_fenced(
    db_session,
):
    task, created, outcome = _failed_runtime(db_session)
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome
    )
    replay_input = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=created.attempt.id,
            recovery_source="runtime_attempt_failed",
            expected_task_state_version=task.state_version,
            runtime_outcome_id=outcome.id,
            input_idempotency_key="recovery-input-1",
        )
    )
    assert replay_input.replayed is True
    result = _authorize(db_session, recovery_input)
    db_session.commit()
    replay = _authorize(db_session, recovery_input)
    assert replay.replayed is True
    assert replay.authorization.id == result.authorization.id
    with pytest.raises(ExecutionTaskRecoveryError) as key_conflict:
        _authorize(db_session, recovery_input, key="another-recovery-key")
    assert key_conflict.value.code == "recovery_authorization_already_exists"
    assert db_session.query(ExecutionTaskRecoveryAuthorization).count() == 1
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(reason_code="recovery_retry_authorized")
        .count()
        == 1
    )


def test_non_retryable_runtime_failure_terminalizes_without_replacement(db_session):
    task, created, outcome = _failed_runtime(
        db_session, failure_category="runtime_exception", failure_code="invalid_request"
    )
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome
    )
    result = _authorize(db_session, recovery_input, key="terminal-recovery-1")
    db_session.commit()
    assert result.authorization.authorization_status == "non_retryable"
    assert result.replacement_attempt is None
    assert result.transition.reason_code == "recovery_non_retryable"
    assert task.status == "failed"
    assert db_session.query(ExecutionTaskAttempt).count() == 1
    assert service.verify_execution_task_recovery_integrity(task.id).verified


def test_exhausted_budget_is_terminal_and_counts_original_attempt(db_session):
    task, created, outcome = _failed_runtime(db_session)
    plan = db_session.get(type(task.execution_plan), task.execution_plan_id)
    plan.recovery_policy_id = "one_attempt_policy"
    plan.recovery_policy_version = 1
    db_session.commit()
    policy = RecoveryPolicySpecification(
        policy_id="one_attempt_policy", policy_version=1, max_attempts=1
    )
    service = ExecutionTaskRecoveryService(db_session, policies=(policy,))
    recovery_input = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=created.attempt.id,
            recovery_source="runtime_attempt_failed",
            expected_task_state_version=task.state_version,
            runtime_outcome_id=outcome.id,
            input_idempotency_key="exhausted-input-1",
        )
    ).recovery_input
    result = service.authorize_recovery(
        AuthorizeRecoveryCommand(
            recovery_input_id=recovery_input.id,
            expected_task_state_version=task.state_version,
            authorization_idempotency_key="exhausted-auth-1",
        )
    )
    db_session.commit()
    assert result.authorization.authorization_status == "exhausted"
    assert result.authorization.retry_budget_before == 0
    assert result.authorization.next_attempt_generation is None
    assert result.replacement_attempt is None
    assert task.status == "failed"


def test_unsupported_policy_strategy_and_blocked_source_do_not_change_lifecycle(
    db_session,
):
    task, created, outcome = _failed_runtime(db_session)
    plan = db_session.get(type(task.execution_plan), task.execution_plan_id)
    plan.recovery_policy_id = "unsupported_strategy_policy"
    plan.recovery_policy_version = 1
    db_session.commit()
    policy = RecoveryPolicySpecification(
        policy_id="unsupported_strategy_policy",
        policy_version=1,
        strategy_order=("sibling_candidate",),
    )
    service = ExecutionTaskRecoveryService(db_session, policies=(policy,))
    recovery_input = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=created.attempt.id,
            recovery_source="runtime_attempt_failed",
            expected_task_state_version=task.state_version,
            runtime_outcome_id=outcome.id,
            input_idempotency_key="blocked-input-1",
        )
    ).recovery_input
    result = service.authorize_recovery(
        AuthorizeRecoveryCommand(
            recovery_input_id=recovery_input.id,
            expected_task_state_version=task.state_version,
            authorization_idempotency_key="blocked-auth-1",
        )
    )
    db_session.commit()
    assert result.authorization.authorization_status == "blocked"
    assert result.replacement_attempt is None
    assert task.status == "awaiting_recovery"
    assert task.state_version == recovery_input.task_state_version_at_creation

    with pytest.raises(ExecutionTaskRecoveryError) as invalid_source:
        service.create_recovery_input(
            CreateRecoveryInputCommand(
                execution_task_id=task.id,
                failed_attempt_id=created.attempt.id,
                recovery_source="validation_blocked",
                expected_task_state_version=task.state_version,
                input_idempotency_key="blocked-source-1",
            )
        )
    assert invalid_source.value.code == "recovery_source_unsupported"


def test_validation_rejection_is_authoritative_recovery_input_but_blocked_review_is_not(
    db_session,
):
    from test_phase29c7c_validation_run_acceptance import (
        _FailingReferenceValidator,
        _prepared_runtime,
        _validation_command,
    )
    from app.services.execution.candidate_evidence import (
        DeterministicValidatorRegistry,
    )
    from app.services.execution.validation_run import ValidationRunService
    from app.models import ExecutionTaskAcceptanceDecision

    task, outcome, specification = _prepared_runtime(db_session)
    registry = DeterministicValidatorRegistry(configuration_hash="a" * 64)
    registry.register(
        predicate_id="output_reference_exists",
        predicate_version=1,
        validator_id="output_reference_exists",
        validator_version=1,
        validator=_FailingReferenceValidator(),
    )
    validation = ValidationRunService(db_session).execute_validation_run(
        _validation_command(task, outcome, specification), registry=registry
    )
    db_session.commit()
    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()
    service = ExecutionTaskRecoveryService(db_session)
    recovery_input = service.create_recovery_input(
        CreateRecoveryInputCommand(
            execution_task_id=task.id,
            failed_attempt_id=outcome.execution_task_attempt_id,
            recovery_source="validation_rejected",
            expected_task_state_version=task.state_version,
            validation_run_id=validation.run.id,
            acceptance_decision_id=decision.id,
            input_idempotency_key="validation-rejection-input-1",
        )
    ).recovery_input
    db_session.commit()
    assert recovery_input.recovery_source == "validation_rejected"
    assert recovery_input.acceptance_decision_id == decision.id
    assert service.verify_recovery_input_integrity(recovery_input.id).verified

    for blocked_source in (
        "validation_blocked",
        "validation_error",
        "review_required",
    ):
        with pytest.raises(ExecutionTaskRecoveryError) as blocked:
            service.create_recovery_input(
                CreateRecoveryInputCommand(
                    execution_task_id=task.id,
                    failed_attempt_id=outcome.execution_task_attempt_id,
                    recovery_source=blocked_source,
                    expected_task_state_version=task.state_version,
                    input_idempotency_key=f"{blocked_source}-input-1",
                )
            )
        assert blocked.value.code == "recovery_source_unsupported"


def test_operator_required_is_distinct_and_consumes_no_budget_or_attempt(db_session):
    task, created, outcome = _failed_runtime(
        db_session,
        failure_category="runtime_exception",
        failure_code="operator_required",
    )
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome, key="operator-input-1"
    )
    result = service.authorize_recovery(
        AuthorizeRecoveryCommand(
            recovery_input_id=recovery_input.id,
            expected_task_state_version=task.state_version,
            authorization_idempotency_key="operator-auth-1",
        )
    )
    db_session.commit()
    assert result.authorization.authorization_status == "operator_required"
    assert result.authorization.operator_required is True
    assert (
        result.authorization.retry_budget_before
        == result.authorization.retry_budget_after
    )
    assert result.replacement_attempt is None
    assert result.transition is None
    assert task.status == "awaiting_recovery"


def test_atomic_finalization_rolls_back_authorization_attempt_and_transition(
    db_session, monkeypatch
):
    task, created, outcome = _failed_runtime(db_session)
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome, key="atomic-input-1"
    )

    def fail_transition(*args, **kwargs):
        raise ExecutionTaskRecoveryError(
            "recovery_decision_conflict", "injected transition failure"
        )

    monkeypatch.setattr(
        "app.services.execution.execution_task_recovery_service.ExecutionTaskTransitionService.transition",
        fail_transition,
    )
    with pytest.raises(ExecutionTaskRecoveryError):
        _authorize(db_session, recovery_input, key="atomic-auth-1")
    db_session.rollback()
    db_session.expire_all()
    assert task.status == "awaiting_recovery"
    assert db_session.query(ExecutionTaskRecoveryAuthorization).count() == 0
    assert db_session.query(ExecutionTaskAttempt).count() == 1
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(reason_code="recovery_retry_authorized")
        .count()
        == 0
    )


def test_scheduler_reentry_claims_and_dispatches_only_replacement_generation(
    db_session,
):
    task, created, outcome = _failed_runtime(db_session)
    service, recovery_input = _recovery_input(
        db_session, task, created.attempt, outcome
    )
    result = _authorize(db_session, recovery_input, key="reentry-auth-1")
    db_session.commit()
    replacement = result.replacement_attempt
    claim = _claimed(db_session, task, key="reentry-claim-1")
    dispatch = ExecutionTaskDispatchService(db_session).create_dispatch_intent(
        _intent_command(db_session, task, claim, key="reentry-dispatch-1")
    )
    assert dispatch.attempt.id == replacement.id
    assert dispatch.attempt.attempt_number == 2
    assert dispatch.attempt.predecessor_attempt_id == created.attempt.id
    assert dispatch.attempt.runtime_start is None
    assert dispatch.attempt.runtime_outcome is None
    assert dispatch.intent.id != created.intent.id


def test_recovery_migration_is_additive_replay_safe_and_creates_no_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c8.db'}")
    Base.metadata.create_all(engine)
    run_schema_migrations(engine)
    run_schema_migrations(engine)
    inspector = inspect(engine)
    assert {
        "execution_task_recovery_inputs",
        "execution_task_recovery_authorizations",
    } <= set(inspector.get_table_names())
    assert {
        "predecessor_attempt_id",
        "recovery_authorization_id",
        "strategy_parameter_hash",
    } <= {column["name"] for column in inspector.get_columns("execution_task_attempts")}
    with engine.connect() as connection:
        assert (
            connection.execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM execution_task_recovery_inputs"
                )
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM execution_task_recovery_authorizations"
                )
            ).scalar_one()
            == 0
        )
    engine.dispose()


def test_recovery_migration_rebuild_preserves_pre_phase_attempts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c8-preserve.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE execution_task_runtime_leases"))
        connection.execute(text("DROP TABLE execution_task_runtime_starts"))
        connection.execute(text("DROP TABLE execution_task_attempt_outcomes"))
        connection.execute(text("DROP TABLE execution_task_attempts"))
    pre_recovery = tuple(
        migration
        for migration in MIGRATIONS
        if migration.version <= "038_execution_task_validation_contract"
    )
    run_schema_migrations(engine, pre_recovery)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_task_attempts "
                "(id, execution_plan_id, execution_task_id, dispatch_intent_id, "
                "attempt_number, attempt_identity, broker_task_id, attempt_status, "
                "created_at) VALUES (1, 1, 1, 1, 1, 'legacy-attempt', "
                "'legacy-broker', 'submitted', '2026-07-23T00:00:00')"
            )
        )
    run_schema_migrations(engine)
    run_schema_migrations(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT attempt_identity, broker_task_id "
                "FROM execution_task_attempts WHERE id = 1"
            )
        ).one() == ("legacy-attempt", "legacy-broker")
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_task_recovery_inputs")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_task_recovery_authorizations")
            ).scalar_one()
            == 0
        )
    engine.dispose()
