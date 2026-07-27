"""Phase 31E-2 grounded implementation-heavy planning repair tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    plan_has_concrete_source_materialization,
)
from app.services.orchestration.planning.workspace_identity import (
    PlannerWorkspaceIdentity,
)
from app.services.orchestration.validation.validator import ValidatorService
from scripts.maintenance.phase31_certification_runner import (
    planner_contract_payload_for_scenario,
)
from scripts.maintenance.phase31_certification_scenarios import scenario_spec


def _contract() -> dict:
    return planner_contract_payload_for_scenario(scenario_spec("S1-2"))


def _repair_prompt(tmp_path: Path, malformed_output: str | None = None) -> str:
    malformed_output = malformed_output or json.dumps(
        [
            {
                "step_number": 1,
                "description": "Inspect the workspace and add the endpoint",
                "commands": ["python3 -m pytest -q"],
                "verification": "python3 -m pytest -q",
                "rollback": None,
                "expected_files": [],
                "ops": [],
            }
        ]
    )
    identity = PlannerWorkspaceIdentity.from_paths(
        project_workspace=tmp_path,
        physical_runtime_root=tmp_path,
        logical_project_name="empty-certification-workspace",
    )
    return PlannerService.build_planning_repair_prompt_with_metadata(
        task_description="Add the certification endpoint and focused test.",
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=[
            "Implementation task plan does not materialize any source changes",
            "placeholder_only_implementation",
        ],
        planner_contract=_contract(),
        workspace_identity=identity,
    ).prompt


def _concrete_s1_2_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Implement the registered certification endpoint",
            "commands": ["python3 -m py_compile app/main.py"],
            "verification": "python3 -m py_compile app/main.py",
            "rollback": None,
            "expected_files": ["app/main.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/main.py",
                    "content": (
                        "from fastapi import FastAPI\n"
                        "\n"
                        "app = FastAPI()\n"
                        "\n"
                        "@app.get('/api/certification/status')\n"
                        "def certification_status():\n"
                        "    return {'status': 'ok', 'certification_stage': '31C'}\n"
                    ),
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Add the registered focused test",
            "commands": ["python3 -m pytest -q tests/test_certification.py"],
            "verification": "python3 -m pytest -q tests/test_certification.py",
            "rollback": None,
            "expected_files": ["tests/test_certification.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_certification.py",
                    "content": (
                        "from app.main import certification_status\n"
                        "\n"
                        "\n"
                        "def test_certification_status():\n"
                        "    assert certification_status() == {\n"
                        "        'status': 'ok',\n"
                        "        'certification_stage': '31C',\n"
                        "    }\n"
                    ),
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Run the focused verification suite",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]


def test_repair_prompt_requires_concrete_registered_source_and_test_ops(tmp_path):
    prompt = _repair_prompt(tmp_path)

    assert "ST23-PLANNER-001" in prompt
    assert "contract_version: v1" in prompt
    assert "scenario_id: S1-2" in prompt
    assert "SOURCE_PRESENT" in prompt
    assert "EXPECTED_TEST_PRESENT" in prompt
    assert "REVIEW_NOT_REQUIRED" in prompt
    assert "PUBLICATION_REQUIRED" in prompt
    assert "app/main.py" in prompt
    assert "tests/test_certification.py" in prompt
    assert "concrete" in prompt.lower()
    assert "ops.write_file" in prompt
    assert "inspection-only" in prompt.lower()
    assert prompt.count("AUTHORITATIVE REGISTERED PLANNER") == 1


def test_contract_aware_repair_does_not_require_workspace_discovery(tmp_path):
    assert not (tmp_path / "app" / "main.py").exists()
    assert not (tmp_path / "tests" / "test_certification.py").exists()

    prompt = _repair_prompt(tmp_path)

    assert "workspace discovery is not a prerequisite" in prompt.lower()
    assert "app/main.py" in prompt
    assert "tests/test_certification.py" in prompt


def test_missing_concrete_source_materialization_remains_rejected(tmp_path):
    test_only_plan = _concrete_s1_2_plan()
    test_only_plan[0]["ops"] = []
    test_only_plan[0]["expected_files"] = []

    outcome = ValidatorService.validate_plan(
        test_only_plan,
        output_text=json.dumps(test_only_plan),
        task_prompt="Add the certification endpoint and focused test.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="new backend API endpoint",
        description="Implement the endpoint in app/main.py and add its test.",
        is_first_ordered_task=True,
        planner_contract=_contract(),
    )

    assert outcome.verdict.status != "accepted"
    assert any(
        "materialize" in reason.lower() or "bootstrap" in reason.lower()
        for reason in outcome.verdict.reasons
    )
    assert not plan_has_concrete_source_materialization(
        test_only_plan,
        tmp_path,
        authoritative_source_paths={"app/main.py"},
    )


def test_concrete_registered_source_and_test_writes_reach_plan_validation(tmp_path):
    plan = _concrete_s1_2_plan()

    assert plan_has_concrete_source_materialization(
        plan,
        tmp_path,
        authoritative_source_paths={"app/main.py"},
    )

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Add the certification endpoint and focused test.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="new backend API endpoint",
        description="Implement the endpoint in app/main.py and add its test.",
        is_first_ordered_task=True,
        planner_contract=_contract(),
    )

    assert outcome.verdict.status == "accepted", outcome.verdict.reasons
    assert outcome.verdict.details["task1_bootstrap_contract"]["passed"] is True


def test_repair_rejects_materialization_outside_registered_inventory(tmp_path):
    plan = _concrete_s1_2_plan()
    plan[0]["ops"].append(
        {
            "op": "write_file",
            "path": "unrelated.py",
            "content": "def unrelated():\n    return True\n",
        }
    )
    plan[0]["expected_files"].append("unrelated.py")

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Add the certification endpoint and focused test.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="new backend API endpoint",
        description="Implement the endpoint in app/main.py and add its test.",
        is_first_ordered_task=True,
        planner_contract=_contract(),
    )

    assert outcome.verdict.status != "accepted"
    assert "unexpected_registered_contract_paths" in outcome.verdict.details


def test_legacy_repair_prompt_has_no_certification_contract(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Build a small unrelated feature.",
        malformed_output="[]",
        project_dir=tmp_path,
        rejection_reasons=["missing verification"],
    )

    assert "AUTHORITATIVE REGISTERED PLANNER REPAIR CONTRACT" not in prompt
    assert "app/main.py" not in prompt


def test_provider_rate_limit_is_not_a_repair_success():
    rate_limit_response = "⚠️ API rate limit reached. Please try again later."

    assert (
        PlannerService._normalize_repair_json_array_output(rate_limit_response) is None
    )


def test_contract_aware_repair_prompt_stays_under_bounded_limit(tmp_path):
    malformed = json.dumps(
        [
            {
                "step_number": 1,
                "description": "invalid plan " + ("x" * 2500),
                "commands": [],
                "verification": "python3 -m pytest -q",
                "rollback": None,
                "expected_files": ["app/main.py", "tests/test_certification.py"],
                "ops": [],
            }
        ]
    )

    prompt = _repair_prompt(tmp_path, malformed)

    assert len(prompt) <= 8000
    assert "app/main.py" in prompt
    assert "tests/test_certification.py" in prompt
