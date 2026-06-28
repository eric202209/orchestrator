"""Tests for derive_orchestration_state_block and the additive API field.

Covers:
- current_phase derivation from session.status
- terminal_reason from latest failed task execution
- allowed_actions mapping
- dashboard endpoint includes orchestration_state
- mobile summary endpoint includes orchestration_state
"""

from __future__ import annotations

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.session_inspection_service import (
    derive_orchestration_state_block,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_project(db):
    project = Project(name="OrcStateTest", workspace_path="/tmp/ost_test")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status="pending"):
    session = SessionModel(
        project_id=project.id,
        name="State Test Session",
        description="test",
        status=status,
        is_active=status in {"running", "paused", "awaiting_input"},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(db, project):
    task = Task(
        project_id=project.id,
        title="Test Task",
        description="do something",
        status=TaskStatus.PENDING,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_failed_execution(db, session, task, *, failure_category="execution_error"):
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.FAILED,
        failure_category=failure_category,
        attempt_number=1,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


# ── current_phase derivation ──────────────────────────────────────────────────


def test_pending_session_returns_null_phase(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="pending")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] is None
    assert block["is_terminal"] is False


def test_running_session_returns_step_executing(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "step_executing"
    assert block["is_terminal"] is False
    assert block["coordinator"] == "ExecutionCoordinator"


def test_paused_session_returns_awaiting_input(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "awaiting_input"
    assert block["is_terminal"] is False
    assert block["coordinator"] == "ExecutionCoordinator"


def test_awaiting_input_session_returns_awaiting_input(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="awaiting_input")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "awaiting_input"
    assert block["coordinator"] == "ExecutionCoordinator"


def test_stopped_session_returns_cancelled(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="stopped")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "cancelled"
    assert block["is_terminal"] is True


def test_done_session_returns_done(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="done")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "done"
    assert block["is_terminal"] is True
    assert block["coordinator"] is None


def test_failed_session_returns_failed(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")
    block = derive_orchestration_state_block(db_session, session)
    assert block["current_phase"] == "failed"
    assert block["is_terminal"] is True


# ── terminal_reason derivation ────────────────────────────────────────────────


def test_terminal_reason_null_for_running_session(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running")
    task = _make_task(db_session, project)
    _make_failed_execution(db_session, session, task, failure_category="some_error")
    block = derive_orchestration_state_block(db_session, session)
    # running is not a terminal phase, so terminal_reason stays null
    assert block["terminal_reason"] is None


def test_terminal_reason_derived_from_latest_failed_execution(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")
    task = _make_task(db_session, project)
    _make_failed_execution(
        db_session, session, task, failure_category="repair_budget_exhausted"
    )
    block = derive_orchestration_state_block(db_session, session)
    assert block["terminal_reason"] == "repair_budget_exhausted"


def test_terminal_reason_uses_provided_execution_over_db_query(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")
    task = _make_task(db_session, project)
    db_exec = _make_failed_execution(
        db_session, session, task, failure_category="db_category"
    )
    # Simulate caller providing a pre-fetched execution with different category
    db_exec.failure_category = "provided_category"
    block = derive_orchestration_state_block(
        db_session, session, latest_task_execution=db_exec
    )
    assert block["terminal_reason"] == "provided_category"


def test_terminal_reason_null_when_no_failed_execution(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")
    block = derive_orchestration_state_block(db_session, session)
    assert block["terminal_reason"] is None


def test_terminal_reason_null_for_done_session_with_no_failures(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="done")
    block = derive_orchestration_state_block(db_session, session)
    assert block["terminal_reason"] is None


def test_terminal_reason_derived_for_stopped_session(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="stopped")
    task = _make_task(db_session, project)
    _make_failed_execution(
        db_session, session, task, failure_category="backend_capacity_limit"
    )
    block = derive_orchestration_state_block(db_session, session)
    assert block["terminal_reason"] == "backend_capacity_limit"


# ── allowed_actions mapping ───────────────────────────────────────────────────


def test_allowed_actions_for_pending(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="pending")
    block = derive_orchestration_state_block(db_session, session)
    assert "start_session" in block["allowed_actions"]
    assert "view_logs" in block["allowed_actions"]


def test_allowed_actions_for_running(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running")
    block = derive_orchestration_state_block(db_session, session)
    assert "pause_session" in block["allowed_actions"]
    assert "stop_session" in block["allowed_actions"]
    assert "resume_session" not in block["allowed_actions"]


def test_allowed_actions_for_paused(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused")
    block = derive_orchestration_state_block(db_session, session)
    assert "resume_session" in block["allowed_actions"]
    assert "stop_session" in block["allowed_actions"]
    assert "pause_session" not in block["allowed_actions"]


def test_allowed_actions_for_awaiting_input(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="awaiting_input")
    block = derive_orchestration_state_block(db_session, session)
    assert "submit_guidance" in block["allowed_actions"]
    assert "stop_session" in block["allowed_actions"]


def test_allowed_actions_for_stopped(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="stopped")
    block = derive_orchestration_state_block(db_session, session)
    assert "resume_session" in block["allowed_actions"]
    assert "stop_session" not in block["allowed_actions"]


def test_allowed_actions_for_failed(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")
    block = derive_orchestration_state_block(db_session, session)
    assert "retry_task" in block["allowed_actions"]
    assert "resume_session" in block["allowed_actions"]


def test_allowed_actions_for_done(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="done")
    block = derive_orchestration_state_block(db_session, session)
    assert "view_logs" in block["allowed_actions"]
    assert "view_timeline" in block["allowed_actions"]
    assert "start_session" in block["allowed_actions"]


# ── dashboard endpoint includes orchestration_state ───────────────────────────


def test_dashboard_session_endpoint_includes_orchestration_state(authenticated_client):
    from app.models import Project, Session as SessionModel

    client = authenticated_client
    project_resp = client.post(
        "/api/v1/projects",
        json={"name": "OrcStateEndpointTest", "workspace_path": "/tmp/oset"},
    )
    assert project_resp.status_code == 201
    project_id = project_resp.json()["id"]

    session_resp = client.post(
        "/api/v1/sessions",
        json={"project_id": project_id, "name": "State Test"},
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    resp = client.get(f"/api/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "orchestration_state" in body
    state = body["orchestration_state"]
    assert state is not None
    assert "current_phase" in state
    assert "is_terminal" in state
    assert "allowed_actions" in state
    assert isinstance(state["allowed_actions"], list)


# ── mobile summary endpoint includes orchestration_state ──────────────────────


def test_mobile_summary_endpoint_includes_orchestration_state(api_client, db_session):
    from app.config import settings

    settings.MOBILE_GATEWAY_API_KEY = "test-mobile-key"

    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running")

    resp = api_client.get(
        f"/api/v1/mobile/sessions/{session.id}/summary",
        headers={"X-OpenClaw-API-Key": "test-mobile-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "orchestration_state" in body
    state = body["orchestration_state"]
    assert state is not None
    assert state["current_phase"] == "step_executing"
    assert state["coordinator"] == "ExecutionCoordinator"
    assert "pause_session" in state["allowed_actions"]
