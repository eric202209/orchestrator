"""Phase 31E-1 live planner contract propagation tests."""

from __future__ import annotations

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.task_bootstrap_contract import (
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.planning.planner_contract_registry import (
    planner_grounding_evidence,
)
from scripts.maintenance.phase31_certification_runner import (
    planner_contract_payload_for_scenario,
)
from scripts.maintenance.phase31_certification_scenarios import scenario_spec


def _step(*ops, expected_files=None):
    return {
        "step_number": 1,
        "description": "Materialize the registered implementation slice",
        "commands": [],
        "verification": "python -m pytest -q",
        "rollback": None,
        "expected_files": list(expected_files or []),
        "ops": list(ops),
    }


def test_registered_scenario_facts_arrive_unchanged_at_task1_bootstrap():
    payload = planner_contract_payload_for_scenario(scenario_spec("S1-2"))

    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                {
                    "op": "write_file",
                    "path": "app/main.py",
                    "content": "def status():\n    return {'status': 'ok'}\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_certification.py",
                    "content": "def test_status():\n    assert True\n",
                },
                expected_files=["app/main.py", "tests/test_certification.py"],
            )
        ],
        task_prompt="The prompt must not decide the contract.",
        planner_contract=payload,
        require_registered_contract=True,
    )

    assert verdict.passed, verdict.violations
    assert verdict.contract.contract_id == payload["contract_id"]
    assert verdict.contract.scenario_id == payload["scenario_id"]
    assert verdict.contract.source_expectation == payload["source_expectation"]
    assert verdict.contract.test_expectation == payload["test_expectation"]
    assert verdict.contract.expected_source_files == ["app/main.py"]
    assert verdict.contract.expected_test_files == ["tests/test_certification.py"]


def test_planning_and_repair_prompts_receive_the_same_authoritative_contract(tmp_path):
    payload = planner_contract_payload_for_scenario(scenario_spec("S1-2"))

    planning_prompt = PlannerService.build_minimal_planning_prompt(
        "Infer whatever tests are needed.",
        tmp_path,
        planner_contract=payload,
    )
    repair_prompt = PlannerService.build_planning_repair_prompt(
        "Infer whatever tests are needed.",
        '[{"step_number": 1}]',
        tmp_path,
        rejection_reasons=["missing verification"],
        planner_contract=payload,
    )

    for prompt in (planning_prompt, repair_prompt):
        assert '"scenario_id":"S1-2"' in prompt.replace(" ", "")
        assert "SOURCE_PRESENT" in prompt
        assert "EXPECTED_TEST_PRESENT" in prompt
        assert "REVIEW_NOT_REQUIRED" in prompt
        assert "PUBLICATION_REQUIRED" in prompt
        assert "app/main.py" in prompt
        assert "tests/test_certification.py" in prompt
        assert "do not infer" in prompt.lower()


def test_grounding_evidence_proves_authoritative_contract_and_runtime_context():
    payload = planner_contract_payload_for_scenario(scenario_spec("S1-2"))

    evidence = planner_grounding_evidence(
        payload,
        runtime_context={"session_id": 11, "task_id": 22},
        planner_prompt="authoritative planner prompt",
    )

    assert evidence["authoritative_contract_available"] is True
    assert evidence["registered_scenario_contract"]["scenario_id"] == "S1-2"
    assert evidence["propagated_planner_contract"] == payload
    assert evidence["source_expectations"]["source"] == "SOURCE_PRESENT"
    assert evidence["test_expectations"]["test"] == "EXPECTED_TEST_PRESENT"
    assert evidence["review_expectations"]["review"] == "REVIEW_NOT_REQUIRED"
    assert evidence["publication_expectations"]["publication"] == "PUBLICATION_REQUIRED"
    assert evidence["required_source_inventory"] == ["app/main.py"]
    assert evidence["runtime_planner_context"] == {"session_id": 11, "task_id": 22}
    assert evidence["planner_prompt"] == "authoritative planner prompt"


def test_non_certification_planning_keeps_legacy_prompt_shape(tmp_path):
    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a small unrelated feature.",
        tmp_path,
    )

    assert "AUTHORITATIVE REGISTERED PLANNER CONTRACT" not in prompt
