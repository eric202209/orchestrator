from __future__ import annotations

from app.services.orchestration.review_policy import (
    CHANGE_SET_REVIEW_POLICY_VERSION,
    decide_change_set_review,
)


def test_change_set_review_policy_holds_nontrivial_warning_flags():
    decision = decide_change_set_review(
        {
            "changed_count": 2,
            "warning_flags": ["dependency_files_changed"],
        },
        workspace_review_policy="hold_nontrivial",
    )

    assert decision["held_for_review"] is True
    assert decision["outcome"] == "hold_for_review"
    assert decision["reason"] == "nontrivial_change_set_review_required"
    assert decision["blocking_findings"] == ["dependency_files_changed"]
    assert decision["policy_version"] == CHANGE_SET_REVIEW_POLICY_VERSION


def test_change_set_review_policy_auto_promotes_clean_hold_nontrivial_change_set():
    decision = decide_change_set_review(
        {
            "changed_count": 1,
            "warning_flags": [],
        },
        workspace_review_policy="hold_nontrivial",
    )

    assert decision["held_for_review"] is False
    assert decision["outcome"] == "auto_promote"
    assert decision["reason"] is None
    assert decision["blocking_findings"] == []


def test_change_set_review_policy_preserves_workspace_policy_modes():
    hold_all = decide_change_set_review(
        {
            "changed_count": 0,
            "warning_flags": [],
        },
        workspace_review_policy="hold_all",
    )
    auto_publish = decide_change_set_review(
        {
            "changed_count": 5,
            "warning_flags": ["deleted_files"],
        },
        workspace_review_policy="auto_publish_all",
    )

    assert hold_all["held_for_review"] is True
    assert hold_all["reason"] == "hold_all_review_required"
    assert auto_publish["held_for_review"] is False
    assert auto_publish["outcome"] == "auto_promote"
    assert auto_publish["warning_findings"] == ["deleted_files"]


def test_change_set_review_policy_records_evaluator_evidence_as_shadow_only():
    decision = decide_change_set_review(
        {
            "changed_count": 1,
            "warning_flags": [],
        },
        workspace_review_policy="hold_nontrivial",
        workflow_profile="docs_static",
        evaluator_evidence={
            "verdict": "low_confidence",
            "confidence": 0.42,
            "ignored": "not persisted",
        },
    )

    assert decision["held_for_review"] is False
    assert decision["outcome"] == "auto_promote"
    assert decision["workflow_profile"] == "docs_static"
    assert decision["evaluator_influence"] == "shadow"
    assert decision["evaluator_evidence"] == {
        "confidence": 0.42,
        "verdict": "low_confidence",
    }


def test_change_set_review_policy_allows_low_risk_profile_warnings():
    decision = decide_change_set_review(
        {
            "changed_count": 2,
            "warning_flags": ["scaffold_or_test_surface_changed"],
        },
        workspace_review_policy="hold_nontrivial",
        workflow_profile="docs_static",
    )

    assert decision["held_for_review"] is False
    assert decision["outcome"] == "allow_with_warning"
    assert decision["reason"] == "low_risk_profile_warning_allowed"
    assert decision["blocking_findings"] == []


def test_change_set_review_policy_holds_source_risk_even_for_low_risk_profile():
    decision = decide_change_set_review(
        {
            "changed_count": 2,
            "warning_flags": ["dependency_files_changed"],
        },
        workspace_review_policy="hold_nontrivial",
        workflow_profile="docs_static",
    )

    assert decision["held_for_review"] is True
    assert decision["outcome"] == "hold_for_review"
    assert decision["reason"] == "nontrivial_change_set_review_required"
