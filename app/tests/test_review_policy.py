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


def _registered_contract(
    *,
    review_expectation: str = "REVIEW_NOT_REQUIRED",
    publication_expectation: str = "PUBLICATION_REQUIRED",
    top_level_review: str | None = None,
    top_level_publication: str | None = None,
    scenario_id: str = "S1-2",
) -> dict:
    review = {
        "contract_id": "ST23-REVIEW-001",
        "contract_version": "v1",
        "expectation": review_expectation,
        "scenario_id": scenario_id,
    }
    publication = {
        "contract_id": "ST23-PUBLICATION-001",
        "contract_version": "v1",
        "expectation": publication_expectation,
        "scenario_id": scenario_id,
    }
    return {
        "contract_source": "phase31_certification_runner",
        "contract_id": "ST23-PLANNER-001",
        "contract_version": "v1",
        "scenario_id": scenario_id,
        "review_expectation": top_level_review or review_expectation,
        "publication_expectation": top_level_publication or publication_expectation,
        "review_contract": review,
        "publication_contract": publication,
        "registered_scenario_contract": {
            "scenario_id": scenario_id,
            "review_contract": review,
            "publication_contract": publication,
        },
    }


def test_registered_no_review_required_and_publication_required_release_safe_certification_change():
    decision = decide_change_set_review(
        {
            "changed_count": 2,
            "warning_flags": ["scaffold_or_test_surface_changed"],
        },
        workspace_review_policy="hold_nontrivial",
        planner_contract=_registered_contract(),
    )

    assert decision["held_for_review"] is False
    assert decision["outcome"] == "auto_promote"
    assert decision["publication_allowed"] is True
    assert decision["publication_required"] is True
    assert decision["registered_review_expectation"] == "REVIEW_NOT_REQUIRED"
    assert decision["registered_publication_expectation"] == "PUBLICATION_REQUIRED"
    assert decision["policy_source"] == "registered_certification_contract"
    assert decision["stronger_safety_override"] is False


def test_absent_registered_contract_preserves_fail_closed_review_behavior():
    decision = decide_change_set_review(
        {
            "changed_count": 2,
            "warning_flags": ["scaffold_or_test_surface_changed"],
        },
        workspace_review_policy="hold_nontrivial",
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "nontrivial_change_set_review_required"
    assert decision["policy_source"] == "workspace_review_policy"


def test_ambiguous_registered_contract_remains_held():
    decision = decide_change_set_review(
        {"changed_count": 2, "warning_flags": []},
        workspace_review_policy="hold_nontrivial",
        planner_contract=_registered_contract(top_level_review="REVIEW_REQUIRED"),
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "registered_contract_ambiguous"
    assert decision["contract_resolution"] == "ambiguous"


def test_registered_review_required_remains_held():
    decision = decide_change_set_review(
        {"changed_count": 0, "warning_flags": []},
        workspace_review_policy="auto_publish_all",
        planner_contract=_registered_contract(review_expectation="REVIEW_REQUIRED"),
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "registered_review_required"
    assert decision["publication_allowed"] is True


def test_registered_publication_not_required_does_not_allow_automatic_publication():
    decision = decide_change_set_review(
        {"changed_count": 1, "warning_flags": []},
        workspace_review_policy="auto_publish_all",
        planner_contract=_registered_contract(
            publication_expectation="PUBLICATION_NOT_REQUIRED"
        ),
    )

    assert decision["held_for_review"] is False
    assert decision["publication_allowed"] is False
    assert decision["publication_required"] is False
    assert decision["reason"] == "publication_not_required"


def test_registered_high_risk_policy_overrides_review_not_required():
    decision = decide_change_set_review(
        {"changed_count": 1, "warning_flags": ["security_high_risk_command"]},
        workspace_review_policy="hold_nontrivial",
        planner_contract=_registered_contract(),
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "mandatory_safety_review_required"
    assert decision["stronger_safety_override"] is True


def test_registered_contract_does_not_override_hold_all_safety_policy():
    decision = decide_change_set_review(
        {"changed_count": 0, "warning_flags": []},
        workspace_review_policy="hold_all",
        planner_contract=_registered_contract(),
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "hold_all_review_required"
    assert decision["stronger_safety_override"] is True


def test_registered_contract_does_not_override_template_review_hold():
    decision = decide_change_set_review(
        {"changed_count": 1, "warning_flags": []},
        workspace_review_policy="auto_publish_all",
        template_review_policy={"auto_promote_eligible": False},
        planner_contract=_registered_contract(),
    )

    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_auto_promote_not_eligible"
    assert decision["stronger_safety_override"] is True


def test_registered_policy_is_not_selected_by_task_or_scenario_id():
    decision = decide_change_set_review(
        {"changed_count": 2, "warning_flags": ["scaffold_or_test_surface_changed"]},
        workspace_review_policy="hold_nontrivial",
        planner_contract=_registered_contract(scenario_id="unrelated-contract-lane"),
    )

    assert decision["held_for_review"] is False
    assert decision["publication_allowed"] is True
