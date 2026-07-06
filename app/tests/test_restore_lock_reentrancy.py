"""Regression test — HG-P2 restore-lock reentrancy fix.

execute_canonical_root_task holds the project mutation lock.
restore_workspace_snapshot_if_needed must NOT attempt to re-acquire it;
it must call the unlocked snapshot restore path instead.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    project_mutation_lock,
)
from app.tasks.worker_support.workspace import _restore_workspace_snapshot_if_needed


class _FakeProject:
    def __init__(self, project_id: int, root: Path):
        self.id = project_id
        self.workspace_path = str(root)


class _FakeOrchestrationState:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir


def _make_ctx(tmp_path: Path, project_id: int = 1):
    project = _FakeProject(project_id, tmp_path)
    state = _FakeOrchestrationState(tmp_path)
    emit_live = MagicMock()
    task_service = MagicMock()
    task_service.restore_workspace_snapshot.return_value = {
        "restored": False,
        "reason": "snapshot_missing",
    }
    task_service.restore_workspace_snapshot_unlocked = MagicMock(
        return_value={"restored": False, "reason": "snapshot_missing"}
    )
    return project, state, emit_live, task_service


# ---------------------------------------------------------------------------
# Core reentrancy regression
# ---------------------------------------------------------------------------


def test_lock_already_held_calls_unlocked_path(tmp_path):
    """When lock_already_held=True, restore goes through skip_lock=True → no lock taken."""
    project, state, emit_live, task_service = _make_ctx(tmp_path)

    with patch(
        "app.tasks.worker_support.workspace._restore_workspace_after_abort"
    ) as mock_restore:
        mock_restore.return_value = {"restored": False, "reason": "snapshot_missing"}

        _restore_workspace_snapshot_if_needed(
            "task exception",
            project=project,
            session_id=1,
            task_id=10,
            task_execution_id=100,
            orchestration_state=state,
            policy_profile_name="balanced",
            runs_in_canonical_baseline=True,
            task_service=task_service,
            emit_live=emit_live,
            force_restore=True,
            lock_already_held=True,
        )

    mock_restore.assert_called_once()
    _, kwargs = mock_restore.call_args
    assert kwargs.get("lock_already_held") is True


def test_lock_not_held_calls_locked_path(tmp_path):
    """When lock_already_held=False (default), lock_already_held=False is forwarded."""
    project, state, emit_live, task_service = _make_ctx(tmp_path)

    with patch(
        "app.tasks.worker_support.workspace._restore_workspace_after_abort"
    ) as mock_restore:
        mock_restore.return_value = {"restored": False, "reason": "snapshot_missing"}

        _restore_workspace_snapshot_if_needed(
            "task exception",
            project=project,
            session_id=1,
            task_id=10,
            task_execution_id=100,
            orchestration_state=state,
            policy_profile_name="balanced",
            runs_in_canonical_baseline=False,
            task_service=task_service,
            emit_live=emit_live,
            force_restore=True,
            lock_already_held=False,
        )

    mock_restore.assert_called_once()
    _, kwargs = mock_restore.call_args
    assert kwargs.get("lock_already_held") is False


def test_no_projectmutationlockerror_when_lock_held(tmp_path):
    """Full path: lock held → restore_workspace_snapshot called with skip_lock=True → no error."""
    from app.services.orchestration.execution.runtime import (
        restore_workspace_after_abort,
    )
    from app.services.tasks.service import TaskService

    project, state, emit_live, task_service = _make_ctx(tmp_path)

    # Simulate: the project mutation lock is held by the outer task
    lock_dir = tmp_path / ".agent" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"project-{project.id}.mutation.lock"
    lock_path.write_text(
        json.dumps(
            {
                "project_id": project.id,
                "operation": "execute_canonical_root_task",
                "owner": "session:1:task:10",
                "token": "outer-token",
                "created_at_epoch": 0,
            }
        ),
        encoding="utf-8",
    )

    # With lock_already_held=True, restore_workspace_after_abort should call
    # task_service.restore_workspace_snapshot(skip_lock=True) and NOT raise.
    with patch(
        "app.services.orchestration.execution.runtime.workspace_snapshot_key",
        return_value="snap-key",
    ):
        result = restore_workspace_after_abort(
            task_service,
            project,
            task_id=10,
            target_dir=tmp_path,
            task_execution_id=100,
            preserve_project_root_rules=True,
            lock_already_held=True,
        )

    task_service.restore_workspace_snapshot.assert_called_once_with(
        project,
        tmp_path,
        snapshot_key="snap-key",
        preserve_project_root_rules=True,
        skip_lock=True,
    )

    # Clean up the fake lock
    lock_path.unlink(missing_ok=True)


def test_task_service_skip_lock_calls_unlocked(tmp_path):
    """task_service.restore_workspace_snapshot(skip_lock=True) delegates to snapshots.unlocked."""
    from app.services.tasks.service import TaskService

    ts = MagicMock(spec=TaskService)
    snapshots = MagicMock()
    snapshots.restore_workspace_snapshot_unlocked.return_value = {
        "restored": False,
        "reason": "snapshot_missing",
    }
    snapshots.restore_workspace_snapshot.return_value = {
        "restored": False,
        "reason": "snapshot_missing",
    }
    ts.snapshots = snapshots

    project = MagicMock()
    target_dir = tmp_path

    # Call the real method body (not the mock's auto-spec)
    from app.services.tasks.service import TaskService as RealTaskService

    real_method = (
        RealTaskService.restore_workspace_snapshot.__wrapped__
        if hasattr(RealTaskService.restore_workspace_snapshot, "__wrapped__")
        else RealTaskService.restore_workspace_snapshot
    )

    real_method(ts, project, target_dir, snapshot_key="k", skip_lock=True)
    snapshots.restore_workspace_snapshot_unlocked.assert_called_once()
    snapshots.restore_workspace_snapshot.assert_not_called()


def test_task_service_no_skip_lock_calls_locked(tmp_path):
    """task_service.restore_workspace_snapshot(skip_lock=False) delegates to snapshots.locked."""
    from app.services.tasks.service import TaskService as RealTaskService

    ts = MagicMock()
    snapshots = MagicMock()
    snapshots.restore_workspace_snapshot.return_value = {
        "restored": False,
        "reason": "snapshot_missing",
    }
    ts.snapshots = snapshots

    project = MagicMock()
    target_dir = tmp_path

    real_method = RealTaskService.restore_workspace_snapshot
    real_method(ts, project, target_dir, snapshot_key="k", skip_lock=False)
    snapshots.restore_workspace_snapshot.assert_called_once()
    snapshots.restore_workspace_snapshot_unlocked.assert_not_called()
