"""Focused tests for Phase 29B-2 Planning-to-Execution Commit Boundary."""

from __future__ import annotations

import uuid

import pytest

from app.models import (
    ExecutionPlan,
    PlanningCheckpoint,
    PlanningCommitManifest,
    PlanningCompletionManifest,
    PlanningReviewEvent,
    PlanningSession,
    Project,
    Task,
    TaskExecution,
)
from app.services.planning.execution_commit import (
    ExecutionCommitError,
    ExecutionCommitRequest,
    PlanningExecutionCommitService,
)
from app.services.planning.operator_review import ReviewActor
from app.services.planning.operator_review_persistence import OperatorReviewService
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)
from app.services.planning.structured_task_plan import validate_structured_task_plan

from app.tests.test_phase29b1_execution_plan_commit_service import (
    _brief,
    _manifest,
    _plan,
    _seed_session,
)


def _actor(subject: str = "operator@example.test") -> ReviewActor:
    return ReviewActor(subject, "project_owner", "project_owner")


def _build_pending_review_session(db_session):
    """Build a Protocol v2 session with an accepted Brief and a
    review-required (failed) Structured Task Plan candidate, review opened."""

    project, session = _seed_session(db_session)
    persistence = PlanningProtocolPersistenceService(db_session)

    manifest = _manifest(session.id, session.generation_id)
    persistence.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    brief_checkpoint = persistence.record_planning_brief(
        session.id,
        brief=brief,
        stage_generation_id="brief-stage",
        attempt_id="brief-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )

    from dataclasses import replace as _replace
    from app.services.planning.structured_task_plan import (
        BriefReference,
        InputManifestReference,
    )

    plan = _plan(manifest_id=manifest.manifest_id)
    plan = _replace(
        plan,
        brief_ref=BriefReference(str(brief_checkpoint.id), brief.content_hash),
        input_manifest_ref=InputManifestReference(
            manifest.manifest_id, manifest.manifest_hash
        ),
    )
    validation = validate_structured_task_plan(
        plan, brief=brief, input_manifest=manifest
    )
    assert validation.protocol_acceptable, validation.errors

    candidate = persistence.record_structured_task_plan(
        session.id,
        task_plan=plan,
        validation=validation,
        status="failed",
        stage_generation_id="task-plan-stage",
        attempt_id="task-plan-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=(brief_checkpoint.id,),
        review_reason_codes=("explicit_operator_review",),
    )
    session.status = "failed"
    db_session.commit()

    reviews = OperatorReviewService(db_session)
    review = reviews.open_review_for_candidate(session.id, candidate.id)
    # A real Planning session releases its processing lease once generation
    # finishes; the execution-commit boundary is only ever invoked well
    # after that, asynchronously.  Clear it here to match production state.
    session.processing_token = None
    session.processing_started_at = None
    db_session.commit()
    return project, session, plan, brief_checkpoint, candidate, review


def _build_approved_session(db_session, *, idempotency_key="approve-1"):
    project, session, plan, brief_checkpoint, candidate, review = (
        _build_pending_review_session(db_session)
    )
    reviews = OperatorReviewService(db_session)
    result = reviews.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key=idempotency_key,
        comment="The exact canonical Task Plan is approved unchanged.",
    )
    db_session.commit()
    promotion_checkpoint = db_session.get(
        PlanningCheckpoint, result.promotion.checkpoint_id
    )
    return project, session, plan, review.review_id, result, promotion_checkpoint


def _request_for(promotion_checkpoint, session, *, idempotency_key="exec-commit-1"):
    return ExecutionCommitRequest(
        idempotency_key=idempotency_key,
        structured_task_plan_checkpoint_id=promotion_checkpoint.id,
        structured_task_plan_hash=promotion_checkpoint.content_hash,
        expected_session_generation_id=session.generation_id,
    )


def test_approved_task_plan_releases_successfully_with_execution_graph(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    result = service.commit(session.id, _request_for(promotion_checkpoint, session))

    assert result.boundary_state == "released"
    assert result.integrity_status == "valid"
    assert result.idempotent_replay is False
    assert result.review_id == review_id
    assert result.approval_event_id == approval.event_id
    assert result.structured_task_plan_hash == promotion_checkpoint.content_hash
    assert result.execution_plan_id is not None
    assert result.task_count == len(plan.tasks)
    assert result.dependency_edge_count == len(plan.dependencies)
    assert result.group_count == len(plan.execution_groups)

    manifest = db_session.get(
        PlanningCommitManifest, result.planning_commit_manifest_id
    )
    assert manifest.task_provenance["schema"] == "planning_execution_commit.v1"
    assert manifest.task_provenance["structured_task_plan_hash"] == plan.content_hash
    assert sorted(manifest.task_provenance["task_ids"]) == sorted(
        task.id for task in plan.tasks
    )
    assert manifest.task_provenance["review_id"] == review_id

    execution_plan = db_session.get(ExecutionPlan, result.execution_plan_id)
    assert execution_plan.planning_commit_manifest_id == manifest.id
    assert execution_plan.source_plan_hash == plan.content_hash


def test_first_commit_creates_exactly_one_planning_commit_manifest(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    service.commit(session.id, _request_for(promotion_checkpoint, session))

    manifests = (
        db_session.query(PlanningCommitManifest)
        .filter(PlanningCommitManifest.planning_session_id == session.id)
        .all()
    )
    assert len(manifests) == 1


def test_identical_replay_returns_same_manifest_and_execution_plan(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    first = service.commit(session.id, _request_for(promotion_checkpoint, session))
    second = service.commit(
        session.id,
        _request_for(promotion_checkpoint, session, idempotency_key="different-key"),
    )

    assert second.idempotent_replay is True
    assert second.planning_commit_manifest_id == first.planning_commit_manifest_id
    assert second.execution_plan_id == first.execution_plan_id

    manifests = (
        db_session.query(PlanningCommitManifest)
        .filter(PlanningCommitManifest.planning_session_id == session.id)
        .all()
    )
    assert len(manifests) == 1
    plans = (
        db_session.query(ExecutionPlan)
        .filter(ExecutionPlan.planning_session_id == session.id)
        .all()
    )
    assert len(plans) == 1


def test_protocol_v1_session_is_rejected(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    session.protocol_version = "v1"
    db_session.commit()

    service = PlanningExecutionCommitService(db_session)
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, _request_for(promotion_checkpoint, session))
    assert exc_info.value.code == "protocol_v2_required"


def test_pending_review_is_rejected(db_session):
    project, session, plan, brief_checkpoint, candidate, review = (
        _build_pending_review_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-pending",
        structured_task_plan_checkpoint_id=candidate.id,
        structured_task_plan_hash=candidate.content_hash,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "task_plan_not_approved"


def test_rejected_review_is_rejected(db_session):
    project, session, plan, brief_checkpoint, candidate, review = (
        _build_pending_review_session(db_session)
    )
    reviews = OperatorReviewService(db_session)
    reviews.reject_review(
        review.review_id,
        _actor(),
        idempotency_key="reject-1",
        reason="Needs another look before release.",
    )
    db_session.commit()

    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-rejected",
        structured_task_plan_checkpoint_id=candidate.id,
        structured_task_plan_hash=candidate.content_hash,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "task_plan_not_approved"


def test_cancelled_review_is_rejected(db_session):
    project, session, plan, brief_checkpoint, candidate, review = (
        _build_pending_review_session(db_session)
    )
    reviews = OperatorReviewService(db_session)
    reviews.cancel_review(
        review.review_id,
        _actor(),
        idempotency_key="cancel-1",
        reason="Superseded by a fresh request.",
    )
    db_session.commit()

    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-cancelled",
        structured_task_plan_checkpoint_id=candidate.id,
        structured_task_plan_hash=candidate.content_hash,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "task_plan_not_approved"


def test_unreviewed_directly_accepted_task_plan_is_rejected(db_session):
    """An accepted Structured Task Plan with no promotion_review_event_id
    (i.e. never went through operator review) must not be releasable."""

    project, session = _seed_session(db_session)
    persistence = PlanningProtocolPersistenceService(db_session)
    manifest = _manifest(session.id, session.generation_id)
    persistence.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    brief_checkpoint = persistence.record_planning_brief(
        session.id,
        brief=brief,
        stage_generation_id="brief-stage",
        attempt_id="brief-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )
    from dataclasses import replace as _replace
    from app.services.planning.structured_task_plan import (
        BriefReference,
        InputManifestReference,
    )

    plan = _plan(manifest_id=manifest.manifest_id)
    plan = _replace(
        plan,
        brief_ref=BriefReference(str(brief_checkpoint.id), brief.content_hash),
        input_manifest_ref=InputManifestReference(
            manifest.manifest_id, manifest.manifest_hash
        ),
    )
    validation = validate_structured_task_plan(
        plan, brief=brief, input_manifest=manifest
    )
    checkpoint = persistence.record_structured_task_plan(
        session.id,
        task_plan=plan,
        validation=validation,
        stage_generation_id="task-plan-stage",
        attempt_id="task-plan-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=(brief_checkpoint.id,),
    )
    session.processing_token = None
    session.processing_started_at = None
    db_session.commit()
    assert checkpoint.promotion_review_event_id is None

    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-unreviewed",
        structured_task_plan_checkpoint_id=checkpoint.id,
        structured_task_plan_hash=checkpoint.content_hash,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "task_plan_not_approved"


def test_wrong_checkpoint_id_is_rejected(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-wrong-id",
        structured_task_plan_checkpoint_id=promotion_checkpoint.id + 999,
        structured_task_plan_hash=promotion_checkpoint.content_hash,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "authority_stale"


def test_wrong_task_plan_hash_is_rejected(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-wrong-hash",
        structured_task_plan_checkpoint_id=promotion_checkpoint.id,
        structured_task_plan_hash="0" * 64,
        expected_session_generation_id=session.generation_id,
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "authority_stale"


def test_wrong_session_generation_is_rejected(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-wrong-generation",
        structured_task_plan_checkpoint_id=promotion_checkpoint.id,
        structured_task_plan_hash=promotion_checkpoint.content_hash,
        expected_session_generation_id=str(uuid.uuid4()),
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "authority_stale"


def test_wrong_expected_review_id_is_rejected(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    request = ExecutionCommitRequest(
        idempotency_key="exec-commit-wrong-review",
        structured_task_plan_checkpoint_id=promotion_checkpoint.id,
        structured_task_plan_hash=promotion_checkpoint.content_hash,
        expected_session_generation_id=session.generation_id,
        expected_review_id="not-the-real-review-id",
    )
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, request)
    assert exc_info.value.code == "authority_stale"


def test_missing_completion_manifest_is_deterministically_created(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    assert (
        db_session.query(PlanningCompletionManifest)
        .filter(PlanningCompletionManifest.planning_session_id == session.id)
        .one_or_none()
        is None
    )
    service = PlanningExecutionCommitService(db_session)
    result = service.commit(session.id, _request_for(promotion_checkpoint, session))
    assert result.completion_manifest_id is not None
    manifest = db_session.get(PlanningCompletionManifest, result.completion_manifest_id)
    assert manifest.planning_session_id == session.id
    assert session.processing_token is None
    assert session.processing_started_at is None


def test_first_execution_commit_creates_no_legacy_runtime_rows(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    before_tasks = db_session.query(Task).count()
    before_sessions_legacy = db_session.query(TaskExecution).count()
    service = PlanningExecutionCommitService(db_session)
    service.commit(session.id, _request_for(promotion_checkpoint, session))

    assert db_session.query(Task).count() == before_tasks
    assert db_session.query(TaskExecution).count() == before_sessions_legacy


def test_planning_checkpoints_and_review_events_are_unchanged(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    before_checkpoint_count = (
        db_session.query(PlanningCheckpoint)
        .filter(PlanningCheckpoint.planning_session_id == session.id)
        .count()
    )
    before_event_count = (
        db_session.query(PlanningReviewEvent)
        .filter(PlanningReviewEvent.planning_session_id == session.id)
        .count()
    )
    before_hash = promotion_checkpoint.content_hash

    service = PlanningExecutionCommitService(db_session)
    service.commit(session.id, _request_for(promotion_checkpoint, session))

    after_checkpoint_count = (
        db_session.query(PlanningCheckpoint)
        .filter(PlanningCheckpoint.planning_session_id == session.id)
        .count()
    )
    after_event_count = (
        db_session.query(PlanningReviewEvent)
        .filter(PlanningReviewEvent.planning_session_id == session.id)
        .count()
    )
    db_session.refresh(promotion_checkpoint)
    assert after_checkpoint_count == before_checkpoint_count
    assert after_event_count == before_event_count
    assert promotion_checkpoint.content_hash == before_hash


def test_soft_deleted_project_is_not_releasable(db_session):
    from datetime import datetime, timezone

    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    project.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    service = PlanningExecutionCommitService(db_session)
    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, _request_for(promotion_checkpoint, session))
    assert exc_info.value.code == "session_not_found"


def test_forced_execution_graph_failure_preserves_planning_commit_manifest_and_is_replayable(
    db_session, monkeypatch
):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    from app.services.execution import execution_plan_commit_service as ecs_module

    original_map = dict(ecs_module.DEPENDENCY_RUNTIME_CLASS_MAP)
    monkeypatch.setattr(ecs_module, "DEPENDENCY_RUNTIME_CLASS_MAP", {})

    service = PlanningExecutionCommitService(db_session)
    result = service.commit(session.id, _request_for(promotion_checkpoint, session))
    assert result.boundary_state == "released_execution_pending"
    assert result.execution_plan_id is None
    assert result.execution_failure_reason

    manifest = db_session.get(
        PlanningCommitManifest, result.planning_commit_manifest_id
    )
    assert manifest is not None

    plans = (
        db_session.query(ExecutionPlan)
        .filter(ExecutionPlan.planning_session_id == session.id)
        .all()
    )
    assert plans == []

    monkeypatch.setattr(ecs_module, "DEPENDENCY_RUNTIME_CLASS_MAP", original_map)
    replay = service.commit(
        session.id,
        _request_for(promotion_checkpoint, session, idempotency_key="replay-key"),
    )
    assert replay.boundary_state == "released"
    assert replay.planning_commit_manifest_id == manifest.id

    plans_after = (
        db_session.query(ExecutionPlan)
        .filter(ExecutionPlan.planning_session_id == session.id)
        .all()
    )
    assert len(plans_after) == 1


def test_different_authority_with_existing_commit_manifest_conflicts(db_session):
    project, session, plan, review_id, approval, promotion_checkpoint = (
        _build_approved_session(db_session)
    )
    service = PlanningExecutionCommitService(db_session)
    service.commit(session.id, _request_for(promotion_checkpoint, session))

    manifest = (
        db_session.query(PlanningCommitManifest)
        .filter(PlanningCommitManifest.planning_session_id == session.id)
        .one()
    )
    tampered = dict(manifest.task_provenance)
    tampered["structured_task_plan_hash"] = "1" * 64
    manifest.task_provenance = tampered
    db_session.commit()

    with pytest.raises(ExecutionCommitError) as exc_info:
        service.commit(session.id, _request_for(promotion_checkpoint, session))
    assert exc_info.value.code == "commit_manifest_conflict"
