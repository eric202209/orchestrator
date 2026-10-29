from types import SimpleNamespace
from unittest.mock import Mock

from app.services.orchestration.phases import (
    planning_repair_arbitration_control as repair_control,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState


def _context():
    return SimpleNamespace(
        orchestration_state=object(),
        emit_live=Mock(),
        execution_profile="full_lifecycle",
    )


def _handle(*, monkeypatch, should_repair, verification_only_task=False):
    monkeypatch.setattr(
        repair_control,
        "_truncated_multistep_collapse_diagnostics",
        lambda **kwargs: {"repair_stage": kwargs["repair_stage"]},
    )
    monkeypatch.setattr(
        repair_control, "_should_repair_truncated_single_step_plan", should_repair
    )
    monkeypatch.setattr(
        repair_control,
        "_emit_planning_diagnostics_contract_violation",
        Mock(),
    )
    monkeypatch.setattr(
        repair_control,
        "_build_repair_rejection_reasons",
        lambda reasons, diagnostics: [*reasons, diagnostics["repair_stage"]],
    )
    retry_state = _PlanningRetryState()
    repair_result = {"status": "repair"}
    minimal_result = {"status": "minimal"}
    repair = Mock(return_value=repair_result)
    retry = Mock(return_value=minimal_result)
    result = repair_control.handle_initial_truncated_multistep_plan(
        ctx=_context(),
        retry_state=retry_state,
        output_text="truncated response",
        extracted_plan=[{"step_number": 1}],
        prompt_profile="local_qwen_json_array",
        verification_only_task=verification_only_task,
        planning_timeout_seconds=30,
        planning_knowledge_ctx="knowledge",
        looks_like_truncated_multistep_plan=lambda _text, _plan: True,
        repair_planning_output=repair,
        retry_with_minimal_prompt=retry,
    )
    return retry_state, repair, retry, result, repair_result, minimal_result


def test_initial_truncated_plan_uses_repair_branch(monkeypatch):
    monkeypatch.setattr(repair_control, "emit_phase_event", Mock())
    state, repair, retry, result, repair_result, _ = _handle(
        monkeypatch=monkeypatch,
        should_repair=lambda **kwargs: True,
    )

    assert result == (True, repair_result)
    assert repair.call_args.kwargs["reason"] == "truncated_multistep_plan_detected"
    assert repair.call_args.kwargs["rejection_reasons"][-1] == "before_first_repair"
    assert retry.call_count == 0
    assert state.repair_prompt_used is True
    assert state.minimal_prompt_used is False
    assert state.consecutive_failures == 1


def test_initial_truncated_plan_uses_minimal_retry_when_repair_is_not_selected(
    monkeypatch,
):
    state, repair, retry, result, _, minimal_result = _handle(
        monkeypatch=monkeypatch,
        should_repair=lambda **kwargs: False,
        verification_only_task=True,
    )

    assert result == (True, minimal_result)
    assert repair.call_count == 0
    assert retry.call_args.kwargs["reason"] == "truncated_multistep_plan_detected"
    assert retry.call_args.kwargs["knowledge_context"] == "knowledge"
    assert state.minimal_prompt_used is True
    assert state.repair_prompt_used is False
    assert state.consecutive_failures == 1


def test_truncated_plan_after_minimal_retry_uses_repair(monkeypatch):
    diagnostics = {"stage": "before_first_repair"}
    monkeypatch.setattr(
        repair_control,
        "_truncated_multistep_collapse_diagnostics",
        lambda **kwargs: diagnostics,
    )
    monkeypatch.setattr(
        repair_control,
        "_emit_planning_diagnostics_contract_violation",
        Mock(),
    )
    monkeypatch.setattr(
        repair_control,
        "_build_repair_rejection_reasons",
        lambda _reasons, value: [value["stage"]],
    )
    state = _PlanningRetryState()
    state.minimal_prompt_used = True
    repair_result = {"status": "repaired"}
    repair = Mock(return_value=repair_result)

    result = repair_control.handle_truncated_plan_after_minimal_retry(
        ctx=_context(),
        retry_state=state,
        output_text="truncated response",
        extracted_plan=[{"step_number": 1}],
        prompt_profile="local_qwen_json_array",
        planning_timeout_seconds=30,
        looks_like_truncated_multistep_plan=lambda _text, _plan: True,
        repair_planning_output=repair,
    )

    assert result == (True, repair_result)
    assert repair.call_args.kwargs["reason"] == "truncated_multistep_plan_after_minimal"
    assert repair.call_args.kwargs["rejection_reasons"] == ["before_first_repair"]
    assert state.repair_prompt_used is True
    assert state.minimal_prompt_used is True
    assert state.consecutive_failures == 1
