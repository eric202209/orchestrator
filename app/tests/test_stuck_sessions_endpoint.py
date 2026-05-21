"""Tests for GET /sessions/stuck endpoint and worker-boot recovery scan.

10H-A: crash-recoverable session runtime — F6 + F12.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.session_lifecycle_service import (
    recover_stale_running_sessions,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _project(db, name="Stuck Test Project"):
    p = Project(name=name, workspace_path="/tmp/stuck_test")
    db.add(p)
    db.flush()
    return p


def _running_session(db, project, *, name="stuck-session", started_minutes_ago=20):
    past = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    s = SessionModel(
        project_id=project.id,
        name=name,
        status="running",
        is_active=True,
        started_at=past,
    )
    db.add(s)
    db.flush()
    return s


def _task(db, project, title="stuck-task"):
    t = Task(project_id=project.id, title=title, status=TaskStatus.PENDING)
    db.add(t)
    db.flush()
    return t


def _session_task_link(db, session, task, *, status=TaskStatus.RUNNING):
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=status,
    )
    db.add(link)
    db.flush()
    return link


def _task_execution(db, session, task, *, status=TaskStatus.RUNNING, attempt=1):
    te = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=status,
    )
    db.add(te)
    db.flush()
    return te


# ── GET /sessions/stuck ───────────────────────────────────────────────────────


def test_stuck_sessions_empty_when_no_sessions(authenticated_client, db_session):
    db_session.commit()
    resp = authenticated_client.get("/api/v1/sessions/stuck")
    assert resp.status_code == 200
    assert resp.json() == []


def test_stuck_sessions_returns_orphaned_running_session(
    authenticated_client, db_session
):
    """Running session with no TaskExecution in running state and stalled > default threshold."""
    project = _project(db_session)
    session = _running_session(db_session, project, started_minutes_ago=20)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/sessions/stuck?stalled_minutes=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["session_id"] == session.id
    assert data[0]["stalled_minutes"] >= 10


def test_stuck_sessions_excludes_session_with_running_task_execution(
    authenticated_client, db_session
):
    """Session with an in-progress TaskExecution is not stuck — execution is live."""
    project = _project(db_session)
    session = _running_session(db_session, project, started_minutes_ago=20)
    task = _task(db_session, project)
    _task_execution(db_session, session, task, status=TaskStatus.RUNNING)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/sessions/stuck?stalled_minutes=10")
    assert resp.status_code == 200
    assert resp.json() == []


def test_stuck_sessions_excludes_recently_started_session(
    authenticated_client, db_session
):
    """Session started 2 minutes ago is below the 10-minute threshold."""
    project = _project(db_session)
    _running_session(db_session, project, started_minutes_ago=2)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/sessions/stuck?stalled_minutes=10")
    assert resp.status_code == 200
    assert resp.json() == []


def test_stuck_sessions_excludes_inactive_session(authenticated_client, db_session):
    """Session with is_active=False is not included — it already stopped."""
    project = _project(db_session)
    past = datetime.now(timezone.utc) - timedelta(minutes=20)
    session = SessionModel(
        project_id=project.id,
        name="inactive-session",
        status="running",
        is_active=False,
        started_at=past,
    )
    db_session.add(session)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/sessions/stuck?stalled_minutes=10")
    assert resp.status_code == 200
    assert resp.json() == []


def test_stuck_sessions_returns_last_task_id(authenticated_client, db_session):
    """Stuck session response includes last_task_id from the latest SessionTask link."""
    project = _project(db_session)
    session = _running_session(db_session, project, started_minutes_ago=20)
    task = _task(db_session, project)
    _session_task_link(db_session, session, task, status=TaskStatus.PENDING)
    db_session.commit()

    resp = authenticated_client.get("/api/v1/sessions/stuck?stalled_minutes=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["last_task_id"] == task.id


# ── worker-boot recovery ──────────────────────────────────────────────────────


def test_worker_boot_recovery_recovers_stale_orphaned_session(db_session):
    """recover_stale_running_sessions(stale_after_seconds=60) recovers a session
    stalled for > 60 seconds with no running task link — mirrors on_worker_ready logic.
    """
    project = _project(db_session)
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    session = SessionModel(
        project_id=project.id,
        name="orphaned-on-boot",
        status="running",
        is_active=True,
        started_at=past,
    )
    db_session.add(session)
    db_session.commit()

    recovered = recover_stale_running_sessions(db_session, stale_after_seconds=60)
    assert len(recovered) == 1
    assert recovered[0]["session_id"] == session.id

    db_session.refresh(session)
    assert session.status == "stopped"
    assert session.is_active is False


def test_worker_boot_recovery_skips_session_with_running_task(db_session):
    """Session with a running SessionTask link is not recovered — execution is live."""
    project = _project(db_session)
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    session = SessionModel(
        project_id=project.id,
        name="active-session",
        status="running",
        is_active=True,
        started_at=past,
    )
    db_session.add(session)
    db_session.flush()

    task = _task(db_session, project)
    _session_task_link(db_session, session, task, status=TaskStatus.RUNNING)
    db_session.commit()

    recovered = recover_stale_running_sessions(db_session, stale_after_seconds=60)
    # The task is running so the session is not recovered by the no-link path,
    # but the task progress check may or may not recover it depending on log age.
    # The key assertion: the session is not prematurely stopped.
    db_session.refresh(session)
    # Still running — active task means no orphan recovery.
    assert session.is_active is True


def test_worker_boot_recovery_skips_fresh_session(db_session):
    """Session started 10 seconds ago is not recovered — within the 60s grace window."""
    project = _project(db_session)
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    session = SessionModel(
        project_id=project.id,
        name="fresh-session",
        status="running",
        is_active=True,
        started_at=recent,
    )
    db_session.add(session)
    db_session.commit()

    recovered = recover_stale_running_sessions(db_session, stale_after_seconds=60)
    assert recovered == []
    db_session.refresh(session)
    assert session.is_active is True
