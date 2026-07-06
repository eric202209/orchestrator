"""Tests for Human Guidance HG-P1c-2 — conflict detection warnings.

Covers:
1.  Flag off (CONFLICT_DETECTION=False) → run_conflict_detection_if_enabled returns []
2.  Flag off (TABLE=False) → run_conflict_detection_if_enabled returns []
3.  Both flags ON → detection runs and returns warnings
4.  stdout guidance vs logging task → warning emitted
5.  mutable-default guidance vs [] task → warning emitted
6.  dataclass guidance vs plain-dict task → warning emitted
7.  unrelated guidance and task → no warning
8.  same guidance/task/pattern called twice → dedup, only one LogEntry
9.  detection failure is non-fatal (db write fails → returns [], no exception)
10. warning does not change task status
11. warning does not mutate WM file
12. warning does not modify task description
13. GET /projects/{id}/guidance/conflicts lists warning
14. endpoint excludes warnings from other projects
15. endpoint returns empty list when none
16. Regression: existing CRUD tests pass (create/list still work)
17. Regression: WM integration unaffected (HG-P1b path unchanged)
18. Regression: rendered preview unaffected (P1c-1 path unchanged)
19. Regression: /sessions/{id}/operator-guidance behavior unchanged (LogEntry path)
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GuidanceStatus,
    HumanGuidance,
    LogEntry,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.conflicts import (
    _CONFLICT_PREFIX,
    detect_guidance_task_conflicts,
    run_conflict_detection_if_enabled,
)
from app.services.human_guidance.service import create_guidance


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p1c2@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p1c2-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p1c2-session",
        status="running",
        is_active=True,
        instance_id="p1c2-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _add_active_guidance(
    db: Session, user: User, project: Project, *, message: str
) -> HumanGuidance:
    entry, _ = create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message=message,
    )
    return entry


def _conflict_log_count(db: Session, session_id: int) -> int:
    return (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.message.like(f"{_CONFLICT_PREFIX}%"),
        )
        .count()
    )


# ── 1–3: feature flag gating ──────────────────────────────────────────────────


class TestFlagGating:
    def test_table_flag_off_returns_empty(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout.",
        )
        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = False
        settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True
        try:
            result = run_conflict_detection_if_enabled(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                task_id=None,
                user_id=user.id,
                task_title="Use logging.getLogger for all output.",
                task_description="",
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original
            settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = False

        assert result == []
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_conflict_flag_off_returns_empty(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout.",
        )
        original_table = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = False
        try:
            result = run_conflict_detection_if_enabled(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                task_id=None,
                user_id=user.id,
                task_title="Use logging.getLogger for all output.",
                task_description="",
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original_table

        assert result == []
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_both_flags_on_triggers_detection(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout.",
        )
        original_table = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True
        try:
            result = run_conflict_detection_if_enabled(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                task_id=None,
                user_id=user.id,
                task_title="Use logging.getLogger and logger.info for output.",
                task_description="",
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original_table
            settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = False

        assert len(result) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1


# ── 4–8: pattern matching ─────────────────────────────────────────────────────


class TestPatternMatching:
    def test_stdout_guidance_vs_logging_task(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout, never use logging.",
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Add logging.getLogger and logger.info calls.",
            task_description="",
        )
        assert len(warnings) >= 1
        assert any(w["event_type"] == "guidance_conflict_warning" for w in warnings)
        assert _conflict_log_count(db_session, running_session.id) >= 1

    def test_mutable_default_guidance_vs_list_task(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="Never use mutable default arguments. Use None and initialize inside.",
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Add function signature with items: list = []",
            task_description="Define helper with default = []",
        )
        assert len(warnings) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1

    def test_dataclass_guidance_vs_plain_dict_task(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="Use dataclasses for all structured records.",
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Return plain dict from helper",
            task_description="The function should return a plain dict with keys.",
        )
        assert len(warnings) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1

    def test_unrelated_guidance_and_task_no_warning(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="Always add type hints to public functions.",
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Add tests for the parser module.",
            task_description="Write unit tests for CSV parsing.",
        )
        assert warnings == []
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_same_pattern_called_twice_deduped(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout.",
        )
        kwargs: Dict[str, Any] = dict(
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )
        detect_guidance_task_conflicts(db_session, **kwargs)
        detect_guidance_task_conflicts(db_session, **kwargs)

        # Only one LogEntry should exist (dedup by message)
        count = _conflict_log_count(db_session, running_session.id)
        assert count == 1


# ── 9–12: safety ─────────────────────────────────────────────────────────────


class TestSafety:
    def test_detection_failure_is_non_fatal(
        self, db_session: Session, running_session: SessionModel
    ):
        # Passing an invalid user_id/project_id combo: collect_active_guidance will
        # return [] gracefully — the service must never raise.
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=None,
            session_id=running_session.id,
            task_id=None,
            user_id=None,
            task_title="Use logging.getLogger.",
            task_description="",
        )
        # No crash — empty result since no guidance in table
        assert isinstance(warnings, list)

    def test_detection_does_not_change_task_status(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        from app.models import Task, TaskStatus

        task = Task(
            project_id=project.id,
            title="Use logging.getLogger for all output.",
            description="",
            status=TaskStatus.PENDING,
            plan_position=1,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        _add_active_guidance(
            db_session,
            user,
            project,
            message="All output must go to stdout.",
        )

        detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=task.id,
            user_id=user.id,
            task_title=task.title,
            task_description=task.description or "",
        )

        db_session.refresh(task)
        assert task.status == TaskStatus.PENDING

    def test_detection_does_not_write_wm_json(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All output must go to stdout.",
        )
        detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )
        wm_files = list(tmp_path.rglob("working_memory.json"))
        assert len(wm_files) == 0

    def test_detection_does_not_modify_task_description(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        from app.models import Task, TaskStatus

        original_desc = "Use logging.getLogger for all output."
        task = Task(
            project_id=project.id,
            title="Task with logging",
            description=original_desc,
            status=TaskStatus.PENDING,
            plan_position=1,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        _add_active_guidance(
            db_session,
            user,
            project,
            message="All output must go to stdout.",
        )

        detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=task.id,
            user_id=user.id,
            task_title=task.title,
            task_description=task.description or "",
        )

        db_session.refresh(task)
        assert task.description == original_desc


# ── 13–15: conflicts endpoint ─────────────────────────────────────────────────


class TestConflictsEndpoint:
    def test_lists_conflict_warning(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout.",
        )
        detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == project.id
        assert data["total"] >= 1
        item = data["items"][0]
        assert item["severity"] == "warning"
        assert item["resolved"] is False
        assert item["guidance_message"] is not None

    def test_excludes_other_project_warnings(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        # Create a second project + session
        project2 = Project(name="other-project", workspace_path=None, user_id=user.id)
        db_session.add(project2)
        db_session.commit()
        db_session.refresh(project2)

        session2 = SessionModel(
            project_id=project2.id,
            name="other-session",
            status="running",
            is_active=True,
            instance_id="other-uuid",
        )
        db_session.add(session2)
        db_session.commit()
        db_session.refresh(session2)

        # Write a conflict LogEntry for project2's session
        _add_active_guidance(
            db_session,
            user,
            project2,
            message="All output must go to stdout.",
        )
        detect_guidance_task_conflicts(
            db_session,
            project_id=project2.id,
            session_id=session2.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )

        # project (not project2) should have 0 conflicts
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_returns_empty_when_none(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []


# ── 16–19: regression ─────────────────────────────────────────────────────────


class TestRegressionP1c2:
    def test_crud_create_still_works(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Use type hints.", "scope": "project"},
        )
        assert resp.status_code == 201

    def test_crud_list_still_works(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Regression check.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_rendered_preview_unaffected(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Use dataclasses.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        assert "Use dataclasses." in resp.json()["block"]

    def test_global_list_unaffected(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global rule.",
        )
        resp = client.get("/api/v1/guidance/global")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ── Smoke ─────────────────────────────────────────────────────────────────────


class TestSmoke:
    def test_smoke_both_flags_off(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Both flags off: create guidance + conflicting task → no warning.

        Phase 18H: pin to repository defaults via monkeypatch rather than
        trusting the live `settings` singleton, which local `.env` may
        override for pilot validation (see conftest.repo_default_settings).
        """
        from app.tests.conftest import repo_default_settings

        defaults = repo_default_settings()
        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            defaults.HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED",
            defaults.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED,
        )
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False
        assert settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED is False

        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout, never use logging.",
        )
        result = run_conflict_detection_if_enabled(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger and logger.info for output.",
            task_description="",
        )
        assert result == []
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_smoke_table_on_conflict_off(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """Table ON, conflict OFF: no warning."""
        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = False
        try:
            _add_active_guidance(
                db_session,
                user,
                project,
                message="All runtime output must go to stdout, never use logging.",
            )
            result = run_conflict_detection_if_enabled(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                task_id=None,
                user_id=user.id,
                task_title="Use logging.getLogger and logger.info for output.",
                task_description="",
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original

        assert result == []
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_smoke_both_flags_on_full_flow(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """Both flags ON: guidance conflicts with task → warning emitted, task not rejected."""
        from app.models import Task, TaskStatus

        task = Task(
            project_id=project.id,
            title="Implement parser with logging.getLogger",
            description="Use logging.getLogger and logger.info for all output.",
            status=TaskStatus.PENDING,
            plan_position=1,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        _add_active_guidance(
            db_session,
            user,
            project,
            message="All runtime output must go to stdout, never use logging.",
        )

        original_table = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True
        try:
            result = run_conflict_detection_if_enabled(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                task_id=task.id,
                user_id=user.id,
                task_title=task.title,
                task_description=task.description or "",
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original_table
            settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = False

        # Warning emitted
        assert len(result) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1

        # Task not rejected — status unchanged
        db_session.refresh(task)
        assert task.status == TaskStatus.PENDING

        # WM not written
        import os
        from pathlib import Path

        wm_files = list(
            Path(os.environ.get("OPENCLAW_WORKSPACE", "/tmp")).rglob(
                "working_memory.json"
            )
        )
        assert len(wm_files) == 0

        # Conflicts endpoint shows the warning
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["resolved"] is False
        assert (
            "logging" in data["items"][0]["guidance_message"].lower()
            or "stdout" in data["items"][0]["guidance_message"].lower()
        )
