"""Phase 17A: Tests for FailureClassifier."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.recovery.failure_classifier import FailureClassifier
from app.services.orchestration.recovery.failure_event import FailureEvent


def _state(status: str = "executing"):
    """Minimal orchestration_state stub."""
    s = SimpleNamespace()
    s.status = SimpleNamespace(value=status)
    return s


def _done_state():
    return _state("done")


def _exc(msg: str = "something failed") -> Exception:
    return RuntimeError(msg)


def _timeout_exc(msg: str = "time limit exceeded") -> Exception:
    return RuntimeError(msg)


# ── wrapper_timeout_noise ─────────────────────────────────────────────────────


def test_wrapper_timeout_noise_when_status_done():
    result = FailureClassifier.classify(
        _timeout_exc(),
        _done_state(),
        session_id=1,
        task_id=2,
    )
    assert result.failure_class == "wrapper_timeout_noise"
    assert result.source == "execution"


def test_wrapper_timeout_noise_requires_done_status():
    # Timeout but not DONE → not wrapper noise
    result = FailureClassifier.classify(
        _timeout_exc(),
        _state("executing"),
        session_id=1,
        task_id=2,
    )
    assert result.failure_class != "wrapper_timeout_noise"


def test_wrapper_timeout_noise_requires_timeout_exc():
    # DONE status but not a timeout → not wrapper noise
    result = FailureClassifier.classify(
        _exc("generic failure"),
        _done_state(),
    )
    assert result.failure_class != "wrapper_timeout_noise"


def test_wrapper_timeout_noise_with_timed_out_wording():
    result = FailureClassifier.classify(
        _timeout_exc("timed out waiting"),
        _done_state(),
    )
    assert result.failure_class == "wrapper_timeout_noise"


# ── planning_lock_contention ─────────────────────────────────────────────────


def test_planning_lock_contention_from_runtime_diagnostics():
    exc = RuntimeError("planning lock failed")
    exc.runtime_diagnostics = {"timeout_boundary": "planning_lock_wait"}
    result = FailureClassifier.classify(exc, _state())
    assert result.failure_class == "planning_lock_contention"


def test_planning_lock_contention_from_exc_string():
    exc = RuntimeError("OpenClaw planning lock wait timed out")
    result = FailureClassifier.classify(exc, _state())
    assert result.failure_class == "planning_lock_contention"


# ── debug_parse_error ─────────────────────────────────────────────────────────


def test_debug_parse_error_from_exc_string():
    exc = RuntimeError("JSON parse error in response")
    result = FailureClassifier.classify(exc, _state())
    assert result.failure_class == "debug_parse_error"


# ── unknown_failure ───────────────────────────────────────────────────────────


def test_unknown_failure_fallback():
    result = FailureClassifier.classify(
        _exc("something completely unrecognised"), _state()
    )
    assert result.failure_class == "unknown_failure"
    assert result.source == "unknown"


def test_unknown_failure_with_none_state():
    result = FailureClassifier.classify(_exc("boom"), None)
    assert result.failure_class == "unknown_failure"


# ── metadata propagation ──────────────────────────────────────────────────────


def test_session_and_task_id_propagated():
    result = FailureClassifier.classify(
        _exc("err"),
        _state(),
        session_id=7,
        task_id=99,
    )
    assert result.session_id == 7
    assert result.task_id == 99


def test_exception_type_captured():
    exc = ValueError("bad value")
    result = FailureClassifier.classify(exc, _state())
    assert result.exception_type == "ValueError"


def test_orchestration_status_captured():
    result = FailureClassifier.classify(_exc("err"), _state("revising_plan"))
    assert result.orchestration_status == "revising_plan"


def test_result_is_failure_event():
    result = FailureClassifier.classify(_exc("err"), _state())
    assert isinstance(result, FailureEvent)
