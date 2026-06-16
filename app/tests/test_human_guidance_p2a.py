"""Tests for HG-P2a Guidance Selection Engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceUsage,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance_selection_service import (
    select_guidance_for_injection,
)
from app.services.human_guidance_service import create_guidance, record_guidance_usage


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p2a@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p2a-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p2a-session",
        status="running",
        is_active=True,
        instance_id="p2a-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _entry(
    id_: int,
    message: str,
    *,
    priority: int = 0,
    scope: str = "project",
    created_at: str = "2026-06-15T00:00:00+00:00",
    status: str = "active",
    expires_at: str | None = None,
    usage_count: int = 0,
) -> dict:
    return {
        "id": id_,
        "message": message,
        "priority": priority,
        "scope": scope,
        "created_at": created_at,
        "status": status,
        "expires_at": expires_at,
        "usage_count": usage_count,
    }


class TestGuidanceSelection:
    def test_higher_priority_wins(self):
        result = select_guidance_for_injection(
            [
                _entry(1, "low", priority=1),
                _entry(2, "high", priority=5),
            ],
            max_chars=500,
        )
        assert [e["id"] for e in result["selected"]] == [2, 1]

    def test_session_beats_project_on_equal_priority(self):
        result = select_guidance_for_injection(
            [
                _entry(1, "project", scope="project", priority=1),
                _entry(2, "session", scope="session", priority=1),
            ],
            max_chars=500,
        )
        assert [e["id"] for e in result["selected"]] == [2, 1]

    def test_recency_breaks_ties(self):
        result = select_guidance_for_injection(
            [
                _entry(1, "older", created_at="2026-06-14T00:00:00+00:00"),
                _entry(2, "newer", created_at="2026-06-15T00:00:00+00:00"),
            ],
            max_chars=500,
        )
        assert [e["id"] for e in result["selected"]] == [2, 1]

    def test_expired_archived_and_disabled_excluded(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        result = select_guidance_for_injection(
            [
                _entry(1, "keep"),
                _entry(2, "expired", expires_at=past),
                _entry(3, "archived", status="archived"),
                _entry(4, "disabled", status="disabled"),
            ],
            max_chars=500,
        )
        assert [e["id"] for e in result["selected"]] == [1]

    def test_budget_overflow_trims_entries_and_keeps_highest_score(self):
        result = select_guidance_for_injection(
            [
                _entry(1, "x" * 80, priority=1),
                _entry(2, "high", priority=10),
            ],
            max_chars=40,
        )
        assert [e["id"] for e in result["selected"]] == [2]
        assert [e["id"] for e in result["trimmed"]] == [1]

    def test_deterministic_ordering(self):
        entries = [
            _entry(1, "one", priority=1),
            _entry(2, "two", priority=1),
            _entry(3, "three", priority=2),
        ]
        first = select_guidance_for_injection(entries, max_chars=500)
        second = select_guidance_for_injection(entries, max_chars=500)
        assert first["selected"] == second["selected"]
        assert first["trimmed"] == second["trimmed"]

    def test_no_guidance_returns_empty_selection(self):
        result = select_guidance_for_injection([], max_chars=500)
        assert result["selected"] == []
        assert result["trimmed"] == []


class TestSelectionTelemetry:
    def test_selection_score_selected_and_trimmed_recorded(
        self, db_session: Session, user: User, project: Project
    ):
        selected_entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Selected telemetry.",
            priority=3,
        )
        trimmed_entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Trimmed telemetry.",
            priority=1,
        )

        record_guidance_usage(
            db_session,
            entries=[
                {
                    "id": selected_entry.id,
                    "message": selected_entry.message,
                    "selection_score": 325,
                }
            ],
            trimmed_entries=[
                {
                    "id": trimmed_entry.id,
                    "message": trimmed_entry.message,
                    "selection_score": 125,
                }
            ],
            project_id=project.id,
            session_id=None,
            task_id=None,
        )

        selected_row = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == selected_entry.id)
            .one()
        )
        trimmed_row = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == trimmed_entry.id)
            .one()
        )
        assert selected_row.selection_score == 325
        assert selected_row.selected is True
        assert selected_row.trimmed is False
        assert trimmed_row.selection_score == 125
        assert trimmed_row.selected is False
        assert trimmed_row.trimmed is True


class TestSelectionPreviewAndReadiness:
    def test_preview_returns_selected_and_trimmed_ids(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        monkeypatch,
    ):
        import app.services.orchestration.working_memory as wm_mod

        monkeypatch.setattr(wm_mod, "_INJECTION_BUDGET", 40)
        high, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="High.",
            priority=10,
        )
        low, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="x" * 80,
            priority=1,
        )

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")

        assert resp.status_code == 200
        data = resp.json()
        assert data["selected_ids"] == [high.id]
        assert data["trimmed_ids"] == [low.id]
        assert data["selected_count"] == 1
        assert data["trimmed_count"] == 1

    def test_readiness_returns_guidance_statistics(
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
            message="Readiness statistics.",
        )

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")

        assert resp.status_code == 200
        stats = resp.json()["guidance_statistics"]
        assert stats["active_guidance"] == 1
        assert stats["selected_guidance"] == 1
        assert stats["trimmed_guidance"] == 0
