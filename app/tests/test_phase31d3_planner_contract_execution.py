"""Phase 31D-3 planner contract and bootstrap evidence tests."""

from __future__ import annotations

from app.services.orchestration.planning.task_bootstrap_contract import (
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.validation.validator import ValidatorService


def _step(*ops, expected_files=None):
    return {
        "step_number": 1,
        "description": "Apply the registered planning slice",
        "commands": [],
        "verification": "python -m pytest -q",
        "rollback": None,
        "expected_files": list(expected_files or []),
        "ops": list(ops),
    }


def _contract(scenario_id, *, source, tests):
    return {
        "contract_id": "ST23-PLANNER-001",
        "contract_version": "v1",
        "scenario_id": scenario_id,
        "source_expectation": source,
        "test_expectation": tests,
        "structural_evidence": [
            "CONTRACT_REGISTERED",
            "SCENARIO_ID_MATCH",
            "SOURCE_EXPECTATION_DECLARED",
            "TEST_EXPECTATION_DECLARED",
        ],
    }


def test_s1_2_uses_registered_contract_and_intentionally_absent_tests():
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                {
                    "op": "write_file",
                    "path": "app/api.py",
                    "content": "def endpoint():\n    return {'ok': True}\n",
                },
                expected_files=["app/api.py"],
            )
        ],
        task_prompt="This wording must not decide test intent.",
        planner_contract=_contract(
            "S1-2",
            source="SOURCE_MATERIALIZED",
            tests="EXPECTED_TEST_NOT_REQUIRED",
        ),
        require_registered_contract=True,
    )

    assert verdict.passed, verdict.violations
    contract = verdict.contract
    assert contract.terminal_classification == "tests_intentionally_absent"
    assert contract.test_expectation == "EXPECTED_TEST_NOT_REQUIRED"
    assert "EXPECTED_TEST_NOT_REQUIRED" in contract.structural_evidence_used
    assert contract.contract_id == "ST23-PLANNER-001"
    assert contract.scenario_id == "S1-2"
    assert "task1_bootstrap_missing_expected_test_files" not in verdict.violation_codes


def test_s1_3_uses_source_present_and_test_policy_without_prompt_heuristics():
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                {
                    "op": "write_file",
                    "path": "src/money.py",
                    "content": "def format_money(value):\n    return f'${value}'\n",
                },
                expected_files=["src/money.py"],
            )
        ],
        task_prompt="Add tests for this unrelated phrase.",
        existing_files={"src/money.py", "tests/test_money.py"},
        planner_contract=_contract(
            "S1-3",
            source="SOURCE_PRESENT",
            tests="EXPECTED_TEST_NOT_REQUIRED",
        ),
        require_registered_contract=True,
    )

    assert verdict.passed, verdict.violations
    assert verdict.contract.selected_planning_path.endswith(
        ":expected_test_not_required"
    )
    assert "SOURCE_PRESENT" in verdict.contract.structural_evidence_used
    assert verdict.contract.terminal_classification == "ready"


def test_registered_present_test_policy_distinguishes_missing_required_tests():
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def answer():\n    return 42\n",
                },
                expected_files=["src/app.py"],
            )
        ],
        planner_contract=_contract(
            "S1-2",
            source="SOURCE_MATERIALIZED",
            tests="EXPECTED_TEST_PRESENT",
        ),
        require_registered_contract=True,
    )

    assert not verdict.passed
    assert verdict.contract.terminal_classification == "missing_required_tests"
    assert "task1_bootstrap_missing_expected_test_files" in verdict.violation_codes
    assert verdict.contract.limitation_id == "LIM-31D-03"


def test_registered_source_policy_distinguishes_missing_source():
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                {
                    "op": "write_file",
                    "path": "README.md",
                    "content": "A real artifact with enough evidence.\n",
                },
                expected_files=["README.md"],
            )
        ],
        planner_contract=_contract(
            "S1-3",
            source="SOURCE_MATERIALIZED",
            tests="EXPECTED_TEST_NOT_REQUIRED",
        ),
        require_registered_contract=True,
    )

    assert not verdict.passed
    assert verdict.contract.terminal_classification == "missing_source"
    assert "task1_bootstrap_missing_expected_source_files" in verdict.violation_codes


def test_missing_registered_facts_are_a_traceable_terminal_limitation():
    verdict = validate_task1_bootstrap_contract(
        plan=[_step()],
        task_prompt="Tests if needed.",
        planner_contract={},
        require_registered_contract=True,
    )

    assert not verdict.passed
    assert verdict.contract.terminal_classification == "terminal_limitation"
    assert verdict.contract.limitation_id == "LIM-31D-03"
    assert (
        verdict.contract.selected_planning_path == "hold_for_registered_contract_facts"
    )
    assert "infer_test_policy_from_prompt" in verdict.contract.rejected_alternatives
    assert (
        "task1_bootstrap_missing_registered_contract_facts" in verdict.violation_codes
    )


def test_registered_planner_evidence_is_replay_deterministic():
    kwargs = {
        "plan": [
            _step(
                {
                    "op": "write_file",
                    "path": "app/api.py",
                    "content": "def endpoint():\n    return {'ok': True}\n",
                },
                expected_files=["app/api.py"],
            )
        ],
        "planner_contract": _contract(
            "S1-2",
            source="SOURCE_MATERIALIZED",
            tests="EXPECTED_TEST_NOT_REQUIRED",
        ),
        "require_registered_contract": True,
    }

    first = validate_task1_bootstrap_contract(**kwargs).to_dict()
    second = validate_task1_bootstrap_contract(**kwargs).to_dict()
    assert first == second


def test_validator_task1_gate_consumes_the_registered_contract(tmp_path):
    plan = [
        _step(
            {
                "op": "write_file",
                "path": "app/api.py",
                "content": "def endpoint():\n    return {'ok': True}\n",
            },
            expected_files=["app/api.py"],
        )
    ]
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Tests if needed.",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
        planner_contract=_contract(
            "S1-2",
            source="SOURCE_MATERIALIZED",
            tests="EXPECTED_TEST_NOT_REQUIRED",
        ),
    )

    assert verdict.accepted
    evidence = verdict.details["task1_bootstrap_contract"]
    assert evidence["contract_id"] == "ST23-PLANNER-001"
    assert evidence["terminal_classification"] == "tests_intentionally_absent"


def test_validator_does_not_turn_prompt_words_into_missing_test_evidence(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                {
                    "op": "write_file",
                    "path": "app/api.py",
                    "content": "def endpoint():\n    return {'ok': True}\n",
                },
                expected_files=["app/api.py"],
            )
        ],
        output_text="[]",
        task_prompt="Build this endpoint with tests if needed.",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    evidence = verdict.details["task1_bootstrap_contract"]
    assert (
        "task1_bootstrap_missing_expected_test_files" not in evidence["violation_codes"]
    )
    assert evidence["terminal_classification"] == "terminal_limitation"
