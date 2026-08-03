"""Phase 32C-1 red contract tests for planner source grounding.

These tests intentionally describe the contract that a future bounded repair
must satisfy. No production planner behavior is changed in this phase.
"""

import json

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt,
)
from app.services.orchestration.validation.validator import ValidatorService


def _plan(*, operation, path, expected_files=None, commands=None):
    return [
        {
            "step_number": 1,
            "description": "Apply the grounded source change",
            "commands": commands or [],
            "ops": (
                [{"op": operation, "path": path, "old": "old", "new": "new"}]
                if operation == "replace_in_file"
                else [
                    {
                        "op": operation,
                        "path": path,
                        "content": "def existing():\n    return 2\n",
                    }
                ]
            ),
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": expected_files or [path],
        }
    ]


def _validate(plan, tmp_path, task_prompt="Update existing.py"):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task_prompt,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Phase 32C-1 contract test",
        description=task_prompt,
    )


def test_replace_old_text_must_exist_before_plan_acceptance(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = _plan(
        operation="replace_in_file",
        path="existing.py",
        commands=[],
    )

    outcome = _validate(plan, tmp_path)

    assert not outcome.accepted, "stale old_text must be rejected before acceptance"


def test_future_read_step_cannot_ground_later_static_replace(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = [
        {
            "step_number": 1,
            "description": "Read the current source",
            "commands": ["{'op': 'read_file', 'path': 'existing.py'}"],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Apply an exact replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "existing.py",
                    "old": "invented source",
                    "new": "new source",
                }
            ],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": ["existing.py"],
        },
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan, tmp_path)

    assert issues["stale_replace_ops_steps"] == [2]


def test_missing_source_repair_receives_exact_current_source(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("def current_source():\n    return 1\n", encoding="utf-8")
    malformed = json.dumps(
        _plan(operation="replace_in_file", path="existing.py", commands=[])
    )

    prompt = build_planning_repair_prompt(
        task_description="Update existing.py",
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=["missing_source_materialization"],
    )

    assert "def current_source():" in prompt


def test_repair_without_progressing_evidence_is_not_improvement(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    stale_plan = _plan(operation="replace_in_file", path="existing.py", commands=[])

    arbitration = classify_planning_repair_candidate(
        previous_plan=stale_plan,
        repaired_plan=stale_plan,
        project_dir=tmp_path,
        immediate_repair_issues={"stale_replace_ops_steps": [1]},
    )

    assert arbitration["outcome"] != "improved_or_preserved"
    assert "stale_replace" in arbitration["regression_labels"]


def test_existing_file_write_requires_explicit_replace_authorization(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = _plan(operation="write_file", path="existing.py", commands=[])

    outcome = _validate(plan, tmp_path)

    assert not outcome.accepted, "existing-file write needs explicit authorization"


def test_expected_files_must_be_materialized_before_validation(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Declare an output without materializing it",
            "commands": [],
            "ops": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["new.py"],
        }
    ]

    outcome = _validate(plan, tmp_path, task_prompt="Create new.py")

    assert not outcome.accepted
    assert "unmaterialized_expected_files" in outcome.details


def test_task_scope_remains_authoritative(tmp_path):
    plan = _plan(
        operation="write_file",
        path="outside_scope.py",
        commands=[],
        expected_files=["outside_scope.py"],
    )

    outcome = _validate(
        plan,
        tmp_path,
        task_prompt="Modify only app/allowed.py; do not change other files",
    )

    assert not outcome.accepted, "the validator must enforce the task scope"


def test_grounded_valid_plan_passes_without_repair(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("def existing():\n    return 1\n", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Apply the exact current-source replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "existing.py",
                    "old": "def existing():\n    return 1\n",
                    "new": "def existing():\n    return 2\n",
                }
            ],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": ["existing.py"],
        }
    ]

    outcome = _validate(plan, tmp_path)

    assert outcome.accepted, outcome.reasons
