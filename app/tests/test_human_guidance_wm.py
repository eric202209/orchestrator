"""Tests for Human Guidance HG-P1b — WM integration and usage telemetry.

Covers:
1. collect_active_guidance scope inclusion/exclusion
2. collect_active_guidance ordering (task > session > project > global)
3. collect_active_guidance excludes disabled/archived/expired entries
4. collect_active_guidance priority ordering within scope
5. POST /sessions/{id}/operator-guidance flag OFF: LogEntry only, response unchanged
6. POST /sessions/{id}/operator-guidance flag ON: LogEntry + HumanGuidance
7. POST /sessions/{id}/operator-guidance flag ON: deduplicates HumanGuidance
8. write_working_memory flag OFF: legacy LogEntry path
9. write_working_memory flag ON: table-backed path with id/scope/status/priority
10. write_working_memory flag ON: records HumanGuidanceUsage rows
11. Usage telemetry failure does not fail write_working_memory
12. Existing TestHumanGuidance WM persistence tests still pass
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceUsage,
    LogEntry,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.service import (
    collect_active_guidance,
    create_guidance,
    record_guidance_usage,
)
from app.services.orchestration.working_memory import (
    _FILENAME,
    _HUMAN_GUIDANCE_LIMIT,
    write_working_memory,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="hg-wm@example.com",
        hashed_password="hashed",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="hg-wm-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="hg-wm-session",
        status="running",
        is_active=True,
        instance_id="hg-wm-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_orch_state(project_dir: str) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.plan = []
    state.changed_files = []
    state.validation_history = []
    state.project_context = ""
    return state


def _make_logger() -> MagicMock:
    return MagicMock()


def _make_task(task_id: int = 1, title: str = "test") -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.title = title
    t.plan_position = task_id
    return t


# ── 1. collect_active_guidance scope inclusion ────────────────────────────────


class TestCollectActiveGuidance:
    def test_includes_global_scope(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global rule.",
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert any(r["message"] == "Global rule." for r in results)

    def test_includes_project_scope(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project rule.",
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert any(r["message"] == "Project rule." for r in results)

    def test_includes_session_scope(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Session rule.",
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
        )
        assert any(r["message"] == "Session rule." for r in results)

    def test_excludes_disabled_entries(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Disabled rule.",
            status=GuidanceStatus.DISABLED,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert not any(r["message"] == "Disabled rule." for r in results)

    def test_excludes_archived_entries(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Archived rule.",
            status=GuidanceStatus.ARCHIVED,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert not any(r["message"] == "Archived rule." for r in results)

    def test_excludes_expired_entries(
        self, db_session: Session, user: User, project: Project
    ):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Expired rule.",
            status=GuidanceStatus.ACTIVE,
            expires_at=past,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert not any(r["message"] == "Expired rule." for r in results)

    def test_includes_future_expiry(
        self, db_session: Session, user: User, project: Project
    ):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Not yet expired.",
            expires_at=future,
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert any(r["message"] == "Not yet expired." for r in results)

    def test_ordering_task_before_session_before_project_before_global(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        # Create a task record for task-scope
        from app.models import Task, TaskStatus

        task = Task(
            project_id=project.id,
            title="scope-order-task",
            status=TaskStatus.RUNNING,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Session.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=task.id,
            scope="task",
            message="Task.",
        )

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=task.id,
        )
        messages = [r["message"] for r in results]
        assert messages.index("Task.") < messages.index("Session.")
        assert messages.index("Session.") < messages.index("Project.")
        assert messages.index("Project.") < messages.index("Global.")

    def test_priority_desc_within_scope(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Low priority.",
            priority=0,
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="High priority.",
            priority=10,
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        messages = [r["message"] for r in results]
        assert messages.index("High priority.") < messages.index("Low priority.")

    def test_returns_normalized_dict_fields(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Check fields.",
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert len(results) == 1
        r = results[0]
        assert "id" in r
        assert r["message"] == "Check fields."
        assert r["source"] == "operator_guidance"
        assert r["scope"] == "project"
        assert r["status"] == "active"
        assert "priority" in r
        assert "created_at" in r

    def test_returns_empty_when_db_is_none(self):
        results = collect_active_guidance(
            None,
            user_id=1,
            project_id=1,
            session_id=None,
            task_id=None,
        )
        assert results == []


# ── 5-7. operator-guidance endpoint ──────────────────────────────────────────


class TestOperatorGuidanceWithFlag:
    def test_flag_off_writes_log_entry_only(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", False)
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_cp:
            mock_cp.return_value = "cp_name"
            resp = authenticated_client.post(
                f"/api/v1/sessions/{running_session.id}/operator-guidance",
                json={"guidance": "Use stdout for all output."},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == running_session.id
        assert data["non_blocking"] is True

        # No HumanGuidance row should be created
        count = (
            db_session.query(HumanGuidance)
            .filter(HumanGuidance.session_id == running_session.id)
            .count()
        )
        assert count == 0

        # LogEntry should be present
        log = (
            db_session.query(LogEntry)
            .filter(
                LogEntry.session_id == running_session.id,
                LogEntry.message.like("[OPERATOR_GUIDANCE]%"),
            )
            .first()
        )
        assert log is not None

    def test_flag_on_writes_log_entry_and_human_guidance(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_cp:
            mock_cp.return_value = "cp_name"
            resp = authenticated_client.post(
                f"/api/v1/sessions/{running_session.id}/operator-guidance",
                json={"guidance": "Never use logging; use stdout."},
            )
        assert resp.status_code == 200
        assert resp.json()["non_blocking"] is True

        # HumanGuidance row should now exist
        hg = (
            db_session.query(HumanGuidance)
            .filter(HumanGuidance.session_id == running_session.id)
            .first()
        )
        assert hg is not None
        assert hg.message == "Never use logging; use stdout."
        assert hg.scope.value == "session"

        # LogEntry still present
        log = (
            db_session.query(LogEntry)
            .filter(
                LogEntry.session_id == running_session.id,
                LogEntry.message.like("[OPERATOR_GUIDANCE]%"),
            )
            .first()
        )
        assert log is not None

    def test_flag_on_deduplicates_human_guidance(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)
        body = {"guidance": "Deduplicate this guidance."}
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_cp:
            mock_cp.return_value = "cp_1"
            authenticated_client.post(
                f"/api/v1/sessions/{running_session.id}/operator-guidance",
                json=body,
            )
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_cp:
            mock_cp.return_value = "cp_2"
            authenticated_client.post(
                f"/api/v1/sessions/{running_session.id}/operator-guidance",
                json=body,
            )

        count = (
            db_session.query(HumanGuidance)
            .filter(
                HumanGuidance.session_id == running_session.id,
                HumanGuidance.message == "Deduplicate this guidance.",
            )
            .count()
        )
        assert count == 1


# ── 8-11. write_working_memory integration ────────────────────────────────────


class TestWriteWorkingMemoryWithFlag:
    def test_flag_off_uses_legacy_log_entry_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", False)

        entry = MagicMock()
        entry.message = "[OPERATOR_GUIDANCE] Legacy guidance rule."
        entry.task_id = 1
        entry.created_at = None

        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = [entry]
        mock_db = MagicMock()
        mock_db.query.return_value = mock_query

        state = _make_orch_state(str(tmp_path))
        state.session_id = 42
        task = _make_task(task_id=1)

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=mock_db,
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Legacy guidance rule." in messages

    def test_flag_on_uses_table_path_and_includes_metadata(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Table-backed guidance rule.",
            priority=5,
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        task = _make_task(task_id=1)

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=db_session,
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert len(data["human_guidance"]) == 1
        g = data["human_guidance"][0]
        assert g["message"] == "Table-backed guidance rule."
        assert g["scope"] == "session"
        assert g["status"] == "active"
        assert g["priority"] == 5
        assert "id" in g
        assert g["id"] is not None

    def test_flag_on_records_usage_rows(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)

        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Rule with usage telemetry.",
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        task = _make_task(task_id=2)

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=db_session,
        )

        usage = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == entry.id)
            .first()
        )
        assert usage is not None
        assert usage.rendered is True
        assert usage.trimmed is False
        assert usage.source == "human_guidance_table"
        assert usage.rendered_chars == len("Rule with usage telemetry.")

    def test_telemetry_failure_does_not_fail_write_working_memory(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Telemetry will fail but WM still written.",
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        task = _make_task(task_id=3)
        logger = _make_logger()

        with patch(
            "app.services.human_guidance.service.record_guidance_usage",
            side_effect=Exception("telemetry boom"),
        ):
            write_working_memory(
                orchestration_state=state,
                task=task,
                summary="done",
                logger=logger,
                db=db_session,
            )

        # WM file must still be written despite telemetry failure
        wm_path = tmp_path / ".agent" / _FILENAME
        assert wm_path.exists()
        data = json.loads(wm_path.read_text())
        assert len(data["human_guidance"]) == 1

    def test_flag_on_no_guidance_rows_writes_empty(
        self, tmp_path, db_session: Session, running_session: SessionModel, monkeypatch
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        task = _make_task(task_id=4)

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=db_session,
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert data["human_guidance"] == []


# ── record_guidance_usage unit tests ─────────────────────────────────────────


class TestRecordGuidanceUsage:
    def test_writes_usage_rows(self, db_session: Session, user: User, project: Project):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Usage record test.",
        )
        entries = [
            {
                "id": entry.id,
                "message": entry.message,
                "scope": "project",
                "status": "active",
                "priority": 0,
                "source": "operator_guidance",
                "created_at": "",
                "task_id": None,
            }
        ]
        record_guidance_usage(
            db_session,
            entries=entries,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )

        row = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == entry.id)
            .first()
        )
        assert row is not None
        assert row.rendered is True
        assert row.render_position == 0
        assert row.message_hash is not None

    def test_telemetry_never_raises(self, db_session: Session):
        # Should not raise even with bad data
        record_guidance_usage(
            db_session,
            entries=[{"id": None, "message": "x"}],
            project_id=None,
            session_id=None,
            task_id=None,
        )


# ── smoke-style integration tests ─────────────────────────────────────────────


class TestSmoke:
    """Smoke validation for flag OFF and flag ON paths."""

    def test_smoke_flag_off_wm_populated_from_log_entry(self, tmp_path, monkeypatch):
        """Smoke 1 — flag OFF: WM still populated from LogEntry legacy path."""
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", False)

        mock_entry = MagicMock()
        mock_entry.message = "[OPERATOR_GUIDANCE] Smoke flag-off guidance."
        mock_entry.task_id = 1
        mock_entry.created_at = None

        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = [mock_entry]
        mock_db = MagicMock()
        mock_db.query.return_value = mock_query

        state = _make_orch_state(str(tmp_path))
        state.session_id = 1
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=1),
            summary="T1 done",
            logger=_make_logger(),
            db=mock_db,
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Smoke flag-off guidance." in messages
        # No scope/id in legacy entries
        g = data["human_guidance"][0]
        assert g.get("source") == "operator_guidance"

    def test_smoke_flag_on_wm_has_table_metadata(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Smoke 2 — flag ON: WM entry contains id/scope/status/priority."""
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)

        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Smoke table-backed guidance.",
            priority=7,
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=1),
            summary="T1 done",
            logger=_make_logger(),
            db=db_session,
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert len(data["human_guidance"]) == 1
        g = data["human_guidance"][0]
        assert g["id"] == entry.id
        assert g["scope"] == "session"
        assert g["status"] == "active"
        assert g["priority"] == 7

        usage = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == entry.id)
            .first()
        )
        assert usage is not None
        assert usage.rendered is True
