"""P43-PRODUCT-02: the requested-test finding drives the real Planning repair path.

P43-PRODUCT-01 made Plan validation return a repairable finding when a Task
explicitly requests regression tests and the Plan mutates no test path. These
cases check what happens next, end to end through the real Planning flow:
discovery reader, materializer, Validator, repair funnel
(``_build_repair_rejection_reasons`` -> ``PlannerService.repair_output``),
repair arbitration and the terminal-failure branch. Only the provider seams
(Planning/Discovery dispatch and ``_invoke_repair_prompt``) are replaced; no
provider is called and nothing is executed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.orchestration.phases import planning_support
from app.services.orchestration.phases.planning_flow import execute_planning_phase
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.planner_timeout_test_helpers import (
    _patch_planning_flow_external_writes,
)
from app.tests.test_p43_product01_requested_test_deliverable import (
    FUNCTIONAL_ONLY_TASK,
    REQUESTED_TEST_CODE,
    V17W_TASK,
    _source_only_plan,
    _workspace,
)

FINDING = "Task explicitly requested regression or new tests"
NEW_TEST = "tests/test_inventory_reservation.py"
REQUIREMENT = "add focused regression tests"
TERMINAL_AFTER_REPAIR = "planning_validation_failed_after_repair"


def _plan_with_new_test_file() -> list[dict[str, Any]]:
    plan = _source_only_plan()
    plan.insert(
        2,
        {
            "step_number": 3,
            "description": "Add focused regression tests",
            "commands": [],
            "rollback": None,
            "verification": f"python -m pytest {NEW_TEST} -q",
            "expected_files": [NEW_TEST],
            "ops": [
                {
                    "op": "write_file",
                    "path": NEW_TEST,
                    "content": (
                        "from inventory.service import Inventory\n\n\n"
                        "def test_unknown_sku_has_no_available_stock() -> None:\n"
                        '    assert Inventory().available_stock("SKU-3") == 0\n'
                    ),
                }
            ],
        },
    )
    for number, step in enumerate(plan, start=1):
        step["step_number"] = number
    return plan


class _Calls:
    def __init__(self) -> None:
        self.planning = 0
        self.repair_prompts: list[str] = []
        self.repair_kwargs: list[dict] = []
        self.verdicts: list[Any] = []
        self.terminal: list[dict] = []


@pytest.fixture(autouse=True)
def _isolate_flow(monkeypatch):
    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Implement inventory reservations",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run pytest"],
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


def _run(tmp_path: Path, monkeypatch, task: str, initial, repaired):
    root = _workspace(tmp_path).resolve()
    calls = _Calls()

    async def planning_lock(cls, runtime_service, prompt, **kwargs):
        if kwargs.get("diagnostic_label") == "PLANNING_DISCOVERY":
            return {
                "status": "completed",
                "output": json.dumps(
                    {"action": "read_file", "path": "inventory/service.py"}
                ),
            }
        calls.planning += 1
        return {"status": "completed", "output": json.dumps(initial)}

    async def fake_repair(runtime_service, repair_prompt, repair_timeout, **kwargs):
        calls.repair_prompts.append(repair_prompt)
        return {"status": "completed", "output": json.dumps(repaired)}

    original_repair_output = PlannerService.repair_output.__func__

    def recording_repair_output(cls, *args, **kwargs):
        calls.repair_kwargs.append(kwargs)
        return original_repair_output(cls, *args, **kwargs)

    original_validate_plan = ValidatorService.validate_plan.__func__

    def recording_validate_plan(cls, *args, **kwargs):
        verdict = original_validate_plan(cls, *args, **kwargs)
        calls.verdicts.append(verdict)
        return verdict

    original_finalize = planning_support._finalize_planning_terminal_failure

    def recording_finalize(**kwargs):
        calls.terminal.append(kwargs)
        return original_finalize(**kwargs)

    monkeypatch.setattr(
        PlannerService, "_execute_task_with_planning_lock", classmethod(planning_lock)
    )
    monkeypatch.setattr(
        PlannerService, "_invoke_repair_prompt", staticmethod(fake_repair)
    )
    monkeypatch.setattr(
        PlannerService, "repair_output", classmethod(recording_repair_output)
    )
    monkeypatch.setattr(
        ValidatorService, "validate_plan", classmethod(recording_validate_plan)
    )
    for module in ("planning_support", "planning_flow"):
        monkeypatch.setattr(
            f"app.services.orchestration.phases.{module}."
            "_finalize_planning_terminal_failure",
            recording_finalize,
        )

    state = MagicMock()
    state.project_dir = root
    state.project_context = ""
    state.plan = []
    state.current_step_index = 0
    state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial)}

    task_row = MagicMock()
    task_row.title = "Implement Inventory Reservation Support"
    task_row.description = task
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(),
        project=MagicMock(),
        task=task_row,
        session_task_link=MagicMock(),
        session_id=4302,
        task_id=4302,
        prompt=task,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.p43_product02"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )
    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )
    return root, ctx, calls, result


def _collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _plan_paths(plan) -> list[tuple[str, str]]:
    return [
        (operation["op"], operation["path"])
        for step in plan
        for operation in step.get("ops") or []
    ]


@pytest.fixture(params=[None, 32768], ids=["pytest-budget", "runtime-budget"])
def repair_budget(request, monkeypatch):
    """Both repair-prompt shapes: the 8000-char floor and the runtime 32768."""

    monkeypatch.setattr(
        settings, "PLANNING_REPAIR_CONTEXT_TOKENS", request.param, raising=False
    )
    return request.param


def test_omission_is_repaired_into_an_accepted_plan_with_tests(
    tmp_path, monkeypatch, repair_budget
):
    root, ctx, calls, result = _run(
        tmp_path,
        monkeypatch,
        V17W_TASK,
        _source_only_plan(),
        _plan_with_new_test_file(),
    )

    # (1) the initial Plan carries the repairable requested-test finding.
    first = calls.verdicts[0]
    assert not first.accepted and first.repairable
    assert first.details["requested_test_deliverable"]["code"] == REQUESTED_TEST_CODE
    assert any(reason.startswith(FINDING) for reason in first.reasons)

    # (2) the finding reaches the one Planning repair as a rejection reason.
    assert calls.planning == 1
    assert len(calls.repair_prompts) == 1
    kwargs = calls.repair_kwargs[0]
    assert kwargs["reason"].startswith("plan_validation_failed: ")
    assert FINDING in kwargs["reason"]
    assert any(reason.startswith(FINDING) for reason in kwargs["rejection_reasons"])

    # (3) the repair prompt keeps both the finding and the user's requirement.
    prompt = _collapsed(calls.repair_prompts[0])
    assert FINDING in prompt
    if repair_budget == 32768:
        # The deployed runtime budget renders the full repair prompt, which
        # now carries the user's objective with the finding.  The 8000-char
        # floor uses the compact observation prompt and still omits it.
        assert "Task objective (preserve): " in prompt
        assert REQUIREMENT in prompt.split("Task objective (preserve): ", 1)[1]
        # P43-PRODUCT-03: the live flow also carries the admissible test target.
        assert "## REQUESTED TEST DELIVERABLE" in prompt
        assert "listed in that step's expected_files" in prompt
    assert kwargs["task_description"] == V17W_TASK

    # (4) the corrected Plan passes the unchanged Validator and is admitted.
    assert result == {"status": "completed"}, calls.terminal
    final = calls.verdicts[-1]
    assert final.accepted, final.reasons
    assert final.details["requested_test_deliverable"] == {
        "requested": True,
        "test_mutation_paths": [NEW_TEST],
        "code": None,
    }
    assert ("write_file", NEW_TEST) in _plan_paths(ctx.orchestration_state.plan)
    assert calls.terminal == []
    # Admission only: Planning executes nothing.
    assert not (root / NEW_TEST).exists()


def test_unresolved_omission_terminates_instead_of_being_accepted(
    tmp_path, monkeypatch, repair_budget
):
    plan = _source_only_plan()
    root, ctx, calls, result = _run(tmp_path, monkeypatch, V17W_TASK, plan, plan)

    # (5) one bounded repair, then a deterministic Planning failure.
    assert len(calls.repair_prompts) == 1
    assert result.get("status") == "failed"
    assert [call["failure_type"] for call in calls.terminal] == [TERMINAL_AFTER_REPAIR]
    assert FINDING in calls.terminal[0]["failure_reason"]
    assert not any(verdict.accepted for verdict in calls.verdicts)
    assert (root / "inventory" / "models.py").read_text(encoding="utf-8").count(
        "reserved_stock"
    ) == 0


def test_functional_only_task_is_admitted_without_repair(tmp_path, monkeypatch):
    plan = _source_only_plan()
    _, ctx, calls, result = _run(
        tmp_path, monkeypatch, FUNCTIONAL_ONLY_TASK, plan, plan
    )

    # (6) no explicit new-test requirement: unaffected, no repair.
    assert result == {"status": "completed"}
    assert calls.repair_prompts == []
    assert all(
        "requested_test_deliverable" not in verdict.details
        for verdict in calls.verdicts
    )


def test_initial_plan_with_tests_needs_no_repair(tmp_path, monkeypatch):
    plan = _plan_with_new_test_file()
    _, ctx, calls, result = _run(tmp_path, monkeypatch, V17W_TASK, plan, plan)

    assert result == {"status": "completed"}
    assert calls.repair_prompts == []
    assert calls.verdicts[-1].details["requested_test_deliverable"]["code"] is None


def test_repair_prompt_keeps_objective_only_for_the_requested_test_finding(
    tmp_path, monkeypatch
):
    # CI may not provide the deployment-only repair context setting.  That
    # selects the bounded compact fallback, which must preserve the same
    # requested-test objective contract as the full prompt.
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    root = _workspace(tmp_path)
    plan = json.dumps(_source_only_plan())

    def prompt(reasons: list[str]) -> str:
        return PlannerService.build_planning_repair_prompt_with_metadata(
            V17W_TASK, plan, root, rejection_reasons=reasons
        ).prompt

    finding = (
        "Task explicitly requested regression or new tests, but no plan "
        "operation adds or edits a test file (use append_file, replace_in_file, "
        "or authorized write_file on a test path)"
    )
    with_finding = _collapsed(prompt([finding]))
    assert "Task objective (preserve): " in with_finding
    assert REQUIREMENT in with_finding

    # Other findings keep the existing "repair the plan, not the task" shape.
    other = _collapsed(prompt(["Step 3: verification command is weak"]))
    assert "Task objective (preserve)" not in other
