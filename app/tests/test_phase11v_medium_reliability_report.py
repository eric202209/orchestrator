from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_report_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "phase11v_medium_reliability_report.py"
    )
    spec = importlib.util.spec_from_file_location(
        "phase11v_medium_reliability_report", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load_report_module()


def _write_report(
    tmp_path: Path,
    name: str,
    *,
    phase: str,
    stdout_tail: str,
    clean_success: bool = False,
    execution_reached: bool = True,
    debug_repair_reached: bool = True,
) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-30T00:00:00+00:00",
                "result": {
                    "clean_success": clean_success,
                    "verifier_passed": clean_success,
                    "blockers": [] if clean_success else ["verifier_failed"],
                },
                "path_observability": {
                    "primary_failure_phase": phase,
                    "execution_reached": execution_reached,
                    "debug_repair_reached": debug_repair_reached,
                    "bounded_execution_debug_repair_used": debug_repair_reached,
                    "diff_scoped_debug_repair_used": False,
                },
                "events": {
                    "repair_events": {
                        "debug_repair_attempted": 1 if debug_repair_reached else 0,
                        "repair_rejected": 1 if debug_repair_reached else 0,
                    }
                },
                "verifier": {
                    "stdout_tail": stdout_tail,
                    "stderr_tail": "",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_phase11v_report_extracts_argparse_failure_signature(tmp_path):
    path = _write_report(
        tmp_path,
        "medium-1.json",
        phase="debug_repair",
        stdout_tail=(
            "argparse.ArgumentError: argument command: invalid choice: "
            "'summary' (choose from 'list')"
        ),
    )

    payload = reporter.build_report([path], source="test")

    assert payload["failure_signature_distribution"] == {
        "debug_repair:argparse_invalid_choice_summary": 1
    }
    assert payload["stable_failure_signature"] is True


def test_phase11v_report_summarizes_repeat_three_stability(tmp_path):
    paths = [
        _write_report(
            tmp_path,
            f"medium-{index}.json",
            phase="debug_repair",
            stdout_tail=(
                "argparse.ArgumentError: argument command: invalid choice: "
                "'summary' (choose from 'list')"
            ),
        )
        for index in range(3)
    ]

    payload = reporter.build_report(paths, source="test")

    assert payload["report_count"] == 3
    assert payload["execution_reached_rate"] == 1.0
    assert payload["primary_failure_phase_distribution"] == {"debug_repair": 3}
    assert payload["stable_primary_failure_phase"] is True
    assert payload["repair_convergence_proxy"]["runs_with_repair_rejection"] == 3


def test_phase11v_report_prefers_blocker_when_verifier_passed(tmp_path):
    path = _write_report(
        tmp_path,
        "medium-clean-verifier-missing-terminal.json",
        phase="execution",
        stdout_tail="6 passed in 0.12s",
        clean_success=False,
        debug_repair_reached=False,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"]["verifier_passed"] = True
    payload["result"]["blockers"] = ["task_completed_event_missing"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = reporter.build_report([path], source="test")

    assert summary["failure_signature_distribution"] == {
        "execution:task_completed_event_missing": 1
    }


def test_phase11v_report_uses_journal_validation_reasons_for_planning_signature(
    tmp_path,
):
    report_path = _write_report(
        tmp_path,
        "medium-planning.json",
        phase="planning_validation",
        stdout_tail="pytest failed after planning validation",
        execution_reached=False,
        debug_repair_reached=False,
    )
    journal_path = tmp_path / "events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "validation_result",
                        "details": {
                            "reasons": [
                                "Plan writes Python decorators whose root name is undefined (files: ['src/medium_cli/cli.py'])"
                            ]
                        },
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    aggregate_path = tmp_path / "aggregate.json"
    aggregate_path.write_text(
        json.dumps(
            {
                "run_report_paths": [str(report_path)],
                "score_readiness_summary": {"journal_paths": [str(journal_path)]},
            }
        ),
        encoding="utf-8",
    )

    summary = reporter.build_report(
        [report_path],
        source="test",
        runner_aggregate_path=aggregate_path,
    )

    assert summary["failure_signature_distribution"] == {
        "planning_validation:undefined_decorator_root": 1
    }
    assert summary["runs"][0]["planning_validation_reasons"]
