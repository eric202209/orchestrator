"""Focused Phase 29D-3B apply-gated success and dependency eligibility tests."""

from __future__ import annotations

import json

import pytest

from app.models import (
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskChangeSet,
    ExecutionTaskTransition,
    ExecutionTaskValidationSpecification,
)
from app.services.execution.apply_requirement import (
    APPLY_NOT_REQUIRED,
    APPLY_REQUIRED,
    APPLY_REQUIREMENT_BLOCKED,
    determine_apply_requirement,
)
from app.services.execution.candidate_content import (
    CHANGESET_MEDIA_TYPE,
    CandidateContentIngestionService,
    IngestCandidateContentCommand,
    LocalContentAddressedStore,
)
from app.services.execution.changeset import (
    CHANGESET_FORMAT,
    ChangeSetIngestionService,
    IngestChangeSetCommand,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_transition_service import (
    ALLOWED_EXECUTION_TASK_TRANSITIONS,
    EXECUTION_TASK_REASON_CODES,
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
    _validate_transition_reason_contract,
)
from app.services.execution.validation_run import ValidationRunService

from test_phase29c2_execution_eligibility import (
    _build_context,
    _dependent,
    _predecessor,
)
from test_phase29c6b_runtime_evidence import _owned, _record_command, _start_command
from test_phase29c7b_evidence_validator import _contract as primitive_contract
from test_phase29c7b_evidence_validator import _structured_runtime
from test_phase29c7c_validation_run_acceptance import (
    _rebind_contract,
    _validation_command,
)
from test_phase29d1_changeset_apply_authorization import (
    _bypass_source_integrity,
    _evidence_reference,
)


def _structured_runtime_unconstrained_output(db_session):
    """Same as ``_structured_runtime`` (minus its contract binding, which the
    caller applies via ``_rebind_contract``) but records no runtime-declared
    output hash, so a real (non-fabricated) own candidate content row can be
    ingested afterward under any media type/payload the test needs, rather
    than being forced to match C6B's fixed OUTPUT_HASH fixture constant."""

    from app.services.execution.execution_task_runtime_execution_service import (
        ExecutionTaskRuntimeExecutionService,
    )

    task, created, ownership, _ = _owned(db_session)
    start = ExecutionTaskRuntimeExecutionService(
        db_session
    ).mark_runtime_execution_started(_start_command(task, created, ownership))
    db_session.commit()
    outcome = ExecutionTaskRuntimeExecutionService(
        db_session
    ).record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="candidate_completed",
            key=f"candidate-evidence-unconstrained-{task.id}",
        )
    )
    db_session.commit()
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    return task, created, outcome.outcome, specification


def _prepared_runtime_with_content(db_session, tmp_path, *, media_type):
    """Build one structured-executable task and, before validation, optionally
    ingest its own real candidate content under the given media type."""

    store = LocalContentAddressedStore(tmp_path)
    if media_type == CHANGESET_MEDIA_TYPE:
        task, created, outcome, specification = (
            _structured_runtime_unconstrained_output(db_session)
        )
        content = (
            CandidateContentIngestionService(db_session, store=store)
            .ingest(
                IngestCandidateContentCommand(
                    execution_plan_id=task.execution_plan_id,
                    execution_task_id=task.id,
                    execution_task_attempt_id=created.attempt.id,
                    attempt_generation=created.attempt.attempt_generation,
                    candidate_outcome_id=outcome.id,
                    content=b'{"format":"orchestrator-changeset/1"}',
                    media_type=media_type,
                    ingestion_idempotency_key=f"content-{task.id}",
                )
            )
            .content
        )
        db_session.commit()
    else:
        task, created, outcome, specification = _structured_runtime(db_session)
        content = None
        if media_type is not None:
            content = (
                CandidateContentIngestionService(db_session, store=store)
                .ingest(
                    IngestCandidateContentCommand(
                        execution_plan_id=task.execution_plan_id,
                        execution_task_id=task.id,
                        execution_task_attempt_id=created.attempt.id,
                        attempt_generation=created.attempt.attempt_generation,
                        candidate_outcome_id=outcome.id,
                        content=b"candidate",
                        media_type=media_type,
                        ingestion_idempotency_key=f"content-{task.id}",
                    )
                )
                .content
            )
            db_session.commit()
    contract = primitive_contract("output_reference_exists")
    _rebind_contract(db_session, task, specification, contract)
    db_session.commit()
    return task, created, outcome, specification, store, content


# ---------------------------------------------------------------------------
# Apply requirement policy
# ---------------------------------------------------------------------------


def test_no_candidate_content_is_apply_not_required(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(db_session, tmp_path, media_type=None)
    )
    decision = determine_apply_requirement(
        db_session, candidate_outcome_id=outcome.id, store=store
    )
    assert decision.outcome == APPLY_NOT_REQUIRED
    assert decision.candidate_content_id is None


def test_non_changeset_media_type_is_apply_not_required(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(db_session, tmp_path, media_type="text/plain")
    )
    decision = determine_apply_requirement(
        db_session, candidate_outcome_id=outcome.id, store=store
    )
    assert decision.outcome == APPLY_NOT_REQUIRED
    assert decision.candidate_content_id == content.id


def test_changeset_media_type_is_apply_required(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(
            db_session, tmp_path, media_type=CHANGESET_MEDIA_TYPE
        )
    )
    decision = determine_apply_requirement(
        db_session, candidate_outcome_id=outcome.id, store=store
    )
    assert decision.outcome == APPLY_REQUIRED
    assert decision.candidate_content_id == content.id


def test_tampered_content_fails_closed_as_blocked(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(
            db_session, tmp_path, media_type=CHANGESET_MEDIA_TYPE
        )
    )
    content.content_sha256 = "f" * 64
    db_session.commit()
    decision = determine_apply_requirement(
        db_session, candidate_outcome_id=outcome.id, store=store
    )
    assert decision.outcome == APPLY_REQUIREMENT_BLOCKED
    assert decision.blocked_reasons


# ---------------------------------------------------------------------------
# Acceptance finalization routing
# ---------------------------------------------------------------------------


def test_non_apply_accepted_candidate_still_succeeds(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(db_session, tmp_path, media_type=None)
    )
    service = ValidationRunService(db_session, content_store=store)
    result = service.execute_validation_run(
        _validation_command(task, outcome, specification)
    )
    db_session.refresh(task)
    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()
    assert result.run.run_status == "accepted"
    assert decision.decision_status == "accepted"
    assert task.status == "succeeded"
    assert decision.resulting_task_state == "succeeded"
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="succeeded")
        .count()
        == 1
    )


def test_apply_required_accepted_candidate_enters_awaiting_apply(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(
            db_session, tmp_path, media_type=CHANGESET_MEDIA_TYPE
        )
    )
    service = ValidationRunService(db_session, content_store=store)
    result = service.execute_validation_run(
        _validation_command(task, outcome, specification)
    )
    db_session.refresh(task)
    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()
    assert result.run.run_status == "accepted"
    assert decision.decision_status == "accepted"
    assert task.status == "awaiting_apply"
    assert decision.resulting_task_state == "awaiting_apply"
    assert (
        not db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="succeeded")
        .count()
    )
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="awaiting_apply")
        .count()
        == 1
    )


def test_apply_required_finalization_is_idempotent_on_replay(db_session, tmp_path):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(
            db_session, tmp_path, media_type=CHANGESET_MEDIA_TYPE
        )
    )
    service = ValidationRunService(db_session, content_store=store)
    command = _validation_command(task, outcome, specification)
    first = service.execute_validation_run(command)
    replay = service.execute_validation_run(command)
    assert replay.run.id == first.run.id
    assert db_session.query(ExecutionTaskAcceptanceDecision).count() == 1
    db_session.refresh(task)
    assert task.status == "awaiting_apply"
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="awaiting_apply")
        .count()
        == 1
    )


def test_ambiguous_content_blocks_acceptance_and_leaves_task_awaiting_validation(
    db_session, tmp_path
):
    task, created, outcome, specification, store, content = (
        _prepared_runtime_with_content(
            db_session, tmp_path, media_type=CHANGESET_MEDIA_TYPE
        )
    )
    content.content_sha256 = "f" * 64
    db_session.commit()
    service = ValidationRunService(db_session, content_store=store)
    result = service.execute_validation_run(
        _validation_command(task, outcome, specification)
    )
    db_session.refresh(task)
    assert result.run.run_status == "blocked"
    assert task.status == "awaiting_validation"
    assert (
        not db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="succeeded")
        .count()
    )
    assert (
        not db_session.query(ExecutionTaskTransition)
        .filter_by(to_state="awaiting_apply")
        .count()
    )


# ---------------------------------------------------------------------------
# Dependency eligibility
#
# Reuses the Phase 29C-2 accepted two-task dependent plan fixture (a real
# committed graph, not a fabricated task/edge) and drives the predecessor
# directly through the lifecycle to `awaiting_apply` with the exact reason
# codes the transition boundary requires, to prove dependents stay blocked.
# ---------------------------------------------------------------------------


def _advance(db, task, to_state, *, reason_code, key=None):
    return ExecutionTaskTransitionService(db).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state=task.status,
            expected_state_version=task.state_version,
            to_state=to_state,
            reason_code=reason_code,
            actor_type="test",
            actor_id="d3b-eligibility-test",
            idempotency_key=key or f"{task.id}-{to_state}-{task.state_version}",
        )
    )


def test_apply_pending_predecessor_does_not_release_dependent(db_session):
    context = _build_context(db_session)
    db = context["db"]
    predecessor = _predecessor(context)
    dependent = _dependent(context)

    _advance(db, predecessor, "ready", reason_code="dependencies_satisfied")
    _advance(db, predecessor, "running", reason_code="execution_started")
    _advance(
        db,
        predecessor,
        "awaiting_validation",
        reason_code="runtime_candidate_completed",
    )
    _advance(db, predecessor, "awaiting_apply", reason_code="validation_accepted")
    db.flush()
    assert predecessor.status == "awaiting_apply"

    decision = ExecutionEligibilityService(db).evaluate_task(dependent.id)
    assert decision.eligible is False
    assert decision.dependency_results[0].result == "waiting"
    assert decision.dependency_results[0].reason_code == "predecessor_awaiting_apply"
    assert decision.recommended_state != "ready"

    reconciliation = ExecutionEligibilityService(db).reconcile_task(
        dependent.id, "pending", 0, "d3b-eligibility-test", "d3b-reconcile-1"
    )
    assert reconciliation.no_op is True
    assert dependent.status == "pending"


# ---------------------------------------------------------------------------
# D-1 ChangeSet compatibility from a non-terminal owning task
# ---------------------------------------------------------------------------


def test_changeset_ingestion_succeeds_from_apply_pending_task(
    db_session, tmp_path, monkeypatch
):
    task, created, outcome, specification = _structured_runtime_unconstrained_output(
        db_session
    )
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="d3b-ev-1", store=store
    )
    payload = {
        "format": CHANGESET_FORMAT,
        "base_state": {"project_id": task.execution_plan.project_id},
        "operations": [
            {
                "operation": "create_file",
                "path": "src/d3b_example.py",
                "content_reference": evidence_ref,
            }
        ],
    }
    content = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(
            IngestCandidateContentCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                attempt_generation=created.attempt.attempt_generation,
                candidate_outcome_id=outcome.id,
                content=json.dumps(payload).encode("utf-8"),
                media_type=CHANGESET_MEDIA_TYPE,
                ingestion_idempotency_key=f"content-{task.id}",
            )
        )
        .content
    )
    db_session.commit()
    contract = primitive_contract("output_reference_exists")
    _rebind_contract(db_session, task, specification, contract)
    db_session.commit()

    service = ValidationRunService(db_session, content_store=store)
    service.execute_validation_run(_validation_command(task, outcome, specification))
    db_session.commit()
    db_session.refresh(task)
    assert task.status == "awaiting_apply"

    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()
    assert decision.decision_status == "accepted"

    # Pre-existing (pre-D-3B) gap, not introduced here: runtime outcome
    # integrity still asserts the owning task is in {awaiting_validation,
    # awaiting_recovery}, so it already needed bypassing for a `succeeded`
    # owner too (see test_phase29d1's `_bypass_source_integrity`). An
    # `awaiting_apply` owner hits the identical, already-scoped-out check.
    _bypass_source_integrity(monkeypatch)
    changeset_service = ChangeSetIngestionService(db_session, store=store)
    result = changeset_service.ingest(
        IngestChangeSetCommand(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=created.attempt.id,
            attempt_generation=created.attempt.attempt_generation,
            candidate_outcome_id=outcome.id,
            acceptance_decision_id=decision.id,
            source_candidate_content_id=content.id,
            ingestion_idempotency_key="d3b-changeset-1",
        )
    )
    db_session.commit()
    assert result.change_set.operation_count == 1
    assert db_session.query(ExecutionTaskChangeSet).count() == 1

    db_session.refresh(task)
    assert task.status == "awaiting_apply"


# ---------------------------------------------------------------------------
# Lifecycle invariants
# ---------------------------------------------------------------------------


def test_no_transition_out_of_succeeded_exists():
    assert ALLOWED_EXECUTION_TASK_TRANSITIONS["succeeded"] == frozenset()


def test_awaiting_apply_transitions_are_bounded():
    assert ALLOWED_EXECUTION_TASK_TRANSITIONS["awaiting_apply"] == frozenset(
        {"succeeded", "awaiting_recovery", "paused", "cancelled"}
    )


def test_reserved_future_transitions_exist_and_are_not_invoked_by_d3b():
    assert "controlled_apply_verified" in EXECUTION_TASK_REASON_CODES
    assert "controlled_apply_failed" in EXECUTION_TASK_REASON_CODES


def test_reserved_apply_verified_transition_requires_dedicated_reason():
    # Structural-only check: the reason contract must reject a generic actor
    # attempting to fabricate `awaiting_apply -> succeeded` without the
    # dedicated reason reserved for verified post-apply success.
    with pytest.raises(ExecutionTaskTransitionError) as excinfo:
        _validate_transition_reason_contract(
            from_state="awaiting_apply",
            to_state="succeeded",
            reason_code="system_reconciliation",
            actor_type="system",
        )
    assert excinfo.value.code == "transition_reason_not_authorized"
    # Structurally valid with the reserved reason; D-3B never calls this path.
    _validate_transition_reason_contract(
        from_state="awaiting_apply",
        to_state="succeeded",
        reason_code="controlled_apply_verified",
        actor_type="system",
    )
