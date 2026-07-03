"""Phase 17C-3: canonical recovery lifecycle tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.prompt_templates import OrchestrationState


def _make_context(tmp_path, *, session_id: int = 701, task_id: int = 702):
    return RecoveryContext(
        project_dir=tmp_path,
        session_id=session_id,
        task_id=task_id,
        evidence=ExecutionRecoveryEvidence(
            task_title="Task",
            task_description="Do work",
            failed_command="pytest",
            exit_code=1,
            stdout_excerpt="",
            stderr_excerpt="ImportError",
            traceback_excerpt="",
            failure_class="import_error",
        ),
        orchestration_state=OrchestrationState(
            session_id="lifecycle",
            task_description="lifecycle test",
        ),
        scope="step",
        step_index=4,
    )


def _events(tmp_path, session_id=701, task_id=702):
    recovery_types = {
        EventType.RECOVERY_DECISION_ROUTED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_COMPLETED,
        EventType.RECOVERY_RESUMED,
        EventType.RECOVERY_FAILED,
    }
    return [
        event
        for event in read_orchestration_events(
            tmp_path,
            session_id=session_id,
            task_id=task_id,
        )
        if event["event_type"] in recovery_types
    ]


def _event_types(events):
    return [event["event_type"] for event in events]


def test_successful_recovery_lifecycle_ordering_and_outcome(tmp_path):
    context = _make_context(tmp_path)
    strategy_result = {"status": "success", "patch_path": "src/foo.py"}

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        return_value=strategy_result,
    ) as mock_attempt:
        outcome = RecoveryStrategyRegistry.execute_recovery(context=context)

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.succeeded is True
    assert outcome.resumed_execution is True
    assert outcome.recovery_context is context
    assert outcome.strategy_result is strategy_result
    assert outcome.get("status") == "success"
    mock_attempt.assert_called_once()

    types = _event_types(_events(tmp_path))
    assert types == [
        EventType.RECOVERY_DECISION_ROUTED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_COMPLETED,
        EventType.RECOVERY_RESUMED,
    ]
    assert len(outcome.audit_event_ids) == 3


def test_failed_recovery_lifecycle_has_no_resumed_or_completed(tmp_path):
    context = _make_context(tmp_path)
    strategy_result = {"status": "failed", "reason": "rerun_still_failing"}

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        return_value=strategy_result,
    ):
        outcome = RecoveryStrategyRegistry.execute_recovery(context=context)

    assert outcome.succeeded is False
    assert outcome.resumed_execution is False
    assert outcome == strategy_result

    types = _event_types(_events(tmp_path))
    assert types == [
        EventType.RECOVERY_DECISION_ROUTED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_FAILED,
    ]
    assert types.count(EventType.RECOVERY_STARTED) == 1
    assert types.count(EventType.RECOVERY_FAILED) == 1
    assert EventType.RECOVERY_RESUMED not in types
    assert EventType.RECOVERY_COMPLETED not in types


def test_recovery_lifecycle_failed_event_before_strategy_exception_propagates(tmp_path):
    context = _make_context(tmp_path)

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            RecoveryStrategyRegistry.execute_recovery(context=context)

    types = _event_types(_events(tmp_path))
    assert types == [
        EventType.RECOVERY_DECISION_ROUTED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_FAILED,
    ]


def test_lifecycle_events_preserve_context_details(tmp_path):
    context = _make_context(tmp_path, session_id=801, task_id=802)

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        return_value={"status": "success"},
    ):
        RecoveryStrategyRegistry.execute_recovery(context=context)

    lifecycle_events = [
        event
        for event in _events(tmp_path, session_id=801, task_id=802)
        if event["event_type"]
        in {
            EventType.RECOVERY_STARTED,
            EventType.RECOVERY_COMPLETED,
            EventType.RECOVERY_RESUMED,
        }
    ]
    assert len(lifecycle_events) == 3
    for event in lifecycle_events:
        details = event["details"]
        assert details["failure_class"] == "import_error"
        assert details["strategy"] == "execution_recovery"
        assert details["scope"] == "step"
        assert details["step_index"] == 4
        assert details["session_id"] == 801
        assert details["task_id"] == 802
