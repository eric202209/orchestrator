from __future__ import annotations

import copy

from scripts.benchmark.attribution import (
    observed_mechanisms,
    root_cause,
    upgrade_to_v2,
)
from scripts.benchmark.evidence_loader import _empty_attempt
from scripts.benchmark.schema import (
    ATTEMPT_SCHEMA_VERSION,
    ATTEMPT_SCHEMA_V2_VERSION,
    validate_attempt,
    validate_case,
)


def _record(stage: str = "execution") -> dict:
    record = _empty_attempt(
        "sample",
        {"phase": "phase40d3", "source_identity": {}, "provider": {}},
        {"slot": "sample-1"},
    )
    record["attempted"] = True
    record["not_attempted_reason"] = None
    record["reliability"].update(
        failure_stage=stage,
        independently_accepted=False,
        execution_authority_violation=False,
    )
    record["execution"].update(
        reached=True,
        source_mutation_captured=False,
        debug_repair_count=0,
        debug_repair_rejections=[],
    )
    record["acceptance"].update(
        attempted=False,
        result=None,
        source_paths=[],
        evidence_ref=None,
    )
    return record


def _case() -> dict:
    return {"baseline": {"expected_failed_tests": ["test_one", "test_two"]}}


def test_wrong_file_requires_explicit_target_contract():
    record = _record()
    record["acceptance"]["source_paths"] = ["src/unrelated.py"]
    case = _case()
    case["target_contract"] = {
        "source_paths": ["src/required.py"],
        "basis": "verified task contract",
    }

    mechanisms = observed_mechanisms(record, case)

    assert {
        "mechanism": "candidate_outside_declared_target",
        "evidence": "case.target_contract",
    } in mechanisms
    assert root_cause(record, mechanisms)["attribution"] == "model_output"
    assert (
        validate_case(
            {
                "schema_version": "benchmark.case.v1",
                "case_id": "sample",
                "title": "sample",
                "status": "complete",
                "incomplete_reasons": [],
                "original_task": {
                    "title": "sample",
                    "description": "sample task",
                    "origin_task_id": 1,
                    "evidence_ref": None,
                },
                "seed": {
                    "repository": "sample",
                    "commit": "a" * 40,
                    "tree": "b" * 40,
                    "evidence_ref": None,
                },
                "baseline": {
                    "command": "pytest",
                    "expected_failed_tests": [],
                    "expected_failed": 0,
                    "expected_passed": 1,
                    "evidence_ref": None,
                },
                "acceptance": {
                    "command": "pytest",
                    "requires_unchanged_tests": True,
                },
                "authorized_source_paths": ["src/"],
                "protected_test_paths": ["tests/"],
                "behavioral_contract": "sample",
                "historical_evidence_refs": [],
                "target_contract": case["target_contract"],
            }
        )
        == []
    )


def test_verification_and_acceptance_failures_leave_root_cause_unknown(tmp_path):
    verification = _record("verification")
    verification["reliability"]["verification_failure"] = ["test_failure"]
    mechanisms = observed_mechanisms(verification, _case())
    assert "verification_failed:test_failure" in {
        item["mechanism"] for item in mechanisms
    }
    assert root_cause(verification, mechanisms)["attribution"] == "unknown"

    acceptance = _record("acceptance")
    acceptance["acceptance"].update(
        attempted=True,
        result="failed",
        evidence_ref="acceptance.txt",
    )
    (tmp_path / "acceptance.txt").write_text(
        "FAILED tests/test_sample.py::test_two - assertion failed\n",
        encoding="utf-8",
    )
    mechanisms = observed_mechanisms(
        acceptance, _case(), acceptance_text=(tmp_path / "acceptance.txt").read_text()
    )
    names = {item["mechanism"] for item in mechanisms}
    assert "independent_acceptance_failed" in names
    assert "candidate_partially_resolved_baseline_failures" in names
    assert root_cause(acceptance, mechanisms)["attribution"] == "unknown"


def test_debug_budget_exhaustion_is_observed_but_not_attributed():
    record = _record()
    record["execution"].update(
        debug_repair_count=1,
        debug_repair_rejections=["debug_repair_budget_exhausted"],
    )

    mechanisms = observed_mechanisms(record, _case())

    assert {item["mechanism"] for item in mechanisms} == {
        "debug_repair_attempted",
        "debug_repair_budget_exhausted",
    }
    result = root_cause(record, mechanisms)
    assert result["attribution"] == "unknown"
    assert result["confidence"] == "unproven"


def test_multiple_observed_mechanisms_are_kept_separate():
    record = _record("verification")
    record["reliability"].update(
        verification_failure=["test_failure"], safety_rejections=["task_scope"]
    )
    record["execution"].update(
        debug_repair_count=1,
        debug_repair_rejections=["debug_repair_budget_exhausted"],
    )

    mechanisms = observed_mechanisms(record, _case())

    assert {item["mechanism"] for item in mechanisms} == {
        "safety_rejection_observed:task_scope",
        "verification_failed:test_failure",
        "debug_repair_attempted",
        "debug_repair_budget_exhausted",
    }
    assert root_cause(record, mechanisms)["attribution"] == "unknown"


def test_unproven_failure_stays_unknown():
    record = _record("execution")

    result = root_cause(record, observed_mechanisms(record, _case()))

    assert result == {
        "attribution": "unknown",
        "basis": "unproven:no_mechanism_observed",
        "confidence": "unproven",
        "evidence_refs": [],
    }


def test_model_safety_rejection_and_reported_application_defect_differ():
    rejected = _record("planning_validation")
    rejected["reliability"]["terminal_safety_rejections"] = ["task_scope"]
    assert (
        root_cause(rejected, observed_mechanisms(rejected, _case()))["attribution"]
        == "model_output"
    )

    defect = _record("execution")
    defect["reliability"]["execution_authority_violation"] = True
    assert (
        root_cause(defect, observed_mechanisms(defect, _case()))["attribution"]
        == "application_defect"
    )


def test_v1_records_remain_valid_and_upgrade_to_v2_without_mutation(tmp_path):
    original = _record("execution")
    original["attempted"] = False
    original["not_attempted_reason"] = "infrastructure: unavailable"
    original["reliability"]["failure_stage"] = "not_attempted"
    original["reliability"]["independently_accepted"] = None
    assert original["schema_version"] == ATTEMPT_SCHEMA_VERSION
    assert validate_attempt(original) == []

    upgraded = upgrade_to_v2(original, _case(), repo_root=tmp_path)

    assert upgraded["schema_version"] == ATTEMPT_SCHEMA_V2_VERSION
    assert validate_attempt(upgraded) == []
    assert original["schema_version"] == ATTEMPT_SCHEMA_VERSION
    assert "root_cause" not in original["reliability"]
    broken_v1 = copy.deepcopy(original)
    broken_v1["reliability"]["root_cause"] = {}
    assert validate_attempt(broken_v1)
