from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path("scripts/evals/analyze_phase12e_debug_repair_failures.py")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    path = REPO_ROOT / SCRIPT
    spec = importlib.util.spec_from_file_location("phase12e_attribution", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


phase12e = _load_script_module()


def _write_run_report(
    path: Path,
    *,
    event_journal_path: Path | None = None,
    repair_events: dict[str, int] | None = None,
    touched_files: list[str] | None = None,
    verifier_passed: bool = False,
) -> None:
    path.write_text(
        json.dumps(
            {
                "case": {"case_id": "debug_import_error_repair"},
                "events": {
                    "repair_events": repair_events
                    or {
                        "debug_feedback_captured": 1,
                        "debug_repair_attempted": 1,
                        "repair_applied": 0,
                        "repair_rejected": 1,
                    }
                },
                "input": {
                    "event_journal_path": (
                        str(event_journal_path) if event_journal_path else None
                    )
                },
                "path_observability": {
                    "primary_failure_phase": "debug_repair",
                    "cross_stage_convergence_class": (
                        "cross_stage_contract_regression"
                    ),
                    "planning_root_cause": "unknown",
                },
                "result": {
                    "clean_success": False,
                    "task_completed_event_present": False,
                    "task_failed_event_present": False,
                },
                "touch_scope": {"touched_files": touched_files or []},
                "verifier": {"passed": verifier_passed, "exit_code": 1},
            }
        ),
        encoding="utf-8",
    )


def _write_aggregate(path: Path, *run_reports: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "case_id": "debug_import_error_repair",
                "repeat_seed": "phase12e-test-set",
                "run_report_paths": [str(report) for report in run_reports],
            }
        ),
        encoding="utf-8",
    )


def test_phase12e_classifies_rejected_debug_repair_output(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps({"event_type": "debug_feedback_captured", "details": {}}),
                json.dumps({"event_type": "debug_repair_attempted", "details": {}}),
                json.dumps(
                    {
                        "event_type": "repair_rejected",
                        "details": {
                            "debug_repair_rejection_reason": "missing_command",
                            "debug_repair_parsed_shape": {
                                "type": "list",
                                "length": 1,
                                "first_item_type": "dict",
                                "first_item_keys": [
                                    "ops",
                                    "repair_type",
                                    "verification_command",
                                ],
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_report = tmp_path / "debug-r01.json"
    aggregate = tmp_path / "aggregate.json"
    _write_run_report(run_report, event_journal_path=journal)
    _write_aggregate(aggregate, run_report)

    summary = phase12e.build_summary([aggregate])

    [row] = summary["rows"]
    assert row["failure_class"] == "debug_repair_output_rejected_no_patch_applied"
    assert row["failure_feedback_present"] is True
    assert row["repair_attempted"] is True
    assert row["repair_rejected"] is True
    assert row["repair_rejection_reason"] == "missing_command"
    assert row["repair_output_shape"] == (
        "list[1] first_item=dict keys=ops,repair_type,verification_command"
    )


def test_phase12e_classifies_applied_patch_that_failed_verifier(tmp_path):
    run_report = tmp_path / "debug-r02.json"
    aggregate = tmp_path / "aggregate.json"
    _write_run_report(
        run_report,
        repair_events={
            "debug_feedback_captured": 1,
            "debug_repair_attempted": 1,
            "repair_applied": 1,
            "repair_rejected": 0,
        },
        touched_files=["src/import_repair/formatters.py"],
        verifier_passed=False,
    )
    _write_aggregate(aggregate, run_report)

    summary = phase12e.build_summary([aggregate])

    [row] = summary["rows"]
    assert row["failure_class"] == "repair_patch_applied_but_verifier_failed"
    assert row["repair_output_shape"] == "unknown"
    assert row["touched_files"] == ["src/import_repair/formatters.py"]


def test_phase12e_classifies_rejected_output_after_workspace_change(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps({"event_type": "debug_feedback_captured", "details": {}}),
                json.dumps({"event_type": "debug_repair_attempted", "details": {}}),
                json.dumps(
                    {
                        "event_type": "repair_rejected",
                        "details": {
                            "debug_repair_rejection_reason": "missing_command",
                            "debug_repair_parsed_shape": {
                                "type": "list",
                                "length": 1,
                                "first_item_type": "dict",
                                "first_item_keys": ["ops"],
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_report = tmp_path / "debug-r03.json"
    aggregate = tmp_path / "aggregate.json"
    _write_run_report(
        run_report,
        event_journal_path=journal,
        touched_files=["src/import_repair/formatters.py"],
    )
    _write_aggregate(aggregate, run_report)

    summary = phase12e.build_summary([aggregate])

    [row] = summary["rows"]
    assert row["failure_class"] == (
        "debug_repair_output_rejected_after_workspace_change"
    )


def test_phase12e_cli_writes_json_and_markdown_outputs(tmp_path):
    run_report = tmp_path / "debug-r03.json"
    aggregate = tmp_path / "aggregate.json"
    output = tmp_path / "summary.json"
    markdown = tmp_path / "summary.md"
    _write_run_report(run_report)
    _write_aggregate(aggregate, run_report)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--markdown-output",
            str(markdown),
            str(aggregate),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Phase 12E debug repair attribution" in result.stdout
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "read_only_attribution"
    assert payload["row_count"] == 1
    markdown_text = markdown.read_text(encoding="utf-8")
    assert "Repair output shape" in markdown_text
    assert "debug_repair_output_rejected_no_patch_applied" in markdown_text
