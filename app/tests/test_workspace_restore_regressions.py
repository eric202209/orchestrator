from __future__ import annotations

import json
import asyncio
from pathlib import Path

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.checkpoint_service import CheckpointService
from app.services.orchestration.policy import should_restore_workspace_on_failure
from app.services import session_lifecycle_service
from app.services.session_lifecycle_service import stop_session_lifecycle
from app.services.task_service import TaskService


def test_workspace_restore_policy_only_allows_isolation_failures():
    assert should_restore_workspace_on_failure("workspace isolation violation")
    assert should_restore_workspace_on_failure("debug workspace isolation violation")

    assert not should_restore_workspace_on_failure("planning parse error")
    assert not should_restore_workspace_on_failure("debug parse error")
    assert not should_restore_workspace_on_failure("max step attempts reached")
    assert not should_restore_workspace_on_failure("session paused")
    assert not should_restore_workspace_on_failure("task exception")
    assert not should_restore_workspace_on_failure("completion validation failed")


def test_restore_workspace_snapshot_preserves_existing_files_when_snapshot_is_empty(
    db_session, tmp_path: Path
):
    project_root = tmp_path / "restore-regression-project"
    project_root.mkdir(parents=True, exist_ok=True)

    project = Project(
        name="restore-regression-project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    target_dir = project_root / "task-regression"
    target_dir.mkdir(parents=True, exist_ok=True)
    existing_file = target_dir / "keep.txt"
    existing_file.write_text("preserve me", encoding="utf-8")

    snapshot_dir = project_root / ".openclaw" / "auto-snapshots" / "task-1-pre-run"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    result = TaskService(db_session).restore_workspace_snapshot(
        project,
        target_dir,
        snapshot_key="task-1-pre-run",
    )

    assert result["restored"] is False
    assert result["reason"] == "empty_snapshot_preserved_existing_workspace"
    assert existing_file.exists()
    assert existing_file.read_text(encoding="utf-8") == "preserve me"


def test_resume_prefers_richer_checkpoint_over_empty_requested_one(
    db_session, tmp_path: Path
):
    project = Project(
        name="checkpoint-regression-project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Checkpoint Regression Session",
        status="paused",
        is_active=False,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    checkpoint_service = CheckpointService(db_session)
    checkpoint_service.checkpoint_dir = (tmp_path / "checkpoints").resolve()
    checkpoint_service.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    empty_checkpoint = {
        "session_id": session.id,
        "checkpoint_name": "paused_20260419_204853",
        "created_at": "2026-04-19T20:48:53",
        "context": {},
        "orchestration_state": {
            "plan": [],
            "execution_results": [],
            "current_step_index": 0,
        },
        "current_step_index": 0,
        "step_results": [],
    }
    rich_checkpoint = {
        "session_id": session.id,
        "checkpoint_name": "autosave_latest",
        "created_at": "2026-04-19T23:02:42",
        "context": {"task_id": 99},
        "orchestration_state": {
            "plan": [
                {"step_number": 1},
                {"step_number": 2},
                {"step_number": 3},
                {"step_number": 4},
            ],
            "execution_results": [
                {"step_number": 1, "status": "success"},
                {"step_number": 2, "status": "success"},
            ],
            "current_step_index": 2,
        },
        "current_step_index": 2,
        "step_results": [
            {"step_number": 1, "status": "success"},
            {"step_number": 2, "status": "success"},
        ],
    }

    empty_path = (
        checkpoint_service.checkpoint_dir
        / f"session_{session.id}_{empty_checkpoint['checkpoint_name']}.json"
    )
    rich_path = (
        checkpoint_service.checkpoint_dir
        / f"session_{session.id}_{rich_checkpoint['checkpoint_name']}.json"
    )
    empty_path.write_text(json.dumps(empty_checkpoint), encoding="utf-8")
    rich_path.write_text(json.dumps(rich_checkpoint), encoding="utf-8")

    resolved_name = checkpoint_service.resolve_resume_checkpoint_name(
        session.id,
        requested_checkpoint_name=empty_checkpoint["checkpoint_name"],
    )
    assert resolved_name == "autosave_latest"

    checkpoint_data = checkpoint_service.load_resume_checkpoint(
        session_id=session.id,
        checkpoint_name=empty_checkpoint["checkpoint_name"],
    )
    assert (
        checkpoint_data["_requested_checkpoint_name"]
        == empty_checkpoint["checkpoint_name"]
    )
    assert checkpoint_data["_resolved_checkpoint_name"] == "autosave_latest"
    assert checkpoint_data["current_step_index"] == 2

    listed = checkpoint_service.list_checkpoints(session.id)
    assert any(
        item["name"] == "autosave_latest" and item["recommended"] is True
        for item in listed
    )


def test_checkpoint_api_exposes_recommended_resume_checkpoint(
    authenticated_client, db_session, tmp_path: Path
):
    project = Project(
        name="checkpoint-api-project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Checkpoint API Session",
        status="paused",
        is_active=False,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    checkpoint_service = CheckpointService(db_session)
    checkpoint_service.checkpoint_dir = (tmp_path / "checkpoints-api").resolve()
    checkpoint_service.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = (
        checkpoint_service.checkpoint_dir / f"session_{session.id}_autosave_latest.json"
    )
    checkpoint_path.write_text(
        json.dumps(
            {
                "session_id": session.id,
                "checkpoint_name": "autosave_latest",
                "created_at": "2026-04-19T23:30:00",
                "context": {},
                "orchestration_state": {
                    "plan": [{"step_number": 1}, {"step_number": 2}],
                    "execution_results": [{"step_number": 1, "status": "success"}],
                    "current_step_index": 1,
                },
                "current_step_index": 1,
                "step_results": [{"step_number": 1, "status": "success"}],
            }
        ),
        encoding="utf-8",
    )

    original_init = CheckpointService.__init__

    def patched_init(self, db):
        original_init(self, db)
        self.checkpoint_dir = checkpoint_service.checkpoint_dir

    CheckpointService.__init__ = patched_init
    try:
        response = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/checkpoints"
        )
    finally:
        CheckpointService.__init__ = original_init

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended_checkpoint_name"] == "autosave_latest"
    assert payload["checkpoints"][0]["name"] == "autosave_latest"
    assert payload["checkpoints"][0]["recommended"] is True


def test_stop_session_resets_running_task_state_for_clean_resume(
    db_session, tmp_path: Path
):
    project = Project(
        name="stop-session-regression-project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Resume Safety Task",
        description="Regression task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-resume-safety",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    session = SessionModel(
        project_id=project.id,
        name="Stop Session Regression",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db_session.add(link)
    db_session.commit()

    original_revoke = session_lifecycle_service.revoke_session_celery_tasks
    session_lifecycle_service.revoke_session_celery_tasks = (
        lambda db, session_id, terminate=True: []
    )
    try:
        result = asyncio.run(stop_session_lifecycle(db_session, session.id, force=True))
    finally:
        session_lifecycle_service.revoke_session_celery_tasks = original_revoke

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)

    assert result["status"] == "stopped"
    assert session.status == "stopped"
    assert session.is_active is False
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
