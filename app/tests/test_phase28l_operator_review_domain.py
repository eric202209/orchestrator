"""Focused Protocol v2 operator-review domain and persistence tests."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.models import PlanningReviewEvent
from app.services.planning.operator_review import (
    PlanningReviewEvent as DomainReviewEvent,
    ReviewActor,
    ReviewCandidateBinding,
    ReviewDecisionRequest,
    ReviewPredecessorBinding,
    ReviewValidationSnapshot,
    canonical_json_bytes,
    event_hash,
)
from app.services.planning.operator_review_persistence import (
    OperatorReviewService,
)
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
    ProtocolPersistenceError,
)

from app.tests.test_phase28f_planning_brief import (
    _brief,
    _manifest,
    _seed_session,
)
from app.tests.test_phase28j_structured_task_plan_stage import (
    _engine as _task_plan_engine,
    _plan_candidate,
    _seed as _seed_task_plan,
)


def _review_fixture(db_session):
    session = _seed_session(db_session)
    manifest = _manifest(session.id, session.generation_id)
    protocol = PlanningProtocolPersistenceService(db_session)
    protocol.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    candidate = protocol.record_planning_brief(
        session.id,
        brief=brief,
        status="failed",
        stage_generation_id="brief-stage-1",
        attempt_id="brief-attempt-1",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        protocol_version="v2",
    )
    candidate.validation_json = {
        **candidate.validation_json,
        "review_reason_codes": ["explicit_operator_review"],
    }
    db_session.flush()
    return session, manifest, brief, candidate


def _actor() -> ReviewActor:
    return ReviewActor("operator@example.test", "project_owner", "project_owner")


def test_review_domain_uses_immutable_records_and_deterministic_hashing():
    binding = ReviewCandidateBinding(
        planning_session_id=1,
        project_id=2,
        protocol_version="v2",
        session_generation_id="session-generation",
        stage_name="planning_brief",
        stage_version=1,
        stage_generation_id="stage-generation",
        candidate_checkpoint_id=3,
        candidate_checkpoint_version=1,
        candidate_content_hash="a" * 64,
        validation_hash="b" * 64,
        validator_version="validator/1",
        input_manifest_id="manifest:abc",
        input_manifest_hash="c" * 64,
        predecessors=(ReviewPredecessorBinding(4, "d" * 64),),
        stage_configuration_fingerprint="e" * 64,
    )
    validation = ReviewValidationSnapshot(
        validator_version="validator/1",
        validation_hash="b" * 64,
        schema_valid=True,
        semantically_valid=True,
        protocol_acceptable=False,
        review_reason_codes=("explicit_operator_review",),
    )
    actor = ReviewActor("operator", "owner", "project_owner")
    event = DomainReviewEvent(
        event_id="event-1",
        review_id="review-1",
        event_sequence=1,
        event_type="review_opened",
        candidate_binding=binding,
        validation=validation,
        actor=actor,
        idempotency_key="open-1",
        canonical_request_hash="f" * 64,
        prior_review_head_sequence=0,
        resulting_sequence=1,
        review_concurrency_token="root",
    )
    assert event.event_hash == event_hash(event)
    assert canonical_json_bytes({"é": "café"}) == canonical_json_bytes(
        {"e\u0301": "cafe\u0301"}
    )
    with pytest.raises((AttributeError, TypeError)):
        binding.candidate_checkpoint_id = 9


def test_review_opens_once_and_reconstructs_from_event_chain(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    first = service.open_review_for_candidate(session.id, candidate.id)
    second = service.open_review_for_candidate(session.id, candidate.id)
    assert first.review_id == second.review_id
    assert first.state == "pending"
    assert first.current_sequence == 1
    assert db_session.query(PlanningReviewEvent).count() == 1
    recovered = service.recover_review(first.review_id)
    assert recovered.review_head_token == first.review_head_token
    assert recovered.review_required_reasons == ("explicit_operator_review",)


def test_approval_promotes_identical_bytes_and_leaves_failed_candidate(db_session):
    session, _manifest_value, brief, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    result = service.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key="approve-1",
        comment="The bounded canonical candidate is acceptable.",
    )
    assert result.promotion is not None
    promotion = db_session.get(type(candidate), result.promotion.checkpoint_id)
    db_session.refresh(candidate)
    assert candidate.status == "failed"
    assert promotion.status == "accepted"
    assert promotion.content == candidate.content
    assert promotion.content_hash == candidate.content_hash
    assert promotion.promotion_review_event_id == result.event_id
    assert {edge.parent_checkpoint_id for edge in promotion.dependencies} == {
        candidate.id
    }
    assert (
        PlanningProtocolPersistenceService(db_session)
        .load_accepted_planning_brief(session.id)
        .content_hash
        == brief.content_hash
    )


def test_approval_requires_human_comment_and_is_idempotent(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    with pytest.raises(Exception, match="approval comment is required"):
        service.approve_review_unchanged(
            review.review_id, _actor(), idempotency_key="approve-1"
        )
    first = service.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key="approve-1",
        comment="Approve unchanged canonical bytes.",
    )
    replay = service.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key="approve-1",
        comment="Approve unchanged canonical bytes.",
    )
    assert replay.replayed is True
    assert replay.promotion.checkpoint_id == first.promotion.checkpoint_id
    with pytest.raises(Exception, match="review_already_decided"):
        service.reject_review(
            review.review_id,
            _actor(),
            idempotency_key="reject-1",
            reason="changed mind",
        )


def test_rejection_cancellation_and_acknowledgment_are_typed_decisions(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    ack = service.acknowledge_review(
        review.review_id, _actor(), idempotency_key="ack-1", comment="Noted."
    )
    assert ack.state == "pending"
    rejected = service.reject_review(
        review.review_id,
        _actor(),
        idempotency_key="reject-1",
        reason="Requires a new candidate.",
    )
    assert rejected.state == "rejected"
    with pytest.raises(Exception, match="review_already_decided"):
        service.approve_review_unchanged(
            review.review_id, _actor(), idempotency_key="approve-1", comment="Too late."
        )
    projection = service.get_review(review.review_id)
    assert projection.state == "rejected"
    assert projection.rejection_reason == "Requires a new candidate."


def test_invalid_and_stale_candidates_are_not_reviewable(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    candidate.content = "not canonical JSON"
    invalid = service.classify_candidate(session.id, candidate.id)
    assert invalid.classification == "invalid"
    db_session.rollback()
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    candidate.session_generation_id = "old-generation"
    stale = service.classify_candidate(session.id, candidate.id)
    assert stale.classification == "stale"


def test_missing_promotion_event_is_integrity_failure(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    result = service.approve_review_unchanged(
        review.review_id, _actor(), idempotency_key="approve-1", comment="Approved."
    )
    db_session.query(PlanningReviewEvent).filter(
        PlanningReviewEvent.event_id == result.event_id
    ).delete()
    with pytest.raises(ProtocolPersistenceError, match="missing its approval event"):
        PlanningProtocolPersistenceService(db_session).load_accepted_planning_brief(
            session.id
        )


def test_request_amendment_is_persisted_without_dispatch(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    result = service.request_amendment(
        review.review_id,
        _actor(),
        idempotency_key="amend-1",
        guidance="Regenerate with the clarified scope.",
    )
    assert result.state == "amendment_requested"
    assert service.get_review(review.review_id).command_identity


def test_recovery_reports_broken_event_chain_without_acting(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    row = (
        db_session.query(PlanningReviewEvent)
        .filter(PlanningReviewEvent.review_id == review.review_id)
        .one()
    )
    row.event_hash = "0" * 64
    db_session.flush()
    recovered = service.recover_review(review.review_id)
    assert recovered.state == "integrity_failure"
    assert recovered.allowed_decisions == ()


def test_promotion_byte_tampering_fails_accepted_recovery(db_session):
    session, _manifest_value, _brief_value, candidate = _review_fixture(db_session)
    service = OperatorReviewService(db_session)
    review = service.open_review_for_candidate(session.id, candidate.id)
    result = service.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key="approve-tamper-1",
        comment="Approve the exact candidate.",
    )
    promotion = db_session.get(type(candidate), result.promotion.checkpoint_id)
    promotion.content = promotion.content + " "
    with pytest.raises(ProtocolPersistenceError, match="promotion bytes"):
        PlanningProtocolPersistenceService(db_session).load_accepted_planning_brief(
            session.id
        )


def test_task_plan_review_candidate_promotes_through_accepted_loader(db_session):
    session, _manifest_value = _seed_task_plan(db_session)
    candidate = _plan_candidate()
    candidate["tasks"][0]["category"] = "operator_action"
    candidate["tasks"][0]["execution_profile"]["owner_role"] = "operator"
    candidate["tasks"][0]["execution_profile"]["write_scope"] = "operator_only"
    engine, _brief_provider, _task_provider = _task_plan_engine(
        db_session, session, _manifest_value, candidate
    )
    execution = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert execution.status.value == "failed"
    checkpoint = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id,
        stage_versions={"structured_task_plan": 1},
    )[("structured_task_plan", 1)]
    service = OperatorReviewService(db_session)
    eligibility = service.classify_candidate(session.id, checkpoint.id)
    assert eligibility.classification == "valid_review_required"
    review = service.open_review_for_candidate(session.id, checkpoint.id)
    result = service.approve_review_unchanged(
        review.review_id,
        _actor(),
        idempotency_key="task-plan-approve-1",
        comment="Operator-owned work is explicitly approved.",
    )
    assert result.promotion is not None
    loaded = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_structured_task_plan(session.id)
    assert loaded is not None
    assert loaded.content_hash == checkpoint.content_hash
