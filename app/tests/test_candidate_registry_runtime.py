"""Phase 17G: registry runtime Candidate Recovery tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.planning.candidate_planning_outcome import CandidatePlanningOutcome
from app.services.planning.candidate_recovery import CandidateRuntimeResult
from app.services.planning.plan_candidate import PlanCandidate


def _context(tmp_path, *, executor, runtime_profile="standard", scope="planning"):
    evidence = ExecutionRecoveryEvidence(
        task_title="Task",
        task_description="Prompt",
        failed_command="planning_validation",
        exit_code=None,
        stdout_excerpt="",
        stderr_excerpt="bad plan",
        traceback_excerpt="",
        validator_rejection_reason="bad plan",
        failure_class="planning_validation_failed",
    )
    return RecoveryContext(
        project_dir=tmp_path,
        session_id=10,
        task_id=11,
        scope=scope,
        evidence=evidence,
        orchestration_state=SimpleNamespace(),
        runtime_profile=runtime_profile,
        recovery_metadata={
            "planning_failure_signature": "sig-1",
            "candidate_executor": executor,
        },
    )


def _selected_result():
    candidate = PlanCandidate(
        candidate_id="candidate-sibling-1",
        operator="sibling_generation",
        validator_status="accepted",
    )
    return CandidateRuntimeResult(
        outcome=CandidatePlanningOutcome(
            selected_candidate=candidate,
            candidate_count=2,
            operator_sequence=("original", "sibling_generation"),
            outcome="selected",
        ),
        selected_plan=[{"step_number": 1}],
    )


def _exhausted_result():
    return CandidateRuntimeResult(
        outcome=CandidatePlanningOutcome(
            selected_candidate=None,
            candidate_count=2,
            operator_sequence=("original", "sibling_generation"),
            outcome="exhausted",
        )
    )


def test_registry_candidate_runtime_success_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, executor=_selected_result)
    )

    assert outcome.succeeded is True
    assert outcome.resumed_execution is True
    assert outcome["status"] == "success"
    event_types = [
        event["event_type"]
        for event in read_orchestration_events(tmp_path, session_id=10, task_id=11)
    ]
    assert EventType.RECOVERY_DECISION_ROUTED in event_types
    assert EventType.RECOVERY_STARTED in event_types
    assert EventType.RECOVERY_COMPLETED in event_types
    assert EventType.RECOVERY_RESUMED in event_types


def test_registry_candidate_runtime_exhaustion_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, executor=_exhausted_result)
    )

    assert outcome.succeeded is False
    assert outcome["status"] == "failed"
    assert outcome["reason"] == "candidate_exhausted"
    event_types = [
        event["event_type"]
        for event in read_orchestration_events(tmp_path, session_id=10, task_id=11)
    ]
    assert EventType.RECOVERY_FAILED in event_types
    assert EventType.RECOVERY_RESUMED not in event_types


def test_registry_candidate_runtime_skips_non_standard_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, executor=_selected_result, runtime_profile="medium")
    )

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "unsupported_runtime_profile"


def test_registry_candidate_runtime_dedups_signature(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    calls = {"count": 0}

    def executor():
        calls["count"] += 1
        return _selected_result()

    state = SimpleNamespace()
    first = _context(tmp_path, executor=executor)
    second = _context(tmp_path, executor=executor)
    first = first.__class__(**{**first.__dict__, "orchestration_state": state})
    second = second.__class__(**{**second.__dict__, "orchestration_state": state})

    RecoveryStrategyRegistry.execute_candidate_planning(context=first)
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(context=second)

    assert calls["count"] == 1
    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "signature_already_attempted"
