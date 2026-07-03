"""Phase 17C-2: registry integration with RecoveryContext."""

from __future__ import annotations

from unittest.mock import patch

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.prompt_templates import OrchestrationState


def _make_evidence() -> ExecutionRecoveryEvidence:
    return ExecutionRecoveryEvidence(
        task_title="Task",
        task_description="Do work",
        failed_command="pytest",
        exit_code=1,
        stdout_excerpt="",
        stderr_excerpt="ImportError",
        traceback_excerpt="",
        failure_class="import_error",
    )


def _make_state() -> OrchestrationState:
    return OrchestrationState(session_id="ctx", task_description="context test")


def test_registry_unpacks_context_without_copying_or_mutating(tmp_path):
    evidence = _make_evidence()
    state = _make_state()
    before = context = RecoveryContext(
        project_dir=tmp_path,
        session_id=501,
        task_id=502,
        evidence=evidence,
        orchestration_state=state,
        scope="completion",
        parent_event_id="evt-parent",
        recovery_metadata={"reserved": True},
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery"
    ) as mock_attempt:
        mock_attempt.return_value = {"status": "success"}

        result = RecoveryStrategyRegistry.execute_recovery(context=context)

    assert result == {"status": "success"}
    _, kwargs = mock_attempt.call_args
    assert kwargs["project_dir"] is tmp_path
    assert kwargs["session_id"] == 501
    assert kwargs["task_id"] == 502
    assert kwargs["evidence"] is evidence
    assert kwargs["orchestration_state"] is state
    assert kwargs["scope"] == "completion"
    assert kwargs["step_index"] is None
    assert kwargs["parent_event_id"] == "evt-parent"
    assert context is before
    assert context.recovery_metadata == {"reserved": True}


def test_registry_requires_single_context_object():
    import inspect

    signature = inspect.signature(RecoveryStrategyRegistry.execute_recovery)

    assert list(signature.parameters) == ["context"]
    assert signature.parameters["context"].annotation == "RecoveryContext"
