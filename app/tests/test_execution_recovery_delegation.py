"""Phase 17C-1R: execution-boundary call sites delegate through the registry.

Verifies that the two production call sites for bounded execution recovery —
completion_coordinator.py (completion scope) and execution_loop.py (step
scope) — route through RecoveryStrategyRegistry.execute_recovery() rather
than calling ExecutionRecoveryService.attempt_recovery() directly, and that
completion resumes exactly as before when recovery succeeds.
"""

from __future__ import annotations

import importlib

from app.services.orchestration.coordinators import completion_coordinator
from app.services.orchestration.phases import execution_loop
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.execution_states import TerminalReason
from app.services.orchestration.types import ValidationVerdict

from app.tests.test_phase14b1_completion_coordinator import (
    CompletionCoordinator,
    _NOOP_FN,
    _make_ctx,
    _patch_coordinator_delegates,
)


def test_completion_coordinator_module_has_no_direct_execution_recovery_service_symbol():
    """17C-1R scope: completion_coordinator no longer imports ExecutionRecoveryService."""
    assert not hasattr(completion_coordinator, "ExecutionRecoveryService")
    assert hasattr(completion_coordinator, "RecoveryStrategyRegistry")
    assert completion_coordinator.RecoveryStrategyRegistry is RecoveryStrategyRegistry


def test_execution_loop_module_has_no_direct_execution_recovery_service_symbol():
    """17C-1R scope: execution_loop no longer imports ExecutionRecoveryService."""
    assert not hasattr(execution_loop, "ExecutionRecoveryService")
    assert hasattr(execution_loop, "RecoveryStrategyRegistry")
    assert execution_loop.RecoveryStrategyRegistry is RecoveryStrategyRegistry


def test_completion_coordinator_calls_registry_not_service_directly(
    tmp_path, monkeypatch
):
    """Completion-scope recovery flows through RecoveryStrategyRegistry.execute_recovery."""
    rejected_verdict = ValidationVerdict(
        stage="task_completion",
        status="rejected",
        profile="implementation",
        reasons=["No files changed"],
        details={},
    )
    ctx = _make_ctx(tmp_path)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=rejected_verdict)
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.persist_debug_feedback_envelope",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.build_debug_feedback_envelope",
        lambda **kwargs: __import__("types").SimpleNamespace(
            failure_class="missing_files",
            eligible_for_debug_repair=False,
            stderr_excerpt="",
            return_code=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_task_attempt_failed",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_session_paused",
        _NOOP_FN,
    )

    calls = []

    def _fake_execute_recovery(**kwargs):
        calls.append(kwargs)
        return {"status": "skipped", "reason": "budget_exhausted"}

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator."
        "RecoveryStrategyRegistry.execute_recovery",
        _fake_execute_recovery,
    )

    result = CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=_NOOP_FN,
        save_orchestration_checkpoint_fn=_NOOP_FN,
    )

    assert len(calls) == 1
    assert list(calls[0]) == ["context"]
    assert isinstance(calls[0]["context"], RecoveryContext)
    assert calls[0]["context"].scope == "completion"
    assert calls[0]["context"].evidence.failure_class == "missing_files"
    assert calls[0]["context"].orchestration_state is ctx.orchestration_state
    assert result["status"] == "failed"
    assert result["reason"] == TerminalReason.COMPLETION_VALIDATION_FAILED


def test_completion_resumes_exactly_as_before_when_registry_recovery_succeeds(
    tmp_path, monkeypatch
):
    """When registry-routed recovery succeeds, completion re-validates and finishes as
    'completed' — identical outcome to the pre-17C-1R direct-call behavior."""
    rejected_then_accepted = iter(
        [
            ValidationVerdict(
                stage="task_completion",
                status="rejected",
                profile="implementation",
                reasons=["Missing symbol"],
                details={},
            ),
            ValidationVerdict(
                stage="task_completion",
                status="accepted",
                profile="implementation",
                reasons=[],
                details={},
            ),
        ]
    )
    ctx = _make_ctx(tmp_path)
    _patch_coordinator_delegates(
        monkeypatch,
        validation_verdict=ValidationVerdict(
            stage="task_completion",
            status="rejected",
            profile="implementation",
            reasons=["Missing symbol"],
            details={},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: next(rejected_then_accepted),
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.persist_debug_feedback_envelope",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.build_debug_feedback_envelope",
        lambda **kwargs: __import__("types").SimpleNamespace(
            failure_class="missing_requested_symbol",
            eligible_for_debug_repair=False,
            stderr_excerpt="",
            return_code=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator."
        "RecoveryStrategyRegistry.execute_recovery",
        lambda **kwargs: {"status": "success", "patch_path": "src/foo.py"},
    )

    result = CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=_NOOP_FN,
        save_orchestration_checkpoint_fn=_NOOP_FN,
    )

    assert result["status"] == "completed"
