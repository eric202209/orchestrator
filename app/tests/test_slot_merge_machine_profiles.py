"""Phase 17H: Slot Merge machine profile and registry tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.phases.planning_candidate_recovery import (
    slot_merge_recovery_precheck,
)
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.planning.candidate_planning_outcome import CandidatePlanningOutcome
from app.services.planning.candidate_recovery import CandidateRuntimeResult
from app.services.planning.plan_candidate import PlanCandidate


def _verdict(status: str, reasons: tuple[str, ...] = ("bad",)):
    return SimpleNamespace(
        status=status,
        accepted=status in {"accepted", "warning"},
        warning=status == "warning",
        repairable=status == "repair_required",
        rejected=status == "rejected",
        reasons=list(reasons),
        details={},
    )


def _context(tmp_path, *, executor, runtime_profile="medium", operator="slot_merge"):
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
        session_id=40,
        task_id=41,
        scope="planning",
        evidence=evidence,
        orchestration_state=SimpleNamespace(),
        runtime_profile=runtime_profile,
        recovery_metadata={
            "planning_failure_signature": "slot-sig-1",
            "candidate_operator": operator,
            "candidate_executor": executor,
        },
    )


def _selected_result():
    candidate = PlanCandidate(
        candidate_id="candidate-slot-merge-1",
        operator="slot_merge",
        validator_status="accepted",
    )
    return CandidateRuntimeResult(
        outcome=CandidatePlanningOutcome(
            selected_candidate=candidate,
            candidate_count=2,
            operator_sequence=("original", "repair_mutation", "slot_merge"),
            outcome="selected",
        ),
        selected_plan=[{"step_number": 1}],
    )


def test_slot_merge_precheck_requires_machine_b_and_flags(monkeypatch):
    retry_state = SimpleNamespace(repair_prompt_used=True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "medium")

    assert (
        slot_merge_recovery_precheck(
            SimpleNamespace(),
            retry_state,
            _verdict("rejected"),
            [{"step_number": 1}],
            _verdict("repair_required"),
        )
        is True
    )

    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "standard")
    assert (
        slot_merge_recovery_precheck(
            SimpleNamespace(),
            retry_state,
            _verdict("rejected"),
            [{"step_number": 1}],
            _verdict("repair_required"),
        )
        is False
    )


def test_registry_medium_slot_merge_requires_slot_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, executor=_selected_result)
    )

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "unsupported_runtime_profile"


def test_registry_medium_slot_merge_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", True)

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, executor=_selected_result)
    )

    assert outcome.succeeded is True
    assert outcome["candidate_outcome"]["operator_sequence"] == [
        "original",
        "repair_mutation",
        "slot_merge",
    ]


def test_registry_rejects_slot_merge_on_machine_a(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", True)

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path,
            executor=_selected_result,
            runtime_profile="standard",
            operator="slot_merge",
        )
    )

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "unsupported_runtime_profile"


def test_registry_slot_merge_dedups_signature(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", True)
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
