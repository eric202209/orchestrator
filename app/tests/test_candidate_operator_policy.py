"""Phase 17I: Candidate operator policy consolidation tests."""

from __future__ import annotations

from app.services.planning.candidate_operator_policy import (
    CANDIDATE_OPERATOR_POLICY,
    OPERATOR_SIBLING_GENERATION,
    OPERATOR_SLOT_MERGE,
    PROFILE_COMPACT_LOCAL,
    PROFILE_LOW_RESOURCE,
    PROFILE_MEDIUM,
    PROFILE_STANDARD,
    evaluate_candidate_operator_policy,
    operator_for_runtime_profile,
)


def test_candidate_operator_policy_maps_machine_profiles():
    assert operator_for_runtime_profile(PROFILE_STANDARD) == OPERATOR_SIBLING_GENERATION
    assert operator_for_runtime_profile(PROFILE_MEDIUM) == OPERATOR_SLOT_MERGE
    assert operator_for_runtime_profile(PROFILE_LOW_RESOURCE) == ""
    assert operator_for_runtime_profile(PROFILE_COMPACT_LOCAL) == ""


def test_candidate_operator_policy_preserves_feature_flags():
    sibling = CANDIDATE_OPERATOR_POLICY[PROFILE_STANDARD]
    slot_merge = CANDIDATE_OPERATOR_POLICY[PROFILE_MEDIUM]

    assert sibling.feature_flag is None
    assert slot_merge.feature_flag == "CANDIDATE_SLOT_MERGE_ENABLED"


def test_candidate_operator_policy_allows_machine_a_implicit_sibling():
    decision = evaluate_candidate_operator_policy(
        runtime_profile=PROFILE_STANDARD,
        candidate_operator="",
        candidate_recovery_enabled=True,
        slot_merge_enabled=False,
    )

    assert decision.allowed is True
    assert decision.operator == OPERATOR_SIBLING_GENERATION


def test_candidate_operator_policy_requires_machine_b_explicit_slot_merge_flag():
    missing_operator = evaluate_candidate_operator_policy(
        runtime_profile=PROFILE_MEDIUM,
        candidate_operator="",
        candidate_recovery_enabled=True,
        slot_merge_enabled=True,
    )
    missing_flag = evaluate_candidate_operator_policy(
        runtime_profile=PROFILE_MEDIUM,
        candidate_operator=OPERATOR_SLOT_MERGE,
        candidate_recovery_enabled=True,
        slot_merge_enabled=False,
    )
    allowed = evaluate_candidate_operator_policy(
        runtime_profile=PROFILE_MEDIUM,
        candidate_operator=OPERATOR_SLOT_MERGE,
        candidate_recovery_enabled=True,
        slot_merge_enabled=True,
    )

    assert missing_operator.allowed is False
    assert missing_operator.reason == "unsupported_runtime_profile"
    assert missing_flag.allowed is False
    assert missing_flag.reason == "unsupported_runtime_profile"
    assert allowed.allowed is True
    assert allowed.operator == OPERATOR_SLOT_MERGE


def test_candidate_operator_policy_disables_machine_c_profiles():
    for profile in (PROFILE_LOW_RESOURCE, PROFILE_COMPACT_LOCAL):
        decision = evaluate_candidate_operator_policy(
            runtime_profile=profile,
            candidate_operator=OPERATOR_SLOT_MERGE,
            candidate_recovery_enabled=True,
            slot_merge_enabled=True,
        )

        assert decision.allowed is False
        assert decision.reason == "unsupported_runtime_profile"


def test_candidate_operator_policy_preserves_candidate_recovery_flag_rollback():
    decision = evaluate_candidate_operator_policy(
        runtime_profile=PROFILE_STANDARD,
        candidate_operator="",
        candidate_recovery_enabled=False,
        slot_merge_enabled=True,
    )

    assert decision.allowed is False
    assert decision.reason == "not_enabled"
