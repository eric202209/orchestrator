"""Phase 17C-2: RecoveryContext dataclass contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_policy import PolicyTable
from app.services.orchestration.prompt_templates import OrchestrationState


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


def test_recovery_context_construction_preserves_identity(tmp_path):
    evidence = _make_evidence()
    state = _make_state()

    def llm_callable(_prompt: str) -> str:
        return "ok"

    def validator_callable(_path: str):
        return True, ""

    def command_runner(_command: str):
        return 0, "", ""

    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=101,
        task_id=202,
        evidence=evidence,
        orchestration_state=state,
        scope="step",
        step_index=3,
        parent_event_id="evt-1",
        llm_callable=llm_callable,
        validator_callable=validator_callable,
        command_runner=command_runner,
    )

    assert context.project_dir == tmp_path
    assert context.session_id == 101
    assert context.task_id == 202
    assert context.evidence is evidence
    assert context.orchestration_state is state
    assert context.llm_callable is llm_callable
    assert context.validator_callable is validator_callable
    assert context.command_runner is command_runner
    assert context.scope == "step"
    assert context.step_index == 3
    assert context.parent_event_id == "evt-1"


def test_recovery_context_defaults(tmp_path):
    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=1,
        task_id=2,
        evidence=_make_evidence(),
        orchestration_state=_make_state(),
        scope="completion",
    )

    assert context.step_index is None
    assert context.parent_event_id is None
    assert context.llm_callable is None
    assert context.validator_callable is None
    assert context.command_runner is None
    assert context.policy_version == PolicyTable.VERSION
    assert isinstance(context.runtime_profile, str)
    assert context.reflection_result is None
    assert context.working_memory is None
    assert context.human_guidance is None
    assert context.recovery_metadata is None


def test_recovery_context_is_immutable(tmp_path):
    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=1,
        task_id=2,
        evidence=_make_evidence(),
        orchestration_state=_make_state(),
        scope="step",
    )

    with pytest.raises(FrozenInstanceError):
        context.scope = "completion"
