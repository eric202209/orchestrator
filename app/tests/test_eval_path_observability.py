from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_scorer_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "score_orchestrator_eval_case.py"
    )
    spec = importlib.util.spec_from_file_location("score_orchestrator_eval_case", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = _load_scorer_module()


def _summary(events):
    return scorer._event_summary(events)


def _required(case, summary):
    return scorer._required_event_results(case, summary["event_type_counts"])


def test_path_observability_classifies_planning_validation_only_failure():
    case = {
        "case_id": "python_cli_small_feature",
        "category": "baseline_success",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "validation_result",
            "details": {"stage": "plan", "status": "repair_required"},
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "planning"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["planning_reached"] is True
    assert result["execution_reached"] is False
    assert result["debug_repair_reached"] is False
    assert result["intended_path_observed"] is False
    assert result["primary_failure_phase"] == "planning_validation"


def test_path_observability_reports_planning_terminal_root_cause():
    case = {
        "case_id": "python_cli_small_feature",
        "category": "baseline_success",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "validation_result",
            "details": {"stage": "plan", "status": "repair_required"},
        },
        {
            "event_type": "phase_event",
            "details": {
                "phase": "planning",
                "reason": "planning_circuit_breaker_opened",
                "terminal_state": "planning_circuit_breaker_opened",
                "planning_root_cause": "missing_verification",
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "planning"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["planning_terminal_state"] == "planning_circuit_breaker_opened"
    assert result["planning_root_cause"] == "missing_verification"
    assert result["cross_stage_convergence_class"] == "planning_only_blocker"


def test_path_observability_classifies_root_cause_oscillation():
    case = {
        "case_id": "python_cli_small_feature",
        "category": "baseline_success",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "planning_repair_arbitration",
            "details": {
                "invalid_output": True,
                "planning_root_cause": "invalid_python",
            },
        },
        {
            "event_type": "validation_result",
            "details": {
                "stage": "plan",
                "status": "repair_required",
                "reasons": ["Plan is missing verification commands"],
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "planning"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["cross_stage_convergence_class"] == "root_cause_oscillation"


def test_path_observability_honors_explicit_root_cause_oscillation_event():
    case = {
        "case_id": "python_cli_small_feature",
        "category": "baseline_success",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "cross_stage_convergence",
            "details": {
                "reason": "root_cause_oscillation_no_progress",
                "cross_stage_convergence_class": "root_cause_oscillation",
                "oscillation_detected": True,
                "oscillation_root_causes": ["invalid_python", "missing_verification"],
                "oscillation_stage_sequence": [
                    "planning_repair_arbitration",
                    "post_repair_missing_verification_second_pass",
                ],
                "oscillation_action": "stop_repair_loop",
                "planning_root_cause": "invalid_python",
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "planning"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["primary_failure_phase"] == "planning_validation"
    assert result["planning_root_cause"] == "invalid_python"
    assert result["cross_stage_convergence_class"] == "root_cause_oscillation"


def test_path_observability_does_not_report_historical_root_cause_after_planning_acceptance():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured"],
    }
    events = [
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "phase_event",
            "details": {
                "phase": "planning",
                "reason": "post_repair_python_source_syntax_second_pass",
                "planning_root_cause": "invalid_python",
            },
        },
        {
            "event_type": "phase_finished",
            "details": {"phase": "planning", "status": "accepted"},
        },
        {"event_type": "phase_started", "details": {"phase": "execution"}},
        {"event_type": "debug_feedback_captured", "details": {}},
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["planning_terminal_state"] == "accepted"
    assert result["planning_root_cause"] == "unknown"
    assert result["primary_failure_phase"] == "debug_repair"
    assert (
        result["cross_stage_convergence_class"]
        == "debug_repair_after_accepted_planning"
    )


def test_path_observability_classifies_cross_stage_contract_regression():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured"],
    }
    events = [
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "planning_repair_arbitration",
            "details": {
                "regression_labels": ["source_api_regression"],
                "source_api_contract": {
                    "missing_required_symbols": ["medium_cli.cli.build_parser"]
                },
            },
        },
        {
            "event_type": "phase_finished",
            "details": {"phase": "planning", "status": "accepted"},
        },
        {"event_type": "phase_started", "details": {"phase": "execution"}},
        {
            "event_type": "debug_feedback_captured",
            "details": {
                "pytest_excerpt": (
                    "ImportError: cannot import name 'build_parser' "
                    "from 'medium_cli.cli'"
                )
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["cross_stage_convergence_class"] == (
        "cross_stage_contract_regression"
    )


def test_path_observability_detects_phase7f_debug_repair():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    events = [
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {"event_type": "phase_started", "details": {"phase": "execution"}},
        {"event_type": "step_started", "details": {"step_index": 1}},
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode": "phase7f_bounded_debug_repair",
                "debug_prompt_mode_architecture": "bounded_execution_debug_repair",
            },
        },
        {"event_type": "repair_rejected", "details": {"reason": "invalid"}},
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["planning_reached"] is True
    assert result["execution_reached"] is True
    assert result["step_started_count"] == 1
    assert result["debug_repair_reached"] is True
    assert result["phase7f_used"] is True
    assert result["bounded_execution_debug_repair_used"] is True
    assert result["phase7g_used"] is False
    assert result["diff_scoped_debug_repair_used"] is False
    assert result["repair_rejected_count"] == 1
    assert result["intended_path_observed"] is True
    assert result["primary_failure_phase"] == "debug_repair"


def test_path_observability_detects_phase7g_and_checkpoint_paths():
    debug_case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    debug_events = [
        {"event_type": "step_started", "details": {}},
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode_architecture": "diff_scoped_debug_repair",
                "diff_repair_fallback_reason": None,
                "diff_capsule_line_count": 8,
            },
        },
    ]
    debug_summary = _summary(debug_events)

    debug_result = scorer._path_observability(
        case=debug_case,
        events=debug_events,
        snapshots=[],
        event_summary=debug_summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(debug_case, debug_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert debug_result["phase7g_used"] is True
    assert debug_result["diff_scoped_debug_repair_used"] is True
    assert debug_result["phase7f_used"] is False
    assert debug_result["bounded_execution_debug_repair_used"] is False

    checkpoint_case = {
        "case_id": "checkpoint_resume_mid_task",
        "category": "checkpoint_recovery",
        "required_events": ["checkpoint_loaded"],
    }
    checkpoint_events = [
        {"event_type": "checkpoint_loaded", "details": {}},
        {"event_type": "task_completed", "details": {}},
    ]
    checkpoint_summary = _summary(checkpoint_events)

    checkpoint_result = scorer._path_observability(
        case=checkpoint_case,
        events=checkpoint_events,
        snapshots=[],
        event_summary=checkpoint_summary,
        verifier={"available": True, "passed": True},
        clean_success=True,
        required_events=_required(checkpoint_case, checkpoint_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert checkpoint_result["checkpoint_loaded"] is True
    assert checkpoint_result["intended_path_observed"] is True
    assert checkpoint_result["primary_failure_phase"] is None


def test_path_observability_reads_architecture_named_debug_prompt_modes():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    events = [
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode_architecture": ("bounded_execution_debug_repair"),
            },
        },
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode_architecture": "diff_scoped_debug_repair",
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["phase7f_used"] is True
    assert result["bounded_execution_debug_repair_used"] is True
    assert result["phase7g_used"] is True
    assert result["diff_scoped_debug_repair_used"] is True


def test_path_observability_prefers_architecture_prompt_modes_over_compatibility_modes():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    events = [
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode": "phase7f_bounded_debug_repair",
                "debug_prompt_mode_architecture": "diff_scoped_debug_repair",
            },
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["phase7f_used"] is False
    assert result["bounded_execution_debug_repair_used"] is False
    assert result["phase7g_used"] is True
    assert result["diff_scoped_debug_repair_used"] is True


def test_path_observability_falls_back_to_compatibility_prompt_modes():
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    events = [
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {"debug_prompt_mode": "phase7f_bounded_debug_repair"},
        },
        {
            "event_type": "debug_repair_attempted",
            "details": {"debug_prompt_mode": "phase7g_diff_repair"},
        },
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert result["phase7f_used"] is True
    assert result["bounded_execution_debug_repair_used"] is True
    assert result["phase7g_used"] is True
    assert result["diff_scoped_debug_repair_used"] is True


def test_path_observability_requires_repair_rejection_for_stale_replace_case():
    case = {
        "case_id": "stale_replace_repair",
        "category": "repair_rejection",
        "required_events": ["task_started"],
    }
    no_rejection_events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "execution"}},
    ]
    no_rejection_summary = _summary(no_rejection_events)

    no_rejection = scorer._path_observability(
        case=case,
        events=no_rejection_events,
        snapshots=[{"status": "executing"}],
        event_summary=no_rejection_summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, no_rejection_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert no_rejection["intended_path_observed"] is False

    rejection_events = [
        *no_rejection_events,
        {"event_type": "repair_rejected", "details": {"reason": "stale_replace"}},
    ]
    rejection_summary = _summary(rejection_events)

    rejection = scorer._path_observability(
        case=case,
        events=rejection_events,
        snapshots=[{"status": "executing"}],
        event_summary=rejection_summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, rejection_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert rejection["repair_rejected_count"] == 1
    assert rejection["intended_path_observed"] is True

    planning_stale_replace_events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "planning"}},
        {
            "event_type": "validation_result",
            "details": {
                "stage": "plan",
                "status": "repair_required",
                "planning_root_cause": "stale_replace",
            },
        },
    ]
    planning_stale_replace_summary = _summary(planning_stale_replace_events)

    planning_stale_replace = scorer._path_observability(
        case=case,
        events=planning_stale_replace_events,
        snapshots=[{"status": "planning"}],
        event_summary=planning_stale_replace_summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, planning_stale_replace_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert planning_stale_replace["planning_root_cause"] == "stale_replace"
    assert planning_stale_replace["intended_path_observed"] is True

    clean_direct_repair = scorer._path_observability(
        case=case,
        events=no_rejection_events,
        snapshots=[{"status": "executing"}],
        event_summary=no_rejection_summary,
        verifier={"available": True, "passed": True},
        clean_success=True,
        required_events=_required(case, no_rejection_summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert clean_direct_repair["intended_path_observed"] is True
    assert clean_direct_repair["primary_failure_phase"] is None


def test_path_observability_treats_missing_report_as_completion_validation_signal():
    case = {
        "case_id": "missing_report_artifact",
        "category": "completion_validation",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "execution"}},
    ]
    summary = _summary(events)

    result = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": True},
        clean_success=False,
        required_events=_required(case, summary),
        files={
            "missing_required_files": ["reports/repair-summary.md"],
            "present_forbidden_existing_files": [],
        },
        scope={"forbidden_touched_files": []},
    )

    assert result["execution_reached"] is True
    assert result["intended_path_observed"] is True
    assert result["primary_failure_phase"] == "execution"

    clean_report_created = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": True},
        clean_success=True,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert clean_report_created["intended_path_observed"] is True
    assert clean_report_created["primary_failure_phase"] is None


def test_path_observability_treats_fake_verification_artifact_as_guard_signal():
    case = {
        "case_id": "fake_verification_artifact_guard",
        "category": "verification_artifact_guard",
        "required_events": ["task_started"],
    }
    events = [
        {"event_type": "task_started", "details": {}},
        {"event_type": "phase_started", "details": {"phase": "execution"}},
    ]
    summary = _summary(events)

    no_artifact = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert no_artifact["intended_path_observed"] is False

    fake_artifact = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
        files={
            "missing_required_files": [],
            "present_forbidden_existing_files": ["verification.txt"],
        },
        scope={"forbidden_touched_files": []},
    )

    assert fake_artifact["intended_path_observed"] is True

    planning_only_fake_artifact = scorer._path_observability(
        case=case,
        events=[{"event_type": "task_started", "details": {}}],
        snapshots=[{"status": "planning"}],
        event_summary=_summary([{"event_type": "task_started", "details": {}}]),
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(
            case, _summary([{"event_type": "task_started", "details": {}}])
        ),
        files={
            "missing_required_files": [],
            "present_forbidden_existing_files": ["verification.txt"],
        },
        scope={"forbidden_touched_files": []},
    )

    assert planning_only_fake_artifact["intended_path_observed"] is False

    clean_real_verification = scorer._path_observability(
        case=case,
        events=events,
        snapshots=[{"status": "executing"}],
        event_summary=summary,
        verifier={"available": True, "passed": True},
        clean_success=True,
        required_events=_required(case, summary),
        files={"missing_required_files": [], "present_forbidden_existing_files": []},
        scope={"forbidden_touched_files": []},
    )

    assert clean_real_verification["intended_path_observed"] is True
    assert clean_real_verification["primary_failure_phase"] is None
