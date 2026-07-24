"""Focused Phase 29D-4 post-apply validation, recovery, and lifecycle tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from app.models import (
    ExecutionTask,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskApplyResult,
    ExecutionTaskChangeSet,
    ExecutionTaskPostApplyValidation,
    ExecutionTaskRecoveryDecision,
    ExecutionTaskRecoveryResult,
    ExecutionTaskTransition,
)
from app.services.execution.apply_execution import (
    ApplyExecutionService,
    ExecuteApplyCommand,
)
from app.services.execution.apply_lifecycle import (
    ApplyLifecycleError,
    CompleteControlledApplyCommand,
    ExecutionTaskApplyLifecycleService,
)
from app.services.execution.apply_recovery import (
    DecideRecoveryCommand,
    ExecuteRecoveryCommand,
    RecoveryDecisionService,
    RecoveryExecutionService,
)
from app.services.execution.candidate_content import (
    CHANGESET_MEDIA_TYPE,
    CandidateContentIngestionService,
    IngestCandidateContentCommand,
    LocalContentAddressedStore,
    verify_candidate_content_integrity,
)
from app.services.execution.changeset import (
    CHANGESET_FORMAT,
    ChangeSetIngestionService,
    IngestChangeSetCommand,
)
from app.services.execution.controlled_apply import (
    ApplyApprovalService,
    ApplyAttemptService,
    ApplyAuthorizationV2Service,
    AuthorizeApplyV2Command,
    CreateApplyApprovalCommand,
    CreateApplyAttemptCommand,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_transition_service import (
    ALLOWED_EXECUTION_TASK_TRANSITIONS,
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
)
from app.services.execution.post_apply_validation import (
    PostApplyValidationService,
    ValidatePostApplyCommand,
)
from app.services.execution.workspace_authority import (
    WorkspaceBaseStateService,
    WorkspaceTargetService,
)

from test_phase29c2_execution_eligibility import (
    _build_context,
    _dependent,
    _predecessor,
)
from test_phase29d3_controlled_apply import _evidence
from test_phase29d3b_apply_gated_success import _structured_runtime_unconstrained_output
from test_phase29c7b_evidence_validator import _contract as primitive_contract
from test_phase29c7c_validation_run_acceptance import (
    _rebind_contract,
    _validation_command,
)
from app.services.execution.validation_run import ValidationRunService


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fake_git(monkeypatch):
    def run_git(root, args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(root).encode()
        if args == ("rev-parse", "HEAD"):
            return ("a" * 40).encode()
        if args == ("status", "--porcelain=v1", "-z"):
            return b""
        raise AssertionError(args)

    monkeypatch.setattr("app.services.execution.workspace_authority._run_git", run_git)


def _awaiting_apply_authority(
    db_session, tmp_path, monkeypatch, *, operations, initial_files=None
):
    """Build one real, non-bypassed accepted-into-`awaiting_apply` task with a
    real ChangeSet/Apply Attempt, ready for ``ApplyExecutionService``.

    Unlike the D-3/D-3A/D-3B fixtures (which either accept unrelated content
    or fabricate the ChangeSet's source content), this ingests the exact
    ChangeSet JSON as the accepted outcome's own real candidate content
    *before* validation runs, so ``determine_apply_requirement`` legitimately
    routes acceptance to `awaiting_apply` and every later integrity
    verification (ChangeSet creation, Controlled Apply) runs unbypassed.
    """

    _fake_git(monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src").mkdir()
    for relative, content in (initial_files or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(
        "app.services.execution.workspace_authority.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )

    task, created, outcome, specification = _structured_runtime_unconstrained_output(
        db_session
    )
    store = LocalContentAddressedStore(tmp_path / "content-store")
    evidence_refs = {}
    for key, content in {
        item["content_key"]: item["content"]
        for item in operations
        if item.get("content_key")
    }.items():
        evidence_refs[key] = _evidence(
            db_session, task, created, store, content=content, key=f"evidence-{key}"
        )
    canonical_operations = []
    for item in operations:
        operation = {
            key: evidence_refs.get(value, value)
            for key, value in item.items()
            if key not in {"content_key", "content"}
        }
        if item.get("content_key"):
            operation["content_reference"] = evidence_refs[item["content_key"]]
        canonical_operations.append(operation)

    task_project = task.execution_plan.project
    task_project.workspace_path = "workspace"
    changeset_payload = {
        "format": CHANGESET_FORMAT,
        "base_state": {"project_id": task_project.id},
        "operations": canonical_operations,
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
                content=json.dumps(changeset_payload).encode("utf-8"),
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
    ValidationRunService(db_session, content_store=store).execute_validation_run(
        _validation_command(task, outcome, specification)
    )
    db_session.commit()
    db_session.refresh(task)
    assert task.status == "awaiting_apply"
    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()

    assert verify_candidate_content_integrity(
        db_session, content.id, store=store
    ).verified

    change_set = (
        ChangeSetIngestionService(db_session, store=store)
        .ingest(
            IngestChangeSetCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                attempt_generation=created.attempt.attempt_generation,
                candidate_outcome_id=outcome.id,
                acceptance_decision_id=decision.id,
                source_candidate_content_id=content.id,
                ingestion_idempotency_key=f"changeset-{task.id}",
            )
        )
        .change_set
    )
    target = (
        WorkspaceTargetService(db_session)
        .register(task_project.id, registration_idempotency_key=f"target-{task.id}")
        .target
    )
    base_state = (
        WorkspaceBaseStateService(db_session)
        .inspect(
            workspace_target_id=target.id,
            change_set_id=change_set.id,
            observation_idempotency_key=f"base-{task.id}",
        )
        .base_state
    )
    approval = (
        ApplyApprovalService(db_session)
        .decide(
            CreateApplyApprovalCommand(
                change_set_id=change_set.id,
                workspace_target_id=target.id,
                base_state_id=base_state.id,
                decision="approved",
                reviewed_summary_payload={"operation_count": len(operations)},
                approval_idempotency_key=f"approval-{task.id}",
            )
        )
        .approval
    )
    authorization = (
        ApplyAuthorizationV2Service(db_session, store=store)
        .authorize(
            AuthorizeApplyV2Command(
                change_set_id=change_set.id,
                workspace_target_id=target.id,
                base_state_id=base_state.id,
                approval_id=approval.id,
                authorization_idempotency_key=f"authorization-{task.id}",
            )
        )
        .authorization
    )
    apply_attempt = (
        ApplyAttemptService(db_session)
        .create(
            CreateApplyAttemptCommand(
                authorization_id=authorization.id,
                approval_id=approval.id,
                apply_attempt_idempotency_key=f"apply-attempt-{task.id}",
            )
        )
        .apply_attempt
    )
    db_session.commit()
    return {
        "db": db_session,
        "root": root,
        "store": store,
        "task": task,
        "attempt": apply_attempt,
    }


def _apply(context):
    outcome = ApplyExecutionService(context["db"], store=context["store"]).execute(
        ExecuteApplyCommand(context["attempt"].id)
    )
    context["db"].commit()
    return outcome.result


# ---------------------------------------------------------------------------
# Lifecycle closure: success path
# ---------------------------------------------------------------------------


def test_successful_validation_transitions_awaiting_apply_to_succeeded(
    db_session, tmp_path, monkeypatch
):
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            }
        ],
    )
    result = _apply(context)
    assert result.status == "applied"

    outcome = ExecutionTaskApplyLifecycleService(
        db_session, store=context["store"]
    ).complete(CompleteControlledApplyCommand(apply_result_id=result.id))
    db_session.commit()

    assert outcome.to_state == "succeeded"
    assert outcome.transition_replayed is False
    assert outcome.recovery_decision_id is None
    assert outcome.recovery_result_id is None
    task = db_session.get(ExecutionTask, context["task"].id)
    assert task.status == "succeeded"

    validation = db_session.get(
        ExecutionTaskPostApplyValidation, outcome.post_apply_validation_id
    )
    assert validation.status == "passed"
    assert validation.apply_result_id == result.id
    assert validation.checked_operation_count == 1
    assert (
        db_session.query(ExecutionTaskRecoveryDecision).count() == 0
    ), "success path must not fabricate a recovery decision"


def test_accepted_awaiting_apply_content_passes_legitimate_integrity(
    db_session, tmp_path, monkeypatch
):
    """The D-3B blocked report flagged that `verify_attempt_outcome_integrity`
    rejected a legitimately `awaiting_apply` owner. This is the D-4 fix under
    direct test: no bypass is installed anywhere in this fixture."""

    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            }
        ],
    )
    assert db_session.get(ExecutionTask, context["task"].id).status == "awaiting_apply"
    change_set = db_session.query(ExecutionTaskChangeSet).one()
    assert change_set.operation_count == 1


def test_no_transition_out_of_succeeded_exists():
    assert ALLOWED_EXECUTION_TASK_TRANSITIONS["succeeded"] == frozenset()


# ---------------------------------------------------------------------------
# Dependency eligibility around the awaiting_apply -> succeeded boundary
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
            actor_id="d4-eligibility-test",
            idempotency_key=key or f"{task.id}-{to_state}-{task.state_version}",
        )
    )


def test_dependency_blocked_before_and_released_after_verified_success(db_session):
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

    before = ExecutionEligibilityService(db).evaluate_task(dependent.id)
    assert before.eligible is False
    assert before.dependency_results[0].reason_code == "predecessor_awaiting_apply"

    _advance(db, predecessor, "succeeded", reason_code="controlled_apply_verified")
    db.flush()

    after = ExecutionEligibilityService(db).evaluate_task(dependent.id)
    assert after.eligible is True
    assert after.recommended_state == "ready"


# ---------------------------------------------------------------------------
# Rollback mechanics (create/replace/delete, drift, IO failure)
# ---------------------------------------------------------------------------


def _rollback_after_validation_failure(context, result):
    """Tamper a path so post-apply validation fails, then drive recovery."""

    validation = (
        PostApplyValidationService(context["db"], store=context["store"])
        .validate(ValidatePostApplyCommand(apply_result_id=result.id))
        .validation
    )
    decision = (
        RecoveryDecisionService(context["db"])
        .decide(DecideRecoveryCommand(apply_result_id=result.id))
        .decision
    )
    recovery_result = (
        RecoveryExecutionService(context["db"], store=context["store"])
        .execute(ExecuteRecoveryCommand(recovery_decision_id=decision.id))
        .result
    )
    context["db"].commit()
    return validation, decision, recovery_result


def test_create_replace_delete_rollback_succeeds(db_session, tmp_path, monkeypatch):
    old = b"old bytes\n"
    removed = b"deleted bytes\n"
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/replace.txt": old, "src/delete.txt": removed},
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            },
            {
                "operation": "replace_file",
                "path": "src/replace.txt",
                "expected_previous_sha256": _sha(old),
                "content_reference": "placeholder",
                "content_key": "replace",
                "content": b"replaced\n",
            },
            {
                "operation": "delete_file",
                "path": "src/delete.txt",
                "expected_previous_sha256": _sha(removed),
            },
        ],
    )
    result = _apply(context)
    assert result.status == "applied"
    root = context["root"]
    assert (root / "src/create.txt").read_bytes() == b"created\n"
    assert (root / "src/replace.txt").read_bytes() == b"replaced\n"
    assert not (root / "src/delete.txt").exists()

    # Tamper the replaced file after apply so post-apply validation fails and
    # rollback is genuinely required.
    (root / "src/replace.txt").write_bytes(b"corrupted\n")

    validation, decision, recovery_result = _rollback_after_validation_failure(
        context, result
    )
    assert validation.status == "failed"
    assert decision.decision == "rollback_required"
    assert recovery_result.status == "recovered"
    assert not (root / "src/create.txt").exists()
    assert (root / "src/replace.txt").read_bytes() == old
    assert (root / "src/delete.txt").read_bytes() == removed


def test_rollback_drift_blocks(db_session, tmp_path, monkeypatch):
    old = b"before\n"
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/file.txt": old},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(old),
                "content_reference": "placeholder",
                "content_key": "replacement",
                "content": b"after\n",
            }
        ],
    )
    result = _apply(context)
    assert result.status == "applied"
    root = context["root"]
    assert (root / "src/file.txt").read_bytes() == b"after\n"

    # Tamper so validation fails, observing "validation-time bytes"...
    (root / "src/file.txt").write_bytes(b"validation-time bytes\n")
    validation = (
        PostApplyValidationService(db_session, store=context["store"])
        .validate(ValidatePostApplyCommand(apply_result_id=result.id))
        .validation
    )
    assert validation.status == "failed"

    # ...then drift again before rollback actually runs: this no longer
    # matches what validation observed, the ChangeSet's promised post-apply
    # state, or the pre-apply snapshot -- genuine, unrecoverable drift.
    (root / "src/file.txt").write_bytes(b"rollback-time drift\n")

    decision = (
        RecoveryDecisionService(db_session)
        .decide(DecideRecoveryCommand(apply_result_id=result.id))
        .decision
    )
    assert decision.decision == "rollback_required"
    recovery_result = (
        RecoveryExecutionService(db_session, store=context["store"])
        .execute(ExecuteRecoveryCommand(recovery_decision_id=decision.id))
        .result
    )
    db_session.commit()
    assert recovery_result.status == "blocked"
    assert recovery_result.failure_reason == "rollback_drift_detected"
    assert (root / "src/file.txt").read_bytes() == b"rollback-time drift\n"


def test_rollback_io_failure_requires_intervention(db_session, tmp_path, monkeypatch):
    old = b"before\n"
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/file.txt": old},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(old),
                "content_reference": "placeholder",
                "content_key": "replacement",
                "content": b"after\n",
            }
        ],
    )
    result = _apply(context)
    assert result.status == "applied"
    (context["root"] / "src/file.txt").write_bytes(b"tampered\n")

    validation = (
        PostApplyValidationService(db_session, store=context["store"])
        .validate(ValidatePostApplyCommand(apply_result_id=result.id))
        .validation
    )
    assert validation.status == "failed"
    decision = (
        RecoveryDecisionService(db_session)
        .decide(DecideRecoveryCommand(apply_result_id=result.id))
        .decision
    )
    assert decision.decision == "rollback_required"

    def _broken_mkstemp(*args, **kwargs):
        raise OSError("simulated rollback IO failure")

    monkeypatch.setattr(
        "app.services.execution.apply_recovery.tempfile.mkstemp", _broken_mkstemp
    )
    recovery_result = (
        RecoveryExecutionService(db_session, store=context["store"])
        .execute(ExecuteRecoveryCommand(recovery_decision_id=decision.id))
        .result
    )
    db_session.commit()
    assert recovery_result.status == "failed"
    assert recovery_result.failure_reason == "rollback_io_failure"


# ---------------------------------------------------------------------------
# Replay and tamper-detection
# ---------------------------------------------------------------------------


def test_replay_does_not_duplicate_validation_recovery_or_transition(
    db_session, tmp_path, monkeypatch
):
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            }
        ],
    )
    result = _apply(context)
    service = ExecutionTaskApplyLifecycleService(db_session, store=context["store"])
    first = service.complete(CompleteControlledApplyCommand(apply_result_id=result.id))
    db_session.commit()
    second = service.complete(CompleteControlledApplyCommand(apply_result_id=result.id))
    db_session.commit()

    assert first.to_state == second.to_state == "succeeded"
    assert first.transition_replayed is False
    assert second.transition_replayed is True
    assert first.post_apply_validation_id == second.post_apply_validation_id
    assert (
        db_session.query(ExecutionTaskPostApplyValidation).count() == 1
    ), "replay must not duplicate the validation authority"
    assert (
        db_session.query(ExecutionTaskTransition)
        .filter(ExecutionTaskTransition.to_state == "succeeded")
        .count()
        == 1
    ), "replay must not duplicate the lifecycle transition"


def test_tampered_apply_result_fails_closed(db_session, tmp_path, monkeypatch):
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            }
        ],
    )
    result = _apply(context)
    assert result.status == "applied"

    db_session.execute(
        text(
            "UPDATE execution_task_apply_results SET canonical_sha256 = "
            "'0' || substr(canonical_sha256, 2) WHERE id = :id"
        ),
        {"id": result.id},
    )
    db_session.commit()

    validation = (
        PostApplyValidationService(db_session, store=context["store"])
        .validate(ValidatePostApplyCommand(apply_result_id=result.id))
        .validation
    )
    assert validation.status == "blocked"
    assert validation.failure_reason == "apply_result_integrity_failure"


def test_blocked_apply_result_requires_no_recovery(db_session, tmp_path, monkeypatch):
    old = b"before\n"
    context = _awaiting_apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/file.txt": old},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(old),
                "content_reference": "placeholder",
                "content_key": "replacement",
                "content": b"after\n",
            }
        ],
    )
    (context["root"] / "src/file.txt").write_bytes(b"already drifted\n")
    result = _apply(context)
    assert result.status == "blocked"

    decision = (
        RecoveryDecisionService(db_session)
        .decide(DecideRecoveryCommand(apply_result_id=result.id))
        .decision
    )
    assert decision.decision == "no_recovery_required"
    recovery_result = (
        RecoveryExecutionService(db_session, store=context["store"])
        .execute(ExecuteRecoveryCommand(recovery_decision_id=decision.id))
        .result
    )
    assert recovery_result.status == "recovered"
    assert recovery_result.rolled_back_operations == []

    outcome = ExecutionTaskApplyLifecycleService(
        db_session, store=context["store"]
    ).complete(CompleteControlledApplyCommand(apply_result_id=result.id))
    db_session.commit()
    assert outcome.to_state == "awaiting_recovery"
    task = db_session.get(ExecutionTask, context["task"].id)
    assert task.status == "awaiting_recovery"
