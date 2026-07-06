"""Tests for Phase 5 LLM summary prompt API contract quality.

Verifies that the TASK_SUMMARY prompt template:
1. Contains explicit API Contract section instructions.
2. Instructs preserving exact dict keys.
3. Instructs preserving sentinel literals.
4. Instructs not to generalize field names.
5. Existing fallback/deterministic summary behavior is unchanged.

Does NOT test WM routing, retention, or injection — those are covered by
test_llm_summary_routing.py and test_wm_summary_retention.py.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.prompt_templates import PromptTemplates
from app.services.orchestration.phases.completion_summary import (
    _deterministic_task_summary,
    _generate_task_summary_with_fallback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_prompt(**overrides) -> str:
    defaults = dict(
        task_description="Bootstrap parse_amount parser",
        plan_summary='[{"step": 1}]',
        execution_results_summary="step=1 verdict=success",
        changed_files=["src/calclib/parser.py", "tests/test_parser.py"],
        num_debug_attempts=0,
        final_status="success",
        execution_profile="full_lifecycle",
    )
    defaults.update(overrides)
    return PromptTemplates.build_task_summary(**defaults)


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.orchestration_state = MagicMock(execution_results=[], plan=[])
    ctx.emit_live = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# 1. Prompt contains explicit API Contract section
# ---------------------------------------------------------------------------


class TestPromptContainsApiContractSection:
    def test_api_contract_heading_present(self):
        prompt = _build_prompt()
        assert "API Contract:" in prompt

    def test_function_signature_field_present(self):
        prompt = _build_prompt()
        assert "function:" in prompt

    def test_success_return_field_present(self):
        prompt = _build_prompt()
        assert "success return:" in prompt

    def test_failure_return_field_present(self):
        prompt = _build_prompt()
        assert "failure return:" in prompt

    def test_sentinel_values_field_present(self):
        prompt = _build_prompt()
        assert "sentinel/error values:" in prompt

    def test_exception_behavior_field_present(self):
        prompt = _build_prompt()
        assert "exception behavior:" in prompt

    def test_keys_fields_present(self):
        prompt = _build_prompt()
        assert "keys/fields:" in prompt

    def test_failure_return_before_success_return(self):
        prompt = _build_prompt()
        failure_pos = prompt.index("failure return:")
        success_pos = prompt.index("success return:")
        assert failure_pos < success_pos, (
            f"'failure return:' must appear before 'success return:' in prompt "
            f"(failure at {failure_pos}, success at {success_pos})"
        )


# ---------------------------------------------------------------------------
# 2. Prompt instructs preserving exact dict keys
# ---------------------------------------------------------------------------


class TestPromptInstructsExactDictKeys:
    def test_instructs_literal_key_names(self):
        prompt = _build_prompt()
        assert "key name" in prompt.lower() or "key names" in prompt.lower()

    def test_instructs_exact_appearance_in_code(self):
        prompt = _build_prompt()
        assert "as it appears in code" in prompt or "exactly as it appears" in prompt


# ---------------------------------------------------------------------------
# 3. Prompt instructs preserving sentinel literals
# ---------------------------------------------------------------------------


class TestPromptInstructsSentinelLiterals:
    def test_instructs_literal_sentinel_string(self):
        prompt = _build_prompt()
        assert "literal" in prompt.lower()

    def test_instructs_not_to_omit_sentinels(self):
        prompt = _build_prompt()
        assert "do not omit" in prompt.lower()

    def test_example_sentinel_values_shown(self):
        prompt = _build_prompt()
        # Template must show examples so the LLM knows the format
        assert "EMPTY" in prompt or "FORMAT" in prompt or "OVERFLOW" in prompt


# ---------------------------------------------------------------------------
# 4. Prompt instructs not to generalize field names
# ---------------------------------------------------------------------------


class TestPromptForbidsGeneralization:
    def test_forbids_standardized_result_dictionary_prose(self):
        prompt = _build_prompt()
        assert "standardized result dictionary" in prompt

    def test_instructs_exact_shape_not_prose(self):
        prompt = _build_prompt()
        assert "exact shape" in prompt or "exact dict" in prompt

    def test_no_substitute_with_prose_instruction(self):
        prompt = _build_prompt()
        assert "Never substitute" in prompt or "never substitute" in prompt.lower()


# ---------------------------------------------------------------------------
# 5. Existing fallback/deterministic summary behavior unchanged
# ---------------------------------------------------------------------------


class TestFallbackBehaviorUnchanged:
    def test_flag_off_returns_deterministic_fallback(self):
        ctx = _make_ctx()
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"
        }
        with patch.dict("os.environ", env, clear=True):
            result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert "Task completed" in result["output"]

    def test_deterministic_summary_still_generates_for_zero_steps(self):
        state = MagicMock()
        state.execution_results = []
        state.plan = []
        summary = _deterministic_task_summary(state)
        assert "Completed steps: 0/0" in summary
        assert "Changed files: none recorded" in summary

    def test_deterministic_summary_lists_changed_files(self):
        from app.services.orchestration.prompt_templates import StepResult

        result = MagicMock()
        result.files_changed = ["src/parser.py"]
        result.status = "completed"
        state = MagicMock()
        state.execution_results = [result]
        state.plan = [{"step_number": 1}]
        summary = _deterministic_task_summary(state)
        assert "src/parser.py" in summary

    def test_exception_in_llm_call_uses_deterministic(self):
        ctx = _make_ctx()

        async def _fail(prompt):
            raise RuntimeError("gateway down")

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=_fail,
            ):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="p"
                )
        assert result["fallback"] is True
        assert "Task completed" in result["output"]

    def test_empty_llm_output_uses_deterministic(self):
        from unittest.mock import AsyncMock

        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=AsyncMock(return_value=""),
            ):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="p"
                )
        assert result["fallback"] is True
        assert "Task completed" in result["output"]

    def test_prompt_template_format_call_unchanged(self):
        # Ensure build_task_summary still accepts the same arguments without error
        prompt = _build_prompt(
            task_description="test task",
            plan_summary="[]",
            execution_results_summary="none",
            changed_files=[],
            num_debug_attempts=2,
            final_status="success",
            execution_profile="execute_only",
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0
