"""Phase 18B: validator rule hit-rate telemetry tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.analytics.validator_rule_telemetry import (
    aggregate_validator_rule_telemetry,
    render_validator_rule_hit_rate_report,
    telemetry_from_candidate_events,
    verify_validator_rule_telemetry_complete,
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
from app.services.orchestration.validation.validator import ValidatorService
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


def _context(tmp_path, *, state, executor, signature="sig-rule-telemetry"):
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
        session_id=1821,
        task_id=1822,
        scope="planning",
        evidence=evidence,
        orchestration_state=state,
        runtime_profile="standard",
        recovery_metadata={
            "planning_failure_signature": signature,
            "candidate_executor": executor,
        },
    )


def _sibling_executor(tmp_path, *, sibling_status="accepted"):
    def _execute():
        request = CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=1821,
            task_id=1822,
            original_plan=[{"step_number": 1, "description": "original"}],
            original_output_text="original",
            original_verdict=_verdict(
                "repair_required",
                ("Missing validation command",),
            ),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling",
            ),
            validate_candidate=lambda _plan, _text: _verdict(sibling_status),
        )
        return execute_single_sibling_candidate_recovery(request)

    return _execute


def _validator_plan_without_verification():
    return [
        {
            "step_number": 1,
            "description": "Implement source",
            "commands": [],
            "verification": "",
            "rollback": "",
            "expected_files": ["src/app.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "print('hello')\n",
                }
            ],
        }
    ]


def _source_validator_verdict(tmp_path):
    return ValidatorService.validate_plan(
        _validator_plan_without_verification(),
        output_text="",
        task_prompt="Write a small Python implementation",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )


def test_validator_verdict_preserves_reasons_and_adds_stable_rule_ids(tmp_path):
    verdict = _source_validator_verdict(tmp_path)

    assert verdict.status == "repair_required"
    assert verdict.reasons == [
        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
    ]
    assert verdict.details["validator_rule_ids"] == ["missing_verification_command"]
    assert verdict.to_dict()["validator_rule_ids"] == ["missing_verification_command"]


def test_candidate_validation_audit_includes_source_validator_rule_ids(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)

    def _execute():
        request = CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=1821,
            task_id=1822,
            original_plan=_validator_plan_without_verification(),
            original_output_text="original",
            original_verdict=_source_validator_verdict(tmp_path),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling",
            ),
            validate_candidate=lambda _plan, _text: _verdict("accepted"),
        )
        return execute_single_sibling_candidate_recovery(request)

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(tmp_path, state=SimpleNamespace(), executor=_execute)
    )
    events = read_orchestration_events(tmp_path, session_id=1821, task_id=1822)
    validated = [
        event
        for event in events
        if event["event_type"] == EventType.PLAN_CANDIDATE_VALIDATED
    ]
    records = telemetry_from_candidate_events(events=events, outcome=outcome)

    assert validated[0]["details"]["validator_reasons"] == [
        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
    ]
    assert validated[0]["details"]["validator_rule_ids"] == [
        "missing_verification_command"
    ]
    assert records[0].rule_id == "missing_verification_command"
    assert records[0].candidate_recovery_rescued is True


def test_validator_rule_telemetry_captures_runtime_candidate_events(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(
            tmp_path,
            state=SimpleNamespace(),
            executor=_sibling_executor(tmp_path),
        )
    )
    events = read_orchestration_events(tmp_path, session_id=1821, task_id=1822)

    records = telemetry_from_candidate_events(events=events, outcome=outcome)
    summary = aggregate_validator_rule_telemetry(records)
    complete, issues = verify_validator_rule_telemetry_complete(records[0])

    assert len(records) == 1
    assert records[0].rule_id == "missing_validation_command"
    assert records[0].validator_status == "repair_required"
    assert records[0].planning_phase == "planning"
    assert records[0].failure_signature == "sig-rule-telemetry"
    assert records[0].candidate_recovery_triggered is True
    assert records[0].candidate_recovery_rescued is True
    assert records[0].selected_candidate_still_had_rule is False
    assert records[0].machine_profile == "machine-a"
    assert records[0].runtime_profile == "standard"
    assert records[0].timestamp
    assert complete is True
    assert issues == []
    assert summary.by_rule_id == {"missing_validation_command": 1}
    assert summary.rescue_correlation["missing_validation_command"].rescue_rate == 1.0


def test_validator_rule_telemetry_prefers_explicit_rule_ids():
    events = [
        {
            "event_type": EventType.RECOVERY_STARTED,
            "timestamp": "2026-07-04T16:00:00+00:00",
            "details": {"planning_failure_signature": "sig-explicit"},
        },
        {
            "event_type": EventType.PLAN_CANDIDATE_VALIDATED,
            "timestamp": "2026-07-04T16:00:01+00:00",
            "details": {
                "candidate_id": "candidate-original",
                "validator_status": "rejected",
                "validator_rule_ids": ["bootstrap.contract"],
                "validator_reasons": ["human text can change"],
                "planning_failure_signature": "sig-explicit",
            },
        },
        {
            "event_type": EventType.PLAN_CANDIDATE_VALIDATED,
            "timestamp": "2026-07-04T16:00:02+00:00",
            "details": {
                "candidate_id": "candidate-sibling-1",
                "validator_status": "warning",
                "validator_rule_ids": ["bootstrap.contract"],
                "planning_failure_signature": "sig-explicit",
            },
        },
        {
            "event_type": EventType.PLAN_CANDIDATE_SELECTED,
            "timestamp": "2026-07-04T16:00:03+00:00",
            "details": {"candidate_id": "candidate-sibling-1"},
        },
    ]

    records = telemetry_from_candidate_events(
        events=events,
        runtime_profile="standard",
    )
    summary = aggregate_validator_rule_telemetry(records)

    assert [record.rule_id for record in records] == [
        "bootstrap_contract",
        "bootstrap_contract",
    ]
    assert all(record.candidate_recovery_triggered for record in records)
    assert all(record.candidate_recovery_rescued for record in records)
    assert all(record.selected_candidate_still_had_rule for record in records)
    assert summary.by_validator_status == {"rejected": 1, "warning": 1}
    assert (
        summary.rescue_correlation["bootstrap_contract"].selected_still_had_rule_rate
        == 1.0
    )


def test_validator_rule_hit_rate_report_renders_frequency_and_correlation():
    events = [
        {
            "event_type": EventType.RECOVERY_STARTED,
            "timestamp": "2026-07-04T16:00:00+00:00",
            "details": {"planning_failure_signature": "sig-report"},
        },
        {
            "event_type": EventType.PLAN_CANDIDATE_VALIDATED,
            "timestamp": "2026-07-04T16:00:01+00:00",
            "details": {
                "candidate_id": "candidate-original",
                "validator_status": "repair_required",
                "validator_rule_ids": ["missing.tests"],
                "planning_failure_signature": "sig-report",
            },
        },
    ]
    summary = aggregate_validator_rule_telemetry(
        telemetry_from_candidate_events(events=events, runtime_profile="standard")
    )

    report = render_validator_rule_hit_rate_report(summary)

    assert "Total rule firings: 1" in report
    assert "`missing_tests`: 1" in report
    assert "rescued=0" in report
