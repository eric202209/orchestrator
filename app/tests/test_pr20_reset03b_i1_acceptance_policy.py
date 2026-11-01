"""PR20-RESET-03B-I1: approved acceptance policy corrections (O-1, O-2, E-2).

- O-1: a reused focused pytest failure emits the same non-repairable
  ``task_acceptance_pytest_failed`` finding as an independently executed one.
- O-2 (Policy B): a recognized acceptance section without any admitted or
  refused command emits a non-repairable *warning*
  ``task_acceptance_no_verifiable_commands``; a missing section is unchanged.
- E-2: a reused focused timeout carries ``infrastructure_failure: "timeout"``.

All tests are offline: temporary workspaces, the real Validator and real local
pytest subprocesses.  Each fixture test appends to ``tests/runs.log`` so the
number of real executions is observed, not inferred from ``commands_run``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services.orchestration.state.persistence import _task_acceptance_evidence
from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_pr20_reset03a_acceptance_runner_failures import _project_python

NO_VERIFIABLE = "task_acceptance_no_verifiable_commands"
REUSED = "- `python -m pytest tests/test_x.py` passes.\n"
# ``-q`` changes the argv, so the same failing test runs independently.
INDEPENDENT = "- `python -m pytest -q tests/test_x.py` passes.\n"
TEST_BODY = (
    "import pathlib\n"
    "import time\n\n\n"
    "def test_x():\n"
    "    log = pathlib.Path(__file__).with_name('runs.log')\n"
    "    with open(log, 'a', encoding='utf-8') as handle:\n"
    "        handle.write('run\\n')\n"
    "{body}"
)
BODIES = {
    "pass": "    assert True\n",
    "fail": "    assert False\n",
    "timeout": "    time.sleep(30)\n",
}
UNVERIFIABLE_SECTIONS = {
    "empty": "",
    "prose_only": "- All tests pass and the API returns 200.\n",
    "non_pytest_command": "- `make test` passes.\n",
    "fenced_only": "```\npython -m pytest tests/test_x.py\n```\n",
}


def _project(tmp_path: Path, test: str = "pass", python: str = "ok") -> Path:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text(
        TEST_BODY.format(body=BODIES[test]), encoding="utf-8"
    )
    (root / "mod.py").write_text("X = 1\n", encoding="utf-8")
    _project_python(root, python)
    return root


def _runs(root: Path) -> int:
    log = root / "tests" / "runs.log"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def _prompt(criteria: str | None) -> str:
    if criteria is None:
        return "Set X in mod.py."
    return f"Set X in mod.py.\n\nAcceptance criteria:\n{criteria}"


def _candidate(root, criteria, *, files=("tests/test_x.py",), enabled=True, t=60):
    return candidate_checks.validate_candidate_delta(
        project_dir=root,
        change_set={"modified_files": list(files)},
        plan=[{"step_number": 1, "description": "x", "expected_files": list(files)}],
        task_prompt=_prompt(criteria),
        include_static_checks=False,
        timeout_seconds=t,
        task_acceptance_enabled=enabled,
    )


def _complete(root, criteria, *, files=("tests/test_x.py",), enabled=True):
    evidence = {
        "candidate_delta_required": True,
        "run_candidate_checks": True,
        "include_static_checks": False,
        "summary_generated": True,
        "execution_results_count": 1,
        "reported_changed_files": list(files),
        "change_set": {
            "added_files": [],
            "modified_files": list(files),
            "deleted_files": [],
        },
    }
    if enabled:
        evidence["run_task_acceptance_checks"] = True
    return ValidatorService.validate_task_completion(
        project_dir=root,
        plan=[{"step_number": 1, "description": "x", "expected_files": list(files)}],
        task_prompt=_prompt(criteria),
        execution_profile="full_lifecycle",
        completion_evidence=evidence,
    )


def _ids(findings) -> list[str]:
    return [finding.rule_id for finding in findings]


def _by_id(findings, rule_id):
    [finding] = [f for f in findings if f.rule_id == rule_id]
    return finding


def _classification(finding) -> tuple:
    return (
        finding.rule_id,
        finding.source,
        finding.category,
        finding.severity,
        finding.attribution,
        finding.repairable,
    )


# --- 1-4: reused versus independent focused pytest results -------------------


def test_reused_focused_success_has_no_acceptance_finding(tmp_path):
    root = _project(tmp_path, "pass")

    run = _candidate(root, REUSED)

    [result] = run.task_acceptance["results"]
    assert result["covered_by"] == "focused_test_selection"
    assert (result["executed"], result["returncode"], result["outcome"]) == (
        True,
        0,
        "passed",
    )
    assert "infrastructure_failure" not in result
    assert run.findings == ()
    assert run.commands_run == (run.selection.command,)
    assert _runs(root) == 1


def test_reused_focused_failure_emits_acceptance_failure_without_rerun(tmp_path):
    root = _project(tmp_path, "fail")

    run = _candidate(root, REUSED)

    assert run.commands_run == (run.selection.command,)
    assert _runs(root) == 1, "the reused command must not be executed again"
    [result] = run.task_acceptance["results"]
    assert result["covered_by"] == "focused_test_selection"
    assert result["executed"] is True
    assert result["returncode"] not in (0, None)
    assert result["outcome"] == "failed"
    assert _ids(run.findings) == [
        "focused_pytest_failed",
        "task_acceptance_pytest_failed",
    ]
    focused = _by_id(run.findings, "focused_pytest_failed")
    assert focused.repairable is True
    acceptance = _by_id(run.findings, "task_acceptance_pytest_failed")
    assert (acceptance.category, acceptance.repairable) == ("test", False)
    assert acceptance.evidence["covered_by"] == "focused_test_selection"
    assert acceptance.evidence["returncode"] == result["returncode"]
    assert acceptance.evidence["returncode"] == focused.evidence["returncode"]
    assert acceptance.evidence["command"] == run.selection.command
    assert acceptance.evidence["task_command"] == "python -m pytest tests/test_x.py"


def test_independent_acceptance_failure_is_executed_and_rejected(tmp_path):
    root = _project(tmp_path, "fail")

    run = _candidate(root, INDEPENDENT)

    assert len(run.commands_run) == 2
    assert _runs(root) == 2
    [result] = run.task_acceptance["results"]
    assert "covered_by" not in result
    assert result["outcome"] == "failed"
    acceptance = _by_id(run.findings, "task_acceptance_pytest_failed")
    assert acceptance.repairable is False
    assert "covered_by" not in acceptance.evidence


def test_reused_and_independent_failures_classify_identically(tmp_path):
    reused_root = _project(tmp_path / "reused", "fail")
    independent_root = _project(tmp_path / "independent", "fail")

    reused = _complete(reused_root, REUSED)
    independent = _complete(independent_root, INDEPENDENT)

    assert [_classification(f) for f in reused.findings] == [
        _classification(f) for f in independent.findings
    ]
    assert reused.status == independent.status == "rejected"
    assert _ids(reused.repairable_findings) == _ids(independent.repairable_findings)
    assert reused.accepted is independent.accepted is False


# --- 5: reused focused timeout ------------------------------------------------


def test_reused_focused_timeout_is_labelled_without_duplicate_finding(tmp_path):
    root = _project(tmp_path, "timeout")

    run = _candidate(root, REUSED, t=3)

    assert run.commands_run == (run.selection.command,)
    assert _runs(root) == 1
    [result] = run.task_acceptance["results"]
    assert result["covered_by"] == "focused_test_selection"
    assert result["returncode"] is None
    assert result["outcome"] == "infrastructure_failure"
    assert result["infrastructure_failure"] == "timeout"
    assert _ids(run.findings) == ["focused_pytest_infrastructure_failure"]

    projected = _task_acceptance_evidence(
        {
            "stage": "task_completion",
            "status": "rejected",
            "details": {"task_acceptance_verification": run.task_acceptance},
        },
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )
    [item] = projected["results"]
    assert item["infrastructure_failure"] == "timeout"
    assert item["covered_by"] == "focused_test_selection"
    assert item["returncode"] is None


def test_reused_focused_timeout_still_rejects_through_validator(tmp_path, monkeypatch):
    root = _project(tmp_path, "timeout")
    monkeypatch.setitem(
        candidate_checks.validate_candidate_delta.__kwdefaults__, "timeout_seconds", 3
    )

    verdict = _complete(root, REUSED)

    assert verdict.status == "rejected"
    assert verdict.accepted is False
    assert verdict.repairable_findings == []
    [result] = verdict.details["task_acceptance_verification"]["results"]
    assert result["infrastructure_failure"] == "timeout"


# --- 6-9: recognized section without verifiable commands ----------------------


@pytest.mark.parametrize(
    "criteria", UNVERIFIABLE_SECTIONS.values(), ids=list(UNVERIFIABLE_SECTIONS)
)
def test_recognized_section_without_commands_warns(tmp_path, criteria):
    root = _project(tmp_path, "pass")

    run = _candidate(root, criteria, files=("mod.py",))

    acceptance = run.task_acceptance
    assert acceptance["section_found"] is True
    assert (acceptance["admitted"], acceptance["refused"]) == ([], [])
    assert acceptance["results"] == []
    [finding] = run.findings
    assert finding.rule_id == NO_VERIFIABLE
    assert finding.source == "task_acceptance"
    assert finding.category == "task_contract"
    assert (finding.severity, finding.repairable) == ("warning", False)
    assert finding.evidence == {
        "section_found": True,
        "extracted_count": 0,
        "admitted_count": 0,
        "refused_count": 0,
        "ignored_count": len(acceptance["ignored"]),
    }
    assert "pass" not in finding.message.lower()
    assert "not verified" in finding.message


@pytest.mark.parametrize(
    "criteria", UNVERIFIABLE_SECTIONS.values(), ids=list(UNVERIFIABLE_SECTIONS)
)
def test_no_verifiable_commands_warning_keeps_completion_eligible(tmp_path, criteria):
    root = _project(tmp_path, "pass")

    verdict = _complete(root, criteria, files=("mod.py",))
    control = _complete(root, criteria, files=("mod.py",), enabled=False)

    assert NO_VERIFIABLE in _ids(verdict.findings)
    assert NO_VERIFIABLE not in _ids(control.findings)
    assert verdict.status == "warning"
    assert verdict.accepted is True
    assert verdict.repairable_findings == []
    message = _by_id(verdict.findings, NO_VERIFIABLE).message
    assert message in verdict.reasons


def test_non_pytest_command_is_counted_as_ignored(tmp_path):
    root = _project(tmp_path, "pass")

    run = _candidate(
        root, UNVERIFIABLE_SECTIONS["non_pytest_command"], files=("mod.py",)
    )

    assert run.findings[0].evidence["ignored_count"] == 1
    assert run.commands_run == ()


# --- 10-12: compatibility -----------------------------------------------------


def test_missing_section_keeps_legacy_behavior(tmp_path):
    root = _project(tmp_path, "pass")

    enabled = _complete(root, None, files=("mod.py",))
    disabled = _complete(root, None, files=("mod.py",), enabled=False)

    assert enabled.details["task_acceptance_verification"]["section_found"] is False
    assert NO_VERIFIABLE not in _ids(enabled.findings)
    assert _ids(enabled.findings) == _ids(disabled.findings)
    assert enabled.status == disabled.status
    assert enabled.accepted is disabled.accepted is True


def test_refused_command_is_rejected_not_warned(tmp_path):
    root = _project(tmp_path, "pass")

    verdict = _complete(
        root, "- `pytest -k slow tests/test_x.py` passes.\n", files=("mod.py",)
    )

    # mod.py has no focused test: the pre-existing "Source mutation verification
    # is insufficient" warning (candidate_warning_1) is unrelated to acceptance.
    acceptance = [f for f in verdict.findings if f.source == "task_acceptance"]
    assert _ids(acceptance) == ["task_acceptance_command_refused"]
    refused = acceptance[0]
    assert (refused.category, refused.repairable) == ("task_contract", False)
    assert verdict.status == "rejected"
    assert verdict.accepted is False


def test_flag_disabled_emits_no_acceptance_findings(tmp_path):
    root = _project(tmp_path, "fail")

    run = _candidate(root, REUSED, enabled=False)
    empty = _candidate(root, "", files=("mod.py",), enabled=False)

    assert run.task_acceptance is None and empty.task_acceptance is None
    assert _ids(run.findings) == ["focused_pytest_failed"]
    assert empty.findings == ()
    assert (
        Settings.model_fields["TASK_ACCEPTANCE_VERIFICATION_ENABLED"].default is False
    )


# --- 13: focused process-start failure is never reused ------------------------


def test_unstarted_focused_run_is_not_reused_or_relabelled(tmp_path):
    root = _project(tmp_path, "pass", python="missing_interpreter")

    run = _candidate(root, REUSED)

    [result] = run.task_acceptance["results"]
    assert "covered_by" not in result
    assert result["executed"] is False
    assert result["infrastructure_failure"] == "process_start_failure"
    assert _ids(run.findings) == [
        "focused_pytest_infrastructure_failure",
        "task_acceptance_pytest_infrastructure_failure",
    ]
    assert _runs(root) == 0


# --- 14: co-occurring findings: rejected, repair-eligible, not completable ----


def test_reused_failure_rejects_but_stays_repair_eligible_until_revalidated(tmp_path):
    root = _project(tmp_path, "fail")

    failing = _complete(root, REUSED)

    assert failing.status == "rejected"
    assert failing.accepted is False
    # Repair is driven by the separate repairable focused finding only.
    assert _ids(failing.repairable_findings) == ["focused_pytest_failed"]
    acceptance = _by_id(failing.findings, "task_acceptance_pytest_failed")
    assert acceptance.repairable is False
    assert acceptance.message in failing.reasons

    # A repaired candidate completes only through a fresh, passing validation.
    (root / "tests" / "test_x.py").write_text(
        TEST_BODY.format(body=BODIES["pass"]), encoding="utf-8"
    )
    repaired = _complete(root, REUSED)

    assert "task_acceptance_pytest_failed" not in _ids(repaired.findings)
    [result] = repaired.details["task_acceptance_verification"]["results"]
    assert (result["covered_by"], result["outcome"]) == (
        "focused_test_selection",
        "passed",
    )
    assert repaired.accepted is True
