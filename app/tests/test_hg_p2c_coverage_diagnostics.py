"""HG-P2c: smoke tests for HG enforcement coverage diagnostics.

Two scenarios:
  A. Qwen planning path — Python receives structured plan_steps → P2b can run.
  B. local_openclaw inline path — plan_steps is empty → P2b logs no_structured_plan.

These are dry-path tests: no DB, no real LLM, no real worker.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.phases.planning_guidance_enforcement import (
    emit_hg_p2b_worker_coverage,
    run_guidance_plan_enforcement,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(plan_steps: list, project_id: int = 1) -> Any:
    project = MagicMock()
    project.id = project_id
    project.user_id = 99

    orchestration_state = MagicMock()
    orchestration_state.plan = plan_steps

    ctx = MagicMock()
    ctx.project = project
    ctx.session_id = 1
    ctx.task_id = 10
    ctx.db = MagicMock()
    ctx.orchestration_state = orchestration_state
    ctx.logger = logging.getLogger("test")
    ctx.emit_live = MagicMock()
    return ctx


def _make_retry_state(repair_prompt_used: bool = False) -> Any:
    rs = MagicMock()
    rs.repair_prompt_used = repair_prompt_used
    rs.hg_repair_prompt_used = False  # HG has its own slot (hardening phase 1)
    return rs


# ---------------------------------------------------------------------------
# Path A — structured plan_steps present (Qwen planning path)
# ---------------------------------------------------------------------------


def test_path_a_p2b_runs_on_nonempty_plan_and_finds_violation():
    """Structured plan → P2b validator is invoked → violation found → repair triggered."""
    violating_plan = [
        {"ops": [{"op": "write_file", "content": "def add(x, y=[]): pass"}]}
    ]
    ctx = _make_ctx(violating_plan)
    retry_state = _make_retry_state(repair_prompt_used=False)

    repair_fn = MagicMock(return_value={"status": "repaired"})
    emit_diag = MagicMock()

    fake_violations = ["mutable_default: plan writes '= []' which violates guidance"]

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=fake_violations,
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text="...",
            planning_timeout_seconds=60,
            prompt_profile="default",
            repair_fn=repair_fn,
            emit_diagnostics_fn=emit_diag,
        )

    assert result == {
        "status": "repaired"
    }, "repair result must be returned when violation found"
    repair_fn.assert_called_once()
    assert retry_state.hg_repair_prompt_used is True


def test_path_a_p2b_runs_on_nonempty_plan_and_finds_compliant():
    """Structured plan → P2b validator is invoked → no violations → returns None."""
    clean_plan = [
        {"ops": [{"op": "write_file", "content": "def add(x, y=None): pass"}]}
    ]
    ctx = _make_ctx(clean_plan)
    retry_state = _make_retry_state(repair_prompt_used=False)

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=[],
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text="...",
            planning_timeout_seconds=60,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    assert result is None


# ---------------------------------------------------------------------------
# Path B — empty plan_steps (local_openclaw inline path)
# ---------------------------------------------------------------------------


def test_path_b_p2b_skips_and_logs_no_structured_plan(caplog):
    """Empty plan → P2b logs no_structured_plan and returns None without calling validator."""
    ctx = _make_ctx(plan_steps=[])
    retry_state = _make_retry_state(repair_prompt_used=False)

    validator_called = []

    def mock_validator(*args, **kwargs):
        validator_called.append(True)
        return []

    with caplog.at_level(logging.INFO, logger="test"), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        side_effect=mock_validator,
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text="...",
            planning_timeout_seconds=60,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    assert result is None
    assert not validator_called, "validator must NOT be called when plan_steps is empty"
    assert (
        "no_structured_plan" in caplog.text
    ), "expected [HG_P2B_COVERAGE] no_structured_plan log in caplog"


def test_path_b_p2b_skips_when_plan_is_none():
    """plan=None treated same as empty list."""
    ctx = _make_ctx(plan_steps=[])
    ctx.orchestration_state.plan = None
    retry_state = _make_retry_state()

    validator_called = []
    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        side_effect=lambda *a, **kw: validator_called.append(1) or [],
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text="",
            planning_timeout_seconds=60,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )
    assert result is None
    assert not validator_called


# ---------------------------------------------------------------------------
# Worker-level coverage diagnostic function
# ---------------------------------------------------------------------------


def _coverage_args(mock_logger) -> tuple:
    """Return the positional args passed to mock_logger.info (skipping the format string)."""
    args = mock_logger.info.call_args[0]
    # args[0] is the format string; args[1:] are the substitution values
    return args[1:]


def test_worker_coverage_local_openclaw_no_separate_runtime():
    """local_openclaw with no separate planning runtime → eligible=False, backend_bypasses."""
    mock_logger = MagicMock()
    emit_hg_p2b_worker_coverage(
        execution_backend="local_openclaw",
        resolved_planning_backend=None,
        use_configured_planning_runtime=False,
        hg_table_enabled=True,
        logger=mock_logger,
    )
    args = _coverage_args(mock_logger)
    # args: (execution_backend, planning_backend, separate_runtime, eligible, reason)
    assert args[3] is False, "hg_p2b_eligible must be False"
    assert args[4] == "backend_bypasses_python_planning"


def test_worker_coverage_separate_planning_backend():
    """PLANNING_BACKEND configured → eligible=True."""
    mock_logger = MagicMock()
    emit_hg_p2b_worker_coverage(
        execution_backend="local_openclaw",
        resolved_planning_backend="direct_ollama",
        use_configured_planning_runtime=True,
        hg_table_enabled=True,
        logger=mock_logger,
    )
    args = _coverage_args(mock_logger)
    assert args[3] is True, "hg_p2b_eligible must be True"
    assert args[4] == "structured_plan_expected"


def test_worker_coverage_flags_off():
    """HG table disabled → eligible=False, flags_off (direct_ollama so bypass branch doesn't fire)."""
    mock_logger = MagicMock()
    emit_hg_p2b_worker_coverage(
        execution_backend="direct_ollama",
        resolved_planning_backend=None,
        use_configured_planning_runtime=False,
        hg_table_enabled=False,
        logger=mock_logger,
    )
    args = _coverage_args(mock_logger)
    assert args[3] is False
    assert args[4] == "flags_off"


def test_worker_coverage_direct_ollama_eligible():
    """direct_ollama generates structured JSON plan → P2b eligible."""
    mock_logger = MagicMock()
    emit_hg_p2b_worker_coverage(
        execution_backend="direct_ollama",
        resolved_planning_backend=None,
        use_configured_planning_runtime=False,
        hg_table_enabled=True,
        logger=mock_logger,
    )
    args = _coverage_args(mock_logger)
    assert (
        args[3] is True
    ), "direct_ollama should be eligible — it returns structured plans"
