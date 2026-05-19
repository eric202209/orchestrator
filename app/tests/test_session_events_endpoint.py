"""Tests for GET /sessions/{id}/tasks/{task_id}/events — orchestration event journal endpoint."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus


def _make_project(db, *, workspace_path="/tmp/evt_test"):
    project = Project(name="Events Test", workspace_path=workspace_path)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status="stopped"):
    existing_count = (
        db.query(SessionModel).filter(SessionModel.project_id == project.id).count()
    )
    session = SessionModel(
        project_id=project.id,
        name=f"Events Session {existing_count + 1}",
        description="test",
        status=status,
        is_active=False,
        execution_mode="manual",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _write_events(
    workspace_path: str, session_id: int, task_id: int, events: list[dict]
) -> None:
    events_dir = Path(workspace_path) / ".openclaw" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / f"session_{session_id}_task_{task_id}.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


def test_events_endpoint_returns_empty_list_when_no_journal(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/tasks/999/events"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == []
        assert body["session_id"] == session.id
        assert body["task_id"] == 999


def test_events_endpoint_returns_all_events(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task_id = 7

        sample_events = [
            {
                "event_type": "phase_started",
                "session_id": session.id,
                "task_id": task_id,
                "details": {},
            },
            {
                "event_type": "step_finished",
                "session_id": session.id,
                "task_id": task_id,
                "details": {},
            },
            {
                "event_type": "task_completed",
                "session_id": session.id,
                "task_id": task_id,
                "details": {},
            },
        ]
        _write_events(tmpdir, session.id, task_id, sample_events)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/tasks/{task_id}/events"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["events"]) == 3
        types = [e["event_type"] for e in body["events"]]
        assert types == ["phase_started", "step_finished", "task_completed"]


def test_events_endpoint_reads_legacy_task_subfolder_journal(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = Task(
            project_id=project.id,
            title="Legacy event task",
            status=TaskStatus.PENDING,
            task_subfolder="task-legacy",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        db_session.add(
            SessionTask(
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.PENDING,
            )
        )
        db_session.commit()

        _write_events(
            str(Path(tmpdir) / "task-legacy"),
            session.id,
            task.id,
            [
                {
                    "event_type": "phase_started",
                    "session_id": session.id,
                    "task_id": task.id,
                    "details": {"phase": "planning"},
                }
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/tasks/{task.id}/events"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert [event["event_type"] for event in body["events"]] == ["phase_started"]


def test_events_endpoint_filters_by_event_type(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task_id = 3

        sample_events = [
            {
                "event_type": "phase_started",
                "session_id": session.id,
                "task_id": task_id,
                "details": {},
            },
            {
                "event_type": "validation_result",
                "session_id": session.id,
                "task_id": task_id,
                "details": {"passed": False},
            },
            {
                "event_type": "phase_finished",
                "session_id": session.id,
                "task_id": task_id,
                "details": {},
            },
            {
                "event_type": "validation_result",
                "session_id": session.id,
                "task_id": task_id,
                "details": {"passed": True},
            },
        ]
        _write_events(tmpdir, session.id, task_id, sample_events)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/tasks/{task_id}/events",
            params={"event_type": "validation_result"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["events"]) == 2
        assert all(e["event_type"] == "validation_result" for e in body["events"])


def test_events_endpoint_rejects_unknown_event_type(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/tasks/3/events",
            params={"event_type": "validation_reslt"},
        )

        assert resp.status_code == 400
        assert "Unknown event_type" in resp.json()["detail"]


def test_events_endpoint_returns_404_for_nonexistent_session(authenticated_client):
    resp = authenticated_client.get("/api/v1/sessions/99999/tasks/1/events")
    assert resp.status_code == 404


def test_events_endpoint_requires_auth(api_client, db_session):
    resp = api_client.get("/api/v1/sessions/1/tasks/1/events")
    assert resp.status_code == 401


def test_session_diff_endpoint_returns_structured_delta(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task_id = 5

        from app.models import SessionTask, Task, TaskStatus
        from app.services.orchestration.state.persistence import (
            write_orchestration_state_snapshot,
        )
        from app.services.prompt_templates import OrchestrationState

        task = Task(
            project_id=project.id,
            title="Diff Task",
            status=TaskStatus.PENDING,
            task_subfolder="task-5",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        db_session.add(
            SessionTask(
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.PENDING,
            )
        )
        db_session.commit()

        state = OrchestrationState(
            session_id=str(session.id),
            task_description="Diff me",
            project_name=project.name,
            task_id=task.id,
        )
        state._project_dir_override = tmpdir
        state.plan = [{"description": "first"}]
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-0",
            trigger="phase_boundary",
        )
        state.current_step_index = 1
        state.changed_files = ["src/demo.py"]
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-1",
            trigger="phase_boundary",
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/diff",
            params={"task_id": task.id, "from_checkpoint": 0, "to_checkpoint": 1},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task.id
        assert body["delta"]["current_step_index"]["change"] == 1
        assert body["delta"]["files_touched"]["added"] == ["src/demo.py"]


def test_session_diff_endpoint_defaults_to_latest_two_snapshots(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)

        from app.models import SessionTask, Task, TaskStatus
        from app.services.orchestration.state.persistence import (
            write_orchestration_state_snapshot,
        )
        from app.services.prompt_templates import OrchestrationState

        task = Task(
            project_id=project.id,
            title="Diff Task Default",
            status=TaskStatus.PENDING,
            task_subfolder="task-9",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        db_session.add(
            SessionTask(
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.PENDING,
            )
        )
        db_session.commit()

        state = OrchestrationState(
            session_id=str(session.id),
            task_description="Diff me later",
            project_name=project.name,
            task_id=task.id,
        )
        state._project_dir_override = tmpdir
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-0",
            trigger="phase_boundary",
        )
        state.current_step_index = 1
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-1",
            trigger="phase_boundary",
        )
        state.current_step_index = 2
        state.changed_files = ["src/last.py"]
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-2",
            trigger="phase_boundary",
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/diff",
            params={"task_id": task.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["from_checkpoint"] == 1
        assert body["to_checkpoint"] == 2
        assert body["delta"]["current_step_index"]["change"] == 1


def test_compare_divergence_endpoint_returns_similar_sessions(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session_a = _make_session(db_session, project, status="stopped")
        session_b = _make_session(db_session, project, status="stopped")

        from app.models import SessionTask, Task, TaskStatus

        task_a = Task(
            project_id=project.id,
            title="Task A",
            status=TaskStatus.FAILED,
            task_subfolder="task-a",
        )
        task_b = Task(
            project_id=project.id,
            title="Task B",
            status=TaskStatus.FAILED,
            task_subfolder="task-b",
        )
        db_session.add(task_a)
        db_session.add(task_b)
        db_session.commit()
        db_session.refresh(task_a)
        db_session.refresh(task_b)

        db_session.add(SessionTask(session_id=session_a.id, task_id=task_a.id))
        db_session.add(SessionTask(session_id=session_b.id, task_id=task_b.id))
        db_session.commit()

        base_events = [
            {
                "event_id": "root-1",
                "event_type": "retry_entered",
                "session_id": session_a.id,
                "task_id": task_a.id,
                "details": {"attempt": 1},
            },
            {
                "event_id": "div-1",
                "parent_event_id": "root-1",
                "event_type": "divergence_detected",
                "session_id": session_a.id,
                "task_id": task_a.id,
                "details": {"reason": "retry_cluster"},
            },
        ]
        compare_events = [
            {
                "event_id": "root-2",
                "event_type": "retry_entered",
                "session_id": session_b.id,
                "task_id": task_b.id,
                "details": {"attempt": 1},
            },
            {
                "event_id": "div-2",
                "parent_event_id": "root-2",
                "event_type": "divergence_detected",
                "session_id": session_b.id,
                "task_id": task_b.id,
                "details": {"reason": "retry_cluster"},
            },
        ]
        _write_events(f"{tmpdir}/task-a", session_a.id, task_a.id, base_events)
        _write_events(f"{tmpdir}/task-b", session_b.id, task_b.id, compare_events)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session_a.id}/compare-divergence",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["current"]["session_id"] == session_a.id
        assert len(body["matches"]) >= 1
        assert body["matches"][0]["session_id"] == session_b.id


def test_session_trace_export_and_dag_endpoints(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project, status="running")

        from app.models import SessionTask, Task, TaskStatus
        from app.services.orchestration.state.persistence import (
            append_orchestration_event,
            write_orchestration_state_snapshot,
        )
        from app.services.prompt_templates import OrchestrationState

        task = Task(
            project_id=project.id,
            title="Trace Task",
            status=TaskStatus.RUNNING,
            task_subfolder="trace-task",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        db_session.add(
            SessionTask(
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.RUNNING,
            )
        )
        db_session.commit()

        state = OrchestrationState(
            session_id=str(session.id),
            task_description="trace me",
            project_name=project.name,
            task_id=task.id,
        )
        state._project_dir_override = tmpdir
        state.plan = [{"description": "phase 1"}]
        phase_event = append_orchestration_event(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            event_type="phase_started",
            details={"phase": "planning"},
        )
        append_orchestration_event(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            event_type="phase_finished",
            parent_event_id=phase_event["event_id"],
            details={"phase": "planning", "status": "completed"},
        )
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="autosave_latest",
            trigger="phase_finished",
            related_event_id=phase_event["event_id"],
        )

        trace_resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/trace-export"
        )
        assert trace_resp.status_code == 200
        trace_body = trace_resp.json()
        assert trace_body["task_id"] == task.id
        assert trace_body["span_count"] >= 1

        dag_resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/execution-dag"
        )
        assert dag_resp.status_code == 200
        dag_body = dag_resp.json()
        assert dag_body["task_id"] == task.id
        assert dag_body["node_count"] >= 2
        assert dag_body["edge_count"] >= 1


def test_session_focus_and_mobile_interruption_endpoints(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project, status="awaiting_input")
        session.is_active = True
        db_session.commit()

        from app.models import InterventionRequest, SessionTask, Task, TaskStatus
        from app.services.orchestration.state.persistence import (
            write_orchestration_state_snapshot,
        )
        from app.services.prompt_templates import OrchestrationState

        task = Task(
            project_id=project.id,
            title="Focus Task",
            status=TaskStatus.RUNNING,
            task_subfolder="focus-task",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        db_session.add(
            SessionTask(
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.RUNNING,
            )
        )
        db_session.add(
            InterventionRequest(
                session_id=session.id,
                task_id=task.id,
                project_id=project.id,
                intervention_type="approval",
                initiated_by="ai",
                prompt="Need approval to deploy",
                status="pending",
            )
        )
        db_session.commit()

        state = OrchestrationState(
            session_id=str(session.id),
            task_description="focus me",
            project_name=project.name,
            task_id=task.id,
        )
        state._project_dir_override = tmpdir
        state.plan = [{"description": "inspect"}, {"description": "deploy"}]
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-0",
            trigger="phase_started",
        )
        state.current_step_index = 1
        state.changed_files = ["src/app.py"]
        write_orchestration_state_snapshot(
            project_dir=tmpdir,
            session_id=session.id,
            task_id=task.id,
            orchestration_state=state,
            checkpoint_name="snap-1",
            trigger="phase_finished",
        )

        focus_resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/focus")
        assert focus_resp.status_code == 200
        focus_body = focus_resp.json()
        assert focus_body["current_task"]["task_id"] == task.id
        assert len(focus_body["active_approvals"]) == 1

        mobile_resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/mobile-interruptions"
        )
        assert mobile_resp.status_code == 200
        mobile_body = mobile_resp.json()
        kinds = [card["kind"] for card in mobile_body["cards"]]
        assert "approval_needed" in kinds
        assert "emergency_stop" in kinds
