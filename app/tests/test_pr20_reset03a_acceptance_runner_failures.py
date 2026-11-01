"""PR20-RESET-03A: typed acceptance runner failure handling.

A command that exits but emits invalid UTF-8 keeps its real return code, and
an acceptance command whose process cannot be started becomes a typed
infrastructure failure instead of an escaping ``OSError``.  All tests are
offline: temporary workspaces, real local subprocesses, in-memory SQLite.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from app.models import TaskCheckpoint
from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_pr20_reset02_acceptance_evidence import (
    SCHEMA,
    _record,
    _validation_events,
)

ACCEPTANCE_PROMPT = (
    "Fix it.\n\nAcceptance criteria:\n- `python -m pytest -q -s` passes.\n"
)
INVALID_UTF8_TEST = (
    "import os\n\n\n"
    "def test_emits_invalid_utf8():\n"
    "    os.write(1, b'head \\xff\\xfe tail\\n')\n"
    "    assert {passes}\n"
)


def _run_acceptance(project_dir: Path, prompt: str = ACCEPTANCE_PROMPT):
    findings: list = []
    commands_run: list[str] = []
    acceptance = candidate_checks._run_task_acceptance(
        project_dir=project_dir,
        task_prompt=prompt,
        focused_command="",
        focused_returncode=None,
        timeout_seconds=60,
        findings=findings,
        commands_run=commands_run,
    )
    return acceptance, findings, commands_run


def _invalid_utf8_project(tmp_path: Path, *, passes: bool) -> Path:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_bytes.py").write_text(
        INVALID_UTF8_TEST.format(passes=passes), encoding="utf-8"
    )
    return root


# --- Defect A: completed process with invalid UTF-8 ---------------------------


@pytest.mark.parametrize("exit_code", [0, 3])
def test_execute_command_decodes_invalid_utf8_with_replacement(tmp_path, exit_code):
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'h\\xc3\\xa9llo \\xff out\\n')\n"
        "sys.stderr.buffer.write(b'err \\xfe\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

    returncode, captured = candidate_checks._execute_command(
        project_dir=tmp_path, command=command, timeout_seconds=30
    )

    assert returncode == exit_code
    assert captured == "héllo � out\nerr �"
    assert candidate_checks._run_command(
        project_dir=tmp_path, command=command, timeout_seconds=30
    ) == (exit_code, captured)


def test_acceptance_invalid_utf8_success_keeps_passed_outcome(tmp_path):
    root = _invalid_utf8_project(tmp_path, passes=True)

    acceptance, findings, _ = _run_acceptance(root)

    result = acceptance["results"][0]
    assert (result["executed"], result["returncode"]) == (True, 0)
    assert result["outcome"] == "passed"
    assert "infrastructure_failure" not in result
    assert findings == []
    output = result["output"]
    assert output["chars"] > 0 and output["bytes"] >= output["chars"]
    assert (output["retained_chars"], output["truncated"]) == (0, False)


def test_acceptance_invalid_utf8_failure_keeps_real_returncode(tmp_path):
    root = _invalid_utf8_project(tmp_path, passes=False)

    acceptance, findings, _ = _run_acceptance(root)

    result = acceptance["results"][0]
    assert result["executed"] is True
    assert result["returncode"] not in (0, None)
    assert result["outcome"] == "failed"
    [finding] = findings
    assert finding.rule_id == "task_acceptance_pytest_failed"
    assert finding.category == "test"
    text = finding.evidence["output"]
    assert "head �� tail" in text
    # chars/bytes/sha256 describe the same decoded text that was retained.
    output = result["output"]
    assert output["truncated"] is False
    assert output["retained_chars"] == output["chars"] == len(text)
    assert output["bytes"] == len(text.encode("utf-8"))
    assert output["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Defect B: process-start OSError ------------------------------------------


def _project_python(root: Path, kind: str) -> Path:
    """A project ``.venv/bin/python`` that passes resolution (executable file)."""

    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    if kind == "ok":
        python.symlink_to(sys.executable)
        return python
    if kind == "missing_interpreter":
        python.write_text("#!/nonexistent-reset03a/bin/python3\n", encoding="utf-8")
    elif kind == "permission_denied":
        target = root / "not-executable-interpreter"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o644)
        python.write_text(f"#!{target}\n", encoding="utf-8")
    elif kind == "exec_format":
        python.write_bytes(b"\x00\x01\x02 not an executable image\n")
    python.chmod(0o755)
    return python


START_FAILURES = [
    ("missing_interpreter", "ENOENT"),
    ("permission_denied", "EACCES"),
    ("exec_format", "ENOEXEC"),
]


@pytest.mark.parametrize("kind, errno_name", START_FAILURES)
def test_process_start_failure_is_typed_infrastructure_failure(
    tmp_path, kind, errno_name
):
    root = _invalid_utf8_project(tmp_path, passes=True)
    python = _project_python(root, kind)

    acceptance, findings, commands_run = _run_acceptance(root)

    [result] = acceptance["results"]
    assert result["executed"] is False
    assert result["returncode"] is None
    assert result["outcome"] == "infrastructure_failure"
    assert result["infrastructure_failure"] == "process_start_failure"
    assert "output" not in result
    assert commands_run == [result["executed_command"]]
    [finding] = findings
    assert finding.rule_id == "task_acceptance_pytest_infrastructure_failure"
    assert finding.category == "infrastructure"
    assert (finding.severity, finding.repairable) == ("error", False)
    assert finding.evidence["returncode"] is None
    assert finding.evidence["infrastructure_failure"] == "process_start_failure"
    assert finding.evidence["error"] == {
        "type": finding.evidence["error"]["type"],
        "errno": errno_name,
    }
    assert getattr(errno, errno_name)
    # Diagnostics never echo the OS error's filename (host path).
    assert str(python) not in json.dumps(finding.evidence["error"])
    assert str(python) not in finding.message


def test_working_project_python_is_compatibility_control(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    print('héllo')\n", encoding="utf-8"
    )
    _project_python(root, "ok")

    acceptance, findings, _ = _run_acceptance(root)

    result = acceptance["results"][0]
    assert (result["outcome"], result["returncode"]) == ("passed", 0)
    assert findings == []


def test_unexpected_runner_errors_are_not_converted(tmp_path, monkeypatch):
    root = _invalid_utf8_project(tmp_path, passes=True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("internal defect")

    monkeypatch.setattr(candidate_checks.subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="internal defect"):
        _run_acceptance(root)


def test_process_start_failure_rejects_verdict_and_projects_bounded_evidence(
    tmp_path, db_session
):
    root = _invalid_utf8_project(tmp_path, passes=True)
    (root / "README.md").write_text("changed\n", encoding="utf-8")
    python = _project_python(root, "missing_interpreter")

    verdict = ValidatorService.validate_task_completion(
        project_dir=root,
        plan=[
            {
                "step_number": 1,
                "description": "Update the readme",
                "expected_files": ["README.md"],
            }
        ],
        task_prompt=ACCEPTANCE_PROMPT,
        execution_profile="full_lifecycle",
        completion_evidence={
            "candidate_delta_required": True,
            "run_candidate_checks": True,
            "include_static_checks": False,
            "run_task_acceptance_checks": True,
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["README.md"],
            "change_set": {
                "added_files": [],
                "modified_files": ["README.md"],
                "deleted_files": [],
            },
        },
    )

    assert verdict.details["focused_test_selection"]["command"] == ""
    assert verdict.status == "rejected"
    assert "Task acceptance pytest could not be started" in verdict.reasons
    assert "task_acceptance_pytest_infrastructure_failure" in [
        f.rule_id for f in verdict.findings
    ]

    _record(db_session, tmp_path, verdict, task_execution_id=301)
    db_session.commit()

    checkpoint = db_session.query(TaskCheckpoint).one()
    stored = json.loads(checkpoint.state_snapshot)
    evidence = stored["details"]["task_acceptance_evidence"]
    assert evidence["schema"] == SCHEMA
    assert evidence["task_execution_id"] == 301
    assert evidence["results"] == [
        {
            "command": "python -m pytest -q -s",
            "admitted": True,
            "executed": False,
            "outcome": "infrastructure_failure",
            "returncode": None,
            "infrastructure_failure": "process_start_failure",
        }
    ]
    details = _validation_events(tmp_path)[-1]["details"]
    event = details["task_acceptance_evidence"]
    assert event["results"] == evidence["results"]
    assert event["checkpoint_ref"]["projected_before_commit"] is True
    assert str(python) not in json.dumps(details)
    assert os.fspath(root / ".venv") not in json.dumps(details)
