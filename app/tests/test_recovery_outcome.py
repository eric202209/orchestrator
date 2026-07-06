"""Phase 17C-3: RecoveryOutcome contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome
from app.services.orchestration.prompt_templates import OrchestrationState


def _context(tmp_path) -> RecoveryContext:
    return RecoveryContext(
        project_dir=tmp_path,
        session_id=1,
        task_id=2,
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
            session_id="outcome",
            task_description="outcome test",
        ),
        scope="step",
    )


def test_recovery_outcome_is_immutable_and_preserves_context_identity(tmp_path):
    context = _context(tmp_path)
    result = {"status": "success", "patch_path": "src/foo.py"}

    outcome = RecoveryOutcome(
        succeeded=True,
        resumed_execution=True,
        strategy_name="execution_recovery",
        duration_ms=12,
        failure_class="import_error",
        recovery_context=context,
        audit_event_ids=("evt-started", "evt-completed", "evt-resumed"),
        strategy_result=result,
    )

    assert outcome.recovery_context is context
    assert outcome.strategy_result is result
    assert outcome.get("status") == "success"
    assert outcome["patch_path"] == "src/foo.py"
    assert outcome == result

    with pytest.raises(FrozenInstanceError):
        outcome.succeeded = False


def test_recovery_outcome_defaults_to_empty_strategy_result(tmp_path):
    outcome = RecoveryOutcome(
        succeeded=False,
        resumed_execution=False,
        strategy_name="execution_recovery",
        duration_ms=0,
        failure_class="import_error",
        recovery_context=_context(tmp_path),
    )

    assert outcome.audit_event_ids == ()
    assert outcome.get("missing", "fallback") == "fallback"
    assert list(outcome.items()) == []
