"""Phase 24A-8 regressions for bounded local verification and lock reuse."""

import json
import os
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.services.orchestration.execution import execution_flow
from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    _lock_path_for_project_root,
    project_mutation_lock,
)


def test_verification_uses_named_300_second_policy_and_preserves_diagnostics(
    tmp_path: Path, monkeypatch
):
    observed = {}

    def fake_run(*args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise execution_flow.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(execution_flow.subprocess, "run", fake_run)
    result = execution_flow.execute_verification_command(
        project_dir=tmp_path, command="python3 -m pytest -q tests/test_cpu_workload.py"
    )

    assert observed["timeout"] == 300
    assert result == {
        "success": False,
        "command": "python3 -m pytest -q tests/test_cpu_workload.py",
        "returncode": None,
        "output": "Verification command timed out after 300s",
    }


def test_config_rejects_verification_above_task_and_celery_limits():
    with pytest.raises(ValueError, match="Timeout policy"):
        Settings(LOCAL_VERIFICATION_TIMEOUT_SECONDS=3601)


def test_old_live_pid_is_not_reclaimed_even_when_age_is_stale(tmp_path: Path):
    project_root = tmp_path / "live-owner"
    lock_dir = project_root / ".agent" / "locks"
    lock_dir.mkdir(parents=True)
    lock_path = _lock_path_for_project_root(project_root)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "token": "live", "created_at_epoch": 1}),
        encoding="utf-8",
    )

    with pytest.raises(ProjectMutationLockError):
        with project_mutation_lock(
            project_id=1,
            project_root=project_root,
            operation="must-wait",
            stale_after_seconds=0,
            wait_timeout_seconds=0,
        ):
            pass

    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "live"


def test_cleanup_cannot_remove_new_replacement_lock(tmp_path: Path):
    project_root = tmp_path / "replacement-owner"
    project_root.mkdir()
    lock_path = _lock_path_for_project_root(project_root)

    with project_mutation_lock(
        project_id=1,
        project_root=project_root,
        operation="old-owner",
        wait_timeout_seconds=0,
    ):
        replacement = {"pid": os.getpid(), "token": "new-owner"}
        lock_path.write_text(json.dumps(replacement), encoding="utf-8")

    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "new-owner"


def test_failed_run_releases_lock_and_waiter_proceeds(tmp_path: Path):
    project_root = tmp_path / "failed-run"
    project_root.mkdir()

    with pytest.raises(RuntimeError):
        with project_mutation_lock(
            project_id=1,
            project_root=project_root,
            operation="failed-run",
            wait_timeout_seconds=0,
        ):
            raise RuntimeError("controlled terminal failure")

    with project_mutation_lock(
        project_id=1,
        project_root=project_root,
        operation="waiter-after-failure",
        wait_timeout_seconds=0,
    ):
        assert True
