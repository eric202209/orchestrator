"""CORE-C2 — deterministic verification interpreter resolution.

Order: project `.venv`, project `venv`, validated Worker interpreter, failure.
No PATH lookup, no silent fallthrough past a broken project interpreter, and
no dependence on process cwd. Fake interpreters here are never executed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.orchestration.execution import python_resolution as pr
from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
)
from app.services.orchestration.execution.python_resolution import (
    ProjectPythonResolutionError,
    ResolvedPython,
    resolve_project_python,
    resolve_project_python_selection,
)
from app.services.orchestration.phases.completion_deterministic_repair import (
    attempt_deterministic_candidate_repair,
)
from app.services.orchestration.validation.candidate_checks import (
    validate_candidate_delta,
)
from app.tests.test_phase34_dcr1_deterministic_candidate_repair import (
    UNFORMATTED,
    _authority,
    _black_finding,
    _verdict,
)


def _interpreter(path: Path, *, executable: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _dangling_dot_venv(project_dir: Path) -> Path:
    link = project_dir / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(project_dir / "missing-python")
    return link


def test_dot_venv_has_priority_over_venv(tmp_path):
    dot_venv = _interpreter(tmp_path / ".venv" / "bin" / "python")
    _interpreter(tmp_path / "venv" / "bin" / "python")

    assert resolve_project_python_selection(tmp_path) == ResolvedPython(
        path=str(dot_venv), source="project_dot_venv"
    )


def test_venv_is_used_when_dot_venv_is_absent(tmp_path):
    venv = _interpreter(tmp_path / "venv" / "bin" / "python")

    assert resolve_project_python_selection(tmp_path) == ResolvedPython(
        path=str(venv), source="project_venv"
    )


def test_worker_interpreter_is_used_without_project_venv(tmp_path):
    assert resolve_project_python_selection(tmp_path) == ResolvedPython(
        path=sys.executable, source="worker"
    )
    assert resolve_project_python(tmp_path) == sys.executable


def test_valid_executable_symlink_is_accepted_without_resolving_it(tmp_path):
    target = _interpreter(tmp_path / "real" / "python3")
    link = tmp_path / "project" / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    resolved = resolve_project_python_selection(tmp_path / "project")

    assert resolved == ResolvedPython(path=str(link), source="project_dot_venv")


def test_dangling_symlink_fails_instead_of_falling_back(tmp_path):
    _dangling_dot_venv(tmp_path)
    _interpreter(tmp_path / "venv" / "bin" / "python")

    with pytest.raises(ProjectPythonResolutionError, match="dangling symlink"):
        resolve_project_python(tmp_path)


def test_non_executable_venv_interpreter_fails(tmp_path):
    _interpreter(tmp_path / "venv" / "bin" / "python", executable=False)

    with pytest.raises(ProjectPythonResolutionError, match="not an executable"):
        resolve_project_python(tmp_path)


def test_directory_at_interpreter_path_fails(tmp_path):
    (tmp_path / ".venv" / "bin" / "python").mkdir(parents=True)

    with pytest.raises(ProjectPythonResolutionError, match="not an executable"):
        resolve_project_python(tmp_path)


def test_relative_project_dir_is_rejected(tmp_path, monkeypatch):
    _interpreter(tmp_path / "project" / ".venv" / "bin" / "python")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ProjectPythonResolutionError, match="must be absolute"):
        resolve_project_python(Path("project"))


def test_resolution_is_independent_of_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    dot_venv = _interpreter(project / ".venv" / "bin" / "python")
    elsewhere = tmp_path / "elsewhere"
    _interpreter(elsewhere / ".venv" / "bin" / "python")

    monkeypatch.chdir(elsewhere)
    first = resolve_project_python_selection(project)
    monkeypatch.chdir(tmp_path)
    second = resolve_project_python_selection(project)

    assert first == second == ResolvedPython(str(dot_venv), "project_dot_venv")


@pytest.mark.parametrize("worker", ["", "python3", "/nonexistent/python3"])
def test_invalid_worker_interpreter_fails(tmp_path, monkeypatch, worker):
    monkeypatch.setattr(pr.sys, "executable", worker)

    with pytest.raises(ProjectPythonResolutionError, match="Worker interpreter"):
        resolve_project_python(tmp_path)


def test_non_executable_worker_interpreter_fails(tmp_path, monkeypatch):
    worker = _interpreter(tmp_path / "worker" / "python3", executable=False)
    monkeypatch.setattr(pr.sys, "executable", str(worker))

    with pytest.raises(ProjectPythonResolutionError, match="Worker interpreter"):
        resolve_project_python(tmp_path / "project")


def test_path_python3_is_never_selected(tmp_path, monkeypatch):
    path_python = _interpreter(tmp_path / "pathbin" / "python3")
    monkeypatch.setenv("PATH", str(path_python.parent))
    monkeypatch.setattr(pr.sys, "executable", "")

    with pytest.raises(ProjectPythonResolutionError):
        resolve_project_python(tmp_path / "project")


def test_resolution_logs_interpreter_provenance(tmp_path, caplog):
    with caplog.at_level("INFO", logger=pr.__name__):
        resolve_project_python(tmp_path)

    record = next(r for r in caplog.records if r.msg == "project_python_resolved")
    assert record.python_source == "worker"
    assert record.python_path == sys.executable
    assert record.project_dir == str(tmp_path.resolve())


def test_python_verification_failure_keeps_existing_response_shape(tmp_path):
    _dangling_dot_venv(tmp_path)

    result = execute_verification_command(
        project_dir=tmp_path, command="python -c 'print(1)'"
    )

    assert set(result) == {"success", "command", "returncode", "output"}
    assert result["success"] is False
    assert result["returncode"] == 2
    assert result["command"] == "python -c 'print(1)'"
    assert "dangling symlink" in result["output"]


def test_non_python_verification_is_unaffected_by_broken_venv(tmp_path):
    _dangling_dot_venv(tmp_path)

    result = execute_verification_command(
        project_dir=tmp_path, command="sh -c 'echo NON_PYTHON_OK'"
    )

    assert result["success"] is True
    assert result["returncode"] == 0
    assert "NON_PYTHON_OK" in result["output"]


def test_validator_reports_unavailable_python_as_finding(tmp_path):
    (tmp_path / "test_mod.py").write_text("def test_x():\n    pass\n")
    _dangling_dot_venv(tmp_path)

    run = validate_candidate_delta(
        project_dir=tmp_path,
        change_set={"added_files": ["test_mod.py"]},
        plan=[],
        task_prompt="task",
        observed_scope=("test_mod.py",),
        verification_scope=("test_mod.py",),
    )

    assert run.selection.command == ""
    assert run.selection.source == "candidate_python_unavailable"
    assert run.commands_run == ()
    assert [f.rule_id for f in run.findings] == ["candidate_python_unavailable"]
    finding = run.findings[0]
    assert finding.category == "infrastructure"
    assert finding.severity == "error"
    assert finding.repairable is False


def test_deterministic_repair_fails_when_python_unavailable(tmp_path):
    (tmp_path / "mod.py").write_text(UNFORMATTED)
    authority = _authority(tmp_path, ["mod.py"])
    _dangling_dot_venv(tmp_path)

    outcome = attempt_deterministic_candidate_repair(
        completion_validation=_verdict([_black_finding(["mod.py"])]),
        project_dir=tmp_path,
        accepted_path_authority=authority,
    )

    assert outcome.status == "failed"
    assert outcome.reason == "deterministic_repair_command_unavailable"
    assert outcome.command == ""
    assert (tmp_path / "mod.py").read_text() == UNFORMATTED
