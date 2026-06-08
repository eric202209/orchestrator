"""Characterization tests: output compliance in incremental execution.

Documents the CURRENT behavior of attempt_incremental_execution for the
four output shapes observed in fresh-process verification (2026-06-08):

Shape 1 — clean Python (no fence):    succeeds   (py_compile passes)
Shape 2 — completion report summary:  verify_failed fallback (garbage written)
Shape 3 — prose prefix + Python:      verify_failed fallback (py_compile fails)
Shape 4 — fenced Python:              succeeds   (search() strips fence)

These tests serve as a regression baseline. If the fix is implemented
(require fenced output or add pre-write syntax check), update shapes 2/3
to assert content_invalid_syntax or content_not_fenced instead.

No live model calls. No DB access. Uses tmp_path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.phases.incremental_flow import (
    attempt_incremental_execution,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


class _FixedRuntime:
    def __init__(self, output: str):
        self.output = output

    async def execute_task(self, prompt: str, timeout_seconds: int = 90):
        return {"output": self.output}


def _make_ctx(project_dir: str, output: str, task_id: int = 1):
    from app.services.prompt_templates import OrchestrationState

    state = OrchestrationState(
        session_id="test-compliance",
        task_description="compliance characterization",
        project_name="test",
        task_id=task_id,
    )
    state._project_dir_override = project_dir
    return SimpleNamespace(
        orchestration_state=state,
        runtime_service=_FixedRuntime(output),
        task_id=task_id,
        session_id=0,
        logger=MagicMock(),
        emit_live=lambda *a, **kw: None,
    )


def _patch_events(monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.incremental_flow.append_orchestration_event",
        lambda **kwargs: {"event_id": "fake"},
    )


# Task description used across all tests.
_TASK_DESC = (
    "Create helpers.py with a function clamp(v, lo, hi) returning "
    "max(lo, min(v, hi)). Verify the file is valid Python."
)


# ── Shape 1: clean Python output (no fence) ──────────────────────────────────


def test_shape1_clean_python_no_fence_succeeds(tmp_path, monkeypatch):
    """Clean Python without a code fence compiles and succeeds."""
    _patch_events(monkeypatch)
    clean_python = (
        "def clamp(v, lo, hi):\n"
        '    """Return v clamped between lo and hi."""\n'
        "    return max(lo, min(v, hi))\n"
    )
    ctx = _make_ctx(str(tmp_path), clean_python)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "completed"


def test_shape1_file_contains_clean_python(tmp_path, monkeypatch):
    """Written file content is the clean Python returned by the model."""
    _patch_events(monkeypatch)
    clean_python = "def clamp(v, lo, hi):\n    return max(lo, min(v, hi))\n"
    ctx = _make_ctx(str(tmp_path), clean_python)
    attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    written = (tmp_path / "helpers.py").read_text()
    assert "def clamp" in written


# ── Shape 2: agent completion report (no fence, not Python) ──────────────────


def test_shape2_completion_report_causes_content_invalid_syntax(tmp_path, monkeypatch):
    """Agent completion summary `` `helpers.py` created... `` fails pre-write compile().

    POST-FIX BEHAVIOR: compile() rejects prose before writing → content_invalid_syntax.
    File is never written. Characterises A+E fix applied 2026-06-08.
    """
    _patch_events(monkeypatch)
    # Exact pattern observed from wrappers.py probe output.
    completion_report = (
        "`helpers.py` created and verified — compiles cleanly and "
        "`clamp(v, lo, hi)` returns the clamped value as expected."
    )
    ctx = _make_ctx(str(tmp_path), completion_report)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "failed"
    assert result["reason"] == "content_invalid_syntax"


def test_shape2_completion_report_does_not_corrupt_plan(tmp_path, monkeypatch):
    """After a completion-report fallback, plan remains empty."""
    _patch_events(monkeypatch)
    completion_report = "`helpers.py` created and verified — clamp works as expected."
    ctx = _make_ctx(str(tmp_path), completion_report)
    attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert ctx.orchestration_state.plan == []


def test_shape2_prose_sentence_causes_content_invalid_syntax(tmp_path, monkeypatch):
    """Prose preamble 'Here is the content for helpers.py:' fails pre-write compile()."""
    _patch_events(monkeypatch)
    prose_prefix = (
        "Here is the content for helpers.py:\n\n"
        "def clamp(v, lo, hi):\n    return max(lo, min(v, hi))\n"
    )
    ctx = _make_ctx(str(tmp_path), prose_prefix)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "failed"
    assert result["reason"] == "content_invalid_syntax"


# ── Shape 3: fenced Python (search() strips, succeeds) ───────────────────────


def test_shape4_fenced_python_succeeds(tmp_path, monkeypatch):
    """search() correctly strips a code fence; py_compile passes."""
    _patch_events(monkeypatch)
    fenced = (
        "```python\n" "def clamp(v, lo, hi):\n" "    return max(lo, min(v, hi))\n" "```"
    )
    ctx = _make_ctx(str(tmp_path), fenced)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "completed"


def test_shape4_fenced_python_with_prose_prefix_succeeds(tmp_path, monkeypatch):
    """search() finds a fence even when preceded by a prose sentence."""
    _patch_events(monkeypatch)
    fenced_with_prose = (
        "Here is the file:\n"
        "```python\n"
        "def clamp(v, lo, hi):\n"
        "    return max(lo, min(v, hi))\n"
        "```"
    )
    ctx = _make_ctx(str(tmp_path), fenced_with_prose)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "completed"


# ── Shape 2 inline-backtick variant ──────────────────────────────────────────


def test_shape2_inline_backtick_report_fails_content_invalid_syntax(
    tmp_path, monkeypatch
):
    """Inline backtick markdown fails pre-write compile() before touching the file."""
    _patch_events(monkeypatch)
    inline_backtick = (
        "`clamp` function written to `helpers.py`. "
        "Returns `max(lo, min(v, hi))` as required."
    )
    ctx = _make_ctx(str(tmp_path), inline_backtick)
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "failed"
    assert result["reason"] == "content_invalid_syntax"


# ── Empty content ─────────────────────────────────────────────────────────────


def test_empty_output_causes_content_empty_fallback(tmp_path, monkeypatch):
    """Empty string output falls back with content_empty before attempting write."""
    _patch_events(monkeypatch)
    ctx = _make_ctx(str(tmp_path), "")
    result = attempt_incremental_execution(ctx=ctx, task_description=_TASK_DESC)
    assert result["status"] == "failed"
    assert result["reason"] == "content_empty"
