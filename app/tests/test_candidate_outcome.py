"""Phase 17F: CandidatePlanningOutcome contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.planning.candidate_planning_outcome import CandidatePlanningOutcome
from app.services.planning.plan_candidate import PlanCandidate


def test_candidate_planning_outcome_is_immutable():
    outcome = CandidatePlanningOutcome.skipped()

    with pytest.raises(FrozenInstanceError):
        outcome.outcome = "selected"


def test_selected_outcome_requires_candidate():
    with pytest.raises(ValueError, match="selected outcome requires"):
        CandidatePlanningOutcome(outcome="selected")


def test_candidate_count_must_be_non_negative():
    with pytest.raises(ValueError, match="candidate_count must be non-negative"):
        CandidatePlanningOutcome(candidate_count=-1)


def test_skipped_constructor_returns_not_enabled_operator_sequence():
    outcome = CandidatePlanningOutcome.skipped(reason="not_enabled")

    assert outcome.selected_candidate is None
    assert outcome.candidate_count == 0
    assert outcome.operator_sequence == ("skipped:not_enabled",)
    assert outcome.outcome == "skipped"
    assert outcome.audit_event_ids == ()


def test_outcome_normalizes_sequences_to_tuples():
    candidate = PlanCandidate(candidate_id="candidate-1", validator_status="accepted")
    outcome = CandidatePlanningOutcome(
        selected_candidate=candidate,
        candidate_count=1,
        operator_sequence=["original"],
        outcome="selected",
        audit_event_ids=["evt-1"],
    )

    assert outcome.operator_sequence == ("original",)
    assert outcome.audit_event_ids == ("evt-1",)


def test_outcome_to_dict_is_json_friendly():
    candidate = PlanCandidate(candidate_id="candidate-1", validator_status="accepted")
    outcome = CandidatePlanningOutcome(
        selected_candidate=candidate,
        candidate_count=1,
        operator_sequence=("original",),
        outcome="selected",
        audit_event_ids=("evt-1",),
    )

    payload = outcome.to_dict()

    assert payload["selected_candidate"]["candidate_id"] == "candidate-1"
    assert payload["candidate_count"] == 1
    assert payload["operator_sequence"] == ["original"]
    assert payload["outcome"] == "selected"
    assert payload["audit_event_ids"] == ["evt-1"]
