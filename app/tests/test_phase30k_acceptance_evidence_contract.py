"""Phase 30K — acceptance-evidence classification contract.

Pure classification tests: no DB, no orchestration runtime. See
app/services/orchestration/acceptance_evidence.py for the classifier this
exercises and docs/roadmap/done/phase30/phase30k-acceptance-evidence-
contract-alignment.md for the certification narrative.
"""

from __future__ import annotations

from app.services.orchestration.acceptance_evidence import (
    AcceptanceEvidenceFacts,
    EvidenceRepairDisposition,
    OutcomeClass,
    ScenarioAcceptanceContract,
    classify_acceptance,
    classify_evidence_defect,
    compute_acceptance_metrics,
)

AUTONOMOUS = ScenarioAcceptanceContract(
    scenario_kind="autonomous_mutating_task",
    mutation_expected=True,
    publication_required=True,
    human_review_expected=False,
    evaluator_required=True,
)

HUMAN_REVIEW = ScenarioAcceptanceContract(
    scenario_kind="human_review_task",
    mutation_expected=True,
    publication_required=False,
    human_review_expected=True,
    evaluator_required=True,
)

ANALYSIS_ONLY = ScenarioAcceptanceContract(
    scenario_kind="analysis_only_task",
    mutation_expected=False,
    publication_required=False,
    human_review_expected=False,
    evaluator_required=False,
)


def facts(**overrides):
    base = dict(task_status="done", execution_completed=True)
    base.update(overrides)
    return AcceptanceEvidenceFacts(**base)


# 1
def test_autonomous_done_published_pass_is_success_published():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            workspace_restored=None,
        ),
    )
    assert result.outcome_class == OutcomeClass.SUCCESS_PUBLISHED
    assert result.product_objective_achieved is True


# 2
def test_autonomous_done_held_for_review_is_not_success():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="NEEDS_REVIEW",
            held_for_review=True,
            baseline_published=False,
        ),
    )
    assert result.outcome_class != OutcomeClass.SUCCESS_PUBLISHED
    assert result.product_objective_achieved is False


# 3
def test_human_review_done_held_is_success_requires_review():
    result = classify_acceptance(
        HUMAN_REVIEW,
        facts(
            evaluator_verdict="NEEDS_REVIEW",
            held_for_review=True,
            baseline_published=False,
        ),
    )
    assert result.outcome_class == OutcomeClass.SUCCESS_REQUIRES_REVIEW


# 4
def test_analysis_only_done_no_publication_is_success_no_publication_required():
    result = classify_acceptance(
        ANALYSIS_ONLY,
        facts(evaluator_verdict=None, held_for_review=None, baseline_published=None),
    )
    assert result.outcome_class == OutcomeClass.SUCCESS_NO_PUBLICATION_REQUIRED


# 5
def test_done_evaluator_fail_is_not_unconditional_success():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="ERROR",
            held_for_review=False,
            baseline_published=False,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE
    assert result.product_objective_achieved is False


# 6
def test_done_missing_evaluator_when_required_is_invalid_evidence():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(evaluator_verdict=None, held_for_review=False, baseline_published=True),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert "missing_evaluator_verdict" in result.invalid_evidence_reasons


# 7
def test_failed_safely_restored_is_failed_safe():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            evaluator_verdict=None,
            held_for_review=None,
            baseline_published=False,
            workspace_restored=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE


# 8
def test_planning_rejection_no_mutation_is_failed_safe():
    contract = ScenarioAcceptanceContract(
        scenario_kind="autonomous_mutating_task",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    )
    result = classify_acceptance(
        contract,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE


# 9
def test_failed_with_outside_mutation_is_failed_unsafe():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            outside_workspace_mutation=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_UNSAFE
    assert result.requires_phase31_program_stop is True


# 10
def test_done_with_outside_mutation_is_failed_unsafe():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            outside_workspace_mutation=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_UNSAFE


# 11
def test_done_held_for_review_published_unexpectedly_is_unsafe():
    result = classify_acceptance(
        HUMAN_REVIEW,
        facts(
            evaluator_verdict="NEEDS_REVIEW",
            held_for_review=True,
            baseline_published=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_UNSAFE
    assert result.outcome_reason_code == "UNSAFE_PUBLICATION_WHILE_HELD"


# 12
def test_duplicate_execution_is_failed_unsafe():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            duplicate_execution=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_UNSAFE
    assert result.outcome_reason_code == "DUPLICATE_EXECUTION"


# 13
def test_invalid_terminal_state_combination_is_invalid_evidence():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            terminal_facts_contradictory=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert "contradictory_terminal_facts" in result.invalid_evidence_reasons


# 14
def test_missing_required_tree_hashes_is_invalid_evidence():
    contract = ScenarioAcceptanceContract(
        scenario_kind="autonomous_mutating_task",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=True,
        required_evidence_fields=("candidate_preserved",),
    )
    result = classify_acceptance(
        contract,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            candidate_preserved=None,
        ),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert (
        "missing_required_field:candidate_preserved" in result.invalid_evidence_reasons
    )


# 15
def test_repair_outcome_inconsistent_is_invalid_evidence_and_stops():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            repair_outcome_consistent=False,
        ),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert "repair_outcome_inconsistent" in result.invalid_evidence_reasons
    assert result.requires_phase31_corrective_action is True


# 16
def test_repeated_nested_root_target_after_bounded_repair_is_safe_plus_corrective():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            repair_outcome_consistent=True,
            target_violation_repeated_after_repair=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE
    assert result.requires_phase31_corrective_action is True


# 17
def test_provider_failure_with_complete_safety_evidence_is_failed_safe():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            reason_hint="PROVIDER_INFRASTRUCTURE_FAILURE",
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE
    assert result.outcome_reason_code == "PROVIDER_INFRASTRUCTURE_FAILURE"


# 18
def test_provider_failure_with_incomplete_evidence_is_invalid_evidence():
    contract = ScenarioAcceptanceContract(
        scenario_kind="autonomous_mutating_task",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=True,
        required_evidence_fields=("workspace_restored",),
    )
    result = classify_acceptance(
        contract,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=None,
        ),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert (
        "missing_required_field:workspace_restored" in result.invalid_evidence_reasons
    )
    assert result.mandatory_safety_passed is True


# 19
def test_j6_style_harness_defect_original_invalid_offline_correction_allowed():
    original = classify_acceptance(
        AUTONOMOUS,
        facts(evaluator_verdict=None, held_for_review=False, baseline_published=True),
    )
    assert original.outcome_class == OutcomeClass.INVALID_EVIDENCE
    disposition = classify_evidence_defect("aggregation_or_classification_only")
    assert disposition == EvidenceRepairDisposition.OFFLINE_RECOMPUTATION_ALLOWED


# 20
def test_runtime_changing_harness_defect_requires_rerun():
    disposition = classify_evidence_defect("harness_affected_runtime_execution")
    assert disposition == EvidenceRepairDisposition.RERUN_REQUIRED


# 21
def test_same_runtime_facts_classified_differently_by_contract():
    same_facts = facts(
        evaluator_verdict="NEEDS_REVIEW",
        held_for_review=True,
        baseline_published=False,
    )
    autonomous_result = classify_acceptance(AUTONOMOUS, same_facts)
    review_result = classify_acceptance(HUMAN_REVIEW, same_facts)
    assert autonomous_result.outcome_class == OutcomeClass.FAILED_SAFE
    assert review_result.outcome_class == OutcomeClass.SUCCESS_REQUIRES_REVIEW


# 22
def test_safety_precedence_overrides_success_classes():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="PASS",
            held_for_review=False,
            baseline_published=True,
            validator_bypassed=True,
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_UNSAFE


# 23
def test_invalid_evidence_precedence_overrides_incomplete_success_claim():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(evaluator_verdict=None, held_for_review=False, baseline_published=True),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert result.outcome_class != OutcomeClass.SUCCESS_PUBLISHED


# 24
def test_metrics_exclude_invalid_evidence_from_valid_denominator():
    results = [
        classify_acceptance(
            AUTONOMOUS,
            facts(
                evaluator_verdict="PASS", held_for_review=False, baseline_published=True
            ),
        ),
        classify_acceptance(
            AUTONOMOUS,
            facts(
                evaluator_verdict=None, held_for_review=False, baseline_published=True
            ),
        ),
    ]
    contracts = [AUTONOMOUS, AUTONOMOUS]
    metrics = compute_acceptance_metrics(results, contracts)
    assert metrics.total_scenarios == 2
    assert metrics.invalid_evidence_count == 1
    assert metrics.valid_evidence_count == 1
    assert metrics.product_success_rate == 1.0


# 25
def test_publication_and_workflow_rates_use_different_denominators():
    results = [
        classify_acceptance(
            AUTONOMOUS,
            facts(
                evaluator_verdict="PASS", held_for_review=False, baseline_published=True
            ),
        ),
        classify_acceptance(
            HUMAN_REVIEW,
            facts(
                evaluator_verdict="NEEDS_REVIEW",
                held_for_review=True,
                baseline_published=False,
            ),
        ),
    ]
    contracts = [AUTONOMOUS, HUMAN_REVIEW]
    metrics = compute_acceptance_metrics(results, contracts)
    assert metrics.autonomous_publication_scenario_count == 1
    assert metrics.autonomous_publication_success_rate == 1.0
    assert metrics.publication_rate == 0.5
    assert metrics.workflow_success_rate == 1.0


# 26
def test_classification_is_deterministic_and_serializable():
    f = facts(evaluator_verdict="PASS", held_for_review=False, baseline_published=True)
    r1 = classify_acceptance(AUTONOMOUS, f)
    r2 = classify_acceptance(AUTONOMOUS, f)
    assert r1 == r2
    d = r1.to_dict()
    assert d["outcome_class"] == "SUCCESS_PUBLISHED"
    assert isinstance(d["invalid_evidence_reasons"], list)


# 27
def test_unknown_reason_hint_fails_safely():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            reason_hint="totally_made_up_code",
        ),
    )
    assert result.outcome_class == OutcomeClass.FAILED_SAFE
    assert result.outcome_reason_code == "NONE"


# 28
def test_missing_optional_evidence_not_required_does_not_invalidate():
    result = classify_acceptance(
        ANALYSIS_ONLY,
        facts(candidate_preserved=None, workspace_restored=None),
    )
    assert result.outcome_class == OutcomeClass.SUCCESS_NO_PUBLICATION_REQUIRED


# 29
def test_done_held_for_review_never_automatically_success_published():
    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            evaluator_verdict="NEEDS_REVIEW",
            held_for_review=True,
            baseline_published=False,
        ),
    )
    assert result.outcome_class != OutcomeClass.SUCCESS_PUBLISHED


# 30
def test_phase30j_evidence_shape_interpreted_without_ad_hoc_text_parsing():
    # Shape mirrors app/tests/test_phase30j_package_root_intent_and_repair_outcome.py:
    # compute_final_repair_outcome's summary dict carries
    # repair_outcome_consistent/target_final_status directly — no text
    # scraping of log lines is required to feed the classifier.
    from app.services.orchestration.phases.planning_support import (
        compute_final_repair_outcome,
    )

    attempts = [
        {
            "targeted_violation_code": "nested_project_folder_command",
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        },
    ]
    final_codes = ["nested_project_folder_command"]
    summary = compute_final_repair_outcome(attempts, final_codes)
    entry = summary["nested_project_folder_command"]
    assert entry["target_final_status"] == "OUTCOME_INCONSISTENT"

    result = classify_acceptance(
        AUTONOMOUS,
        facts(
            task_status="failed",
            baseline_published=False,
            workspace_restored=True,
            repair_outcome_consistent=entry["repair_outcome_consistent"],
        ),
    )
    assert result.outcome_class == OutcomeClass.INVALID_EVIDENCE
    assert result.requires_phase31_corrective_action is True
