from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_report_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "phase11v_repair_convergence_report.py"
    )
    spec = importlib.util.spec_from_file_location(
        "phase11v_repair_convergence_report", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load_report_module()


def _write_report_bundle(
    tmp_path: Path,
    *,
    before_reasons: list[str] | None = None,
    after_reasons: list[str] | None = None,
    before_ops: list[dict] | None = None,
    after_ops: list[dict] | None = None,
) -> tuple[Path, Path]:
    report_path = tmp_path / "run.json"
    journal_path = tmp_path / "events.jsonl"
    snapshot_path = tmp_path / "snapshots.jsonl"
    aggregate_path = tmp_path / "aggregate.json"

    report_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-30T00:00:00+00:00",
                "input": {"state_snapshot_path": str(snapshot_path)},
                "result": {
                    "clean_success": False,
                    "verifier_passed": False,
                    "blockers": ["verifier_failed"],
                },
                "path_observability": {
                    "primary_failure_phase": "planning_validation",
                    "execution_reached": False,
                    "debug_repair_reached": False,
                },
            }
        ),
        encoding="utf-8",
    )
    events = []
    snapshots = []
    if before_reasons is not None:
        events.append(
            {
                "event_id": "before",
                "timestamp": "2026-05-30T00:00:00+00:00",
                "event_type": "validation_result",
                "details": {
                    "stage": "plan",
                    "status": "repair_required",
                    "reasons": before_reasons,
                },
            }
        )
        snapshots.append(
            {
                "related_event_id": "before",
                "trigger": "validation_plan",
                "plan_steps": [
                    {
                        "step_number": 1,
                        "ops": before_ops or [],
                    }
                ],
            }
        )
    if after_reasons is not None:
        events.append(
            {
                "event_id": "after",
                "timestamp": "2026-05-30T00:01:00+00:00",
                "event_type": "validation_result",
                "details": {
                    "stage": "plan",
                    "status": "repair_required",
                    "reasons": after_reasons,
                },
            }
        )
        snapshots.append(
            {
                "related_event_id": "after",
                "trigger": "validation_plan",
                "plan_steps": [
                    {
                        "step_number": 1,
                        "ops": after_ops or [],
                    }
                ],
            }
        )
    journal_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    snapshot_path.write_text(
        "".join(json.dumps(snapshot) + "\n" for snapshot in snapshots),
        encoding="utf-8",
    )
    aggregate_path.write_text(
        json.dumps(
            {
                "run_report_paths": [str(report_path)],
                "score_readiness_summary": {"journal_paths": [str(journal_path)]},
            }
        ),
        encoding="utf-8",
    )
    return report_path, aggregate_path


def _source_write(path: str = "src/app.py") -> dict:
    return {"op": "write_file", "path": path, "content": "print('ok')\n"}


def test_repair_convergence_reports_source_materialization_added(tmp_path):
    _report_path, aggregate_path = _write_report_bundle(
        tmp_path,
        before_reasons=["Plan does not materialize any source changes"],
        after_reasons=[
            "Plan is missing verification commands for implementation-heavy work"
        ],
        before_ops=[],
        after_ops=[_source_write()],
    )

    payload = reporter.build_report(
        [], source="test", runner_aggregate_path=aggregate_path
    )

    attempt = payload["runs"][0]["attempts"][0]
    assert attempt["source_materialization"] == "added"
    assert attempt["final_classification"] == "improved"


def test_repair_convergence_reports_source_materialization_removed(tmp_path):
    _report_path, aggregate_path = _write_report_bundle(
        tmp_path,
        before_reasons=[
            "Plan is missing verification commands for implementation-heavy work"
        ],
        after_reasons=["Plan does not materialize any source changes"],
        before_ops=[_source_write()],
        after_ops=[],
    )

    payload = reporter.build_report(
        [], source="test", runner_aggregate_path=aggregate_path
    )

    attempt = payload["runs"][0]["attempts"][0]
    assert attempt["source_materialization"] == "removed"
    assert attempt["final_classification"] == "regressed"


def test_repair_convergence_reports_syntax_retry_fix(tmp_path):
    _report_path, aggregate_path = _write_report_bundle(
        tmp_path,
        before_reasons=[
            "Plan writes Python source with invalid syntax (python_source_syntax_invalid; src/app.py line 1)"
        ],
        after_reasons=[
            "Plan is missing verification commands for implementation-heavy work"
        ],
        before_ops=[_source_write()],
        after_ops=[_source_write()],
    )

    payload = reporter.build_report(
        [], source="test", runner_aggregate_path=aggregate_path
    )

    attempt = payload["runs"][0]["attempts"][0]
    assert attempt["repair_lane"] == "syntax_retry"
    assert attempt["python_syntax_validity"] == "improved"
    assert attempt["final_classification"] == "improved"


def test_repair_convergence_reports_framework_retry_invalid_json(tmp_path):
    _report_path, aggregate_path = _write_report_bundle(
        tmp_path,
        before_reasons=[
            "framework_mismatch: repaired Python source introduced decorator-style CLI code"
        ],
        after_reasons=["planning_json_error: invalid JSON parse failure"],
        before_ops=[_source_write("src/medium_cli/cli.py")],
        after_ops=[_source_write("src/medium_cli/cli.py")],
    )

    payload = reporter.build_report(
        [], source="test", runner_aggregate_path=aggregate_path
    )

    attempt = payload["runs"][0]["attempts"][0]
    assert attempt["repair_lane"] == "framework_retry"
    assert attempt["after_signature"] == "invalid_json"
    assert attempt["final_classification"] == "invalid_output"


def test_repair_convergence_handles_no_repair_events(tmp_path):
    _report_path, aggregate_path = _write_report_bundle(
        tmp_path,
        before_reasons=None,
        after_reasons=None,
    )

    payload = reporter.build_report(
        [], source="test", runner_aggregate_path=aggregate_path
    )

    assert payload["repair_attempt_count"] == 0
    assert payload["classification_distribution"] == {}
    assert payload["runs"][0]["attempts"] == []
