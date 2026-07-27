"""Phase 31D-4 independent review/publication outcome tests."""

from __future__ import annotations

import pytest

from app.services.orchestration.outcome_adjudication import (
    CertificationOutcome,
    ExpectationComparison,
    OutcomeEvidenceError,
    ProductOutcome,
    PublicationOutcome,
    RegisteredOutcomeContract,
    ReviewOutcome,
    RuntimeOutcome,
    derive_review_publication_outcomes,
)


def contract(**overrides) -> RegisteredOutcomeContract:
    values = {
        "contract_id": "ST23-REVIEW-PUBLICATION-TEST-v1",
        "contract_version": "v1",
        "review_expectation": "REVIEW_REQUIRED",
        "publication_expectation": "PUBLICATION_REQUIRED",
    }
    values.update(overrides)
    return RegisteredOutcomeContract(**values)


def evidence_for(contract: RegisteredOutcomeContract, **facts):
    return {
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "facts": {
            "REVIEW_DECISION_RECORDED": True,
            "PUBLICATION_FACT_RECORDED": True,
            **facts,
        },
    }


def derive(
    scenario_contract: RegisteredOutcomeContract,
    *,
    review_record: dict,
    publication_record: dict,
):
    return derive_review_publication_outcomes(
        scenario_contract,
        evidence_for(scenario_contract),
        {"outcome": "RUNTIME_COMPLETED"},
        {"useful_work_completed": True},
        review_record,
        publication_record,
    )


def test_s1_5_useful_held_work_is_independent_from_certification():
    result = derive(
        contract(),
        review_record={"outcome": "hold_for_review"},
        publication_record={"outcome": "held_for_review"},
    )

    assert result.runtime_outcome == RuntimeOutcome.RUNTIME_COMPLETED
    assert result.product_outcome == ProductOutcome.SUCCESS_REQUIRES_REVIEW
    assert result.review_outcome == ReviewOutcome.REVIEW_HELD
    assert result.publication_outcome == PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW
    assert (
        result.publication_expectation_comparison
        == ExpectationComparison.CERTIFICATION_EXPECTATION_MISMATCH
    )
    assert (
        result.certification_outcome
        == CertificationOutcome.CERTIFICATION_EXPECTATION_MISMATCH
    )


def test_s1_5_updated_contract_can_explicitly_permit_the_review_hold():
    scenario_contract = contract(
        permitted_publication_outcomes=(
            PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW,
        ),
    )
    result = derive(
        scenario_contract,
        review_record={"outcome": "hold_for_review"},
        publication_record={"outcome": "held_for_review"},
    )

    assert result.product_outcome == ProductOutcome.SUCCESS_REQUIRES_REVIEW
    assert (
        result.publication_expectation_comparison
        == ExpectationComparison.PUBLICATION_EXPECTATION_MATCHED
    )
    assert result.certification_outcome == CertificationOutcome.CERTIFICATION_ACCEPTED


def test_s1_6_unexpected_publication_remains_visible_after_product_success():
    scenario_contract = contract(publication_expectation="PUBLICATION_ALLOWED")
    result = derive(
        scenario_contract,
        review_record={"outcome": "auto_promote", "held_for_review": False},
        publication_record={"outcome": "published"},
    )

    assert result.runtime_outcome == RuntimeOutcome.RUNTIME_COMPLETED
    assert result.product_outcome == ProductOutcome.SUCCESS_PUBLISHED
    assert result.review_outcome == ReviewOutcome.REVIEW_EXPECTATION_NOT_MET
    assert (
        result.publication_outcome
        == PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY
    )
    assert (
        result.certification_outcome
        == CertificationOutcome.CERTIFICATION_EXPECTATION_MISMATCH
    )


def test_contract_and_structural_evidence_identity_are_required():
    scenario_contract = contract(required_structural_evidence=("SOURCE_PRESENT",))

    with pytest.raises(OutcomeEvidenceError, match="SOURCE_PRESENT"):
        derive_review_publication_outcomes(
            scenario_contract,
            evidence_for(scenario_contract),
            "RUNTIME_COMPLETED",
            {"useful_work_completed": True},
            {"outcome": "hold_for_review"},
            {"outcome": "held_for_review"},
        )


def test_runtime_completion_does_not_create_product_success():
    result = derive(
        contract(),
        review_record={"outcome": "hold_for_review"},
        publication_record={"outcome": "held_for_review"},
    )
    failed_product = derive_review_publication_outcomes(
        contract(),
        evidence_for(contract()),
        "RUNTIME_COMPLETED",
        {"useful_work_completed": False},
        {"outcome": "hold_for_review"},
        {"outcome": "held_for_review"},
    )

    assert result.product_outcome == ProductOutcome.SUCCESS_REQUIRES_REVIEW
    assert failed_product.product_outcome == ProductOutcome.SAFE_FAILURE


def test_legacy_acceptance_classifier_remains_importable_and_separate():
    from app.services.orchestration.acceptance_evidence import OutcomeClass

    assert OutcomeClass.FAILED_SAFE.value == "FAILED_SAFE"
