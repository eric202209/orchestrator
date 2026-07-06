"""Tests for Human Guidance HG-P1d — conflict persistence and resolve workflow.

Covers:
Model/migration:
1.  Create HumanGuidanceConflict row
2.  Status transitions open → resolved → ignored → open
3.  Table indexes exist (via SQLAlchemy inspection)

Service:
4.  detection creates conflict row in DB
5.  detection writes LogEntry warning too
6.  duplicate detection does not create second conflict row
7.  duplicate detection does not create second LogEntry
8.  DB insert failure is non-fatal (no exception raised, returns [])
9.  conflict_patterns contains pattern name not raw keywords

Endpoint:
10. GET conflicts lists open conflicts
11. GET status=all includes resolved
12. GET excludes unrelated project
13. PATCH resolves conflict
14. PATCH ignores conflict
15. PATCH reopen clears resolved_at
16. PATCH wrong project returns 404
17. PATCH invalid status returns 422

Regression:
18. conflict detection still requires both flags ON
19. warning-only: task status unchanged
20. WM not mutated
21. HG-P1a CRUD still works
22. HG-P1b WM injection path unaffected
23. HG-P1c-1 rendered preview unaffected
24. HG-P1c-2 conflict LogEntry still written alongside conflict row
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    HumanGuidanceConflict,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
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
    u = User(email="p1d@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p1d-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p1d-session",
        status="running",
        is_active=True,
        instance_id="p1d-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _add_stdout_guidance(db: Session, user: User, project: Project) -> object:
    entry, _ = create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="All runtime output must go to stdout, never use logging.",
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


def _conflict_row_count(db: Session, project_id: int) -> int:
    return (
        db.query(HumanGuidanceConflict)
        .filter(HumanGuidanceConflict.project_id == project_id)
        .count()
    )


def _run_stdout_detection(db, project, session, user):
    return detect_guidance_task_conflicts(
        db,
        project_id=project.id,
        session_id=session.id,
        task_id=None,
        user_id=user.id,
        task_title="Use logging.getLogger for all output.",
        task_description="",
    )


# ── 1–3: model / migration ────────────────────────────────────────────────────


class TestModelMigration:
    def test_create_conflict_row(self, db_session: Session, project: Project):
        row = HumanGuidanceConflict(
            project_id=project.id,
            guidance_message="stdout only",
            conflict_excerpt="logging.getLogger",
            conflict_patterns='["stdout_vs_logging"]',
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)

        assert row.id is not None
        assert row.status == "open"
        assert row.severity == "warning"
        assert row.source == "heuristic"
        assert row.resolved_at is None

    def test_status_transitions(self, db_session: Session, project: Project):
        from datetime import UTC, datetime

        row = HumanGuidanceConflict(
            project_id=project.id,
            guidance_message="test",
            conflict_excerpt="exc",
        )
        db_session.add(row)
        db_session.commit()

        assert row.status == "open"

        row.status = "resolved"
        row.resolved_at = datetime.now(UTC)
        row.resolved_by = "operator@example.com"
        db_session.commit()
        db_session.refresh(row)
        assert row.status == "resolved"
        assert row.resolved_at is not None

        row.status = "ignored"
        db_session.commit()
        db_session.refresh(row)
        assert row.status == "ignored"

        row.status = "open"
        row.resolved_at = None
        row.resolved_by = None
        db_session.commit()
        db_session.refresh(row)
        assert row.status == "open"
        assert row.resolved_at is None

    def test_indexes_exist(self, db_session: Session):
        engine = db_session.get_bind()
        inspector = inspect(engine)
        index_names = {
            i["name"] for i in inspector.get_indexes("human_guidance_conflicts")
        }
        # Columns declared with index=True get auto-named indexes
        assert any("guidance_id" in n for n in index_names)
        assert any("project_id" in n for n in index_names)
        assert any("status" in n for n in index_names)


# ── 4–9: service ──────────────────────────────────────────────────────────────


class TestServicePersistence:
    def test_detection_creates_conflict_row(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        warnings = _run_stdout_detection(db_session, project, running_session, user)

        assert len(warnings) >= 1
        assert _conflict_row_count(db_session, project.id) >= 1

    def test_detection_writes_log_entry_too(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        assert _conflict_log_count(db_session, running_session.id) >= 1

    def test_duplicate_detection_no_second_row(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)
        _run_stdout_detection(db_session, project, running_session, user)

        assert _conflict_row_count(db_session, project.id) == 1

    def test_duplicate_detection_no_second_log_entry(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)
        _run_stdout_detection(db_session, project, running_session, user)

        assert _conflict_log_count(db_session, running_session.id) == 1

    def test_insert_failure_is_non_fatal(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)

        with patch.object(db_session, "commit", side_effect=Exception("db_down")):
            result = _run_stdout_detection(db_session, project, running_session, user)

        assert isinstance(result, list)

    def test_conflict_patterns_contain_pattern_name(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        warnings = _run_stdout_detection(db_session, project, running_session, user)

        assert len(warnings) >= 1
        patterns = warnings[0]["conflict_patterns"]
        assert isinstance(patterns, list)
        assert "stdout_vs_logging" in patterns

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        import json

        stored = json.loads(row.conflict_patterns)
        assert "stdout_vs_logging" in stored


# ── 10–17: endpoints ──────────────────────────────────────────────────────────


class TestConflictsEndpointP1d:
    def test_get_lists_open_conflicts(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == project.id
        assert data["total"] >= 1
        item = data["items"][0]
        assert item["id"] is not None
        assert item["status"] == "open"
        assert item["resolved"] is False
        assert item["severity"] == "warning"
        assert item["guidance_message"] is not None
        assert "stdout_vs_logging" in item["conflict_patterns"]

    def test_get_status_all_includes_resolved(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        # Resolve the conflict
        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        conflict_id = row.id

        patch_resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{conflict_id}",
            json={"status": "resolved"},
        )
        assert patch_resp.status_code == 200

        # status=open (default) excludes it
        resp_open = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp_open.json()["total"] == 0

        # status=all includes it
        resp_all = client.get(
            f"/api/v1/projects/{project.id}/guidance/conflicts?status=all"
        )
        assert resp_all.json()["total"] == 1
        assert resp_all.json()["items"][0]["resolved"] is True

    def test_get_excludes_unrelated_project(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        project2 = Project(name="p1d-other", workspace_path=None, user_id=user.id)
        db_session.add(project2)
        db_session.commit()
        db_session.refresh(project2)

        session2 = SessionModel(
            project_id=project2.id,
            name="p1d-other-session",
            status="running",
            is_active=True,
            instance_id="p1d-other-uuid",
        )
        db_session.add(session2)
        db_session.commit()
        db_session.refresh(session2)

        _add_stdout_guidance(db_session, user, project2)
        detect_guidance_task_conflicts(
            db_session,
            project_id=project2.id,
            session_id=session2.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_patch_resolves_conflict(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{row.id}",
            json={"status": "resolved", "resolution_note": "Approved override."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resolved"
        assert data["resolved"] is True
        assert data["resolved_at"] is not None
        assert data["resolved_by"] is not None
        assert data["resolution_note"] == "Approved override."

    def test_patch_ignores_conflict(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{row.id}",
            json={"status": "ignored"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"
        assert data["resolved"] is True

    def test_patch_reopen_clears_resolved_at(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        conflict_id = row.id

        client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{conflict_id}",
            json={"status": "resolved"},
        )

        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{conflict_id}",
            json={"status": "open"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "open"
        assert data["resolved"] is False
        assert data["resolved_at"] is None

    def test_patch_wrong_project_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )

        other_project = Project(
            name="p1d-404-project", workspace_path=None, user_id=user.id
        )
        db_session.add(other_project)
        db_session.commit()
        db_session.refresh(other_project)

        resp = client.patch(
            f"/api/v1/projects/{other_project.id}/guidance/conflicts/{row.id}",
            json={"status": "resolved"},
        )
        assert resp.status_code == 404

    def test_patch_invalid_status_returns_422(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{row.id}",
            json={"status": "deleted"},
        )
        assert resp.status_code == 422


# ── 18–24: regression ─────────────────────────────────────────────────────────


class TestRegressionP1d:
    def test_flags_off_no_conflict_rows(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        # Phase 18H: pin to repository defaults, independent of local `.env`.
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

        _add_stdout_guidance(db_session, user, project)
        result = run_conflict_detection_if_enabled(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for all output.",
            task_description="",
        )
        assert result == []
        assert _conflict_row_count(db_session, project.id) == 0
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_task_status_unchanged(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
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

        _add_stdout_guidance(db_session, user, project)
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

    def test_wm_not_mutated(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        wm_files = list(tmp_path.rglob("working_memory.json"))
        assert len(wm_files) == 0

    def test_p1a_crud_create_and_list(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Type hints required.", "scope": "project"},
        )
        assert resp.status_code == 201

        resp = client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_p1b_wm_injection_path_unaffected(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        from app.services.human_guidance.service import collect_active_guidance

        _add_stdout_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
        )
        assert len(entries) >= 1

    def test_p1c1_rendered_preview_unaffected(
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

    def test_p1c2_log_entry_still_written_alongside_conflict_row(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        _add_stdout_guidance(db_session, user, project)
        warnings = _run_stdout_detection(db_session, project, running_session, user)

        assert len(warnings) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1
        assert _conflict_row_count(db_session, project.id) >= 1


# ── Smoke ─────────────────────────────────────────────────────────────────────


class TestSmokeP1d:
    def test_smoke_flags_off_no_rows(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Smoke 1: both flags OFF → no conflict rows, no LogEntry warnings.

        Phase 18H: pin to repository defaults, independent of local `.env`.
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

        _add_stdout_guidance(db_session, user, project)
        result = run_conflict_detection_if_enabled(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            user_id=user.id,
            task_title="Use logging.getLogger for output.",
            task_description="",
        )
        assert result == []
        assert _conflict_row_count(db_session, project.id) == 0
        assert _conflict_log_count(db_session, running_session.id) == 0

    def test_smoke_both_flags_on_conflict_row_and_log(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """Smoke 2: both flags ON → conflict row created, LogEntry created, task not rejected."""
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

        _add_stdout_guidance(db_session, user, project)

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

        assert len(result) >= 1
        assert _conflict_row_count(db_session, project.id) >= 1
        assert _conflict_log_count(db_session, running_session.id) >= 1

        db_session.refresh(task)
        assert task.status == TaskStatus.PENDING

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["resolved"] is False

    def test_smoke_resolve_flow(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """Smoke 3: PATCH resolved → GET open excludes, GET all includes with resolved=True."""
        _add_stdout_guidance(db_session, user, project)
        _run_stdout_detection(db_session, project, running_session, user)

        row = (
            db_session.query(HumanGuidanceConflict)
            .filter(HumanGuidanceConflict.project_id == project.id)
            .first()
        )
        conflict_id = row.id

        patch_resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/conflicts/{conflict_id}",
            json={"status": "resolved", "resolution_note": "Operator approved."},
        )
        assert patch_resp.status_code == 200

        resp_open = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp_open.json()["total"] == 0

        resp_all = client.get(
            f"/api/v1/projects/{project.id}/guidance/conflicts?status=all"
        )
        all_data = resp_all.json()
        assert all_data["total"] == 1
        assert all_data["items"][0]["resolved"] is True
        assert all_data["items"][0]["status"] == "resolved"
