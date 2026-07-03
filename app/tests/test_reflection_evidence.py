"""Phase 17D: reflection evidence is optional supplemental recovery context."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_patch import build_recovery_prompt
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.recovery.reflection_evidence import ReflectionEvidence
from app.services.prompt_templates import OrchestrationState


def _make_evidence(failure_class: str = "unknown_failure") -> ExecutionRecoveryEvidence:
    return ExecutionRecoveryEvidence(
        task_title="Task",
        task_description="Do work",
        failed_command="pytest",
        exit_code=1,
        stdout_excerpt="",
        stderr_excerpt="Unexpected failure",
        traceback_excerpt="",
        failure_class=failure_class,
    )


def _make_context(
    tmp_path,
    *,
    failure_class: str = "unknown_failure",
    reflection_result=None,
) -> RecoveryContext:
    return RecoveryContext(
        project_dir=tmp_path,
        session_id=901,
        task_id=902,
        evidence=_make_evidence(failure_class),
        orchestration_state=OrchestrationState(
            session_id="reflection",
            task_description="reflection evidence test",
        ),
        scope="step",
        reflection_result=reflection_result,
    )


def test_reflection_evidence_is_immutable():
    evidence = ReflectionEvidence(
        summary="The failure is probably caused by stale import wiring.",
        suggested_fix="Check the package export.",
        confidence="low",
        source="reflection_retry",
    )

    with pytest.raises(FrozenInstanceError):
        evidence.summary = "changed"


def test_reflection_result_normalizes_to_reflection_evidence():
    reflection_result = SimpleNamespace(
        llm_output="Investigate the task registry import path.",
        strategy="retry_with_reflection",
    )

    evidence = ReflectionEvidence.from_reflection_result(reflection_result)

    assert evidence == ReflectionEvidence(
        summary="Investigate the task registry import path.",
        suggested_fix="",
        confidence=None,
        source="retry_with_reflection",
    )


def test_registry_passes_unknown_failure_reflection_evidence_identity(tmp_path):
    reflection = ReflectionEvidence(summary="Check the unexpected None path.")
    context = _make_context(tmp_path, reflection_result=reflection)

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        return_value={"status": "skipped", "reason": "ineligible_failure_class"},
    ) as mock_attempt:
        RecoveryStrategyRegistry.execute_recovery(context=context)

    _, kwargs = mock_attempt.call_args
    assert kwargs["reflection_evidence"] is reflection


def test_registry_does_not_consume_reflection_for_other_failure_classes(tmp_path):
    reflection = ReflectionEvidence(summary="This should be ignored.")
    context = _make_context(
        tmp_path,
        failure_class="import_error",
        reflection_result=reflection,
    )

    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry."
        "ExecutionRecoveryService.attempt_recovery",
        return_value={"status": "skipped", "reason": "not_attempted"},
    ) as mock_attempt:
        RecoveryStrategyRegistry.execute_recovery(context=context)

    _, kwargs = mock_attempt.call_args
    assert kwargs["reflection_evidence"] is None


def test_recovery_prompt_omits_reflection_section_when_absent():
    prompt = build_recovery_prompt(_make_evidence("import_error"))

    assert "Supplemental Diagnostic Context" not in prompt
    assert "Reflection may be incorrect" not in prompt


def test_recovery_prompt_marks_reflection_as_supplemental():
    reflection = ReflectionEvidence(
        summary="Likely missing defensive handling around payload parsing.",
        suggested_fix="Add a guard before reading nested keys.",
        confidence="medium",
        source="reflection_retry",
    )

    prompt = build_recovery_prompt(
        _make_evidence("unknown_failure"),
        reflection_evidence=reflection,
    )

    assert "Supplemental Diagnostic Context" in prompt
    assert "Reflection may be incorrect. Validator remains authoritative." in prompt
    assert "Likely missing defensive handling around payload parsing." in prompt
    assert "Add a guard before reading nested keys." in prompt
    assert "Confidence: medium" in prompt
