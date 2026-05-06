from __future__ import annotations

import json
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskStatus,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.replay import COMPATIBILITY_VERSION, REDUCER_VERSION


def _make_replay_project(db, *, workspace_path: str):
    project = Project(
        name=f"Replay Project {uuid.uuid4()}",
        workspace_path=workspace_path,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_replay_session(db, project):
    session = SessionModel(
        project_id=project.id,
        name=f"Replay Session {uuid.uuid4()}",
        description="replay endpoint test",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_replay_task(db, project, session):
    task = Task(
        project_id=project.id,
        title=f"Replay Task {uuid.uuid4()}",
        status=TaskStatus.PENDING,
        task_subfolder=f"task-{uuid.uuid4()}",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
        )
    )
    db.commit()
    return task


def _event(
    *,
    event_id: str,
    session_id: int,
    task_id: int,
    event_type: str,
    timestamp: datetime,
    details: dict | None = None,
):
    return {
        "event_id": event_id,
        "timestamp": timestamp.isoformat(),
        "event_type": event_type,
        "session_id": session_id,
        "task_id": task_id,
        "parent_event_id": None,
        "details": details or {},
    }


def _write_replay_events(
    workspace_path: str,
    session_id: int,
    task_id: int,
    events: list[dict | str],
) -> None:
    events_dir = Path(workspace_path) / ".openclaw" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / f"session_{session_id}_task_{task_id}.jsonl"
    log_path.write_text(
        "\n".join(
            item if isinstance(item, str) else json.dumps(item) for item in events
        )
        + "\n",
        encoding="utf-8",
    )


def _setup_replay_session(db_session):
    tmpdir = tempfile.TemporaryDirectory()
    project = _make_replay_project(db_session, workspace_path=tmpdir.name)
    session = _make_replay_session(db_session, project)
    task = _make_replay_task(db_session, project, session)
    return tmpdir, session, task


def test_session_replay_endpoint_is_read_only(authenticated_client, db_session):
    tmpdir, session, task = _setup_replay_session(db_session)
    with tmpdir:
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        _write_replay_events(
            tmpdir.name,
            session.id,
            task.id,
            [
                _event(
                    event_id="task-started",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.TASK_STARTED,
                    timestamp=base,
                ),
                _event(
                    event_id="task-completed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.TASK_COMPLETED,
                    timestamp=base + timedelta(seconds=1),
                ),
            ],
        )
        before = {
            "logs": db_session.query(LogEntry).count(),
            "sessions": db_session.query(SessionModel).count(),
            "tasks": db_session.query(Task).count(),
            "session_tasks": db_session.query(SessionTask).count(),
            "checkpoints": db_session.query(TaskCheckpoint).count(),
        }

        response = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/replay",
            params={"task_id": task.id},
        )

        after = {
            "logs": db_session.query(LogEntry).count(),
            "sessions": db_session.query(SessionModel).count(),
            "tasks": db_session.query(Task).count(),
            "session_tasks": db_session.query(SessionTask).count(),
            "checkpoints": db_session.query(TaskCheckpoint).count(),
        }
        assert response.status_code == 200
        body = response.json()
        assert body["reducer_version"] == REDUCER_VERSION
        assert body["compatibility_version"] == COMPATIBILITY_VERSION
        assert body["state"]["status"] == "completed"
        assert before == after


def test_session_replay_endpoint_resolves_relative_project_workspace_path(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    workspace_root = tmp_path / "vault" / "projects"
    project_dir = workspace_root / "microsite"
    monkeypatch.setattr(
        "app.services.workspace.project_isolation_service.get_effective_workspace_root",
        lambda db=None: workspace_root,
    )
    project = _make_replay_project(db_session, workspace_path="microsite")
    session = _make_replay_session(db_session, project)
    task = _make_replay_task(db_session, project, session)
    _write_replay_events(
        str(project_dir),
        session.id,
        task.id,
        [
            _event(
                event_id="task-started",
                session_id=session.id,
                task_id=task.id,
                event_type=EventType.TASK_STARTED,
                timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
            ),
            _event(
                event_id="task-completed",
                session_id=session.id,
                task_id=task.id,
                event_type=EventType.TASK_COMPLETED,
                timestamp=datetime(2026, 5, 5, 12, 0, 1, tzinfo=UTC),
            ),
        ],
    )

    response = authenticated_client.get(
        f"/api/v1/sessions/{session.id}/replay",
        params={"task_id": task.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"]["status"] == "completed"
    assert body["integrity"]["event_count_read"] == 2


def test_session_replay_endpoint_handles_malformed_and_unknown_events(
    authenticated_client, db_session
):
    tmpdir, session, task = _setup_replay_session(db_session)
    with tmpdir:
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        _write_replay_events(
            tmpdir.name,
            session.id,
            task.id,
            [
                "{bad json",
                _event(
                    event_id="future-event",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="future_event_type",
                    timestamp=base,
                ),
                _event(
                    event_id="task-completed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.TASK_COMPLETED,
                    timestamp=base + timedelta(seconds=1),
                ),
            ],
        )

        response = authenticated_client.get(f"/api/v1/sessions/{session.id}/replay")

        assert response.status_code == 200
        body = response.json()
        assert body["integrity"]["confidence"] == "medium"
        assert body["integrity"]["malformed_line_count"] == 1
        assert body["integrity"]["unknown_event_types"] == ["future_event_type"]
        finding_types = {item["type"] for item in body["drift_findings"]}
        assert "malformed_jsonl" in finding_types
        assert "event_type_ignored_by_reducer" in finding_types
        assert body["determinism"]["level"] == "degraded"


def test_session_replay_endpoint_uses_deterministic_event_order(
    authenticated_client, db_session
):
    tmpdir, session, task = _setup_replay_session(db_session)
    with tmpdir:
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        _write_replay_events(
            tmpdir.name,
            session.id,
            task.id,
            [
                _event(
                    event_id="task-completed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.TASK_COMPLETED,
                    timestamp=base + timedelta(seconds=2),
                ),
                _event(
                    event_id="task-started",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.TASK_STARTED,
                    timestamp=base,
                ),
            ],
        )

        response = authenticated_client.get(f"/api/v1/sessions/{session.id}/replay")

        assert response.status_code == 200
        body = response.json()
        assert body["state"]["status"] == "completed"
        finding_types = {item["type"] for item in body["integrity"]["findings"]}
        assert "event_order_anomaly" in finding_types


def test_session_replay_endpoint_checkpoint_boundary_does_not_load_artifact(
    authenticated_client, db_session
):
    tmpdir, session, task = _setup_replay_session(db_session)
    with tmpdir:
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        _write_replay_events(
            tmpdir.name,
            session.id,
            task.id,
            [
                _event(
                    event_id="checkpoint-saved",
                    session_id=session.id,
                    task_id=task.id,
                    event_type=EventType.CHECKPOINT_SAVED,
                    timestamp=base,
                    details={
                        "checkpoint_name": "autosave_latest",
                        "current_step_index": 2,
                        "status": "running",
                    },
                ),
            ],
        )

        response = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/replay",
            params={
                "boundary_mode": "to_checkpoint_name",
                "checkpoint_name": "autosave_latest",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["boundary"]["resolved_event_id"] == "checkpoint-saved"
        assert body["state"]["latest_checkpoint_name"] == "autosave_latest"
        assert body["checkpoint_comparison"]["status"] == "not_requested"


def test_session_replay_endpoint_bounds_findings(authenticated_client, db_session):
    tmpdir, session, task = _setup_replay_session(db_session)
    with tmpdir:
        events = ["{bad json"] * 40
        events.append(
            _event(
                event_id="task-completed",
                session_id=session.id,
                task_id=task.id,
                event_type=EventType.TASK_COMPLETED,
                timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
            )
        )
        _write_replay_events(tmpdir.name, session.id, task.id, events)

        response = authenticated_client.get(f"/api/v1/sessions/{session.id}/replay")

        assert response.status_code == 200
        body = response.json()
        assert body["integrity"]["finding_count"] == 40
        assert len(body["integrity"]["findings"]) == 25
        assert len(body["drift_findings"]) == 25
