"""Phase 10K-b — Task Summary Grounding tests.

Verifies that:
1. build_workspace_evidence_block returns a block containing functions/classes.
2. Evidence extraction correctly lists Python top-level symbols.
3. When task_description requests a symbol absent from the file, the prompt
   rules instruct the LLM to flag it as missing.
4. The TASK_SUMMARY prompt rules forbid claiming symbols not in evidence.
5. Deterministic fallback does not claim a missing symbol was added.
6. Existing TASK_SUMMARY API contract tests still pass (smoke).
7. working_memory implementation_strategy stores the grounded summary.
8. P5d T3 fixture: repeat_string/filter_list/merge_dicts present,
   add_category absent → evidence reflects reality; prompt rules cover the gap.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.phases.completion_summary import (
    _deterministic_task_summary,
    _extract_python_symbols,
    _generate_task_summary_with_fallback,
    build_workspace_evidence_block,
)
from app.services.orchestration.prompt_templates import PromptTemplates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.orchestration_state = MagicMock(execution_results=[], plan=[])
    ctx.emit_live = MagicMock()
    return ctx


def _build_prompt(**overrides) -> str:
    defaults = dict(
        task_description="Add the add_category function to looptools",
        plan_summary='[{"step": 1}]',
        execution_results_summary="step=1 verdict=success",
        changed_files=["looptools/__init__.py"],
        num_debug_attempts=0,
        final_status="success",
        execution_profile="full_lifecycle",
        workspace_evidence="",
    )
    defaults.update(overrides)
    return PromptTemplates.build_task_summary(**defaults)


# ---------------------------------------------------------------------------
# 1. build_workspace_evidence_block returns a block with functions/classes
# ---------------------------------------------------------------------------


class TestEvidenceBlockBasics:
    def test_returns_string(self, tmp_path):
        p = _write_py(tmp_path, "lib.py", "def foo(): pass\n")
        block = build_workspace_evidence_block(["lib.py"], tmp_path)
        assert isinstance(block, str)
        assert len(block) > 0

    def test_empty_changed_files_returns_placeholder(self, tmp_path):
        block = build_workspace_evidence_block([], tmp_path)
        assert "no changed files" in block.lower()

    def test_includes_file_path(self, tmp_path):
        p = _write_py(tmp_path, "mylib.py", "def myfunc(): pass\n")
        block = build_workspace_evidence_block(["mylib.py"], tmp_path)
        assert "mylib.py" in block

    def test_missing_file_noted(self, tmp_path):
        block = build_workspace_evidence_block(["nonexistent.py"], tmp_path)
        assert "nonexistent.py" in block
        assert "not found" in block

    def test_non_python_file_listed_without_symbols(self, tmp_path):
        (tmp_path / "README.md").write_text("hello", encoding="utf-8")
        block = build_workspace_evidence_block(["README.md"], tmp_path)
        assert "README.md" in block
        assert "functions:" not in block


# ---------------------------------------------------------------------------
# 2. Evidence extraction lists correct Python functions and classes
# ---------------------------------------------------------------------------


class TestSymbolExtraction:
    def test_extracts_top_level_functions(self, tmp_path):
        _write_py(
            tmp_path,
            "lib.py",
            """
            def alpha(): pass
            def beta(x): return x
            """,
        )
        functions, classes = _extract_python_symbols(tmp_path / "lib.py")
        assert "alpha" in functions
        assert "beta" in functions

    def test_extracts_top_level_classes(self, tmp_path):
        _write_py(
            tmp_path,
            "lib.py",
            """
            class Foo: pass
            class Bar(Foo): pass
            """,
        )
        _, classes = _extract_python_symbols(tmp_path / "lib.py")
        assert "Foo" in classes
        assert "Bar" in classes

    def test_nested_functions_not_included(self, tmp_path):
        _write_py(
            tmp_path,
            "lib.py",
            """
            def outer():
                def inner(): pass
                return inner
            """,
        )
        functions, _ = _extract_python_symbols(tmp_path / "lib.py")
        assert "outer" in functions
        assert "inner" not in functions

    def test_invalid_python_returns_empty(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def )(: pass", encoding="utf-8")
        functions, classes = _extract_python_symbols(p)
        assert functions == []
        assert classes == []

    def test_evidence_block_shows_functions_line(self, tmp_path):
        _write_py(
            tmp_path,
            "utils.py",
            """
            def normalize_label(x): return x
            def add_label(x, y): return x
            """,
        )
        block = build_workspace_evidence_block(["utils.py"], tmp_path)
        assert "functions:" in block
        assert "normalize_label" in block
        assert "add_label" in block

    def test_evidence_block_shows_na_when_no_functions(self, tmp_path):
        _write_py(tmp_path, "consts.py", "X = 1\n")
        block = build_workspace_evidence_block(["consts.py"], tmp_path)
        assert "functions: N/A" in block

    def test_evidence_block_shows_na_when_no_classes(self, tmp_path):
        _write_py(tmp_path, "funcs.py", "def go(): pass\n")
        block = build_workspace_evidence_block(["funcs.py"], tmp_path)
        assert "classes: N/A" in block


# ---------------------------------------------------------------------------
# 3. Prompt includes evidence block; missing symbol not present in evidence
# ---------------------------------------------------------------------------


class TestPromptIncludesEvidence:
    def test_prompt_contains_workspace_evidence_heading(self):
        prompt = _build_prompt(
            workspace_evidence="- lib.py\n  functions: foo\n  classes: N/A"
        )
        assert "Actual Workspace Evidence" in prompt

    def test_prompt_shows_provided_evidence_content(self):
        evidence = "- looptools/__init__.py\n  functions: repeat_string, filter_list\n  classes: N/A"
        prompt = _build_prompt(workspace_evidence=evidence)
        assert "repeat_string" in prompt
        assert "filter_list" in prompt

    def test_add_category_absent_from_evidence_when_not_in_file(self, tmp_path):
        _write_py(
            tmp_path,
            "looptools.py",
            """
            def repeat_string(s, n): return s * n
            def filter_list(lst, fn): return [x for x in lst if fn(x)]
            def merge_dicts(a, b): return {**a, **b}
            """,
        )
        evidence = build_workspace_evidence_block(["looptools.py"], tmp_path)
        assert "add_category" not in evidence
        assert "repeat_string" in evidence

    def test_prompt_with_missing_symbol_does_not_assert_it_present(self, tmp_path):
        _write_py(
            tmp_path,
            "looptools.py",
            "def repeat_string(s, n): return s * n\n",
        )
        evidence = build_workspace_evidence_block(["looptools.py"], tmp_path)
        prompt = _build_prompt(
            task_description="Add add_category to looptools",
            workspace_evidence=evidence,
        )
        assert "add_category" not in evidence
        assert "add_category" in prompt  # appears in task_description only


# ---------------------------------------------------------------------------
# 4. TASK_SUMMARY prompt rules forbid claiming symbols not in evidence
# ---------------------------------------------------------------------------


class TestPromptRulesForbidUngroundedClaims:
    def test_rule_only_claim_if_in_evidence(self):
        prompt = _build_prompt()
        assert "Actual Workspace Evidence" in prompt
        assert "Only claim" in prompt

    def test_rule_instructs_missing_symbol_flag(self):
        prompt = _build_prompt()
        assert "Requested symbol missing from final workspace" in prompt

    def test_rule_forbids_inferring_from_description(self):
        prompt = _build_prompt()
        assert "Do not infer completion from task description alone" in prompt

    def test_rule_prefers_actual_files_over_prose(self):
        prompt = _build_prompt()
        assert (
            "Prefer actual workspace files and symbols over execution prose" in prompt
        )


# ---------------------------------------------------------------------------
# 5. Deterministic fallback does not claim missing symbol added
# ---------------------------------------------------------------------------


class TestDeterministicFallbackDoesNotOverclaim:
    def test_deterministic_does_not_mention_add_category(self):
        result = MagicMock()
        result.files_changed = ["looptools/__init__.py"]
        result.status = "completed"
        state = MagicMock()
        state.execution_results = [result]
        state.plan = [{"step_number": 1}]
        summary = _deterministic_task_summary(state)
        assert "add_category" not in summary

    def test_deterministic_lists_changed_files_only(self):
        result = MagicMock()
        result.files_changed = ["looptools/__init__.py"]
        result.status = "completed"
        state = MagicMock()
        state.execution_results = [result]
        state.plan = [{"step_number": 1}]
        summary = _deterministic_task_summary(state)
        assert "looptools/__init__.py" in summary
        assert "Completed steps: 1/1" in summary

    def test_flag_off_returns_deterministic_without_symbol_claim(self):
        ctx = _make_ctx()
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"
        }
        with patch.dict("os.environ", env, clear=True):
            result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert "add_category" not in result["output"]


# ---------------------------------------------------------------------------
# 6. Existing TASK_SUMMARY API contract smoke test
# ---------------------------------------------------------------------------


class TestExistingApiContractUnchanged:
    def test_api_contract_section_still_present(self):
        prompt = _build_prompt()
        assert "API Contract:" in prompt

    def test_sentinel_rule_still_present(self):
        prompt = _build_prompt()
        assert "EMPTY" in prompt or "FORMAT" in prompt or "OVERFLOW" in prompt

    def test_exact_shape_rule_still_present(self):
        prompt = _build_prompt()
        assert "exact shape" in prompt or "exact dict" in prompt

    def test_build_task_summary_backward_compat_no_evidence(self):
        prompt = PromptTemplates.build_task_summary(
            task_description="test",
            plan_summary="[]",
            execution_results_summary="none",
            changed_files=[],
            num_debug_attempts=0,
            final_status="success",
        )
        assert isinstance(prompt, str)
        assert "Actual Workspace Evidence" in prompt
        assert "no changed files" in prompt


# ---------------------------------------------------------------------------
# 7. working_memory implementation_strategy stores grounded summary
# ---------------------------------------------------------------------------


class TestWorkingMemoryStoresGroundedSummary:
    def test_llm_output_stored_in_wm_output_key(self):
        from unittest.mock import AsyncMock

        ctx = _make_ctx()
        llm_text = "Task completed. Functions added: repeat_string, filter_list."
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=AsyncMock(return_value=llm_text),
            ):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="p"
                )
        assert result["output"] == llm_text
        assert result.get("fallback") is not True

    def test_pn_summary_is_deterministic_even_with_llm_output(self):
        from unittest.mock import AsyncMock

        ctx = _make_ctx()
        llm_text = "LLM produced content."
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=AsyncMock(return_value=llm_text),
            ):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="p"
                )
        assert "Task completed" in result["pn_summary"]


# ---------------------------------------------------------------------------
# 8. P5d T3 fixture
# ---------------------------------------------------------------------------


class TestP5dT3Fixture:
    """P5d T3: repeat_string/filter_list/merge_dicts present, add_category missing."""

    def _write_looptools(self, tmp_path: Path) -> str:
        source = textwrap.dedent(
            """
            def normalize_label(x): return str(x).strip()
            def repeat_string(s, n): return s * n
            def filter_list(lst, fn): return [x for x in lst if fn(x)]
            def merge_dicts(a, b): return {**a, **b}
            """
        )
        p = tmp_path / "looptools" / "__init__.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source, encoding="utf-8")
        return str(p.parent / "__init__.py").replace(str(tmp_path) + "/", "")

    def test_evidence_contains_present_symbols(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        block = build_workspace_evidence_block([rel], tmp_path)
        assert "repeat_string" in block
        assert "filter_list" in block
        assert "merge_dicts" in block

    def test_evidence_does_not_contain_add_category(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        block = build_workspace_evidence_block([rel], tmp_path)
        assert "add_category" not in block

    def test_prompt_rules_cover_missing_symbol_case(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        block = build_workspace_evidence_block([rel], tmp_path)
        prompt = PromptTemplates.build_task_summary(
            task_description="Add add_category, repeat_string, filter_list, merge_dicts to looptools",
            plan_summary='[{"step": 1}]',
            execution_results_summary="step=1 verdict=success",
            changed_files=[rel],
            num_debug_attempts=0,
            final_status="success",
            workspace_evidence=block,
        )
        assert "repeat_string" in prompt
        assert "add_category" not in block
        assert "Requested symbol missing from final workspace" in prompt
        assert "Only claim" in prompt

    def test_deterministic_summary_does_not_claim_add_category(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        result = MagicMock()
        result.files_changed = [rel]
        result.status = "completed"
        state = MagicMock()
        state.execution_results = [result]
        state.plan = [{"step_number": 1}]
        summary = _deterministic_task_summary(state)
        assert "add_category" not in summary
