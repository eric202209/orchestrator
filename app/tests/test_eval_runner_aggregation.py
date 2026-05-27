from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_runner_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "evals"
        / "run_orchestrator_eval_slice.py"
    )
    spec = importlib.util.spec_from_file_location("run_orchestrator_eval_slice", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner_module()


def _report(
    *,
    clean_success: bool,
    primary_failure_phase: str | None,
    path_observed: bool,
    intended_path_observed: bool,
    execution_reached: bool,
    debug_repair_reached: bool,
    phase7f_used: bool,
    phase7g_used: bool,
    blockers: list[str],
) -> dict:
    return {
        "result": {
            "clean_success": clean_success,
            "path_observed": path_observed,
            "blockers": blockers,
        },
        "path_observability": {
            "primary_failure_phase": primary_failure_phase,
            "intended_path_observed": intended_path_observed,
            "execution_reached": execution_reached,
            "debug_repair_reached": debug_repair_reached,
            "phase7f_used": phase7f_used,
            "phase7g_used": phase7g_used,
        },
    }


def test_aggregate_case_reports_counts_path_observability_and_metadata():
    reports = [
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=True,
            phase7g_used=False,
            blockers=["verifier_failed"],
        ),
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=True,
            phase7g_used=True,
            blockers=["verifier_failed"],
        ),
        _report(
            clean_success=True,
            primary_failure_phase=None,
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=[],
        ),
    ]

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path("run1.json"), Path("run2.json"), Path("run3.json")],
        run_context={
            "git_sha": "abc123",
            "model": "qwen-local",
            "backend": "local_openclaw",
            "runtime_profile": "standard",
            "repeat_seed": "seed-1",
        },
    )

    assert aggregate["repeat_count"] == 3
    assert aggregate["git_sha"] == "abc123"
    assert aggregate["model"] == "qwen-local"
    assert aggregate["backend"] == "local_openclaw"
    assert aggregate["runtime_profile"] == "standard"
    assert aggregate["repeat_seed"] == "seed-1"
    assert aggregate["clean_success_count"] == 1
    assert aggregate["clean_success_rate"] == 1 / 3
    assert aggregate["primary_failure_phase_distribution"] == {
        "clean_success": 1,
        "debug_repair": 2,
    }
    assert aggregate["stable_primary_failure_phase"] is False
    assert aggregate["path_observed_count"] == 3
    assert aggregate["intended_path_observed_count"] == 3
    assert aggregate["execution_reached_count"] == 3
    assert aggregate["debug_repair_reached_count"] == 2
    assert aggregate["phase7f_used_count"] == 2
    assert aggregate["phase7g_used_count"] == 1
    assert aggregate["phase7f_exercised_rate"] == 2 / 3
    assert aggregate["phase7g_exercised_rate"] == 1 / 3
    assert aggregate["most_common_blocker"] == "verifier_failed"
    assert aggregate["run_report_paths"] == ["run1.json", "run2.json", "run3.json"]


def test_aggregate_case_reports_marks_stable_phase_at_eighty_percent_threshold():
    reports = [
        _report(
            clean_success=False,
            primary_failure_phase="planning_validation",
            path_observed=False,
            intended_path_observed=False,
            execution_reached=False,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["task_completed_event_missing"],
        )
        for _ in range(4)
    ]
    reports.append(
        _report(
            clean_success=False,
            primary_failure_phase="execution",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["verifier_failed"],
        )
    )

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path(f"run{index}.json") for index in range(5)],
        run_context={
            "git_sha": None,
            "model": None,
            "backend": None,
            "runtime_profile": None,
            "repeat_seed": None,
        },
    )

    assert aggregate["primary_failure_phase_distribution"] == {
        "execution": 1,
        "planning_validation": 4,
    }
    assert aggregate["stable_primary_failure_phase"] is True
