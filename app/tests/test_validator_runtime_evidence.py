"""Phase 18D: validator runtime evidence collection tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.analytics.validator_runtime_evidence import (
    RuntimeEvidenceCase,
    collect_validator_runtime_evidence,
    render_validator_runtime_evidence_report,
)
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


def _verdict(
    status: str,
    reasons: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
):
    return SimpleNamespace(
        status=status,
        reasons=list(reasons),
        details={"validator_rule_ids": list(rule_ids)} if rule_ids else {},
        accepted=status == "accepted",
        warning=status == "warning",
        repairable=status == "repair_required",
    )


def _plan_without_verification():
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


def _source_validator_verdict(project_dir):
    return ValidatorService.validate_plan(
        _plan_without_verification(),
        output_text="",
        task_prompt="Write a small Python implementation",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
    )


def _context(project_dir, *, executor, signature):
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
        project_dir=project_dir,
        session_id=1841,
        task_id=1842,
        scope="planning",
        evidence=evidence,
        orchestration_state=SimpleNamespace(),
        runtime_profile="standard",
        recovery_metadata={
            "planning_failure_signature": signature,
            "candidate_executor": executor,
        },
    )


def _runtime_case(
    project_dir,
    *,
    case_id: str,
    signature: str,
    sibling_verdict,
):
    def _execute():
        request = CandidateRecoveryRequest(
            project_dir=project_dir,
            session_id=1841,
            task_id=1842,
            original_plan=_plan_without_verification(),
            original_output_text="original",
            original_verdict=_source_validator_verdict(project_dir),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling",
            ),
            validate_candidate=lambda _plan, _text: sibling_verdict,
        )
        return execute_single_sibling_candidate_recovery(request)

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_context(project_dir, executor=_execute, signature=signature)
    )
    events = read_orchestration_events(project_dir, session_id=1841, task_id=1842)
    return RuntimeEvidenceCase(
        case_id=case_id,
        outcome=outcome,
        events=tuple(events),
        feature_flag_enabled=True,
    )


def test_runtime_evidence_collects_rule_metrics_and_rescue_correlation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    rescued = _runtime_case(
        tmp_path / "rescued",
        case_id="rescued",
        signature="sig-rescued",
        sibling_verdict=_verdict("accepted"),
    )
    still_has_rule = _runtime_case(
        tmp_path / "still-has-rule",
        case_id="still-has-rule",
        signature="sig-still-has-rule",
        sibling_verdict=_verdict(
            "warning",
            ("still weak",),
            ("missing_verification_command",),
        ),
    )

    report = collect_validator_runtime_evidence([rescued, still_has_rule])
    evidence = report.by_rule_id["missing_verification_command"]

    assert report.total_cases == 2
    assert report.total_rule_firings == 3
    assert report.stable_rule_id_event_count == 3
    assert evidence.frequency == 3
    assert evidence.validator_status_distribution == {
        "repair_required": 2,
        "warning": 1,
    }
    assert evidence.recovery_trigger_rate == 1.0
    assert evidence.recovery_rescue_rate == 1.0
    assert evidence.selected_candidate_still_had_rule_count == 2
    assert evidence.selected_candidate_still_had_rule_rate == 0.667
    assert evidence.machine_profiles == {"machine-a": 3}
    assert evidence.runtime_profiles == {"standard": 3}
    assert evidence.failure_signatures == {
        "sig-rescued": 1,
        "sig-still-has-rule": 2,
    }
    assert "missing_verification_command" in report.high_frequency_rules
    assert "missing_verification_command" in report.rescue_correlated_rules
    assert "missing_verification_command" in report.noisy_or_redundant_rules


def test_runtime_evidence_report_renders_required_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.CANDIDATE_SLOT_MERGE_ENABLED", False)
    case = _runtime_case(
        tmp_path / "case",
        case_id="case",
        signature="sig-report",
        sibling_verdict=_verdict("accepted"),
    )

    rendered = render_validator_runtime_evidence_report(
        collect_validator_runtime_evidence([case])
    )

    assert "Stable rule-ID audit events: 1" in rendered
    assert "### `missing_verification_command`" in rendered
    assert "validator_status_distribution" in rendered
    assert "recovery_trigger_rate: 1.000" in rendered
    assert "recovery_rescue_rate: 1.000" in rendered
    assert "machine_profiles: {'machine-a': 1}" in rendered
    assert "runtime_profiles: {'standard': 1}" in rendered
    assert "failure_signatures: {'sig-report': 1}" in rendered
    assert "average_latency_ms" in rendered
