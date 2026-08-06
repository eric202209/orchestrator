"""Production-flow coverage for Phase 32N-3 operation repair routing."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.phases.planning_flow import execute_planning_phase
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.types import OrchestrationRunContext
from app.tests.planner_timeout_test_helpers import _patch_planning_flow_external_writes


TARGET = "pkg/current.py"
CURRENT_OLD = "def value():\n    return 1\n"
STALE_OLD = "def value():\n    return 0\n"
REPLACEMENT_NEW = "def value():\n    return 2\n"


def _plan(old: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Update the existing value helper",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": TARGET,
                    "old": old,
                    "new": REPLACEMENT_NEW,
                }
            ],
            "verification": "python3 -m py_compile pkg/current.py",
            "rollback": None,
            "expected_files": [TARGET],
        },
        {
            "step_number": 2,
            "description": "Create the authorized companion file",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "pkg/new.py",
                    "content": "VALUE = 2\n",
                }
            ],
            "verification": "python3 -m py_compile pkg/new.py",
            "rollback": None,
            "expected_files": ["pkg/new.py"],
        },
    ]


def _repair_response() -> str:
    return json.dumps(
        {
            "repairs": [
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "replace_in_file",
                        "path": TARGET,
                        "old": CURRENT_OLD,
                        "new": REPLACEMENT_NEW,
                    },
                }
            ]
        }
    )


def _context(tmp_path, initial_plan):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "current.py").write_text(
        CURRENT_OLD + ("# retained filler\n" * 1_200), encoding="utf-8"
    )

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Update current.py and create new.py"
    task.description = "Update pkg/current.py and create pkg/new.py"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=3203,
        task_id=3203,
        prompt="Update pkg/current.py and create pkg/new.py",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.phase32n3.operation-routing"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )
    return ctx


def _run(ctx):
    return execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )


def _pin_materialization(tmp_path, monkeypatch):
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=[TARGET, "pkg/new.py"],
        task_description="Update pkg/current.py and create pkg/new.py",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.materialize_planner_source_context",
        lambda *args, **kwargs: materialization,
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(
            lambda plan, *args, **kwargs: (
                {"stale_replace_ops_steps": [1]}
                if plan and plan[0]["ops"][0].get("old") == STALE_OLD
                else {}
            )
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_flow(monkeypatch):
    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Update one helper",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Compile both files"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict", (), {"accepted": True, "status": "accepted", "reasons": []}
            )()
        ),
    )


def test_production_flow_repairs_only_rejected_operation_once(tmp_path, monkeypatch):
    initial = _plan(STALE_OLD)
    accepted = _plan(CURRENT_OLD)
    ctx = _context(tmp_path, initial)
    _pin_materialization(tmp_path, monkeypatch)
    operation_calls = []
    complete_plan_calls = []

    def repair_operations(cls, **kwargs):
        operation_calls.append(kwargs)
        return {"output": _repair_response(), "operation_repair_provider_call_count": 1}

    def repair_output(cls, *args, **kwargs):
        complete_plan_calls.append(kwargs)
        return {"output": json.dumps(accepted)}

    monkeypatch.setattr(
        PlannerService, "repair_operations", classmethod(repair_operations)
    )
    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = _run(ctx)

    assert result == {"status": "completed"}
    assert len(operation_calls) == 1
    assert complete_plan_calls == []
    assert ctx.orchestration_state.plan == accepted
    prompt = operation_calls[0]["repair_prompt"]
    assert (
        json.loads(prompt)["rejected_operations"][0]["original_rejected_operation"][
            "old"
        ]
        == STALE_OLD
    )
    assert "pkg/new.py" not in prompt


def test_invalid_operation_merge_stops_without_complete_plan_second_call(
    tmp_path, monkeypatch
):
    ctx = _context(tmp_path, _plan(STALE_OLD))
    _pin_materialization(tmp_path, monkeypatch)
    operation_calls = []
    complete_plan_calls = []

    def repair_operations(cls, **kwargs):
        operation_calls.append(kwargs)
        return {"output": '{"repairs":[]}', "operation_repair_provider_call_count": 1}

    def repair_output(cls, *args, **kwargs):
        complete_plan_calls.append(kwargs)
        return {"output": "[]"}

    monkeypatch.setattr(
        PlannerService, "repair_operations", classmethod(repair_operations)
    )
    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = _run(ctx)

    assert result == {
        "status": "failed",
        "reason": "planning_operation_repair_failed",
    }
    assert len(operation_calls) == 1
    assert complete_plan_calls == []
