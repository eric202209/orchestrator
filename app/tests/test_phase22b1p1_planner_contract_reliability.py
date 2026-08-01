"""Phase 22B-1P1 planner contract regressions."""

from __future__ import annotations

import json
import logging

import pytest

from app.services.orchestration.planning.planner import (
    PlanningRepairOutputContractViolation,
    PlannerService,
)
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt_with_metadata,
)
from app.services.orchestration.validation.validator import ValidatorService


def _stale_app_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Update pagination routes",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["app/api/v1/router.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "app/api/v1/router.py",
                    "old": "stale pagination snippet not present",
                    "new": "current pagination implementation",
                }
            ],
        }
    ]


def _stale_rejection_reasons() -> list[str]:
    return [
        "replace_in_file old text not found in workspace in steps [1]",
        (
            "step 1 replace_in_file old text not found in app/api/v1/router.py. "
            "Use exact text from current file excerpt or choose a different operation. "
            "Current file excerpt: current router excerpt"
        ),
    ]


def test_stale_app_replace_is_rejected_before_mutation(tmp_path):
    plan = _stale_app_plan()

    issues = PlannerService.find_immediate_repair_step_issues(
        plan, project_dir=tmp_path
    )

    assert issues["stale_replace_ops_steps"] == [1]


def test_app_path_stale_repair_uses_bounded_grounded_prompt(tmp_path):
    result = build_planning_repair_prompt_with_metadata(
        task_description="Implement bounded pagination",
        malformed_output=json.dumps(_stale_app_plan()),
        project_dir=tmp_path,
        rejection_reasons=_stale_rejection_reasons(),
        knowledge_context="irrelevant retrieved context",
    )

    assert "Stale replace repair mode." in result.prompt
    assert "Current file excerpt:" in result.prompt
    assert "Bad:" not in result.prompt
    assert result.metadata["repair_prompt_strategy"] == "compact_stale_replace"


def test_repair_json_normalizer_accepts_valid_array_and_rejects_truncation():
    valid = '[{"step_number": 1}]'

    assert PlannerService._normalize_repair_json_array_output(valid) == valid
    assert (
        PlannerService._normalize_repair_json_array_output(
            '[{"step_number": 1, "description": "truncated"}'
        )
        is None
    )


def test_repaired_plan_revalidates_before_execution(tmp_path):
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["rg --files ."],
            "verification": "python3 -m pytest --collect-only -q",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Write the bounded implementation",
            "commands": [],
            "verification": "python3 -m py_compile src/pagination.py",
            "rollback": "rm -f src/pagination.py",
            "expected_files": ["src/pagination.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/pagination.py",
                    "content": "def paginate(items, page, size):\n    return items\n",
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Run the focused verification",
            "commands": ["python3 -m pytest --collect-only -q"],
            "verification": "python3 -m pytest --collect-only -q",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        repaired_plan,
        output_text=json.dumps(repaired_plan),
        task_prompt="Implement bounded pagination in the existing project",
        execution_profile="implementation",
        project_dir=tmp_path,
        title="Implement bounded pagination",
        description="Implement bounded pagination in the existing project",
    )

    assert verdict.accepted, verdict.reasons


def test_malformed_repair_cannot_reach_execution(tmp_path, monkeypatch):
    executed = False

    class Runtime:
        async def invoke_prompt(self, *_args, **_kwargs):
            return {"output": '[{"step_number": 1, "description": "truncated"}'}

        async def execute_task(self, *_args, **_kwargs):
            nonlocal executed
            executed = True
            return {"status": "completed"}

    with pytest.raises(PlanningRepairOutputContractViolation):
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Implement bounded pagination",
            malformed_output="not json",
            project_dir=tmp_path,
            timeout_seconds=10,
            logger=logging.getLogger("test.phase22b1p1.malformed_repair"),
            emit_live=lambda *_args, **_kwargs: None,
            reason="plan_validation_failed",
        )

    assert executed is False
