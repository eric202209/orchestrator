from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.models import (
    ConversationHistory,
    PermissionRequest,
    PlanningArtifact,
    PlanningMessage,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionState,
    Task,
    TaskCheckpoint,
)
from app.services.workspace.project_isolation_service import (
    normalize_project_workspace_path,
)

from app.services.planning.planning_session_service import PlanningSessionService


def _create_project(authenticated_client, name: str = "Planner Project") -> dict:
    response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": name, "description": "Existing app with API and dashboard"},
    )
    assert response.status_code == 201
    return response.json()


def _completed_payload(project_name: str) -> dict:
    return {
        "requirements": "# Requirements\n\n- Support resumable planning",
        "design": "# Design\n\n- Use persisted planning sessions",
        "implementation_plan": "# Implementation Plan\n\n1. Add models\n2. Add API\n3. Add UI",
        "planner_markdown": "\n".join(
            [
                f"# Project: {project_name}",
                "",
                "## Task List",
                "- [ ] TASK_START: Add planning models | Persist planning sessions and messages | order=1 | P1 | effort=medium | profile=full_lifecycle",
                "- [ ] TASK_START: Add planning API | Expose session lifecycle endpoints | order=2 | P1 | effort=medium | profile=full_lifecycle",
                "- [ ] TASK_START: Add planning UI | Build interactive planner experience | order=3 | P1 | effort=medium | profile=full_lifecycle",
            ]
        ),
    }


def test_start_planning_session_requires_unique_active_session(authenticated_client):
    project = _create_project(authenticated_client)

    response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": "Improve planner"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "waiting_for_input"

    duplicate = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": "Another idea"},
    )
    assert duplicate.status_code == 409


def test_planning_session_can_respond_finalize_and_commit_idempotently(
    authenticated_client, monkeypatch
):
    project = _create_project(authenticated_client)

    def fake_run_openclaw(self, prompt: str):
        payload = _completed_payload(project["name"])
        return {"status": "completed", "output": json.dumps(payload)}

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    start = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": "Improve planner"},
    )
    assert start.status_code == 201
    session = start.json()
    assert session["status"] == "waiting_for_input"
    assert session["current_prompt_id"]
    assert len(session["messages"]) == 2

    session_id = session["id"]
    loaded = authenticated_client.get(f"/api/v1/planning/sessions/{session_id}")
    assert loaded.status_code == 200
    assert len(loaded.json()["messages"]) == 2

    respond = authenticated_client.post(
        f"/api/v1/planning/sessions/{session_id}/respond",
        json={
            "response": "Optimize for a project-level planning chat with artifacts, task preview, and safe commit into existing tasks.",
        },
    )
    assert respond.status_code == 200
    completed = respond.json()
    assert completed["status"] == "completed"
    assert {item["artifact_type"] for item in completed["artifacts"]} == {
        "requirements",
        "design",
        "implementation_plan",
        "planner_markdown",
    }
    assert len(completed["tasks_preview"]) == 3

    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session_id}/commit", json={}
    )
    assert commit.status_code == 200
    commit_payload = commit.json()
    assert commit_payload["plan"]["project_id"] == project["id"]
    assert len(commit_payload["tasks"]) == 3
    assert len(commit_payload["committed_task_ids"]) == 3

    repeat_commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session_id}/commit", json={}
    )
    assert repeat_commit.status_code == 200
    repeat_payload = repeat_commit.json()
    assert repeat_payload["committed_task_ids"] == commit_payload["committed_task_ids"]
    assert [task["id"] for task in repeat_payload["tasks"]] == [
        task["id"] for task in commit_payload["tasks"]
    ]


def test_specific_prompt_can_finalize_immediately(authenticated_client, monkeypatch):
    project = _create_project(authenticated_client, name="Specific Planner Project")

    def fake_run_openclaw(self, prompt: str):
        return {
            "status": "completed",
            "output": json.dumps(
                {
                    "requirements": "# Requirements",
                    "design": "# Design",
                    "implementation_plan": "# Implementation Plan",
                    "planner_markdown": "\n".join(
                        [
                            "# Project: Specific Planner Project",
                            "",
                            "## Task List",
                            "- [ ] TASK_START: Add auth workflow | Implement JWT auth across API and frontend | order=1 | P1 | effort=medium | profile=full_lifecycle",
                            "- [ ] TASK_START: Add tests | Cover auth success and failure paths | order=2 | P1 | effort=medium | profile=test_only",
                            "- [ ] TASK_START: Add rollout notes | Document env vars and migration steps | order=3 | P2 | effort=small | profile=review_only",
                        ]
                    ),
                }
            ),
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Add JWT authentication to the FastAPI backend and React frontend, including token refresh, protected routes, and regression tests.",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert len(payload["tasks_preview"]) == 3


def test_malformed_openclaw_output_marks_session_failed(
    authenticated_client, monkeypatch
):
    project = _create_project(authenticated_client, name="Broken Planner Project")

    def fake_run_openclaw(self, prompt: str):
        return {"status": "completed", "output": "not valid json"}

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Build a frontend dashboard with backend sync, websocket activity feed, and tests.",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["last_error"]


def test_planning_commit_rejects_explicit_empty_selection(
    authenticated_client, monkeypatch
):
    project = _create_project(authenticated_client, name="Empty Selection Project")

    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_completed_payload(project["name"])),
        },
    )

    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Add JWT authentication to the FastAPI backend and React frontend, including token refresh, protected routes, and regression tests.",
        },
    ).json()

    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={"selected_tasks": []},
    )
    assert commit.status_code == 422


def test_planning_commit_uses_edited_markdown_for_plan_and_tasks(
    authenticated_client, monkeypatch
):
    project = _create_project(authenticated_client, name="Edited Markdown Project")

    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_completed_payload(project["name"])),
        },
    )

    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Add JWT authentication to the FastAPI backend and React frontend, including token refresh, protected routes, and regression tests.",
        },
    ).json()

    edited_markdown = "\n".join(
        [
            f"# Project: {project['name']}",
            "",
            "## Task List",
            "- [ ] TASK_START: Edited task | Use the edited markdown when committing | order=1 | P1 | effort=medium | profile=full_lifecycle",
        ]
    )
    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={
            "planner_markdown": edited_markdown,
            "selected_tasks": [
                {
                    "title": "Edited task",
                    "description": "Use the edited markdown when committing",
                    "execution_profile": "full_lifecycle",
                    "priority": 1,
                    "plan_position": 1,
                    "estimated_effort": "medium",
                    "include": True,
                }
            ],
        },
    )
    assert commit.status_code == 200
    payload = commit.json()
    assert payload["plan"]["markdown"] == edited_markdown
    assert [task["title"] for task in payload["tasks"]] == ["Edited task"]


def test_soft_deleted_project_blocks_planning_endpoints(
    authenticated_client, monkeypatch
):
    project = _create_project(
        authenticated_client, name="Soft Deleted Planning Project"
    )

    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_completed_payload(project["name"])),
        },
    )

    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Add JWT authentication to the FastAPI backend and React frontend, including token refresh, protected routes, and regression tests.",
        },
    ).json()

    delete_response = authenticated_client.delete(f"/api/v1/projects/{project['id']}")
    assert delete_response.status_code == 200

    get_response = authenticated_client.get(
        f"/api/v1/planning/sessions/{session['id']}"
    )
    assert get_response.status_code == 404

    respond_response = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/respond",
        json={"response": "Still trying to reply"},
    )
    assert respond_response.status_code == 404


def test_purge_soft_deleted_projects_removes_planning_records(
    authenticated_client, db_session
):
    workspace_path = normalize_project_workspace_path(
        "/tmp/purge-planning-project", "Purge Planning Project"
    )
    project = Project(
        name="Purge Planning Project",
        description="Old project",
        workspace_path=workspace_path,
        deleted_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    db_session.add(project)
    db_session.flush()

    planning_session = PlanningSession(
        project_id=project.id,
        title="Old Planning Session",
        prompt="Improve planner",
        status="completed",
        source_brain="local",
    )
    db_session.add(planning_session)
    db_session.flush()

    db_session.add(
        PlanningMessage(
            planning_session_id=planning_session.id,
            role="user",
            content="Prompt",
        )
    )
    db_session.add(
        PlanningArtifact(
            planning_session_id=planning_session.id,
            artifact_type="planner_markdown",
            filename="planner.md",
            content="# Project: Purge Planning Project\n\n## Task List\n- [ ] TASK_START: Keep clean | Remove planning rows on purge",
        )
    )
    task = Task(project_id=project.id, title="Old Task")
    db_session.add(task)
    db_session.flush()

    session = SessionModel(project_id=project.id, name="Old Session", status="stopped")
    db_session.add(session)
    db_session.flush()

    db_session.add(
        SessionState(session_id=session.id, project_id=project.id, current_step=1)
    )
    db_session.add(
        ConversationHistory(
            session_id=session.id,
            role="assistant",
            content="hello",
        )
    )
    db_session.add(
        PermissionRequest(
            project_id=project.id,
            session_id=session.id,
            task_id=task.id,
            operation_type="write",
            status="pending",
        )
    )
    db_session.add(
        TaskCheckpoint(
            task_id=task.id,
            session_id=session.id,
            checkpoint_type="error",
        )
    )
    db_session.commit()

    purge = authenticated_client.delete("/api/v1/projects/purge-soft-deleted")
    assert purge.status_code == 200

    assert db_session.query(PlanningSession).count() == 0
    assert db_session.query(PlanningMessage).count() == 0
    assert db_session.query(PlanningArtifact).count() == 0
    assert db_session.query(SessionState).count() == 0
    assert db_session.query(ConversationHistory).count() == 0
    assert db_session.query(PermissionRequest).count() == 0
    assert db_session.query(TaskCheckpoint).count() == 0
