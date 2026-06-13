"""Tests for Stage 3 LLM summary routing.

Verifies that when ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1:
- progress_notes receives the deterministic summary (pn_summary key)
- working_memory implementation_strategy receives the LLM summary (output key)
- fallback cases leave both destinations with deterministic text
- render and injection flags remain unaffected

Does not test finalize_successful_task end-to-end (too many DB deps).
Tests the routing boundary: _generate_task_summary_with_fallback pn_summary key
plus the write functions that each destination calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.orchestration.phases.completion_summary import (
    _generate_task_summary_with_fallback,
)
from app.services.orchestration.phases.completion_flow import _write_progress_notes
from app.services.orchestration.working_memory import write_working_memory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(plan=None, execution_results=None) -> MagicMock:
    ctx = MagicMock()
    ctx.orchestration_state = MagicMock(
        execution_results=execution_results or [],
        plan=plan or [],
    )
    ctx.emit_live = MagicMock()
    return ctx


def _make_state(project_dir: str) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.execution_results = []
    state.changed_files = []
    state.plan = []
    state.validation_history = []
    return state


def _make_task(task_id: int = 1, title: str = "test task") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.title = title
    t.plan_position = 1
    return t


def _patch_planning_lane(output: str):
    return patch(
        "app.services.orchestration.phases.completion_summary._call_planning_lane",
        new=AsyncMock(return_value=output),
    )


# ---------------------------------------------------------------------------
# 1. Flag OFF: pn_summary equals output (deterministic preserved)
# ---------------------------------------------------------------------------


class TestFlagOff:
    def test_pn_summary_equals_output_when_flag_off(self):
        ctx = _make_ctx()
        env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"}
        with patch.dict("os.environ", env, clear=True):
            result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert result.get("pn_summary") == result["output"]
        assert "Task completed with verified execution evidence" in result["pn_summary"]

    def test_source_is_deterministic_when_flag_off(self):
        ctx = _make_ctx()
        env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"}
        with patch.dict("os.environ", env, clear=True):
            result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result.get("source") == "deterministic"


# ---------------------------------------------------------------------------
# 2. Flag ON + LLM success: pn_summary is deterministic, output is LLM text
# ---------------------------------------------------------------------------


class TestFlagOnLLMSuccess:
    def test_output_is_llm_text(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane("Detailed LLM: parse_number() -> dict {ok, value, error}"):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["output"] == "Detailed LLM: parse_number() -> dict {ok, value, error}"

    def test_pn_summary_is_deterministic(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane("Detailed LLM: parse_number() -> dict {ok, value, error}"):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert "Task completed with verified execution evidence" in result["pn_summary"]

    def test_pn_summary_differs_from_output(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane("Rich LLM narrative"):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["pn_summary"] != result["output"]

    def test_no_fallback_flag(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane("LLM content"):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert not result.get("fallback")


# ---------------------------------------------------------------------------
# 3. Flag ON + WM persistence OFF: no WM file created
# ---------------------------------------------------------------------------


class TestFlagOnPersistenceOff:
    def test_no_wm_file_when_persistence_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False
        )
        state = _make_state(str(tmp_path))
        write_working_memory(
            orchestration_state=state,
            task=_make_task(),
            summary="LLM summary text here",
            logger=MagicMock(),
        )
        assert not (tmp_path / ".agent" / "working_memory.json").exists()

    def test_pn_summary_still_deterministic_when_persistence_off(self):
        """Routing is independent of persistence flag — pn_summary is always deterministic when LLM fires."""
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane("LLM content"):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert "Task completed with verified execution evidence" in result["pn_summary"]
        assert result["pn_summary"] != result["output"]


# ---------------------------------------------------------------------------
# 4. Flag ON + WM persistence ON: WM gets LLM summary, progress_notes gets deterministic
# ---------------------------------------------------------------------------


class TestFlagOnPersistenceOn:
    def test_wm_stores_llm_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        state = _make_state(str(tmp_path))
        llm_text = "parse_number(text: str) -> dict: ok/value/error; INVALID_NUMBER"
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=42),
            summary=llm_text,
            logger=MagicMock(),
        )
        data = json.loads((tmp_path / ".agent" / "working_memory.json").read_text())
        assert data["implementation_strategy"][0]["summary"] == llm_text

    def test_wm_summary_richer_than_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        state = _make_state(str(tmp_path))
        llm_text = "parse_number(text: str) -> dict: ok/value/error; INVALID_NUMBER sentinel"
        write_working_memory(
            orchestration_state=state,
            task=_make_task(),
            summary=llm_text,
            logger=MagicMock(),
        )
        data = json.loads((tmp_path / ".agent" / "working_memory.json").read_text())
        wm_stored = data["implementation_strategy"][0]["summary"]
        assert "INVALID_NUMBER" in wm_stored
        assert "ok" in wm_stored.lower()
        # The deterministic summary would never contain these API-contract details
        assert "Task completed with verified execution evidence" not in wm_stored

    def test_progress_notes_receives_deterministic_not_llm(self, tmp_path):
        """_write_progress_notes writes whatever summary arg it receives.
        Caller (finalize_successful_task) passes pn_summary (deterministic)."""
        state = _make_state(str(tmp_path))
        deterministic = "Task completed with verified execution evidence. Completed steps: 0/0."
        _write_progress_notes(
            orchestration_state=state,
            task=_make_task(),
            prompt="test",
            summary=deterministic,
            logger=MagicMock(),
        )
        notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
        assert "Task completed with verified execution evidence" in notes
        assert "INVALID_NUMBER" not in notes

    def test_progress_notes_does_not_receive_llm_content(self, tmp_path):
        """If caller passes deterministic to _write_progress_notes, LLM content won't appear."""
        state = _make_state(str(tmp_path))
        pn_text = "Task completed with verified execution evidence. Completed steps: 3/3."
        _write_progress_notes(
            orchestration_state=state,
            task=_make_task(),
            prompt="test",
            summary=pn_text,
            logger=MagicMock(),
        )
        notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
        # LLM-specific API contract details should NOT appear here
        assert "INVALID_NUMBER" not in notes
        assert "parse_number" not in notes


# ---------------------------------------------------------------------------
# 5. Fallback case: both destinations get deterministic
# ---------------------------------------------------------------------------


class TestFallbackCase:
    def test_exception_fallback_pn_summary_equals_output(self):
        ctx = _make_ctx()

        async def _fail(prompt):
            raise RuntimeError("network error")

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=_fail,
            ):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert result.get("pn_summary") == result["output"]
        assert "Task completed with verified execution evidence" in result["output"]

    def test_empty_output_fallback_pn_summary_equals_output(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with _patch_planning_lane(""):
                result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert result.get("pn_summary") == result["output"]
        assert "Task completed with verified execution evidence" in result["output"]

    def test_fallback_wm_gets_deterministic(self, tmp_path, monkeypatch):
        """When LLM fails, WM receives the fallback deterministic summary."""
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        state = _make_state(str(tmp_path))
        fallback_text = "Task completed with verified execution evidence. Completed steps: 2/3."
        write_working_memory(
            orchestration_state=state,
            task=_make_task(),
            summary=fallback_text,
            logger=MagicMock(),
        )
        data = json.loads((tmp_path / ".agent" / "working_memory.json").read_text())
        assert data["implementation_strategy"][0]["summary"] == fallback_text


# ---------------------------------------------------------------------------
# 6. Render and injection flags remain OFF (unchanged by routing change)
# ---------------------------------------------------------------------------


class TestRenderInjectionUnchanged:
    def test_render_flag_defaults_false(self):
        from app.config import settings

        assert settings.WORKING_MEMORY_RENDER_ENABLED is False

    def test_injection_flag_defaults_false(self):
        from app.config import settings

        assert settings.WORKING_MEMORY_INJECTION_ENABLED is False

    def test_write_working_memory_no_op_when_render_only_flag_set(
        self, tmp_path, monkeypatch
    ):
        """Render flag has no effect on persistence — WM file is not written."""
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False
        )
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True
        )
        state = _make_state(str(tmp_path))
        write_working_memory(
            orchestration_state=state,
            task=_make_task(),
            summary="some summary",
            logger=MagicMock(),
        )
        # Persistence flag is False, so no file even though render flag is True
        assert not (tmp_path / ".agent" / "working_memory.json").exists()
