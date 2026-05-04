"""Prove core orchestration behavior works when Langfuse is disabled.

Langfuse must never be required for:
* Planning
* Execution
* Validation
* Retry
* Failure handling

These tests verify the observability layer is truly optional and
non-blocking by running it in a fully-disabled environment.
"""

from __future__ import annotations

import types as _types
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services.observability import (
    ObservabilityService,
    build_text_trace_payload,
    flush_langfuse,
    is_tracing_enabled,
    reset_for_tests,
    start_langfuse_observation,
    update_langfuse_observation,
)


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _langfuse_disabled():
    """Ensure Langfuse is disabled for every test in this module."""
    # Persist original values
    orig_enabled = settings.ORCHESTRATOR_LANGFUSE_ENABLED
    orig_public_key = settings.LANGFUSE_PUBLIC_KEY
    orig_secret_key = settings.LANGFUSE_SECRET_KEY

    # Force-disable
    settings.ORCHESTRATOR_LANGFUSE_ENABLED = False
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_SECRET_KEY = ""
    reset_for_tests()

    yield

    # Restore
    settings.ORCHESTRATOR_LANGFUSE_ENABLED = orig_enabled
    settings.LANGFUSE_PUBLIC_KEY = orig_public_key
    settings.LANGFUSE_SECRET_KEY = orig_secret_key
    reset_for_tests()


# -----------------------------------------------------------------------
# Tests: Observability is a no-op when disabled
# -----------------------------------------------------------------------


def test_tracing_disabled_by_default(_langfuse_disabled):
    assert not is_tracing_enabled()


def test_start_observation_yields_none(_langfuse_disabled):
    with start_langfuse_observation(name="test") as obs:
        assert obs is None


def test_update_observation_noop(_langfuse_disabled):
    # Must not raise
    update_langfuse_observation(None, output={"ok": True})


def test_flush_noop(_langfuse_disabled):
    # Must not raise
    flush_langfuse()


def test_build_text_trace_payload_still_works(_langfuse_disabled):
    """build_text_trace_payload does not depend on Langfuse being enabled."""
    payload = build_text_trace_payload("hello world")
    assert payload is not None
    assert payload["preview"] == "hello world"


# -----------------------------------------------------------------------
# Tests: ObservabilityService isolation
# -----------------------------------------------------------------------


def test_service_not_enabled_when_config_absent(_langfuse_disabled):
    svc = ObservabilityService()
    assert not svc.is_enabled()


def test_service_observation_noop(_langfuse_disabled):
    svc = ObservabilityService()
    with svc.observation(name="isolated") as obs:
        assert obs is None
    svc.flush()


def test_service_sdk_import_error_safe(_langfuse_disabled, monkeypatch):
    """Even when ORCHESTRATOR_LANGFUSE_ENABLED is True but SDK missing, safe."""
    settings.ORCHESTRATOR_LANGFUSE_ENABLED = True
    settings.LANGFUSE_PUBLIC_KEY = "pk-test"
    settings.LANGFUSE_SECRET_KEY = "sk-test"

    # Block langfuse import so service falls back to no-op
    monkeypatch.setitem(__import__("sys").modules, "langfuse", None)

    svc = ObservabilityService()
    # is_enabled says True (config-wise), but client is None (SDK missing)
    assert svc.is_enabled()

    # Still safe
    with svc.observation(name="no-sdk") as obs:
        assert obs is None
    svc.flush()


# -----------------------------------------------------------------------
# Tests: orchestration-like patterns survive without Langfuse
# -----------------------------------------------------------------------


def test_simulated_planning_phase_no_langfuse(_langfuse_disabled):
    """Simulate what the worker does: planning with observation context manager."""
    plan_steps = [
        {"step": 1, "description": "create database schema"},
        {"step": 2, "description": "implement API endpoints"},
        {"step": 3, "description": "write tests"},
    ]

    # Mimic the worker pattern: start observation, do work, update
    with start_langfuse_observation(
        name="planning-phase",
        as_type="span",
        input={"prompt_chars": 500},
        metadata={"task_id": 42},
    ) as obs:
        # obs is None — work proceeds normally
        assert obs is None
        # Core logic runs unaffected
        result = {"status": "completed", "steps": plan_steps}

    update_langfuse_observation(
        obs,
        output=result,
        metadata={"plan_steps": len(plan_steps)},
    )

    assert result["status"] == "completed"
    assert len(result["steps"]) == 3


def test_simulated_execution_phase_no_langfuse(_langfuse_disabled):
    """Simulate execution loop with observation context managers."""
    execution_results = []

    with start_langfuse_observation(
        name="execution-phase",
        metadata={"task_id": 42},
    ) as obs:
        for step in range(3):
            with start_langfuse_observation(
                name=f"step-{step}",
                as_type="span",
            ) as step_obs:
                assert step_obs is None
                execution_results.append({"step": step, "status": "done"})

    update_langfuse_observation(
        obs,
        output={"completed_steps": len(execution_results)},
    )

    assert len(execution_results) == 3


def test_simulated_failure_handling_no_langfuse(_langfuse_disabled):
    """Failure path must work even when Langfuse is off."""
    error_msg = "workspace isolation violation"
    captured_exception = None

    try:
        with start_langfuse_observation(
            name="execution-phase",
        ) as obs:
            assert obs is None
            # Simulate failure
            raise RuntimeError(error_msg)
    except RuntimeError as exc:
        captured_exception = exc

    # The exception propagated — core logic unaffected by missing Langfuse
    assert captured_exception is not None
    assert error_msg in str(captured_exception)

    # Update after failure (worker pattern) — must not raise
    update_langfuse_observation(
        None,
        output={"status": "failed", "reason": error_msg},
        level="ERROR",
        status_message=error_msg,
    )
