"""Tests for Human Guidance HG-P1e — activation controls and readiness status.

Covers:
Model:
1.  Create project activation row
2.  Create session activation row
3.  Disable activation
4.  Session activation overrides project

Service:
5.  Effective activation false when global flags off
6.  Effective activation true when global + project on
7.  Session override: disabled session overrides enabled project
8.  Readiness blocking reasons include global flags off
9.  Readiness detects no active guidance
10. Readiness handles DB failure gracefully (non-fatal)

API:
11. GET project readiness (default OFF)
12. PATCH project activation
13. POST disable project activation
14. GET session readiness
15. PATCH session activation
16. POST disable session activation
17. Invalid activation payload rejected (422)

Regression:
18. Human Guidance table path still defaults OFF
19. Conflict detection still defaults OFF
20. Existing CRUD endpoints unchanged
21. Existing conflict endpoints unchanged
22. Existing WM behavior unchanged

Smoke:
S1. All global flags OFF, project activation ON → effective all false, blocking reasons include global flags
S2. Global flag ON, project activation ON, guidance exists → ready=true
S3. Project activation ON, session disabled → session readiness shows activation_disabled
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    HumanGuidanceActivation,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.activation import (
    disable_activation,
    get_effective_activation,
    readiness_status,
    set_project_activation,
    set_session_activation,
)
from app.services.human_guidance.service import create_guidance


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p1e@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p1e-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p1e-session",
        status="running",
        is_active=True,
        instance_id="p1e-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _all_flags_on() -> dict:
    return {
        "table_enabled": True,
        "persistence_enabled": True,
        "render_enabled": True,
        "injection_enabled": True,
        "conflict_detection_enabled": True,
    }


# ── 1–4: model ────────────────────────────────────────────────────────────────


class TestModelActivation:
    def test_create_project_activation(self, db_session: Session, project: Project):
        row = HumanGuidanceActivation(
            project_id=project.id,
            scope="project",
            table_enabled=True,
            status="enabled",
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)

        assert row.id is not None
        assert row.scope == "project"
        assert row.table_enabled is True
        assert row.status == "enabled"
        assert row.disabled_at is None

    def test_create_session_activation(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        row = HumanGuidanceActivation(
            session_id=running_session.id,
            project_id=project.id,
            scope="session",
            table_enabled=True,
            status="enabled",
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)

        assert row.scope == "session"
        assert row.session_id == running_session.id
        assert row.status == "enabled"

    def test_disable_activation(self, db_session: Session, project: Project):
        from datetime import UTC, datetime

        row = HumanGuidanceActivation(
            project_id=project.id,
            scope="project",
            table_enabled=True,
            status="enabled",
        )
        db_session.add(row)
        db_session.commit()

        row.status = "disabled"
        row.disabled_at = datetime.now(UTC)
        row.disabled_by = "admin@example.com"
        db_session.commit()
        db_session.refresh(row)

        assert row.status == "disabled"
        assert row.disabled_at is not None
        assert row.disabled_by == "admin@example.com"

    def test_session_overrides_project_in_db(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        # Project activation: enabled
        set_project_activation(db_session, project.id, _all_flags_on())

        # Session activation: disabled
        disable_activation(db_session, "session", running_session.id)

        result = get_effective_activation(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
        )
        # Disabled session row overrides enabled project
        assert result["requested"]["status"] == "disabled"
        assert result["effective"]["table_enabled"] is False


# ── 5–10: service ─────────────────────────────────────────────────────────────


class TestServiceActivation:
    def test_effective_false_when_global_flags_off(
        self, db_session: Session, project: Project, monkeypatch
    ):
        # Phase 18H: pin to repository defaults, independent of local `.env`.
        # This test's effective-activation result is bounded by three global
        # flags (table, persistence, injection), so all three must be pinned.
        from app.tests.conftest import repo_default_settings

        defaults = repo_default_settings()
        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            defaults.HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        monkeypatch.setattr(
            settings,
            "WORKING_MEMORY_PERSISTENCE_ENABLED",
            defaults.WORKING_MEMORY_PERSISTENCE_ENABLED,
        )
        monkeypatch.setattr(
            settings,
            "WORKING_MEMORY_INJECTION_ENABLED",
            defaults.WORKING_MEMORY_INJECTION_ENABLED,
        )
        # All global flags are False by default
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False

        set_project_activation(db_session, project.id, _all_flags_on())
        result = get_effective_activation(db_session, project_id=project.id)

        # Requested says on
        assert result["requested"]["table_enabled"] is True
        # Effective is off — global flag bounds it
        assert result["effective"]["table_enabled"] is False
        assert result["effective"]["persistence_enabled"] is False
        assert result["effective"]["injection_enabled"] is False

    def test_effective_true_when_global_and_project_on(
        self, db_session: Session, project: Project
    ):
        set_project_activation(db_session, project.id, {"table_enabled": True})

        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        try:
            result = get_effective_activation(db_session, project_id=project.id)
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original

        assert result["requested"]["table_enabled"] is True
        assert result["effective"]["table_enabled"] is True

    def test_session_override_disabled(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        # Project is enabled
        set_project_activation(db_session, project.id, _all_flags_on())
        # Session is explicitly disabled
        disable_activation(db_session, "session", running_session.id)

        result = get_effective_activation(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
        )
        assert result["requested"]["status"] == "disabled"
        assert result["requested"]["table_enabled"] is False

    def test_readiness_blocking_includes_global_flags_off(
        self, db_session: Session, project: Project, monkeypatch
    ):
        # Phase 18H: pin to repository defaults, independent of local `.env`.
        from app.tests.conftest import repo_default_settings

        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            repo_default_settings().HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False
        set_project_activation(db_session, project.id, _all_flags_on())

        result = readiness_status(db_session, project_id=project.id)

        assert result["ready"] is False
        assert "global_table_flag_off" in result["blocking_reasons"]

    def test_readiness_detects_no_active_guidance(
        self, db_session: Session, project: Project
    ):
        set_project_activation(db_session, project.id, _all_flags_on())

        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        try:
            result = readiness_status(db_session, project_id=project.id)
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original

        assert "no_active_guidance" in result["blocking_reasons"]
        assert result["ready"] is False

    def test_readiness_handles_db_failure_gracefully(
        self, db_session: Session, project: Project
    ):
        with patch.object(db_session, "query", side_effect=Exception("db_down")):
            result = readiness_status(db_session, project_id=project.id)

        assert result["ready"] is False
        assert isinstance(result["blocking_reasons"], list)
        assert len(result["blocking_reasons"]) > 0


# ── 11–17: API ────────────────────────────────────────────────────────────────


class TestActivationAPI:
    def test_get_project_readiness_defaults_off(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        monkeypatch,
    ):
        # Phase 18H: pin to repository defaults, independent of local `.env`.
        from app.tests.conftest import repo_default_settings

        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            repo_default_settings().HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == project.id
        assert data["ready"] is False
        assert "blocking_reasons" in data
        assert "global_flags" in data
        assert "global_table_flag_off" in data["blocking_reasons"]
        assert "activation_disabled" in data["blocking_reasons"]

    def test_patch_project_activation(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/activation",
            json={"table_enabled": True, "persistence_enabled": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "project"
        assert data["status"] == "enabled"
        assert data["table_enabled"] is True
        assert data["persistence_enabled"] is True
        assert data["injection_enabled"] is False

    def test_disable_project_activation(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        # First enable
        client.patch(
            f"/api/v1/projects/{project.id}/guidance/activation",
            json={"table_enabled": True},
        )
        # Then disable
        resp = client.post(f"/api/v1/projects/{project.id}/guidance/activation/disable")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"
        assert data["disabled_at"] is not None

    def test_get_session_readiness(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        resp = client.get(f"/api/v1/sessions/{running_session.id}/guidance/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == project.id
        assert data["session_id"] == running_session.id
        assert data["ready"] is False

    def test_patch_session_activation(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        resp = client.patch(
            f"/api/v1/sessions/{running_session.id}/guidance/activation",
            json={"table_enabled": True, "render_enabled": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "session"
        assert data["status"] == "enabled"
        assert data["table_enabled"] is True
        assert data["render_enabled"] is True

    def test_disable_session_activation(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        client.patch(
            f"/api/v1/sessions/{running_session.id}/guidance/activation",
            json={"table_enabled": True},
        )
        resp = client.post(
            f"/api/v1/sessions/{running_session.id}/guidance/activation/disable"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"

    def test_invalid_activation_payload_rejected(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        # null is not a valid bool
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/activation",
            json={"table_enabled": None},
        )
        assert resp.status_code == 422


# ── 18–22: regression ─────────────────────────────────────────────────────────


class TestRegressionP1e:
    def test_table_flag_defaults_off(self, monkeypatch):
        # Phase 18H: pin to repository defaults, independent of local `.env`.
        from app.tests.conftest import repo_default_settings

        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            repo_default_settings().HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False

    def test_conflict_detection_defaults_off(self):
        assert settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED is False

    def test_crud_endpoints_unchanged(
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
        assert resp.json()["total"] == 1

    def test_conflict_endpoints_unchanged(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/conflicts")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_wm_behavior_unchanged(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        """WM does not write files when flags are off, even if activation is set."""
        from app.services.human_guidance.service import collect_active_guidance

        set_project_activation(db_session, project.id, _all_flags_on())
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Type hints required.",
        )
        # collect_active_guidance works (reads DB directly, no flag gate)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
        )
        assert len(entries) >= 1

        # No WM files written (WM flags off)
        wm_files = list(tmp_path.rglob("working_memory.json"))
        assert len(wm_files) == 0


# ── Smoke ─────────────────────────────────────────────────────────────────────


class TestSmokeP1e:
    def test_smoke_flags_off_effective_all_false(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        monkeypatch,
    ):
        """Smoke 1: all global flags OFF, project activation ON → effective all false.

        Phase 18H: pin to repository defaults, independent of local `.env`.
        """
        from app.tests.conftest import repo_default_settings

        monkeypatch.setattr(
            settings,
            "HUMAN_GUIDANCE_TABLE_ENABLED",
            repo_default_settings().HUMAN_GUIDANCE_TABLE_ENABLED,
        )
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False

        # Enable project activation with all flags
        resp = client.patch(
            f"/api/v1/projects/{project.id}/guidance/activation",
            json=_all_flags_on(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "enabled"

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")
        data = resp.json()

        assert data["ready"] is False
        assert data["requested"]["table_enabled"] is True  # activation says yes
        assert data["effective"]["table_enabled"] is False  # global flag says no
        assert "global_table_flag_off" in data["blocking_reasons"]
        assert data["global_flags"]["HUMAN_GUIDANCE_TABLE_ENABLED"] is False

    def test_smoke_global_on_project_on_guidance_exists_ready(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        """Smoke 2: global flag ON, project activation ON, guidance exists → ready=True."""
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Use type hints.",
        )

        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        try:
            client.patch(
                f"/api/v1/projects/{project.id}/guidance/activation",
                json={"table_enabled": True},
            )
            resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original

        data = resp.json()
        assert data["ready"] is True
        assert data["blocking_reasons"] == []

    def test_smoke_session_disabled_overrides_project(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """Smoke 3: project ON, session explicitly disabled → session readiness shows disabled."""
        original = settings.HUMAN_GUIDANCE_TABLE_ENABLED
        settings.HUMAN_GUIDANCE_TABLE_ENABLED = True
        try:
            client.patch(
                f"/api/v1/projects/{project.id}/guidance/activation",
                json={"table_enabled": True},
            )
            client.post(
                f"/api/v1/sessions/{running_session.id}/guidance/activation/disable"
            )
            resp = client.get(
                f"/api/v1/sessions/{running_session.id}/guidance/readiness"
            )
        finally:
            settings.HUMAN_GUIDANCE_TABLE_ENABLED = original

        data = resp.json()
        assert data["ready"] is False
        assert "activation_disabled" in data["blocking_reasons"]
        assert data["requested"]["status"] == "disabled"
