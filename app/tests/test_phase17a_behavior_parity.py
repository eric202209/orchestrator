"""Phase 17A: Behavior parity regression tests.

Proves that existing runtime behaviour is unchanged after 17A infrastructure
is wired in. The only new behaviour is wrapper_timeout_noise → early return
in FailureCoordinator.handle_failure().
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.recovery.execution_recovery_service import (
    ELIGIBLE_RECOVERY_FAILURE_CLASSES,
    RECOVERY_BUDGET,
)
from app.services.orchestration.recovery.recovery_policy import (
    PolicyTable,
    STRATEGY_EXISTING_RECOVERY,
)


# ── 17A-1: FailureEvent infrastructure exists ────────────────────────────────


def test_failure_event_importable():
    from app.services.orchestration.recovery.failure_event import (
        FailureEvent,
        make_failure_event,
    )

    ev = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message="x"
    )
    assert ev.failure_class == "unknown_failure"


def test_failure_classifier_importable():
    from app.services.orchestration.recovery.failure_classifier import FailureClassifier

    result = FailureClassifier.classify(RuntimeError("boom"), None)
    assert result is not None


def test_recovery_policy_importable():
    from app.services.orchestration.recovery.recovery_policy import PolicyTable

    rule = PolicyTable.lookup("unknown_failure")
    assert rule.strategy == "retry_with_reflection"


def test_recovery_strategy_registry_importable():
    from app.services.orchestration.recovery.recovery_strategy_registry import (
        RecoveryStrategyRegistry,
    )
    from app.services.orchestration.recovery.failure_event import make_failure_event

    ev = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message="x"
    )
    decision = RecoveryStrategyRegistry.route(ev)
    assert decision.strategy == "terminal"


# ── 17A-2: Existing recovery constants unchanged ─────────────────────────────


def test_recovery_budget_unchanged():
    assert RECOVERY_BUDGET == 2


def test_eligible_recovery_failure_classes_unchanged():
    expected = frozenset(
        {
            "pytest_failure",
            "import_error",
            "module_not_found",
            "runtime_assertion_failure",
            "completion_validation_failed",
            "missing_dependency",
            "syntax_error",
            "source_step_validation",
            "missing_requested_symbol",
        }
    )
    assert ELIGIBLE_RECOVERY_FAILURE_CLASSES == expected


# ── 17A-3: Policy mirrors existing eligible classes ──────────────────────────


def test_policy_maps_all_eligible_classes_to_existing_recovery():
    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        rule = PolicyTable.lookup(fc)
        assert (
            rule.strategy == STRATEGY_EXISTING_RECOVERY
        ), f"{fc!r} policy changed unexpectedly to {rule.strategy!r}"


# ── 17A-4: New event types are canonical ─────────────────────────────────────


def test_new_event_types_are_known():
    from app.services.orchestration.events.event_types import (
        EventType,
        is_known_event_type,
    )

    assert is_known_event_type(EventType.RECOVERY_DECISION_ROUTED)
    assert is_known_event_type(EventType.RECOVERY_NOISE_ANNOTATED)


# ── 17A-5: wrapper_timeout_noise → early return in FailureCoordinator ────────


def _make_ctx(status: str = "done"):
    """Minimal ctx stub for FailureCoordinator."""
    state = SimpleNamespace()
    state.status = SimpleNamespace(value=status)
    state.project_dir = None
    state.abort_reason = None

    ctx = MagicMock()
    ctx.db = MagicMock()
    ctx.session = None
    ctx.project = None
    ctx.task = None
    ctx.session_task_link = None
    ctx.session_id = 1
    ctx.task_id = 1
    ctx.prompt = ""
    ctx.orchestration_state = state
    ctx.restore_workspace_snapshot_if_needed = None
    ctx.logger = MagicMock()
    ctx.error_handler = MagicMock()
    ctx.error_handler.should_retry.return_value = False
    return ctx


def test_wrapper_timeout_noise_does_not_propagate():
    """When timeout fires after DONE, handle_failure must return without re-raising."""
    from app.services.orchestration.coordinators.failure_coordinator import (
        FailureCoordinator,
    )

    coordinator = FailureCoordinator()
    ctx = _make_ctx(status="done")
    exc = RuntimeError("time limit exceeded")

    self_task = MagicMock()
    self_task.request.retries = 0
    self_task.max_retries = 3

    # Should return normally (not raise)
    coordinator.handle_failure(
        self_task=self_task,
        ctx=ctx,
        exc=exc,
        get_latest_session_task_link_fn=MagicMock(return_value=None),
    )
    # If we reach here without exception, wrapper_timeout_noise was handled


def test_non_wrapper_timeout_still_propagates():
    """A timeout during EXECUTING should not be swallowed — must propagate."""
    from app.services.orchestration.coordinators.failure_coordinator import (
        FailureCoordinator,
    )

    coordinator = FailureCoordinator()
    ctx = _make_ctx(status="executing")
    exc = RuntimeError("time limit exceeded")

    self_task = MagicMock()
    self_task.request.retries = 0
    self_task.max_retries = 3

    with pytest.raises(Exception):
        coordinator.handle_failure(
            self_task=self_task,
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=MagicMock(return_value=None),
        )


def test_generic_failure_still_propagates():
    """A non-timeout failure should still propagate through the normal path."""
    from app.services.orchestration.coordinators.failure_coordinator import (
        FailureCoordinator,
    )

    coordinator = FailureCoordinator()
    ctx = _make_ctx(status="executing")
    exc = ValueError("something else entirely")

    self_task = MagicMock()
    self_task.request.retries = 0
    self_task.max_retries = 3

    with pytest.raises(Exception):
        coordinator.handle_failure(
            self_task=self_task,
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=MagicMock(return_value=None),
        )
