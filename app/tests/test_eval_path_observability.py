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
    )

    assert result["planning_reached"] is True
    assert result["execution_reached"] is False
    assert result["debug_repair_reached"] is False
    assert result["intended_path_observed"] is False
    assert result["primary_failure_phase"] == "planning_validation"


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
            "details": {"debug_prompt_mode": "phase7f_bounded_debug_repair"},
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
    )

    assert result["planning_reached"] is True
    assert result["execution_reached"] is True
    assert result["step_started_count"] == 1
    assert result["debug_repair_reached"] is True
    assert result["phase7f_used"] is True
    assert result["phase7g_used"] is False
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
    )

    assert debug_result["phase7g_used"] is True

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
    )

    assert checkpoint_result["checkpoint_loaded"] is True
    assert checkpoint_result["intended_path_observed"] is True
    assert checkpoint_result["primary_failure_phase"] is None
