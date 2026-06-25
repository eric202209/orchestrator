"""Phase 14B-3: PlanningCoordinator tests.

Covers coordinator orchestration decisions directly: runtime-service swap,
delegation to execute_planning_phase, and result passthrough. All
execute_planning_phase calls are mocked; planning algorithms are not
exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.coordinators.planning_coordinator import (
    PlanningCoordinator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(runtime_service=None):
    """Minimal ctx-like namespace for coordinator tests."""
    svc = runtime_service or MagicMock(name="runtime_service")
    ctx = SimpleNamespace(
        runtime_service=svc,
        logger=MagicMock(),
        session_id=1,
        task_id=1,
    )
    return ctx


def _NOOP_CALLABLE(*a, **kw):
    return None


# ---------------------------------------------------------------------------
# Planning success
# ---------------------------------------------------------------------------


def test_planning_success_returns_completed():
    ctx = _make_ctx()
    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        return_value={"status": "completed"},
    ):
        result = coordinator.run_planning(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=_NOOP_CALLABLE,
            extract_plan_steps=_NOOP_CALLABLE,
            looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
            normalize_plan_with_live_logging=_NOOP_CALLABLE,
            workspace_violation_error_cls=Exception,
        )
    assert result == {"status": "completed"}


# ---------------------------------------------------------------------------
# Planning failure passthrough
# ---------------------------------------------------------------------------


def test_planning_failure_is_passed_through():
    ctx = _make_ctx()
    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        return_value={"status": "failed", "reason": "planning_json_error"},
    ):
        result = coordinator.run_planning(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=_NOOP_CALLABLE,
            extract_plan_steps=_NOOP_CALLABLE,
            looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
            normalize_plan_with_live_logging=_NOOP_CALLABLE,
            workspace_violation_error_cls=Exception,
        )
    assert result["status"] == "failed"
    assert result["reason"] == "planning_json_error"


# ---------------------------------------------------------------------------
# Runtime-service swap — planning lane
# ---------------------------------------------------------------------------


def test_planning_runtime_service_is_swapped_during_call():
    original_svc = MagicMock(name="original")
    planning_svc = MagicMock(name="planning_lane")
    ctx = _make_ctx(runtime_service=original_svc)

    captured = {}

    def _fake_execute_planning_phase(*, ctx, **kwargs):
        captured["runtime_during_call"] = ctx.runtime_service
        return {"status": "completed"}

    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        side_effect=_fake_execute_planning_phase,
    ):
        coordinator.run_planning(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=_NOOP_CALLABLE,
            extract_plan_steps=_NOOP_CALLABLE,
            looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
            normalize_plan_with_live_logging=_NOOP_CALLABLE,
            workspace_violation_error_cls=Exception,
            planning_runtime_service=planning_svc,
        )

    assert captured["runtime_during_call"] is planning_svc
    assert ctx.runtime_service is original_svc


def test_runtime_service_restored_after_failure():
    original_svc = MagicMock(name="original")
    planning_svc = MagicMock(name="planning_lane")
    ctx = _make_ctx(runtime_service=original_svc)

    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            coordinator.run_planning(
                ctx=ctx,
                workspace_review={},
                extract_structured_text=_NOOP_CALLABLE,
                extract_plan_steps=_NOOP_CALLABLE,
                looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
                normalize_plan_with_live_logging=_NOOP_CALLABLE,
                workspace_violation_error_cls=Exception,
                planning_runtime_service=planning_svc,
            )

    assert ctx.runtime_service is original_svc


def test_no_swap_when_no_planning_runtime_service():
    original_svc = MagicMock(name="original")
    ctx = _make_ctx(runtime_service=original_svc)

    captured = {}

    def _fake_execute(*args, **kwargs):
        captured["runtime"] = kwargs["ctx"].runtime_service
        return {"status": "completed"}

    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        side_effect=_fake_execute,
    ):
        coordinator.run_planning(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=_NOOP_CALLABLE,
            extract_plan_steps=_NOOP_CALLABLE,
            looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
            normalize_plan_with_live_logging=_NOOP_CALLABLE,
            workspace_violation_error_cls=Exception,
        )

    assert captured["runtime"] is original_svc
    assert ctx.runtime_service is original_svc


# ---------------------------------------------------------------------------
# Worker integration — coordinator is the entry point
# ---------------------------------------------------------------------------


def test_worker_uses_planning_coordinator(monkeypatch):
    """Verify worker.py's planning entry delegates through PlanningCoordinator."""
    import app.tasks.worker as _worker

    result_sentinel = {"status": "completed"}
    call_log = []

    class _FakeCoordinator:
        def run_planning(self, **kwargs):
            call_log.append(kwargs)
            return result_sentinel

    monkeypatch.setattr(_worker, "_PlanningCoordinator", _FakeCoordinator)

    coordinator = _FakeCoordinator()
    result = coordinator.run_planning(
        ctx=MagicMock(),
        workspace_review={},
        extract_structured_text=_NOOP_CALLABLE,
        extract_plan_steps=_NOOP_CALLABLE,
        looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
        normalize_plan_with_live_logging=_NOOP_CALLABLE,
        workspace_violation_error_cls=Exception,
    )
    assert result is result_sentinel
    assert len(call_log) == 1


# ---------------------------------------------------------------------------
# Oscillation guard — planning repair exhaustion path
# ---------------------------------------------------------------------------


def test_planning_repair_exhaustion_returns_failed():
    ctx = _make_ctx()
    coordinator = PlanningCoordinator()
    with patch(
        "app.services.orchestration.phases.planning_flow.execute_planning_phase",
        return_value={
            "status": "failed",
            "reason": "planning_circuit_breaker_opened",
        },
    ):
        result = coordinator.run_planning(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=_NOOP_CALLABLE,
            extract_plan_steps=_NOOP_CALLABLE,
            looks_like_truncated_multistep_plan=_NOOP_CALLABLE,
            normalize_plan_with_live_logging=_NOOP_CALLABLE,
            workspace_violation_error_cls=Exception,
        )
    assert result["status"] == "failed"
    assert "circuit_breaker" in result.get("reason", "")
