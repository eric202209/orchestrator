"""Phase 18A: controlled Machine A Candidate Recovery rollout tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.analytics.candidate_rollout_validation import (
    aggregate_candidate_rollout,
    compare_rollout_flag_replays,
    deterministic_rollout_trace,
    telemetry_from_recovery_outcome,
    verify_rollout_audit_complete,
    verify_rollout_telemetry_complete,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.planning.candidate_recovery import (
    CandidateRecoveryRequest,
    execute_single_sibling_candidate_recovery,
)


def _verdict(status: str, reasons: tuple[str, ...] = ()):
    return SimpleNamespace(
        status=status,
        reasons=list(reasons),
        accepted=status == "accepted",
        warning=status == "warning",
        repairable=status == "repair_required",
    )


def _context(
    tmp_path,
    *,
    state,
    executor,
    runtime_profile="standard",
    signature="sig-rollout",
):
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
        session_id=1801,
        task_id=1802,
        scope="planning",
        evidence=evidence,
        orchestration_state=state,
        runtime_profile=runtime_profile,
        recovery_metadata={
            "planning_failure_signature": signature,
            "candidate_executor": executor,
        },
    )


def _sibling_executor(
    tmp_path,
    *,
    original_status="repair_required",
    sibling_status="accepted",
    session_id=1801,
    task_id=1802,
):
    def _execute():
        request = CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=session_id,
            task_id=task_id,
            original_plan=[{"step_number": 1, "description": "original"}],
            original_output_text="original",
            original_verdict=_verdict(original_status, ("missing validation",)),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling",
            ),
            validate_candidate=lambda _plan, _text: _verdict(
                sibling_status,
                ("still invalid",) if sibling_status == "rejected" else (),
            ),
        )
        return execute_single_sibling_candidate_recovery(request)

    return _execute


def _rollout_record(
    tmp_path,
    outcome,
    *,
    case_id="case-a",
    feature_flag_enabled=True,
    slot_merge_enabled=False,
):
    events = read_orchestration_events(tmp_path, session_id=1801, task_id=1802)
    return telemetry_from_recovery_outcome(
        case_id=case_id,
        outcome=outcome,
        events=events,
        feature_flag_enabled=feature_flag_enabled,
        slot_merge_enabled=slot_merge_enabled,
    )


def test_candidate_rollout_flag_off_preserves_phase17_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", False)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    state = SimpleNamespace()
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path,
            state=state,
            executor=_sibling_executor(tmp_path),
        )
    )
    record = _rollout_record(
        tmp_path,
        outcome,
        case_id="flag-off",
        feature_flag_enabled=False,
    )

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "not_enabled"
    assert record.triggered is False
    assert record.feature_flag_enabled is False
    assert EventType.RECOVERY_DECISION_ROUTED in record.audit_event_types
    assert EventType.PLAN_CANDIDATE_CREATED not in record.audit_event_types


def test_candidate_rollout_flag_on_machine_a_collects_complete_telemetry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    state = SimpleNamespace()
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path,
            state=state,
            executor=_sibling_executor(tmp_path),
        )
    )
    record = _rollout_record(tmp_path, outcome, case_id="rescued")

    complete, issues = verify_rollout_telemetry_complete(record)
    audit_complete, audit_issues = verify_rollout_audit_complete(record)
    summary = aggregate_candidate_rollout([record])

    assert outcome.succeeded is True
    assert record.triggered is True
    assert record.runtime_profile == "standard"
    assert record.failure_signature == "sig-rollout"
    assert record.original_candidate_status == "repair_required"
    assert record.recovery_candidate_status == "accepted"
    assert record.selected_candidate_id == "candidate-sibling-1"
    assert record.recovery_succeeded is True
    assert record.recovery_exhausted is False
    assert record.candidate_count == 2
    assert record.validator_rejection_reasons == ("missing validation",)
    assert complete is True
    assert issues == []
    assert audit_complete is True
    assert audit_issues == []
    assert summary.trigger_count == 1
    assert summary.rescue_rate == 1.0
    assert summary.average_candidate_count == 2
    assert summary.validator_rejection_distribution == {"missing validation": 1}


def test_candidate_rollout_exhaustion_records_validator_rejections(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    state = SimpleNamespace()
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path,
            state=state,
            executor=_sibling_executor(tmp_path, sibling_status="rejected"),
        )
    )
    record = _rollout_record(tmp_path, outcome, case_id="exhausted")
    complete, issues = verify_rollout_telemetry_complete(record)
    summary = aggregate_candidate_rollout([record])

    assert outcome.succeeded is False
    assert outcome["reason"] == "candidate_exhausted"
    assert record.recovery_exhausted is True
    assert record.validator_result == "exhausted"
    assert record.validator_rejection_reasons == (
        "missing validation",
        "still invalid",
    )
    assert complete is True
    assert issues == []
    assert summary.exhaustion_rate == 1.0
    assert summary.validator_rejection_distribution == {
        "missing validation": 1,
        "still invalid": 1,
    }


def test_candidate_rollout_rollback_requires_only_feature_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", False)
    off_outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "off",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "off"),
        )
    )
    off_record = _rollout_record(
        tmp_path / "off",
        off_outcome,
        case_id="rollback",
        feature_flag_enabled=False,
    )

    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    on_outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "on",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "on"),
        )
    )
    on_record = _rollout_record(tmp_path / "on", on_outcome, case_id="rollback")

    rollback_ok, rollback_issues = compare_rollout_flag_replays(
        [off_record],
        [on_record],
    )

    assert off_record.triggered is False
    assert on_record.triggered is True
    assert rollback_ok is True
    assert rollback_issues == []


def test_candidate_rollout_deterministic_replay_and_duplicate_suppression(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)

    first_outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "first",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "first"),
        )
    )
    second_outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "second",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "second"),
        )
    )
    first_record = _rollout_record(tmp_path / "first", first_outcome)
    second_record = _rollout_record(tmp_path / "second", second_outcome)

    shared_state = SimpleNamespace()
    RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "dedup",
            state=shared_state,
            executor=_sibling_executor(tmp_path / "dedup"),
        )
    )
    dedup_outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "dedup",
            state=shared_state,
            executor=_sibling_executor(tmp_path / "dedup"),
        )
    )
    dedup_record = _rollout_record(tmp_path / "dedup", dedup_outcome)
    summary = aggregate_candidate_rollout([first_record, dedup_record])

    assert deterministic_rollout_trace([first_record]) == deterministic_rollout_trace(
        [second_record]
    )
    assert dedup_outcome["reason"] == "signature_already_attempted"
    assert dedup_record.duplicate_signature_suppressed is True
    assert summary.duplicate_signature_suppression_count == 1


def test_candidate_rollout_machine_b_and_c_remain_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)

    machine_b = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "machine-b",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "machine-b"),
            runtime_profile="medium",
        )
    )
    machine_c = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path / "machine-c",
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path / "machine-c"),
            runtime_profile="low_resource",
        )
    )

    assert machine_b["status"] == "skipped"
    assert machine_b["reason"] == "unsupported_runtime_profile"
    assert machine_c["status"] == "skipped"
    assert machine_c["reason"] == "unsupported_runtime_profile"
