from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from app.services.orchestration.phases import failure_flow


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runtime_naming"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scorer = _load_module(
    "historical_runtime_naming_score_orchestrator_eval_case",
    Path(__file__).resolve().parents[2] / "scripts" / "score_orchestrator_eval_case.py",
)
runner = _load_module(
    "historical_runtime_naming_run_orchestrator_eval_slice",
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "evals"
    / "run_orchestrator_eval_slice.py",
)


def _load_json(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _path_observability(events: list[dict[str, Any]]) -> dict[str, Any]:
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    summary = scorer._event_summary(events)
    return scorer._path_observability(
        case=case,
        events=events,
        snapshots=[],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=scorer._required_event_results(
            case, summary["event_type_counts"]
        ),
    )


def test_old_only_per_run_reports_still_aggregate_to_architecture_outputs():
    reports = [
        _load_json("old_only_phase7f_report.json"),
        _load_json("old_only_phase7g_report.json"),
    ]

    aggregate = runner._aggregate_case_reports(
        case_id="historical_runtime_naming",
        reports=reports,
        report_paths=[
            FIXTURE_DIR / "old_only_phase7f_report.json",
            FIXTURE_DIR / "old_only_phase7g_report.json",
        ],
        run_context={
            "git_sha": None,
            "model": None,
            "backend": None,
            "runtime_profile": None,
            "repeat_seed": None,
        },
    )

    assert aggregate["bounded_execution_debug_repair_used_count"] == 1
    assert aggregate["diff_scoped_debug_repair_used_count"] == 1
    assert aggregate["bounded_execution_debug_repair_exercised_rate"] == 0.5
    assert aggregate["diff_scoped_debug_repair_exercised_rate"] == 0.5
    assert "phase7f_used_count" not in aggregate
    assert "phase7g_used_count" not in aggregate
    assert "phase7f_exercised_rate" not in aggregate
    assert "phase7g_exercised_rate" not in aggregate


def test_old_only_phase7f_event_journal_still_scores_architecture_output():
    events = _load_json("old_only_phase7f_events.json")

    result = _path_observability(events)

    assert result["phase7f_used"] is True
    assert result["bounded_execution_debug_repair_used"] is True
    assert result["phase7g_used"] is False
    assert result["diff_scoped_debug_repair_used"] is False


def test_old_only_phase7g_event_journal_still_scores_architecture_output():
    events = _load_json("old_only_phase7g_events.json")

    result = _path_observability(events)

    assert result["phase7f_used"] is False
    assert result["bounded_execution_debug_repair_used"] is False
    assert result["phase7g_used"] is True
    assert result["diff_scoped_debug_repair_used"] is True


def test_old_only_timeout_diagnostics_still_classifies_bounded_debug_timeout():
    diagnostics = _load_json("old_only_phase7f_timeout_diagnostics.json")

    assert (
        failure_flow._is_bounded_debug_repair_timeout(
            TimeoutError("Task timed out after 180s"), diagnostics
        )
        is True
    )


def test_old_only_rejection_fixture_keeps_removal_candidate_fields():
    events = _load_json("old_only_phase7f_events.json")
    rejected = [event for event in events if event["event_type"] == "repair_rejected"]
    details = rejected[-1]["details"]

    assert details["reason"] == "phase7f_debug_repair_output_invalid"
    assert details["phase7f_rejection_reason"] == "invalid_json"
    assert details["phase7f_parsed_shape"] == {"type": "text"}
    assert details["phase7f_raw_output_excerpt"] == "not json"
    assert "bounded_execution_debug_repair_rejection_reason" not in details
