from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path("scripts/evals/check_phase12c_gates.py")
POLICY = Path("scripts/evals/phase12c-gate-policy.json")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_aggregate(
    path: Path,
    *,
    case_id: str,
    clean_success_rate: float,
    intended_path_observed_rate: float,
    repeat_count: int = 3,
    stable_primary_failure_phase: bool = True,
    most_common_blocker: str = "clean_success",
) -> None:
    path.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "repeat_count": repeat_count,
                "clean_success_count": round(clean_success_rate * repeat_count),
                "clean_success_rate": clean_success_rate,
                "intended_path_observed_count": round(
                    intended_path_observed_rate * repeat_count
                ),
                "intended_path_observed_rate": intended_path_observed_rate,
                "stable_primary_failure_phase": stable_primary_failure_phase,
                "most_common_blocker": most_common_blocker,
            }
        ),
        encoding="utf-8",
    )


def test_phase12c_simulation_reports_would_fail_without_failing_ci(tmp_path):
    aggregate = tmp_path / "missing-report-aggregate.json"
    summary = tmp_path / "summary.json"
    _write_aggregate(
        aggregate,
        case_id="missing_report_artifact",
        clean_success_rate=2 / 3,
        intended_path_observed_rate=1.0,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--summary-output",
            str(summary),
            str(aggregate),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "would_fail" in result.stdout
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["blocking_failure_count"] == 0
    assert payload["simulated_failure_count"] == 1
    [case] = payload["cases"]
    assert case["case_id"] == "missing_report_artifact"
    assert case["role"] == "simulated_hard_gate"
    assert case["would_pass"] is False
    assert case["would_fail"] is True
    assert case["blocking"] is False


def test_phase12c_negative_gate_accepts_verifier_backed_failure(tmp_path):
    aggregate = tmp_path / "fake-guard-aggregate.json"
    summary = tmp_path / "summary.json"
    _write_aggregate(
        aggregate,
        case_id="fake_verification_artifact_guard",
        clean_success_rate=0.0,
        intended_path_observed_rate=1.0,
        stable_primary_failure_phase=False,
        most_common_blocker="verifier_failed",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--output",
            str(summary),
            str(aggregate),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(summary.read_text(encoding="utf-8"))
    [case] = payload["cases"]
    assert case["case_id"] == "fake_verification_artifact_guard"
    assert case["role"] == "simulated_negative_gate"
    assert case["would_pass"] is True
    assert case["would_fail"] is False
    assert case["evidence"]["verifier_backed_guard_evidence"] is True


def test_phase12c_current_phase12b_evidence_is_simulation_clean(tmp_path):
    report_dir = Path("docs/roadmap/reports/evals")
    summary = tmp_path / "summary.json"
    reports = [
        report_dir
        / "orchestrator-eval-v1-debug-import-error-repair-queue-20260601-010627-aggregate.json",
        report_dir
        / "orchestrator-eval-v1-missing-report-artifact-queue-20260601-005610-aggregate.json",
        report_dir
        / "orchestrator-eval-v1-fake-verification-artifact-guard-queue-20260601-010941-aggregate.json",
        report_dir
        / "orchestrator-eval-v1-stale-replace-repair-queue-20260601-005610-aggregate.json",
    ]

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--output",
            str(summary),
            *[str(report) for report in reports],
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["mode"] == "simulation"
    assert payload["simulated_failure_count"] == 0
    assert payload["blocking_failure_count"] == 0
    roles = {case["case_id"]: case["role"] for case in payload["cases"]}
    assert roles == {
        "debug_import_error_repair": "simulated_hard_gate",
        "missing_report_artifact": "simulated_hard_gate",
        "fake_verification_artifact_guard": "simulated_negative_gate",
        "stale_replace_repair": "diagnostic_only",
    }


def test_phase12c_stability_summary_marks_warning_candidates_and_flaky_cases(
    tmp_path,
):
    reports: list[Path] = []
    for index in range(3):
        aggregate = tmp_path / f"missing-{index}.json"
        _write_aggregate(
            aggregate,
            case_id="missing_report_artifact",
            clean_success_rate=1.0,
            intended_path_observed_rate=1.0,
        )
        reports.append(aggregate)

    for index, clean_rate in enumerate((2 / 3, 0.0, 2 / 3)):
        aggregate = tmp_path / f"debug-{index}.json"
        _write_aggregate(
            aggregate,
            case_id="debug_import_error_repair",
            clean_success_rate=clean_rate,
            intended_path_observed_rate=1.0,
        )
        reports.append(aggregate)

    for index in range(3):
        aggregate = tmp_path / f"stale-{index}.json"
        _write_aggregate(
            aggregate,
            case_id="stale_replace_repair",
            clean_success_rate=1 / 3,
            intended_path_observed_rate=2 / 3,
        )
        reports.append(aggregate)

    summary = tmp_path / "summary.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--min-evidence-sets",
            "3",
            "--output",
            str(summary),
            *[str(report) for report in reports],
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "optional_warning_candidates=1" in result.stdout
    payload = json.loads(summary.read_text(encoding="utf-8"))
    stability = payload["stability"]
    assert stability["min_evidence_sets"] == 3
    assert stability["optional_warning_candidate_count"] == 1
    assert stability["flaky_case_count"] == 1

    by_case = {case["case_id"]: case for case in stability["cases"]}
    assert by_case["missing_report_artifact"]["optional_warning_candidate"] is True
    assert by_case["missing_report_artifact"]["promotion_ready"] is False
    assert by_case["debug_import_error_repair"]["flaky"] is True
    assert by_case["debug_import_error_repair"]["simulated_fail_count"] == 1
    assert by_case["stale_replace_repair"]["role"] == "diagnostic_only"
    assert by_case["stale_replace_repair"]["optional_warning_candidate"] is False


def test_phase12c_summary_output_alias_writes_the_same_artifact(tmp_path):
    aggregate = tmp_path / "missing-report-aggregate.json"
    summary = tmp_path / "summary.json"
    _write_aggregate(
        aggregate,
        case_id="missing_report_artifact",
        clean_success_rate=1.0,
        intended_path_observed_rate=1.0,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--summary-output",
            str(summary),
            str(aggregate),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["case_count"] == 1
    assert payload["cases"][0]["case_id"] == "missing_report_artifact"
