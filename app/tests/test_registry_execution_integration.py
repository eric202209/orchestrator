"""Phase 17C-1R: RecoveryStrategyRegistry.execute_recovery() delegation tests.

Verifies that the registry becomes the single orchestration entry point for
active execution recovery at the execution boundary:
  - Delegates to ExecutionRecoveryService.attempt_recovery() exactly once.
  - Preserves evidence, orchestration_state, scope, step_index and all
    callables unchanged (no reconstruction inside the registry).
  - Emits a RECOVERY_DECISION_ROUTED audit event before delegating.
  - Returns exactly the dict produced by ExecutionRecoveryService, unmodified.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.prompt_templates import OrchestrationState


def _make_state(**kwargs) -> OrchestrationState:
    defaults = dict(session_id="s17c1r", task_description="registry delegation")
    defaults.update(kwargs)
    return OrchestrationState(**defaults)


def _make_evidence(**kwargs) -> ExecutionRecoveryEvidence:
    defaults = dict(
        task_title="My task",
        task_description="implement feature X",
        failed_command="pytest tests/test_foo.py -x",
        exit_code=1,
        stdout_excerpt="collected 1 item",
        stderr_excerpt="FAILED tests/test_foo.py::test_bar",
        traceback_excerpt="ImportError: cannot import name 'Foo'",
        changed_files=["src/foo.py"],
        failure_class="import_error",
    )
    defaults.update(kwargs)
    return ExecutionRecoveryEvidence(**defaults)


def _events(tmp_path, session_id, task_id, event_type):
    return read_orchestration_events(
        tmp_path, session_id=session_id, task_id=task_id, event_type_filter=event_type
    )


def test_execute_recovery_delegates_exactly_once(tmp_path):
    """execute_recovery calls ExecutionRecoveryService.attempt_recovery exactly once."""
    evidence = _make_evidence()
    orchestration_state = _make_state()
    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=31,
        task_id=31,
        evidence=evidence,
        orchestration_state=orchestration_state,
        scope="step",
        step_index=2,
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery"
    ) as mock_attempt:
        mock_attempt.return_value = {"status": "skipped", "reason": "budget_exhausted"}

        result = RecoveryStrategyRegistry.execute_recovery(
            context=context,
        )

    mock_attempt.assert_called_once()
    assert result == {"status": "skipped", "reason": "budget_exhausted"}


def test_execute_recovery_preserves_evidence_and_callables_unchanged(tmp_path):
    """The registry passes through the exact objects it received — no reconstruction."""
    evidence = _make_evidence()
    orchestration_state = _make_state()

    def llm_callable(_prompt: str) -> str:
        return ""

    def command_runner(_cmd: str):
        return 0, "", ""

    def validator_callable(_path: str):
        return True, ""

    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=32,
        task_id=32,
        evidence=evidence,
        orchestration_state=orchestration_state,
        scope="completion",
        step_index=None,
        parent_event_id="evt-abc",
        llm_callable=llm_callable,
        command_runner=command_runner,
        validator_callable=validator_callable,
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery"
    ) as mock_attempt:
        mock_attempt.return_value = {"status": "success"}

        RecoveryStrategyRegistry.execute_recovery(
            context=context,
        )

    _, kwargs = mock_attempt.call_args
    assert kwargs["project_dir"] == tmp_path
    assert kwargs["session_id"] == 32
    assert kwargs["task_id"] == 32
    assert kwargs["evidence"] is evidence
    assert kwargs["orchestration_state"] is orchestration_state
    assert kwargs["scope"] == "completion"
    assert kwargs["step_index"] is None
    assert kwargs["parent_event_id"] == "evt-abc"
    assert kwargs["llm_callable"] is llm_callable
    assert kwargs["command_runner"] is command_runner
    assert kwargs["validator_callable"] is validator_callable


def test_execute_recovery_emits_routing_audit_event(tmp_path):
    """A RECOVERY_DECISION_ROUTED event is emitted before delegating."""
    evidence = _make_evidence(failure_class="pytest_failure")
    orchestration_state = _make_state()
    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=33,
        task_id=33,
        evidence=evidence,
        orchestration_state=orchestration_state,
        scope="step",
        step_index=5,
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery"
    ) as mock_attempt:
        mock_attempt.return_value = {
            "status": "failed",
            "reason": "rerun_still_failing",
        }

        RecoveryStrategyRegistry.execute_recovery(
            context=context,
        )

    routed_events = _events(tmp_path, 33, 33, EventType.RECOVERY_DECISION_ROUTED)
    assert len(routed_events) == 1
    details = routed_events[0]["details"]
    assert details["failure_class"] == "pytest_failure"
    assert details["strategy"] == "execution_recovery"
    assert details["scope"] == "step"
    assert details["step_index"] == 5


def test_execute_recovery_no_duplicate_or_additional_recovery_attempts(tmp_path):
    """Two independent calls each delegate exactly once — no double-attempts per call."""
    evidence = _make_evidence()
    orchestration_state = _make_state()
    context_1 = RecoveryContext(
        project_dir=tmp_path,
        session_id=34,
        task_id=34,
        evidence=evidence,
        orchestration_state=orchestration_state,
        scope="step",
        step_index=1,
    )
    context_2 = RecoveryContext(
        project_dir=tmp_path,
        session_id=34,
        task_id=34,
        evidence=evidence,
        orchestration_state=orchestration_state,
        scope="step",
        step_index=2,
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery"
    ) as mock_attempt:
        mock_attempt.return_value = {"status": "success"}

        RecoveryStrategyRegistry.execute_recovery(
            context=context_1,
        )
        assert mock_attempt.call_count == 1

        RecoveryStrategyRegistry.execute_recovery(
            context=context_2,
        )
        assert mock_attempt.call_count == 2
