"""HG-P4d: Mobile gateway guidance routes — 12 tests."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import HumanGuidance, Project
from app.models import GuidanceStatus, User


MOBILE_KEY = "p4d-mobile-test-key"
HEADERS = {"X-OpenClaw-API-Key": MOBILE_KEY}


def _make_user(db) -> User:
    user = User(email="p4d-mobile@example.com", hashed_password="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_project(db, user_id: int) -> Project:
    project = Project(
        name="P4d Mobile Test",
        workspace_path="/tmp/p4d_mobile",
        user_id=user_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_guidance(
    db, project, user_id: int, message: str = "Use type hints.", priority: int = 50
):
    from app.services.human_guidance.service import create_guidance

    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project.id,
        scope="project",
        message=message,
        priority=priority,
    )
    return entry


def _enable_activation(db, project_id: int):
    from app.services.human_guidance.activation import set_project_activation

    return set_project_activation(
        db,
        project_id,
        {
            "table_enabled": True,
            "persistence_enabled": True,
            "render_enabled": True,
            "injection_enabled": True,
            "conflict_detection_enabled": True,
        },
        enabled_by="test",
    )


# ── 1. Readiness succeeds with mobile key ────────────────────────────────────


def test_mobile_readiness_succeeds_with_key(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.get(
        f"/api/v1/mobile/projects/{project.id}/guidance/readiness",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert "ready" in data
    assert "blocking_reasons" in data
    assert "guidance_statistics" in data


# ── 2. Readiness 401 without key ─────────────────────────────────────────────


def test_mobile_readiness_401_without_key(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.get(f"/api/v1/mobile/projects/{project.id}/guidance/readiness")
    assert resp.status_code == 401


# ── 3. List guidance succeeds ─────────────────────────────────────────────────


def test_mobile_list_guidance_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)
    _make_guidance(db_session, project, user.id, "Check return types.")

    resp = api_client.get(
        f"/api/v1/mobile/projects/{project.id}/guidance?status=active",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["total"] == 1
    assert data["items"][0]["message"] == "Check return types."


# ── 4. Create guidance succeeds ───────────────────────────────────────────────


def test_mobile_create_guidance_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.post(
        f"/api/v1/mobile/projects/{project.id}/guidance",
        json={
            "message": "Avoid bare except clauses.",
            "scope": "project",
            "priority": 80,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["message"] == "Avoid bare except clauses."
    assert data["priority"] == 80
    assert data["created_by"] == "mobile"
    assert data["project_id"] == project.id


# ── 5. Create uses all/all/all defaults ───────────────────────────────────────


def test_mobile_create_uses_default_targets(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.post(
        f"/api/v1/mobile/projects/{project.id}/guidance",
        json={"message": "Use pathlib over os.path.", "scope": "project"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["backend_targets"] == ["all"]
    assert data["model_targets"] == ["all"]
    assert data["purpose_targets"] == ["all"]


# ── 6. Rendered preview succeeds ──────────────────────────────────────────────


def test_mobile_rendered_preview_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)
    _make_guidance(db_session, project, user.id, "Prefer explicit over implicit.")

    resp = api_client.get(
        f"/api/v1/mobile/projects/{project.id}/guidance/rendered",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["selected_count"] == 1
    assert "Prefer explicit over implicit." in data["block"]
    assert "selected_ids" in data
    assert "selection_metadata" in data


# ── 7. Conflicts list succeeds ────────────────────────────────────────────────


def test_mobile_conflicts_list_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.get(
        f"/api/v1/mobile/projects/{project.id}/guidance/conflicts?status=open",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["total"] == 0
    assert data["items"] == []


# ── 8. Conflict patch succeeds ────────────────────────────────────────────────


def test_mobile_conflict_patch_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)
    guidance = _make_guidance(db_session, project, user.id, "Check return types.")

    from app.models import HumanGuidanceConflict

    conflict = HumanGuidanceConflict(
        project_id=project.id,
        guidance_id=guidance.id,
        guidance_message=guidance.message,
        task_id=None,
        task_title="Test Task",
        conflict_excerpt="excerpt",
        conflict_patterns="[]",
        severity="warning",
        status="open",
    )
    db_session.add(conflict)
    db_session.commit()
    db_session.refresh(conflict)

    resp = api_client.patch(
        f"/api/v1/mobile/projects/{project.id}/guidance/conflicts/{conflict.id}",
        json={"status": "resolved"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["resolved_by"] == "mobile"


# ── 9. Activation patch succeeds ──────────────────────────────────────────────


def test_mobile_activation_patch_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.patch(
        f"/api/v1/mobile/projects/{project.id}/guidance/activation",
        json={
            "table_enabled": True,
            "persistence_enabled": True,
            "render_enabled": True,
            "injection_enabled": True,
            "conflict_detection_enabled": True,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "enabled"
    assert data["table_enabled"] is True
    assert data["enabled_by"] == "mobile"


# ── 10. Activation disable succeeds ──────────────────────────────────────────


def test_mobile_activation_disable_succeeds(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)
    _enable_activation(db_session, project.id)

    resp = api_client.post(
        f"/api/v1/mobile/projects/{project.id}/guidance/activation/disable",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "disabled"


# ── 11. Existing web guidance tests unaffected ───────────────────────────────


def test_web_guidance_create_still_requires_jwt(api_client, db_session, monkeypatch):
    """Web guidance endpoints still reject unauthenticated requests."""
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.post(
        f"/api/v1/projects/{project.id}/guidance",
        json={"message": "Test", "scope": "project"},
    )
    assert resp.status_code == 401


# ── 12. Mobile key rejected on web guidance endpoints ────────────────────────


def test_mobile_key_not_accepted_on_web_endpoints(api_client, db_session, monkeypatch):
    """Mobile gateway key does not grant access to JWT-protected endpoints."""
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id)

    resp = api_client.get(
        f"/api/v1/projects/{project.id}/guidance/readiness",
        headers=HEADERS,
    )
    assert resp.status_code == 401
