"""Tests for GET /api/v1/mobile/dashboard (get_dashboard) — Phase 22B Cycle 2.

Covers:
  1. Happy path: zero projects/sessions/tasks returns zero counts.
  2. Counts reflect created Project/Session/Task/LogEntry rows.
  3. Soft-deleted projects are excluded from the active-project task counts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from textwrap import dedent

import pytest
from sqlalchemy import text

from app.config import settings
from app.models import LogEntry, Project, Task, TaskStatus
from app.models import Session as SessionModel

MOBILE_KEY = "test-mobile-key-22b"
MOBILE_HEADERS = {"X-OpenClaw-API-Key": MOBILE_KEY}


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_project(db, name: str = "TestProject", **extra) -> Project:
    project = Project(name=name, workspace_path=f"/tmp/{name}", **extra)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(
    db,
    project_id: int,
    status: str = "pending",
    is_active: bool = False,
) -> SessionModel:
    session = SessionModel(
        project_id=project_id,
        name="TestSession",
        status=status,
        is_active=is_active,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(
    db,
    project_id: int,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    task = Task(
        project_id=project_id,
        title="TestTask",
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_log(db, session_id: int) -> LogEntry:
    log = LogEntry(
        session_id=session_id,
        level="INFO",
        message="test log entry",
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def soft_delete_project(db, project: Project) -> None:
    """Set deleted_at on a project via the ORM (avoids raw-SQL text() issues)."""
    project.deleted_at = datetime.now(timezone.utc)
    db.commit()


# ── tests ─────────────────────────────────────────────────────────────────────


def _enable_mobile_key(monkeypatch):
    """Monkeypatch the mobile gateway key so the endpoint authenticates."""
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)


def test_dashboard_zero_counts(api_client, db_session, monkeypatch):
    """Happy path: empty DB returns zero counts."""
    _enable_mobile_key(monkeypatch)

    resp = api_client.get("/api/v1/mobile/dashboard", headers=MOBILE_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "timestamp" in body
    summary = body["summary"]

    assert summary["projects"] == 0
    assert summary["sessions"]["total"] == 0
    assert summary["sessions"]["active"] == 0
    assert summary["sessions"]["running"] == 0
    assert summary["tasks"]["total"] == 0
    assert summary["tasks"]["pending"] == 0
    assert summary["tasks"]["done"] == 0
    assert summary["tasks"]["running"] == 0
    assert summary["tasks"]["failed"] == 0
    assert summary["tasks"]["completion_rate"] == "N/A"
    assert body["alerts"] == []
    assert body["recent_activity"] == []


def test_dashboard_counts_reflect_created_entities(api_client, db_session, monkeypatch):
    """Counts reflect Project/Session/Task/LogEntry rows in the DB."""
    _enable_mobile_key(monkeypatch)

    # Create entities
    project = _make_project(db_session, "CountsProject")
    session = _make_session(db_session, project.id, status="running", is_active=True)
    _make_task(db_session, project.id, status=TaskStatus.PENDING)
    _make_task(db_session, project.id, status=TaskStatus.RUNNING)
    _make_task(db_session, project.id, status=TaskStatus.DONE)
    _make_task(db_session, project.id, status=TaskStatus.FAILED)
    _make_log(db_session, session.id)

    resp = api_client.get("/api/v1/mobile/dashboard", headers=MOBILE_HEADERS)
    assert resp.status_code == 200

    summary = resp.json()["summary"]

    # Project
    assert summary["projects"] == 1

    # Sessions
    assert summary["sessions"]["total"] == 1
    assert summary["sessions"]["active"] == 1  # is_active=True
    assert summary["sessions"]["running"] == 1  # status="running"

    # Tasks (all belong to one non-deleted project)
    assert summary["tasks"]["total"] == 4
    assert summary["tasks"]["pending"] == 1
    assert summary["tasks"]["running"] == 1
    assert summary["tasks"]["done"] == 1
    assert summary["tasks"]["failed"] == 1

    # completion_rate = 1/4 * 100 = 25.0%
    assert summary["tasks"]["completion_rate"] == "25.0%"


def test_dashboard_excludes_soft_deleted_project_tasks(
    api_client, db_session, monkeypatch
):
    """Soft-deleted projects are excluded from the active-project task counts."""
    _enable_mobile_key(monkeypatch)

    # Active project + task
    active_project = _make_project(db_session, "ActiveProject")
    _make_task(db_session, active_project.id, status=TaskStatus.PENDING)

    # Soft-deleted project + task
    deleted_project = _make_project(db_session, "DeletedProject")
    _make_task(db_session, deleted_project.id, status=TaskStatus.PENDING)
    soft_delete_project(db_session, deleted_project)

    resp = api_client.get("/api/v1/mobile/dashboard", headers=MOBILE_HEADERS)
    assert resp.status_code == 200

    summary = resp.json()["summary"]

    # Only the active project counts
    assert summary["projects"] == 1

    # Only the active project's task counts (deleted project's task excluded)
    assert summary["tasks"]["total"] == 1
    assert summary["tasks"]["pending"] == 1
    assert summary["tasks"]["done"] == 0
    assert summary["tasks"]["running"] == 0
    assert summary["tasks"]["failed"] == 0
    assert (
        summary["tasks"]["completion_rate"] == "0.0%"
    )  # 0 done / 1 total (pending task)
