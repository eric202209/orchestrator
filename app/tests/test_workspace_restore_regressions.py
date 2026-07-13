from __future__ import annotations

import json
import asyncio
from pathlib import Path

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.policy import should_restore_workspace_on_failure
import app.tasks.worker_support.checkpoint as checkpoint_support
import app.services.session.session_lifecycle_service as session_lifecycle_service
from app.services.session.session_lifecycle_service import stop_session_lifecycle
from app.services.tasks.service import TaskService


class _CheckpointApplyState:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.project_name = "checkpoint-project"
        self.project_context = ""
        self._task_subfolder_override = None
        self.plan = []
        self.reasoning_artifact = None
        self.current_step_index = 0
        self.debug_attempts = []
        self.changed_files = []
        self.validation_history = []
        self.phase_history = []
        self.last_plan_validation = None
        self.last_completion_validation = None
        self.relaxed_mode = False
        self.completion_repair_attempts = 0
        self.debug_repair_task_execution_ids = []
        self.execution_results = []
        self.status = None


class _CheckpointApplyTask:
    steps = None


def _checkpoint_payload(*, current_step_index: int, execution_result_count: int):
    return {
        "checkpoint_name": "autosave_latest",
        "context": {
            "task_description": "resume task",
            "project_name": "checkpoint-project",
            "task_subfolder": "task-checkpoint",
        },
        "orchestration_state": {
            "status": "executing",
            "plan": [
                {"description": "step 1"},
                {"description": "step 2"},
                {"description": "step 3"},
            ],
            "current_step_index": current_step_index,
            "execution_results": [
                {
                    "step_number": index + 1,
                    "status": "success",
                    "output": "ok",
                    "files_changed": [f"file-{index + 1}.txt"],
                }
                for index in range(execution_result_count)
            ],
        },
    }


def test_workspace_restore_policy_only_allows_isolation_failures():
    assert should_restore_workspace_on_failure("workspace isolation violation")
    assert should_restore_workspace_on_failure("debug workspace isolation violation")

    assert should_restore_workspace_on_failure("planning parse error")
    assert should_restore_workspace_on_failure("debug parse error")
    assert should_restore_workspace_on_failure("max step attempts reached")
    assert not should_restore_workspace_on_failure("session paused")
    assert should_restore_workspace_on_failure("task exception")
    assert should_restore_workspace_on_failure("completion validation failed")
    assert should_restore_workspace_on_failure(
        "planning repair materialization regression"
    )
    assert should_restore_workspace_on_failure(
        "repair candidate rejected by Bootstrap Contract"
    )


def test_checkpoint_save_uses_atomic_file_and_leaves_no_temp_files(
    db_session, tmp_path: Path
):
    checkpoint_service = CheckpointService(db_session)
    checkpoint_service.checkpoint_dir = (tmp_path / "checkpoints").resolve()
    checkpoint_service.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    result = checkpoint_service.save_checkpoint(
        session_id=123,
        checkpoint_name="autosave_latest",
        context_data={"task_id": 456},
        orchestration_state={"status": "EXECUTING", "plan": [{"step_number": 1}]},
        current_step_index=1,
        step_results=[{"status": "success"}],
    )

    checkpoint_path = Path(result["path"])
    assert checkpoint_path.exists()
    assert not list(checkpoint_service.checkpoint_dir.glob("*.tmp"))

    loaded = checkpoint_service.load_checkpoint(123, "autosave_latest")
    assert loaded["checkpoint_name"] == "autosave_latest"
    assert loaded["context"]["task_id"] == 456
    assert loaded["step_results"] == [{"status": "success"}]


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

    snapshot_dir = project_root / ".agent" / "auto-snapshots" / "task-1-pre-run"
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


def test_checkpoint_apply_reconciles_cursor_ahead_of_execution_results(
    tmp_path: Path, monkeypatch
):
    captured_events = []
    live_logs = []

    monkeypatch.setattr(
        checkpoint_support,
        "_append_orchestration_event",
        lambda **kwargs: captured_events.append(kwargs) or {"event_id": "event-1"},
    )
    monkeypatch.setattr(
        checkpoint_support.ValidatorService,
        "assess_plan_workspace_compatibility",
        staticmethod(lambda **_kwargs: {"compatible": True}),
    )

    state = _CheckpointApplyState(tmp_path)
    checkpoint_support._apply_checkpoint_payload(
        _checkpoint_payload(current_step_index=2, execution_result_count=1),
        orchestration_state=state,
        task=_CheckpointApplyTask(),
        session_id=123,
        task_id=456,
        prompt="original",
        emit_live=lambda *args, **kwargs: live_logs.append((args, kwargs)),
    )

    assert state.current_step_index == 1
    assert len(state.execution_results) == 1
    reconciliation_events = [
        event
        for event in captured_events
        if event["event_type"] == EventType.CHECKPOINT_CURSOR_RECONCILED
    ]
    assert reconciliation_events
    assert (
        reconciliation_events[0]["details"]["reason"]
        == "checkpoint_cursor_execution_results_mismatch"
    )
    assert reconciliation_events[0]["details"]["original_current_step_index"] == 2
    assert reconciliation_events[0]["details"]["reconciled_current_step_index"] == 1
    assert any(
        kwargs["metadata"]["reason"] == "checkpoint_cursor_execution_results_mismatch"
        for _args, kwargs in live_logs
    )


def test_checkpoint_apply_reconciles_execution_results_ahead_of_cursor(
    tmp_path: Path, monkeypatch
):
    captured_events = []

    monkeypatch.setattr(
        checkpoint_support,
        "_append_orchestration_event",
        lambda **kwargs: captured_events.append(kwargs) or {"event_id": "event-1"},
    )
    monkeypatch.setattr(
        checkpoint_support.ValidatorService,
        "assess_plan_workspace_compatibility",
        staticmethod(lambda **_kwargs: {"compatible": True}),
    )

    state = _CheckpointApplyState(tmp_path)
    checkpoint_support._apply_checkpoint_payload(
        _checkpoint_payload(current_step_index=0, execution_result_count=2),
        orchestration_state=state,
        task=_CheckpointApplyTask(),
        session_id=123,
        task_id=456,
        prompt="original",
        emit_live=lambda *_args, **_kwargs: None,
    )

    assert state.current_step_index == 2
    assert len(state.execution_results) == 2
    reconciliation_events = [
        event
        for event in captured_events
        if event["event_type"] == EventType.CHECKPOINT_CURSOR_RECONCILED
    ]
    assert reconciliation_events
    assert reconciliation_events[0]["details"]["original_current_step_index"] == 0
    assert reconciliation_events[0]["details"]["reconciled_current_step_index"] == 2


def test_checkpoint_apply_sanitizes_stale_absolute_project_context(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        checkpoint_support.ValidatorService,
        "assess_plan_workspace_compatibility",
        staticmethod(lambda **_kwargs: {"compatible": True}),
    )

    state = _CheckpointApplyState(tmp_path)
    checkpoint = _checkpoint_payload(current_step_index=0, execution_result_count=0)
    checkpoint["context"]["project_context"] = (
        "Read /root/.openclaw/workspace/docs/adr/"
        "0002-advisory-human-guidance-conflict-detection.md"
    )

    checkpoint_support._apply_checkpoint_payload(
        checkpoint,
        orchestration_state=state,
        task=_CheckpointApplyTask(),
        session_id=123,
        task_id=456,
        prompt="original",
        emit_live=lambda *args, **kwargs: None,
    )

    assert (
        "0002-advisory-human-guidance-conflict-detection.md"
        not in state.project_context
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
    assert payload["checkpoints"][0]["resumable"] is True
    assert (
        payload["checkpoints"][0]["resume_reason"] == "Saved execution plan available"
    )
    assert payload["checkpoints"][0]["restore_fidelity"]["status"] == "high"


def test_checkpoint_api_marks_hollow_paused_checkpoint_as_not_resumable(
    authenticated_client, db_session, tmp_path: Path
):
    project = Project(
        name="checkpoint-hollow-project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Checkpoint Hollow Session",
        status="paused",
        is_active=False,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    checkpoint_service = CheckpointService(db_session)
    checkpoint_service.checkpoint_dir = (tmp_path / "checkpoints-hollow").resolve()
    checkpoint_service.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    hollow_path = (
        checkpoint_service.checkpoint_dir
        / f"session_{session.id}_paused_20260424_035730.json"
    )
    hollow_path.write_text(
        json.dumps(
            {
                "session_id": session.id,
                "checkpoint_name": "paused_20260424_035730",
                "created_at": "2026-04-24T03:57:30",
                "context": {},
                "orchestration_state": {},
                "current_step_index": 0,
                "step_results": [],
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
        inspect_response = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/checkpoints/paused_20260424_035730"
        )
    finally:
        CheckpointService.__init__ = original_init

    assert response.status_code == 200
    payload = response.json()
    assert payload["checkpoints"][0]["resumable"] is False
    assert "missing replay state" in payload["checkpoints"][0]["resume_reason"].lower()

    assert inspect_response.status_code == 200
    inspect_payload = inspect_response.json()
    assert inspect_payload["resume_readiness"]["resumable"] is False
    assert inspect_payload["restore_fidelity"]["status"] == "low"


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
    assert task.status == TaskStatus.CANCELLED
    assert task.error_message == "Operator requested stop"
    assert task.completed_at is not None
    assert link.status == TaskStatus.CANCELLED
    assert link.completed_at == task.completed_at
