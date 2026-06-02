"""Phase 12U: Repair Arbitration Bootstrap Contract Alignment.

Focused tests verifying that planning repair arbitration requires Bootstrap
Contract validity as part of the acceptance definition.

Acceptance = repair improved the plan AND repair produced a Bootstrap
Contract-valid plan.

A repaired candidate that fails the Bootstrap Contract must not be classified
as accepted progress. The repair_candidate_rejected_by_bootstrap_contract
diagnostic must be emitted on rejection.

Python source-code Bootstrap Task protections must remain intact.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.phases.planning_repair_arbitration_control import (
    _reject_repair_candidate_by_bootstrap_contract,
    arbitrate_planning_repair_candidate,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState
from app.services.orchestration.validation.validator import ValidatorService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    *,
    ops=None,
    commands=None,
    verification="python -m pytest -q",
    expected_files=None,
):
    step: dict[str, Any] = {
        "step_number": 1,
        "description": "Bootstrap step",
        "commands": commands if commands is not None else [],
        "verification": verification,
        "rollback": None,
        "ops": ops if ops is not None else [],
        "expected_files": expected_files if expected_files is not None else [],
    }
    return step


def _source_code_plan() -> list[dict]:
    return [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def answer():\n    return 42\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_app.py",
                    "content": (
                        "from src.app import answer\n\n"
                        "def test_answer():\n"
                        "    assert answer() == 42\n"
                    ),
                },
            ],
            expected_files=["src/app.py", "tests/test_app.py"],
            verification="python -m pytest -q",
        )
    ]


def _artifact_only_plan(path: str = "reports/status.md") -> list[dict]:
    return [
        _step(
            ops=[{"op": "write_file", "path": path, "content": "# Status\n\nDone.\n"}],
            expected_files=[path],
            verification=f'grep -q "Status" {path}',
        )
    ]


def _plan_without_verification() -> list[dict]:
    """Plan with ops but no verification — fails Bootstrap Contract."""
    return [
        _step(
            ops=[{"op": "write_file", "path": "cli.py", "content": "print('hello')\n"}],
            expected_files=["cli.py"],
            verification="",  # empty — Bootstrap Contract violation
        )
    ]


def _emitted_events(ctx: Any) -> list[tuple]:
    """Return calls made to ctx.emit_live."""
    return ctx.emit_live.call_args_list


def _make_ctx(
    *,
    plan: list,
    project_dir: Path,
    plan_position: int = 1,
    prompt: str = "Bootstrap the first task",
) -> Any:
    task = SimpleNamespace(
        title="Bootstrap task",
        description=prompt,
        plan_position=plan_position,
        status=None,
        error_message=None,
    )
    orchestration_state = SimpleNamespace(
        plan=plan,
        project_dir=project_dir,
        project_context="",
        status=None,
        abort_reason=None,
        reasoning_artifact=None,
    )
    ctx = SimpleNamespace(
        task=task,
        orchestration_state=orchestration_state,
        prompt=prompt,
        execution_profile="full_lifecycle",
        validation_severity="standard",
        workflow_profile=None,
        workflow_stage=None,
        session_id=1,
        task_id=1,
        task_execution_id=None,
        session_instance_id=None,
        logger=logging.getLogger("test.phase12u"),
        emit_live=MagicMock(),
        db=MagicMock(),
        restore_workspace_snapshot_if_needed=None,
    )
    return ctx


def _make_retry_state(*, repair_used: bool = True) -> _PlanningRetryState:
    state = _PlanningRetryState()
    state.repair_prompt_used = repair_used
    return state


def _null_repair(*args, **kwargs) -> dict:
    """Stand-in repair callable that returns empty output (no-op)."""
    return {"output": "[]", "error": None}


# ---------------------------------------------------------------------------
# Bootstrap Contract verdict helpers
# ---------------------------------------------------------------------------


def _bootstrap_failing_verdict(project_dir: Path) -> Any:
    """Return a plan_verdict where Bootstrap Contract explicitly fails."""
    return ValidatorService.validate_plan(
        _plan_without_verification(),
        output_text="[]",
        task_prompt="Bootstrap CLI",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        is_first_ordered_task=True,
    )


def _bootstrap_passing_verdict(project_dir: Path) -> Any:
    """Return a plan_verdict where Bootstrap Contract passes."""
    return ValidatorService.validate_plan(
        _source_code_plan(),
        output_text="[]",
        task_prompt="Bootstrap CLI",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        is_first_ordered_task=True,
    )


# ---------------------------------------------------------------------------
# Tests: Bootstrap Contract appears in ValidatorService verdict
# ---------------------------------------------------------------------------


def test_bootstrap_contract_failing_verdict_has_passed_false(tmp_path):
    """Confirm the test helper produces a verdict with passed=False."""
    verdict = _bootstrap_failing_verdict(tmp_path)
    contract = (verdict.details or {}).get("task1_bootstrap_contract")
    assert isinstance(contract, dict), "task1_bootstrap_contract missing from details"
    assert contract.get("passed") is False


def test_bootstrap_contract_passing_verdict_has_passed_true(tmp_path):
    """Confirm a valid source-code plan passes Bootstrap Contract."""
    verdict = _bootstrap_passing_verdict(tmp_path)
    contract = (verdict.details or {}).get("task1_bootstrap_contract")
    assert isinstance(contract, dict), "task1_bootstrap_contract missing from details"
    assert contract.get("passed") is True


# ---------------------------------------------------------------------------
# Tests: _reject_repair_candidate_by_bootstrap_contract emits diagnostic
# ---------------------------------------------------------------------------


def test_rejection_diagnostic_emitted_with_correct_fields(tmp_path):
    """repair_candidate_rejected_by_bootstrap_contract event must contain
    event, bootstrap_task_type, and failed_requirements."""
    ctx = _make_ctx(plan=_plan_without_verification(), project_dir=tmp_path)
    retry_state = _make_retry_state()
    bootstrap_verdict = _bootstrap_failing_verdict(tmp_path)
    arbitration = {"outcome": "improved_or_preserved", "regression_labels": []}

    _reject_repair_candidate_by_bootstrap_contract(
        ctx=ctx,
        retry_state=retry_state,
        arbitration=arbitration,
        bootstrap_verdict=bootstrap_verdict,
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    emitted_metadata = [
        call.kwargs.get("metadata") or {} for call in ctx.emit_live.call_args_list
    ]
    rejection_events = [
        m
        for m in emitted_metadata
        if isinstance(m, dict)
        and m.get("event") == "repair_candidate_rejected_by_bootstrap_contract"
    ]
    assert (
        rejection_events
    ), "repair_candidate_rejected_by_bootstrap_contract event was not emitted"
    event = rejection_events[0]
    assert "bootstrap_task_type" in event
    assert "failed_requirements" in event
    assert isinstance(event["failed_requirements"], list)


def test_rejection_does_not_emit_classified_candidate_progress(tmp_path):
    """When Bootstrap Contract fails, 'classified candidate progress' must NOT
    be emitted — the candidate is not accepted progress."""
    ctx = _make_ctx(plan=_plan_without_verification(), project_dir=tmp_path)
    retry_state = _make_retry_state()
    bootstrap_verdict = _bootstrap_failing_verdict(tmp_path)
    arbitration = {"outcome": "improved_or_preserved", "regression_labels": []}

    _reject_repair_candidate_by_bootstrap_contract(
        ctx=ctx,
        retry_state=retry_state,
        arbitration=arbitration,
        bootstrap_verdict=bootstrap_verdict,
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    all_messages = [
        call.args[1] if len(call.args) > 1 else ""
        for call in ctx.emit_live.call_args_list
    ]
    assert not any(
        "classified candidate progress" in str(m) for m in all_messages
    ), "'classified candidate progress' must not be emitted for bootstrap-invalid candidates"


# ---------------------------------------------------------------------------
# Tests: arbitrate_planning_repair_candidate Bootstrap Contract pre-check
# ---------------------------------------------------------------------------


def test_arbitration_accepts_bootstrap_valid_repaired_plan(tmp_path, monkeypatch):
    """A repaired plan that satisfies Bootstrap Contract is accepted (action=none)
    and 'classified candidate progress' IS emitted."""
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )

    plan = _source_code_plan()
    ctx = _make_ctx(plan=plan, project_dir=tmp_path, plan_position=1)
    retry_state = _make_retry_state()
    previous_plan: list = []

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=previous_plan,
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    assert (
        result.get("action") == "none"
    ), f"Expected action=none for bootstrap-valid plan, got {result}"
    all_messages = [
        call.args[1] if len(call.args) > 1 else ""
        for call in ctx.emit_live.call_args_list
    ]
    assert any(
        "classified candidate progress" in str(m) for m in all_messages
    ), "'classified candidate progress' must be emitted for bootstrap-valid plans"


def test_arbitration_rejects_bootstrap_invalid_repaired_plan(tmp_path, monkeypatch):
    """A repaired plan that fails Bootstrap Contract must not be accepted.
    The action must not be 'none' (accept) and the diagnostic must be emitted."""
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )

    plan = _plan_without_verification()
    ctx = _make_ctx(plan=plan, project_dir=tmp_path, plan_position=1)
    retry_state = _make_retry_state()
    previous_plan: list = []

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=previous_plan,
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    assert (
        result.get("action") != "none"
    ), "Bootstrap-invalid repaired candidate must not be classified as accepted progress"
    emitted_metadata = [
        call.kwargs.get("metadata") or {} for call in ctx.emit_live.call_args_list
    ]
    rejection_events = [
        m
        for m in emitted_metadata
        if isinstance(m, dict)
        and m.get("event") == "repair_candidate_rejected_by_bootstrap_contract"
    ]
    assert (
        rejection_events
    ), "repair_candidate_rejected_by_bootstrap_contract diagnostic was not emitted"


def test_arbitration_skips_bootstrap_check_for_later_tasks(tmp_path, monkeypatch):
    """Bootstrap Contract pre-check only applies to first ordered tasks.
    Later tasks (plan_position != 1) must still get action=none."""
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )

    # A plan that would fail Bootstrap Contract if checked
    plan = _plan_without_verification()
    ctx = _make_ctx(plan=plan, project_dir=tmp_path, plan_position=2)
    retry_state = _make_retry_state()
    previous_plan: list = []

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=previous_plan,
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    assert result.get("action") == "none", (
        "Bootstrap Contract pre-check must not apply to later tasks; "
        f"got action={result.get('action')}"
    )


def test_arbitration_bootstrap_contract_check_is_bootstrap_contract_only(
    tmp_path, monkeypatch
):
    """The Bootstrap Contract pre-check in arbitration must not weaken existing
    Python source-code Bootstrap Task protections.

    A plan that fails Bootstrap Contract with a source-materialization violation
    must still be rejected by arbitration."""
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control"
        ".append_orchestration_event",
        lambda *args, **kwargs: {},
    )

    # A plan with no ops and no verification — fails Bootstrap Contract
    empty_plan = [_step(ops=[], expected_files=[], verification="", commands=[])]
    ctx = _make_ctx(plan=empty_plan, project_dir=tmp_path, plan_position=1)
    retry_state = _make_retry_state()

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=[],
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=_null_repair,
    )

    # Bootstrap Contract pre-check must catch this
    assert result.get("action") != "none", (
        "Empty plan must not be classified as accepted progress; "
        "Bootstrap Contract pre-check must reject it"
    )
