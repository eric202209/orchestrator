"""Phase 30C, Program 4 — Evidence Harness Hardening self-test pack.

Permanent regression tests proving: invalid evidence is detected, missing
required fields are detected, optional fields are accepted, deterministic
replay is stable, and regression comparison is stable. These exercise the
eval-harness tooling only (scripts/evals + scripts/maintenance) — no
production execution/planning/orchestration code is touched.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "evals" / "scenario_contract.py").is_file():
            return parent
    pytest.skip("repo scripts not present", allow_module_level=True)


def _load_module(relative_path: str, name: str):
    path = _repo_root() / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = _load_module(
    "scripts/maintenance/score_orchestrator_eval_case.py",
    "score_orchestrator_eval_case",
)
# Reuse the exact module instance the scorer already loaded (rather than a
# second independent importlib load) so ScenarioContractError raised inside
# scorer.replay_case is catchable via isinstance here.
scenario_contract = scorer.scenario_contract


VALID_CASE = {
    "case_id": "python_cli_small_feature",
    "category": "baseline_success",
    "purpose": "Control case.",
    "verifier": {"command": "true", "timeout_seconds": 5},
    "required_files": [],
    "forbidden_existing_files": [],
    "allowed_touched_prefixes": [],
    "forbidden_touched_prefixes": [],
    "required_events": ["task_started"],
}


def _event(event_id: str, timestamp: str, event_type: str = "task_started") -> dict:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "session_id": 1,
        "task_id": 1,
        "parent_event_id": None,
        "details": {},
    }


# ---------------------------------------------------------------------------
# Scenario contract validation — required / optional / unknown fields
# ---------------------------------------------------------------------------


def test_validate_case_contract_accepts_valid_case():
    result = scenario_contract.validate_case_contract(VALID_CASE)
    assert result["valid"] is True
    assert result["missing_required"] == []
    assert result["unknown_fields"] == []


def test_validate_case_contract_detects_missing_required_field():
    case = dict(VALID_CASE)
    del case["category"]
    result = scenario_contract.validate_case_contract(case)
    assert result["valid"] is False
    assert "category" in result["missing_required"]


def test_validate_case_contract_detects_missing_verifier_command():
    case = copy.deepcopy(VALID_CASE)
    case["verifier"] = {"timeout_seconds": 5}
    result = scenario_contract.validate_case_contract(case)
    assert result["valid"] is False
    assert "verifier.command" in result["missing_required"]


def test_validate_case_contract_flags_unknown_field_without_failing():
    case = dict(VALID_CASE)
    case["totally_unrecognized_field"] = True
    result = scenario_contract.validate_case_contract(case)
    assert result["valid"] is True
    assert result["unknown_fields"] == ["totally_unrecognized_field"]


def test_validate_case_contract_accepts_documented_optional_fields():
    case = dict(VALID_CASE)
    case["success_criteria"] = ["verifier_exit_code_zero"]
    case["workflow_stage"] = "implementation"
    result = scenario_contract.validate_case_contract(case)
    assert result["valid"] is True
    assert result["unknown_fields"] == []


def test_enforce_case_contract_raises_on_missing_required():
    case = dict(VALID_CASE)
    del case["required_events"]
    with pytest.raises(scenario_contract.ScenarioContractError):
        scenario_contract.enforce_case_contract(case)


def test_enforce_case_contract_silent_on_valid_case():
    scenario_contract.enforce_case_contract(VALID_CASE)  # must not raise


def test_validate_event_record_detects_missing_required_field():
    record = _event("e1", "2026-07-24T00:00:00+00:00")
    del record["timestamp"]
    result = scenario_contract.validate_event_record(record)
    assert result["valid"] is False
    assert "timestamp" in result["missing_required"]


def test_validate_event_record_accepts_optional_fields():
    record = _event("e1", "2026-07-24T00:00:00+00:00")
    result = scenario_contract.validate_event_record(record)
    assert result["valid"] is True
    assert result["unknown_fields"] == []


def test_validate_event_sequence_detects_out_of_order_timestamps():
    records = [
        _event("e1", "2026-07-24T00:00:02+00:00"),
        _event("e2", "2026-07-24T00:00:01+00:00"),
    ]
    result = scenario_contract.validate_event_sequence(records)
    assert result["valid"] is False
    assert result["out_of_order"] == [1]


def test_validate_event_sequence_detects_unparseable_timestamp():
    records = [_event("e1", "not-a-timestamp")]
    result = scenario_contract.validate_event_sequence(records)
    assert result["valid"] is False
    assert result["unparseable_timestamps"] == [0]


def test_validate_event_sequence_accepts_monotonic_timestamps():
    records = [
        _event("e1", "2026-07-24T00:00:01+00:00"),
        _event("e2", "2026-07-24T00:00:02+00:00"),
    ]
    result = scenario_contract.validate_event_sequence(records)
    assert result["valid"] is True


def test_validate_event_records_missing_evidence_is_explicit_not_silent():
    records = [
        {"event_type": "task_started"}
    ]  # missing event_id/timestamp/session/task
    result = scenario_contract.validate_event_records(records)
    assert result["valid"] is False
    assert result["record_violations"][0]["index"] == 0
    assert "event_id" in result["record_violations"][0]["missing_required"]


# ---------------------------------------------------------------------------
# Deterministic replay
# ---------------------------------------------------------------------------


def _replay(tmp_path: Path, events: list[dict]) -> dict:
    manifest = {"benchmark_id": "test", "baseline_label": "test", "schema_version": 1}
    verifier = {"available": True, "command": "true", "passed": True, "exit_code": 0}
    return scorer.replay_case(
        manifest_path=tmp_path / "manifest.json",
        manifest=manifest,
        case=VALID_CASE,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        events=events,
        snapshots=[],
        verifier=verifier,
    )


def test_deterministic_replay_stable_for_identical_evidence(tmp_path):
    events = [_event("e1", "2026-07-24T00:00:01+00:00")]
    report_a = _replay(tmp_path, events)
    report_b = _replay(tmp_path, events)
    comparison = scenario_contract.compare_reports(report_a, report_b)
    assert comparison["match"] is True
    assert comparison["differences"] == []


def test_deterministic_replay_rejects_case_missing_required_field(tmp_path):
    manifest = {"benchmark_id": "test", "baseline_label": "test", "schema_version": 1}
    broken_case = dict(VALID_CASE)
    del broken_case["required_events"]
    with pytest.raises(scenario_contract.ScenarioContractError):
        scorer.replay_case(
            manifest_path=tmp_path / "manifest.json",
            manifest=manifest,
            case=broken_case,
            project_dir=tmp_path,
            session_id=1,
            task_id=1,
            events=[],
            snapshots=[],
            verifier={"available": False},
        )


# ---------------------------------------------------------------------------
# Regression comparison
# ---------------------------------------------------------------------------


def test_compare_reports_stable_for_identical_reports(tmp_path):
    events = [_event("e1", "2026-07-24T00:00:01+00:00", "task_started")]
    report = _replay(tmp_path, events)
    comparison = scenario_contract.compare_reports(report, report)
    assert comparison["match"] is True


def test_compare_reports_flags_decision_regression(tmp_path):
    events_ok = [_event("e1", "2026-07-24T00:00:01+00:00", "task_started")]
    events_regressed = [_event("e1", "2026-07-24T00:00:01+00:00", "unrelated_event")]
    baseline = _replay(tmp_path, events_ok)
    candidate = _replay(tmp_path, events_regressed)
    comparison = scenario_contract.compare_reports(baseline, candidate)
    assert comparison["match"] is False
    paths = {diff["path"] for diff in comparison["differences"]}
    assert any(
        path.startswith("result.") or path.startswith("required_events.")
        for path in paths
    )


def test_compare_reports_ignores_generated_at_and_env_summary():
    baseline = {
        "generated_at": "t1",
        "env_summary": {"git_sha": "aaa"},
        "result": {"ok": True},
    }
    candidate = {
        "generated_at": "t2",
        "env_summary": {"git_sha": "bbb"},
        "result": {"ok": True},
    }
    comparison = scenario_contract.compare_reports(baseline, candidate)
    assert comparison["match"] is True
