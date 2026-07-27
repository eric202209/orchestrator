"""Phase 31D-R1A canonical Stage 1 runner contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from scripts.maintenance import phase31_certification_runner as runner
from scripts.maintenance.phase31_certification_scenarios import (
    DEBUG_SUBSET_CLASSIFICATION,
    SCENARIO_REGISTRY,
    STAGE1_CERTIFICATION_CLASSIFICATION,
    STAGE1_SCENARIO_IDS,
    ScenarioRegistryError,
    scenario_spec,
    validate_requested_scenario_ids,
    validate_scenario_registry,
)


def test_registry_contains_exactly_the_canonical_six_ids():
    assert tuple(SCENARIO_REGISTRY) == STAGE1_SCENARIO_IDS
    assert set(SCENARIO_REGISTRY) == {
        "S1-1",
        "S1-2",
        "S1-3",
        "S1-4",
        "S1-5",
        "S1-6",
    }
    assert set(runner._SCENARIO_TASKS) == set(STAGE1_SCENARIO_IDS)


def test_every_scenario_specification_validates():
    assert validate_scenario_registry() == STAGE1_SCENARIO_IDS


def test_duplicate_ids_fail_closed():
    specifications = tuple(SCENARIO_REGISTRY.values())
    with pytest.raises(ScenarioRegistryError, match="duplicate scenario IDs"):
        validate_scenario_registry((specifications[0], specifications[0]))


def test_missing_required_field_fails_closed():
    from dataclasses import replace

    specifications = list(SCENARIO_REGISTRY.values())
    specifications[0] = replace(specifications[0], task_description="")
    with pytest.raises(ScenarioRegistryError, match="task_description"):
        validate_scenario_registry(specifications)


def test_unknown_ids_fail_before_runner_mutation(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_environment_baseline",
        lambda: pytest.fail("database baseline must not be read"),
    )
    monkeypatch.setattr(
        runner,
        "create_access_token",
        lambda *_args, **_kwargs: pytest.fail("token must not be minted"),
    )
    monkeypatch.setattr(sys, "argv", ["runner", "--scenario-ids", "S1-99"])

    assert runner.main() == 2


def test_incomplete_full_matrix_fails_before_mutation():
    specifications = tuple(SCENARIO_REGISTRY.values())[:-1]
    with pytest.raises(ScenarioRegistryError, match="incomplete Stage 1 registry"):
        validate_scenario_registry(specifications)


def test_subset_is_visibly_non_certification():
    subset = validate_requested_scenario_ids(("S1-2", "S1-3"))
    assert subset.certification_classification == DEBUG_SUBSET_CLASSIFICATION
    assert not subset.is_certification_matrix

    full = validate_requested_scenario_ids(STAGE1_SCENARIO_IDS)
    assert full.certification_classification == STAGE1_CERTIFICATION_CLASSIFICATION
    assert full.is_certification_matrix


def test_planner_bindings_are_explicit_and_registered_for_s1_2_s1_3():
    for scenario_id, source, test in (
        ("S1-2", "SOURCE_PRESENT", "EXPECTED_TEST_PRESENT"),
        ("S1-3", "SOURCE_PRESENT", "EXPECTED_TEST_PRESENT"),
    ):
        binding = scenario_spec(scenario_id).planner_contract
        assert binding is not None
        assert binding.contract_id == "ST23-PLANNER-001"
        assert binding.contract_version == "v1"
        assert binding.scenario_id == scenario_id
        assert binding.source_expectation == source
        assert binding.test_expectation == test
        assert "CONTRACT_REGISTERED" in binding.structural_evidence
        assert "SCENARIO_ID_MATCH" in binding.structural_evidence


def test_review_publication_bindings_are_explicit_for_s1_5_s1_6():
    expected = {
        "S1-5": ("REVIEW_REQUIRED", "PUBLICATION_REQUIRED"),
        "S1-6": ("REVIEW_REQUIRED", "PUBLICATION_ALLOWED"),
    }
    for scenario_id, (review_expectation, publication_expectation) in expected.items():
        specification = scenario_spec(scenario_id)
        assert specification.review_contract.contract_id == "ST23-REVIEW-001"
        assert specification.publication_contract.contract_id == "ST23-PUBLICATION-001"
        assert specification.review_contract.contract_version == "v1"
        assert specification.publication_contract.contract_version == "v1"
        assert specification.review_contract.expectation == review_expectation
        assert specification.publication_contract.expectation == publication_expectation


def test_scenario_serialization_is_deterministic():
    first = json.dumps(
        scenario_spec("S1-5").to_dict(), sort_keys=True, separators=(",", ":")
    )
    second = json.dumps(
        scenario_spec("S1-5").to_dict(), sort_keys=True, separators=(",", ":")
    )
    assert first == second


def test_validation_inspection_does_not_create_projects_tasks_or_workspace_mutation(
    monkeypatch,
):
    monkeypatch.setattr(sys, "argv", ["runner", "--validate-scenarios", "--json"])
    monkeypatch.setattr(
        runner,
        "_environment_baseline",
        lambda: pytest.fail("inspection must not inspect or mutate live DB"),
    )
    monkeypatch.setattr(
        runner,
        "CertificationEvidenceSession",
        lambda *_args, **_kwargs: pytest.fail("inspection must not create evidence"),
    )
    monkeypatch.setattr(
        runner,
        "_api",
        lambda *_args, **_kwargs: pytest.fail("inspection must not dispatch API"),
    )

    assert runner.main() == 0


def test_retained_v4_fixtures_match_the_promoted_task_specs():
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "phase31c_v4_frozen_scenario_task_specs.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert tuple(payload["specs"]) == STAGE1_SCENARIO_IDS
    for scenario_id in STAGE1_SCENARIO_IDS:
        specification = scenario_spec(scenario_id)
        retained = payload["specs"][scenario_id]
        assert specification.task_title == retained["title"]
        assert specification.task_description == retained["description"]
        assert runner._SCENARIO_TASKS[scenario_id] == retained


def test_s1_5_adjudication_consumes_authoritative_review_and_publication_facts():
    specification = scenario_spec("S1-5")
    result = specification.adjudicate_authoritative_outcome(
        structural_evidence={
            "contract_id": "ST23-REVIEW-001",
            "contract_version": "v1",
            "facts": {
                "CONTRACT_REGISTERED": True,
                "SCENARIO_ID_MATCH": True,
                "REVIEW_DECISION_RECORDED": True,
                "PUBLICATION_FACT_RECORDED": True,
            },
        },
        runtime_record={"outcome": "RUNTIME_COMPLETED"},
        product_state={"useful_work_completed": True},
        review_record={"outcome": "hold_for_review"},
        publication_record={"outcome": "held_for_review"},
    )
    assert result.product_outcome.value == "SUCCESS_REQUIRES_REVIEW"
    assert result.review_outcome.value == "REVIEW_HELD"
    assert result.publication_outcome.value == "PUBLICATION_HELD_FOR_REVIEW"


def test_s1_6_adjudication_preserves_unexpected_publication_mismatch():
    specification = scenario_spec("S1-6")
    result = specification.adjudicate_authoritative_outcome(
        structural_evidence={
            "contract_id": "ST23-REVIEW-001",
            "contract_version": "v1",
            "facts": {
                "CONTRACT_REGISTERED": True,
                "SCENARIO_ID_MATCH": True,
                "REVIEW_DECISION_RECORDED": True,
                "PUBLICATION_FACT_RECORDED": True,
            },
        },
        runtime_record={"outcome": "RUNTIME_COMPLETED"},
        product_state={"useful_work_completed": True},
        review_record={"outcome": "auto_promote", "held_for_review": False},
        publication_record={"outcome": "published"},
    )
    assert result.product_outcome.value == "SUCCESS_PUBLISHED"
    assert result.review_outcome.value == "REVIEW_EXPECTATION_NOT_MET"
    assert result.publication_outcome.value == "PUBLICATION_PUBLISHED_UNEXPECTEDLY"
    assert result.certification_outcome.value == "CERTIFICATION_EXPECTATION_MISMATCH"
