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
from app.tests.planner_timeout_test_helpers import (
    _patch_planning_flow_external_writes,
    _stub_read_only_discovery_provider,
)


TARGET = "pkg/current.py"
# The stale anchor drops a blank line the real file has, reproducing the
# Phase 32N-3C provider defect the anchored contract exists to remove.
CURRENT_OLD = "def value():\n\n    return 1\n"


@pytest.fixture(autouse=True)
def _stub_discovery(monkeypatch):
    _stub_read_only_discovery_provider(monkeypatch)


STALE_OLD = "def value():\n    return 1\n"
REPLACEMENT_NEW = "def value():\n\n    return 2\n"
MINIMAL_ANCHOR_ID = "anchor-1-1-1"
MINIMAL_ANCHOR_OLD = "    return 1"
MINIMAL_ANCHOR_NEW = "    return 2"
# `STALE_OLD` diverges from the current source only by the file's own blank
# line, so it is now realigned deterministically before validation and never
# reaches the operation-repair lane. Routing itself is exercised with an anchor
# that names a line the file does not contain, which no derivation can realign.
UNREALIGNABLE_STALE_OLD = "def helper():\n    return 1\n"
UNREALIGNABLE_NEW = "def helper():\n    return 2\n"


def _plan(old: str, new: str = REPLACEMENT_NEW) -> list[dict]:
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
                    "new": new,
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
                    "anchor_id": MINIMAL_ANCHOR_ID,
                    "new": MINIMAL_ANCHOR_NEW,
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
                if plan
                and plan[0]["ops"][0].get("old") in {STALE_OLD, UNREALIGNABLE_STALE_OLD}
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
    initial = _plan(UNREALIGNABLE_STALE_OLD, UNREALIGNABLE_NEW)
    accepted = _plan(CURRENT_OLD)
    # The operation lane reconstructs the operation from the Orchestrator-owned
    # anchor, so the repaired step carries the anchor text rather than the
    # model's widened block.
    anchored = _plan(MINIMAL_ANCHOR_OLD, UNREALIGNABLE_NEW)
    anchored[0]["ops"][0]["new"] = MINIMAL_ANCHOR_NEW
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
    assert ctx.orchestration_state.plan == anchored
    prompt = operation_calls[0]["repair_prompt"]
    rejected = json.loads(prompt)["rejected_operations"][0]
    # The stale anchor is withheld and the exact source anchors are supplied.
    assert "old" not in rejected["original_rejected_operation"]
    assert UNREALIGNABLE_STALE_OLD not in prompt
    assert [anchor["anchor_id"] for anchor in rejected["authorized_anchors"]] == [
        MINIMAL_ANCHOR_ID
    ]
    assert rejected["authorized_anchors"][0]["old"] == MINIMAL_ANCHOR_OLD
    assert "pkg/new.py" not in prompt


def test_invalid_operation_merge_stops_without_complete_plan_second_call(
    tmp_path, monkeypatch
):
    ctx = _context(tmp_path, _plan(UNREALIGNABLE_STALE_OLD, UNREALIGNABLE_NEW))
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


def test_blank_line_divergent_anchor_never_reaches_the_operation_repair_lane(
    tmp_path, monkeypatch
):
    """The blank-line case is resolved before validation, so no repair runs.

    `STALE_OLD` reproduces every significant line of the current source and
    loses only the blank line the file itself carries. Orchestrator owns those
    bytes, so the anchor is realigned first-path and the operation-repair lane
    -- and its provider call -- is never entered.
    """

    ctx = _context(tmp_path, _plan(STALE_OLD))
    _pin_materialization(tmp_path, monkeypatch)
    operation_calls = []
    complete_plan_calls = []

    def repair_operations(cls, **kwargs):
        operation_calls.append(kwargs)
        return {"output": _repair_response(), "operation_repair_provider_call_count": 1}

    def repair_output(cls, *args, **kwargs):
        complete_plan_calls.append(kwargs)
        return {"output": "[]"}

    monkeypatch.setattr(
        PlannerService, "repair_operations", classmethod(repair_operations)
    )
    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = _run(ctx)

    assert result == {"status": "completed"}
    assert operation_calls == []
    assert complete_plan_calls == []
    assert ctx.orchestration_state.plan[0]["ops"][0]["old"] == CURRENT_OLD.rstrip("\n")
    assert ctx.orchestration_state.plan[0]["ops"][0]["new"] == REPLACEMENT_NEW
