from __future__ import annotations

import json

from app.config import settings
from app.models import LogEntry, Project, Session as SessionModel, Task, TaskExecution
from app.services.agents.backend_lane_snapshot import (
    snapshot_for_task_execution_id,
    snapshot_from_task_execution,
)
from app.services.orchestration.phases.planning_support import (
    _classify_planning_timeout_failure,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.workspace.artifact_namespace import (
    artifact_namespace_payload,
    event_journal_dir,
    task_report_dir,
)


def test_backend_lane_snapshot_reads_run_start_identity_and_task_execution(db_session):
    execution = TaskExecution(
        session_id=42,
        task_id=7,
        attempt_number=1,
        backend_id="local_openclaw",
    )
    identity = {
        "source": "task_started_event",
        "captured_at": "2026-06-04T00:00:00+00:00",
        "build": {"build_git_sha": "build-sha", "repo_git_sha": "repo-sha"},
        "lanes": {
            "planning": "openai_responses_api",
            "execution": "local_openclaw",
            "repair": "direct_ollama",
            "debug_repair": "openai_responses_api",
        },
        "models": {
            "planner": "gpt-5",
            "execution": "local",
            "planning_repair": "qwen-repair",
            "debug_repair": "gpt-5-mini",
        },
        "config": {
            "source": "task_started_event",
            "effective": {"agent_backend": "local_openclaw", "agent_model": "local"},
        },
    }

    snapshot = snapshot_from_task_execution(execution, identity)

    assert snapshot["source"] == "task_started_event"
    assert snapshot["lanes"]["planning"] == {
        "role": "planning",
        "backend_id": "openai_responses_api",
        "model": "gpt-5",
    }
    assert snapshot["lanes"]["execution"]["backend_id"] == "local_openclaw"
    assert snapshot["lanes"]["repair"]["backend_id"] == "direct_ollama"
    assert snapshot["lanes"]["debug_repair"]["model"] == "gpt-5-mini"
    assert snapshot["build"]["build_git_sha"] == "build-sha"
    assert snapshot["legacy"]["task_execution_backend_id"] == "local_openclaw"


def test_backend_lane_snapshot_reads_existing_log_metadata_without_writes(db_session):
    project = Project(id=1, name="Snapshot Project")
    session = SessionModel(id=2, project_id=project.id, name="Snapshot Session")
    task = Task(id=3, project_id=project.id, title="Snapshot Task")
    execution = TaskExecution(
        id=4,
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        backend_id="local_openclaw",
    )
    db_session.add_all([project, session, task, execution])
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            level="INFO",
            message="Task started",
            log_metadata=json.dumps(
                {
                    "run_start_runtime_identity": {
                        "source": "task_started_event",
                        "lanes": {"execution": "local_openclaw"},
                        "models": {"execution": "local"},
                    }
                }
            ),
        )
    )
    db_session.commit()

    before_log_count = db_session.query(LogEntry).count()
    snapshot = snapshot_for_task_execution_id(db_session, execution.id)

    assert snapshot is not None
    assert snapshot["task_execution"]["id"] == execution.id
    assert snapshot["lanes"]["execution"]["backend_id"] == "local_openclaw"
    assert db_session.query(LogEntry).count() == before_log_count


def test_artifact_namespace_wraps_existing_openclaw_paths_without_renaming(tmp_path):
    payload = artifact_namespace_payload()

    assert payload["canonical_owner"] == "orchestrator"
    assert payload["compatibility_namespace"] == "openclaw"
    assert payload["event_journal_root"] == ".agent/events"
    assert payload["task_report_root"] == ".agent/task-reports"
    assert str(event_journal_dir(tmp_path)).endswith(".agent/events")
    assert str(task_report_dir(tmp_path)).endswith(".agent/task-reports")


def test_start_openclaw_route_remains_backend_neutral_alias(
    authenticated_client, db_session, monkeypatch
):
    project = Project(id=31, name="Compat Project", user_id=1)
    session = SessionModel(id=32, project_id=project.id, name="Compat Session")
    db_session.add_all([project, session])
    db_session.commit()

    async def fake_start_agent_session_payload(db, session_id, *, task_description):
        return {
            "status": "started",
            "session_id": session_id,
            "task_description": task_description,
        }

    monkeypatch.setattr(
        "app.api.v1.endpoints.sessions._start_agent_session_payload",
        fake_start_agent_session_payload,
    )

    response = authenticated_client.post(
        f"/api/v1/sessions/{session.id}/start-openclaw",
        json={"task_description": "Run through backend-neutral alias"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "started"
    assert response.json()["session_id"] == session.id


def test_mobile_api_accepts_legacy_openclaw_header(authenticated_client, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", "mobile-test-key")
    monkeypatch.setattr(settings, "OPENCLAW_API_KEY", "")

    response = authenticated_client.get(
        "/api/v1/mobile/providers/status",
        headers={"X-OpenClaw-API-Key": "mobile-test-key"},
    )

    assert response.status_code == 200
    assert "providers" in response.json()


def test_planning_openclaw_lock_contention_reason_remains_pinned():
    message = (
        "OpenClaw planning failed: session file locked (timeout 10000ms): "
        "pid=123 /root/.openclaw/agents/main/sessions/sessions.json.lock"
    )

    assert PlannerService.is_openclaw_lock_contention(message) is True
    assert _classify_planning_timeout_failure(TimeoutError(message), None) == (
        "planning_openclaw_lock_contention"
    )
