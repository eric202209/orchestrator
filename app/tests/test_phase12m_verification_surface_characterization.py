from __future__ import annotations

from enum import StrEnum
import importlib.util
from pathlib import Path
import shlex

from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
)
from app.services.orchestration.phases.completion_flow import (
    _execute_completion_verification,
)


class VerificationMismatchType(StrEnum):
    COMMAND_MISMATCH = "COMMAND_MISMATCH"
    CWD_MISMATCH = "CWD_MISMATCH"
    ENV_MISMATCH = "ENV_MISMATCH"
    PYTHONPATH_MISMATCH = "PYTHONPATH_MISMATCH"
    SHELL_MODE_MISMATCH = "SHELL_MODE_MISMATCH"
    TIMEOUT_MISMATCH = "TIMEOUT_MISMATCH"
    ARTIFACT_EXPECTATION_MISMATCH = "ARTIFACT_EXPECTATION_MISMATCH"
    TERMINAL_EVENT_MISMATCH = "TERMINAL_EVENT_MISMATCH"
    SCORER_ONLY_MISMATCH = "SCORER_ONLY_MISMATCH"


def _load_scorer_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "maintenance"
        / "score_orchestrator_eval_case.py"
    )
    spec = importlib.util.spec_from_file_location("score_orchestrator_eval_case", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = _load_scorer_module()


def _python_command(script: str, executable: str = "python") -> str:
    return shlex.join([executable, "-c", script])


def _run_scorer_verifier(
    project_dir: Path,
    command: str,
    *,
    timeout_seconds: int = 10,
) -> dict:
    return scorer._run_verifier(
        project_dir,
        {"verifier": {"command": command, "timeout_seconds": timeout_seconds}},
    )


def _write_src_layout_package(project_dir: Path) -> None:
    package_dir = project_dir / "src" / "phase12m_env"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 12\n", encoding="utf-8")
    (project_dir / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )


def _classify_surface_result_mismatch(
    *,
    step: dict,
    completion: dict,
    score: dict,
) -> set[VerificationMismatchType]:
    types: set[VerificationMismatchType] = set()
    outcomes = {
        bool(step.get("success")),
        bool(completion.get("success")),
        bool(score.get("passed")),
    }
    if len(outcomes) <= 1:
        return types
    completion_output = str(completion.get("output") or "").lower()
    combined_output = " ".join(
        [
            str(step.get("output") or ""),
            str(completion.get("output") or ""),
            str(score.get("stdout_tail") or ""),
            str(score.get("stderr_tail") or ""),
        ]
    ).lower()
    if "unsafe shell metacharacters" in completion_output:
        types.add(VerificationMismatchType.SHELL_MODE_MISMATCH)
    if "modulenotfounderror" in combined_output or "no module named" in combined_output:
        types.add(VerificationMismatchType.ENV_MISMATCH)
        types.add(VerificationMismatchType.PYTHONPATH_MISMATCH)
    if str(completion.get("output") or "").startswith("fake-python"):
        types.add(VerificationMismatchType.COMMAND_MISMATCH)
    if "timed out" in combined_output:
        types.add(VerificationMismatchType.TIMEOUT_MISMATCH)
    return types


def _classify_scorer_terminal_event_mismatch(
    *, score: dict, event_summary: dict
) -> set[VerificationMismatchType]:
    if score.get("passed") and not event_summary.get("task_completed"):
        return {
            VerificationMismatchType.TERMINAL_EVENT_MISMATCH,
            VerificationMismatchType.SCORER_ONLY_MISMATCH,
        }
    return set()


def _classify_scorer_artifact_mismatch(
    score_report: dict,
) -> set[VerificationMismatchType]:
    result = score_report.get("result") or {}
    files = score_report.get("files") or {}
    verifier = score_report.get("verifier") or {}
    if (
        verifier.get("passed")
        and not result.get("clean_success")
        and files.get("missing_required_files")
    ):
        return {
            VerificationMismatchType.ARTIFACT_EXPECTATION_MISMATCH,
            VerificationMismatchType.SCORER_ONLY_MISMATCH,
        }
    return set()


def test_phase12m_same_command_same_workspace_different_pythonpath_environment(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    _write_src_layout_package(tmp_path)
    command = _python_command(
        "import phase12m_env\nassert phase12m_env.VALUE == 12",
        executable="python3",
    )

    step = execute_verification_command(project_dir=tmp_path, command=command)
    completion = _execute_completion_verification(project_dir=tmp_path, command=command)
    score = _run_scorer_verifier(tmp_path, command)

    assert step["success"] is True
    assert completion["success"] is False
    assert score["passed"] is False
    assert _classify_surface_result_mismatch(
        step=step,
        completion=completion,
        score=score,
    ) == {
        VerificationMismatchType.ENV_MISMATCH,
        VerificationMismatchType.PYTHONPATH_MISMATCH,
    }


def test_phase12m_leading_python_command_resolution_differs_across_surfaces(
    tmp_path,
    monkeypatch,
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n" "echo fake-python-from-path >&2\n" "exit 42\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    command = _python_command("print('verification passed')")

    step = execute_verification_command(project_dir=tmp_path, command=command)
    completion = _execute_completion_verification(project_dir=tmp_path, command=command)
    score = _run_scorer_verifier(tmp_path, command)

    assert step["success"] is True
    assert completion["success"] is False
    assert completion["returncode"] == 42
    assert completion["output"] == "fake-python-from-path"
    assert score["passed"] is True
    assert score["command"] != score["original_command"]
    assert _classify_surface_result_mismatch(
        step=step,
        completion=completion,
        score=score,
    ) == {VerificationMismatchType.COMMAND_MISMATCH}


def test_phase12m_same_command_completion_shell_policy_differs_from_step_and_scorer(
    tmp_path,
):
    command = (
        _python_command(
            "from pathlib import Path; Path('phase12m.txt').write_text('ok')"
        )
        + " && "
        + _python_command(
            "from pathlib import Path; assert Path('phase12m.txt').exists()"
        )
    )

    step = execute_verification_command(project_dir=tmp_path, command=command)
    completion = _execute_completion_verification(project_dir=tmp_path, command=command)
    score = _run_scorer_verifier(tmp_path, command)

    assert step["success"] is True
    assert completion["success"] is False
    assert "unsafe shell metacharacters" in completion["output"]
    assert score["passed"] is True
    assert _classify_surface_result_mismatch(
        step=step,
        completion=completion,
        score=score,
    ) == {VerificationMismatchType.SHELL_MODE_MISMATCH}


def test_phase12m_same_command_timeout_budget_differs_across_surfaces(tmp_path):
    command = _python_command("import time\ntime.sleep(0.5)", executable="python3")

    step = execute_verification_command(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=0.05,
    )
    completion = _execute_completion_verification(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=3,
    )
    score = _run_scorer_verifier(tmp_path, command, timeout_seconds=3)

    assert step["success"] is False
    assert "timed out" in step["output"]
    assert completion["success"] is True
    assert score["passed"] is True
    assert _classify_surface_result_mismatch(
        step=step,
        completion=completion,
        score=score,
    ) == {VerificationMismatchType.TIMEOUT_MISMATCH}


def test_phase12m_cwd_surfaces_align_on_project_dir(tmp_path):
    command = _python_command(
        "from pathlib import Path\n" f"assert Path.cwd() == Path({str(tmp_path)!r})",
        executable="python3",
    )

    step = execute_verification_command(project_dir=tmp_path, command=command)
    completion = _execute_completion_verification(project_dir=tmp_path, command=command)
    score = _run_scorer_verifier(tmp_path, command)

    assert step["success"] is True
    assert completion["success"] is True
    assert score["passed"] is True
    assert (
        _classify_surface_result_mismatch(
            step=step,
            completion=completion,
            score=score,
        )
        == set()
    )


def test_phase12m_scorer_verifier_can_pass_without_task_completed_event(tmp_path):
    command = _python_command("print('verification passed')")

    score = _run_scorer_verifier(tmp_path, command)
    event_summary = scorer._event_summary(
        [
            {"event_type": "task_started", "details": {}},
            {"event_type": "step_finished", "details": {"status": "success"}},
        ]
    )

    assert score["passed"] is True
    assert event_summary["task_completed"] is False
    assert _classify_scorer_terminal_event_mismatch(
        score=score,
        event_summary=event_summary,
    ) == {
        VerificationMismatchType.TERMINAL_EVENT_MISMATCH,
        VerificationMismatchType.SCORER_ONLY_MISMATCH,
    }


def test_phase12m_scorer_has_artifact_expectations_beyond_surface_verifier(tmp_path):
    events_dir = tmp_path / ".openclaw" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "session_1_task_1.jsonl").write_text(
        '{"event_type": "task_completed", "details": {}}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest = {"benchmark_id": "phase12m", "schema_version": 1}
    case = {
        "case_id": "artifact_expectation_mismatch",
        "category": "completion_validation",
        "verifier": {
            "command": _python_command("print('verification passed')"),
            "timeout_seconds": 10,
        },
        "required_files": ["deliverable.md"],
    }

    score_report = scorer._score_case(
        manifest_path=manifest_path,
        manifest=manifest,
        case=case,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
    )

    assert score_report["verifier"]["passed"] is True
    assert score_report["result"]["clean_success"] is False
    assert score_report["result"]["blockers"] == ["required_files_missing"]
    assert score_report["files"]["missing_required_files"] == ["deliverable.md"]
    assert _classify_scorer_artifact_mismatch(score_report) == {
        VerificationMismatchType.ARTIFACT_EXPECTATION_MISMATCH,
        VerificationMismatchType.SCORER_ONLY_MISMATCH,
    }
