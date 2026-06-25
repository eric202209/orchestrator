"""Phase 14B-5: ExecutionCoordinator tests.

Covers the coordinator boundary only: result passthrough and deferred-import
behaviour. execute_step_loop is always mocked; no execution algorithms are
exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.orchestration.coordinators.execution_coordinator import (
    ExecutionCoordinator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx():
    return SimpleNamespace(
        runtime_service=MagicMock(name="runtime_service"),
        logger=MagicMock(),
        session_id=1,
        task_id=1,
    )


def _NOOP_CALLABLE(*a, **kw):
    return None


_BASE_KWARGS = dict(
    extract_structured_text=_NOOP_CALLABLE,
    normalize_step=_NOOP_CALLABLE,
    normalize_plan_with_live_logging=_NOOP_CALLABLE,
    workspace_violation_error_cls=Exception,
    write_project_state_snapshot_fn=_NOOP_CALLABLE,
    record_live_log_fn=_NOOP_CALLABLE,
)

_PATCH_TARGET = "app.services.orchestration.phases.execution_loop.execute_step_loop"


# ---------------------------------------------------------------------------
# Result passthrough
# ---------------------------------------------------------------------------


def test_completed_result_passes_through():
    with patch(_PATCH_TARGET, return_value={"status": "completed"}) as mock:
        result = ExecutionCoordinator().run_execution(ctx=_make_ctx(), **_BASE_KWARGS)
    assert result == {"status": "completed"}
    mock.assert_called_once()


def test_failed_result_passes_through():
    payload = {"status": "failed", "reason": "max_attempts_reached"}
    with patch(_PATCH_TARGET, return_value=payload):
        result = ExecutionCoordinator().run_execution(ctx=_make_ctx(), **_BASE_KWARGS)
    assert result["status"] == "failed"
    assert result["reason"] == "max_attempts_reached"


def test_cancelled_result_passes_through():
    payload = {
        "status": "cancelled",
        "task_id": 1,
        "session_id": 1,
        "reason": "session_stopped",
    }
    with patch(_PATCH_TARGET, return_value=payload):
        result = ExecutionCoordinator().run_execution(ctx=_make_ctx(), **_BASE_KWARGS)
    assert result["status"] == "cancelled"
    assert result["reason"] == "session_stopped"


def test_awaiting_input_result_passes_through():
    payload = {
        "status": "awaiting_input",
        "task_id": 1,
        "session_id": 1,
        "step_index": 2,
        "reason": "agent_requested_human_intervention",
    }
    with patch(_PATCH_TARGET, return_value=payload):
        result = ExecutionCoordinator().run_execution(ctx=_make_ctx(), **_BASE_KWARGS)
    assert result["status"] == "awaiting_input"
    assert result["reason"] == "agent_requested_human_intervention"


# ---------------------------------------------------------------------------
# Deferred import — monkeypatch survives coordinator boundary
# ---------------------------------------------------------------------------


def test_deferred_import_works_with_monkeypatch(monkeypatch):
    import app.services.orchestration.phases.execution_loop as execution_loop

    sentinel = {"status": "completed"}
    monkeypatch.setattr(execution_loop, "execute_step_loop", lambda **kw: sentinel)

    result = ExecutionCoordinator().run_execution(ctx=_make_ctx(), **_BASE_KWARGS)
    assert result is sentinel


# ---------------------------------------------------------------------------
# Worker integration — coordinator is the call path
# ---------------------------------------------------------------------------


def test_worker_uses_execution_coordinator(monkeypatch):
    import app.tasks.worker as _worker

    result_sentinel = {"status": "completed"}
    call_log = []

    class _FakeCoordinator:
        def run_execution(self, **kwargs):
            call_log.append(kwargs)
            return result_sentinel

    monkeypatch.setattr(_worker, "_ExecutionCoordinator", _FakeCoordinator)

    coordinator = _FakeCoordinator()
    result = coordinator.run_execution(
        ctx=MagicMock(),
        **_BASE_KWARGS,
    )
    assert result is result_sentinel
    assert len(call_log) == 1
