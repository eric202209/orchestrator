"""Phase 17F: PlanCandidate contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.planning.plan_candidate import PlanCandidate


def test_plan_candidate_is_immutable():
    candidate = PlanCandidate(
        candidate_id="candidate-1",
        artifact_hash="abc123",
        validator_status="accepted",
    )

    with pytest.raises(FrozenInstanceError):
        candidate.validator_status = "rejected"


def test_plan_candidate_normalizes_iterables_to_tuples():
    candidate = PlanCandidate(
        candidate_id="candidate-1",
        parent_candidate_ids=["parent-1", "parent-2"],
        validator_status="repair_required",
        validator_reasons=["missing file", "weak verification"],
    )

    assert candidate.parent_candidate_ids == ("parent-1", "parent-2")
    assert candidate.validator_reasons == ("missing file", "weak verification")


def test_plan_candidate_rejects_empty_candidate_id():
    with pytest.raises(ValueError, match="candidate_id is required"):
        PlanCandidate(candidate_id=" ")


def test_plan_candidate_rejects_unknown_validator_status():
    with pytest.raises(ValueError, match="unsupported validator_status"):
        PlanCandidate(candidate_id="candidate-1", validator_status="scored")


def test_plan_candidate_status_helpers_are_deterministic():
    accepted = PlanCandidate(candidate_id="a", validator_status="warning")
    repairable = PlanCandidate(
        candidate_id="b",
        validator_status="repair_required",
        validator_reasons=("one", "two"),
    )
    rejected = PlanCandidate(
        candidate_id="c",
        validator_status="rejected",
        validator_reasons=("hard",),
    )

    assert accepted.accepted is True
    assert repairable.repairable is True
    assert repairable.repairable_reason_count == 2
    assert rejected.rejected is True
    assert rejected.rejected_reason_count == 1


def test_plan_candidate_to_dict_uses_json_friendly_lists():
    candidate = PlanCandidate(
        candidate_id="candidate-1",
        parent_candidate_ids=("parent-1",),
        operator="slot_merge",
        source_lineage="lineage-a",
        artifact_hash="hash-1",
        validator_status="rejected",
        validator_reasons=("schema invalid",),
        planning_failure_signature="sig-1",
        runtime_profile="medium",
    )

    payload = candidate.to_dict()

    assert payload["candidate_id"] == "candidate-1"
    assert payload["parent_candidate_ids"] == ["parent-1"]
    assert payload["validator_reasons"] == ["schema invalid"]
