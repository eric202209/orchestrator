"""PR20-RESET-03C: Validator runner process-start failures are typed findings.

A focused pytest, static check or ``git diff --check`` whose process cannot be
started becomes a non-repairable infrastructure finding instead of an
``OSError`` escaping ``validate_candidate_delta``.  A temporary-directory
cleanup failure after a completed run keeps the real return code.  All tests
are offline: temporary workspaces, real local subprocesses.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_pr20_reset03a_acceptance_runner_failures import (
    ACCEPTANCE_PROMPT,
    START_FAILURES,
    _project_python,
    _run_acceptance,
)


def _project(tmp_path: Path, python_kind: str, *, style_gates: bool = False) -> Path:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    if style_gates:
        (root / "pyproject.toml").write_text("[tool.black]\n", encoding="utf-8")
        (root / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    _project_python(root, python_kind)
    return root


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


def _validate(root: Path, files: list[str], **kwargs):
    return candidate_checks.validate_candidate_delta(
        project_dir=root,
        change_set={"modified_files": files},
        plan=[{"step_number": 1, "description": "x", "expected_files": files}],
        task_prompt=kwargs.pop("task_prompt", "Fix it."),
        timeout_seconds=60,
        **kwargs,
    )


def _assert_start_failure(finding, *, rule_id: str, source: str, errno_name: str):
    assert finding.rule_id == rule_id
    assert finding.source == source
    assert finding.category == "infrastructure"
    assert (finding.severity, finding.attribution) == ("error", "unknown")
    assert finding.repairable is False
    assert finding.evidence["returncode"] is None
    assert finding.evidence["output"] == ""
    assert finding.evidence["infrastructure_failure"] == "process_start_failure"
    assert finding.evidence["error"]["errno"] == errno_name
    assert set(finding.evidence["error"]) == {"type", "errno"}


@pytest.mark.parametrize("kind, errno_name", START_FAILURES)
def test_focused_pytest_start_failure_is_infrastructure_finding(
    tmp_path, kind, errno_name
):
    root = _project(tmp_path, kind)

    run = _validate(root, ["tests/test_ok.py"], include_static_checks=False)

    assert run.selection.command
    assert run.commands_run == (run.selection.command,)
    [finding] = run.findings
    _assert_start_failure(
        finding,
        rule_id="focused_pytest_infrastructure_failure",
        source="pytest",
        errno_name=errno_name,
    )
    assert finding.evidence["command"] == run.selection.command
    # Never str(exc): no shebang target or cwd path in the error evidence.
    assert str(tmp_path) not in json.dumps(finding.evidence["error"])


def test_unstarted_focused_run_is_not_reused_as_acceptance_evidence(tmp_path):
    root = _project(tmp_path, "missing_interpreter")
    prompt = (
        "Fix.\n\nAcceptance criteria:\n"
        "- `python -m pytest tests/test_ok.py` passes.\n"
    )

    run = _validate(
        root,
        ["tests/test_ok.py"],
        include_static_checks=False,
        task_prompt=prompt,
        task_acceptance_enabled=True,
    )

    [result] = run.task_acceptance["results"]
    assert result["executed_command"] == run.selection.command
    assert "covered_by" not in result
    assert result["executed"] is False
    assert result["infrastructure_failure"] == "process_start_failure"
    assert [f.rule_id for f in run.findings] == [
        "focused_pytest_infrastructure_failure",
        "task_acceptance_pytest_infrastructure_failure",
    ]


@pytest.mark.parametrize("kind, errno_name", START_FAILURES)
def test_static_check_start_failures_keep_source_identity(tmp_path, kind, errno_name):
    root = _project(tmp_path, kind, style_gates=True)

    run = _validate(root, ["mod.py"], include_static_checks=True)

    assert run.selection.command == ""
    assert [command.split()[2] for command in run.commands_run] == [
        "compileall",
        "black",
        "flake8",
    ]
    assert [f.rule_id for f in run.findings] == [
        "compileall_infrastructure_failure",
        "black_infrastructure_failure",
        "flake8_infrastructure_failure",
    ]
    for finding, command in zip(run.findings, run.commands_run):
        _assert_start_failure(
            finding,
            rule_id=finding.rule_id,
            source=finding.rule_id.removesuffix("_infrastructure_failure"),
            errno_name=errno_name,
        )
        assert finding.evidence["command"] == command
        assert finding.evidence["paths"] == ["mod.py"]


def test_git_diff_check_start_failure_is_distinct_from_diff_failure(
    tmp_path, monkeypatch
):
    root = _project(tmp_path, "ok")
    _git_init(root)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    # git is resolved through PATH; an empty PATH makes it unstartable.
    monkeypatch.setenv("PATH", str(empty_bin))

    run = _validate(root, ["README.md"], include_static_checks=True)

    assert run.commands_run == ("git diff --check -- README.md",)
    [finding] = run.findings
    _assert_start_failure(
        finding,
        rule_id="git_diff_check_infrastructure_failure",
        source="git_diff_check",
        errno_name="ENOENT",
    )
    assert finding.evidence["command"] == "git diff --check -- README.md"
    assert finding.evidence["paths"] == ["README.md"]


def test_git_diff_check_real_failure_keeps_static_rule(tmp_path):
    root = _project(tmp_path, "ok")
    _git_init(root)
    (root / "README.md").write_text("trailing   \n", encoding="utf-8")

    run = _validate(root, ["README.md"], include_static_checks=True)

    [finding] = run.findings
    assert finding.rule_id == "candidate_diff_check_failed"
    assert (finding.category, finding.repairable) == ("static", True)
    assert finding.evidence["returncode"] not in (0, None)


def test_working_runners_are_compatibility_control(tmp_path):
    root = _project(tmp_path, "ok")
    _git_init(root)

    run = _validate(
        root,
        ["tests/test_ok.py", "mod.py"],
        include_static_checks=True,
        task_prompt=ACCEPTANCE_PROMPT,
        task_acceptance_enabled=True,
    )

    assert run.findings == ()
    assert len(run.commands_run) == 4
    assert [r["outcome"] for r in run.task_acceptance["results"]] == ["passed"]


@pytest.mark.parametrize(
    "marker", ["pytest", "compileall", "git"], ids=["focused", "static", "diff"]
)
def test_unexpected_runner_errors_are_not_converted(tmp_path, monkeypatch, marker):
    root = _project(tmp_path, "ok")
    _git_init(root)
    real_run = subprocess.run

    def _run(argv, *args, **kwargs):
        if marker in argv:
            raise RuntimeError("internal defect")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(candidate_checks.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="internal defect"):
        _validate(root, ["tests/test_ok.py", "mod.py"], include_static_checks=True)


def test_full_validator_with_broken_interpreter_rejects(tmp_path):
    root = _project(tmp_path, "missing_interpreter")
    _git_init(root)
    files = ["tests/test_ok.py", "mod.py"]

    verdict = ValidatorService.validate_task_completion(
        project_dir=root,
        plan=[{"step_number": 1, "description": "x", "expected_files": files}],
        task_prompt=ACCEPTANCE_PROMPT,
        execution_profile="full_lifecycle",
        completion_evidence={
            "candidate_delta_required": True,
            "run_candidate_checks": True,
            "include_static_checks": True,
            "run_task_acceptance_checks": True,
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": files,
            "change_set": {
                "added_files": [],
                "modified_files": files,
                "deleted_files": [],
            },
        },
    )

    assert verdict.status == "rejected"
    rule_ids = [f.rule_id for f in verdict.findings]
    for rule_id in (
        "focused_pytest_infrastructure_failure",
        "task_acceptance_pytest_infrastructure_failure",
        "compileall_infrastructure_failure",
    ):
        assert rule_id in rule_ids
    assert "Focused candidate pytest could not be started" in verdict.reasons
    [acceptance] = verdict.details["task_acceptance_verification"]["results"]
    assert acceptance["infrastructure_failure"] == "process_start_failure"


# --- F-02: temporary-directory cleanup after a completed run -----------------


def _cleanup_fails_after_removal(monkeypatch) -> None:
    """Make TemporaryDirectory cleanup report EBUSY through its real handler."""

    real_rmtree = tempfile._rmtree

    def _rmtree(path, onexc):
        real_rmtree(path, onexc=onexc)
        # shutil invokes onexc inside its except block; mirror that.
        try:
            raise OSError(errno.EBUSY, "Device or resource busy")
        except OSError as exc:
            onexc(os.rmdir, path, exc)

    monkeypatch.setattr(tempfile, "_rmtree", _rmtree)


def test_cleanup_failure_keeps_completed_acceptance_result(tmp_path, monkeypatch):
    root = _project(tmp_path, "ok")
    _cleanup_fails_after_removal(monkeypatch)

    acceptance, findings, _ = _run_acceptance(root)

    [result] = acceptance["results"]
    assert (result["executed"], result["returncode"]) == (True, 0)
    assert result["outcome"] == "passed"
    assert findings == []


def test_tempdir_creation_failure_is_still_a_start_failure(tmp_path, monkeypatch):
    root = _project(tmp_path, "ok")

    def _mkdtemp(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)

    run = _validate(root, ["tests/test_ok.py"], include_static_checks=False)

    [finding] = run.findings
    _assert_start_failure(
        finding,
        rule_id="focused_pytest_infrastructure_failure",
        source="pytest",
        errno_name="EACCES",
    )
