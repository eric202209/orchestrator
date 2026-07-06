"""Tests for Human Guidance HG-P1a.

Covers:
- Model: create guidance, create revision, archive, status transitions
- Service: deduplication, update creates revision, archive preserves record, list filtering
- API: POST /projects/{id}/guidance, GET, PATCH, DELETE /guidance/{id}
- Regression: POST /sessions/{session_id}/operator-guidance unchanged
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceRevision,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.service import (
    archive_guidance,
    create_guidance,
    get_guidance,
    list_guidance,
    update_guidance,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="guidance-test@example.com",
        hashed_password="hashed",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="guidance-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session_model(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="guidance-session",
        status="running",
        is_active=True,
        instance_id="test-guidance-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def guidance_client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


# ── model tests ───────────────────────────────────────────────────────────────


class TestHumanGuidanceModel:
    def test_create_guidance_row(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable default args.",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()
        db_session.refresh(entry)

        assert entry.id is not None
        assert entry.status == GuidanceStatus.ACTIVE
        assert entry.revision == 1
        assert entry.archived_at is None

    def test_create_revision_row(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Original message.",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()
        db_session.refresh(entry)

        rev = HumanGuidanceRevision(
            guidance_id=entry.id,
            revision=1,
            message="Original message.",
            changed_by="test@example.com",
            change_reason="updated",
        )
        db_session.add(rev)
        db_session.commit()
        db_session.refresh(rev)

        assert rev.id is not None
        assert rev.guidance_id == entry.id
        assert rev.revision == 1

    def test_archive_status_transition(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="To be archived.",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()

        entry.status = GuidanceStatus.ARCHIVED
        entry.archived_at = datetime.now(timezone.utc)
        db_session.commit()
        db_session.refresh(entry)

        assert entry.status == GuidanceStatus.ARCHIVED
        assert entry.archived_at is not None

    def test_disabled_status_transition(
        self, db_session: Session, user: User, project: Project
    ):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope="global",
            message="Some global rule.",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
        )
        db_session.add(entry)
        db_session.commit()

        entry.status = GuidanceStatus.DISABLED
        entry.disabled_at = datetime.now(timezone.utc)
        db_session.commit()
        db_session.refresh(entry)

        assert entry.status == GuidanceStatus.DISABLED
        assert entry.disabled_at is not None


# ── service tests ─────────────────────────────────────────────────────────────


class TestHumanGuidanceService:
    def test_create_returns_entry_and_created_true(
        self, db_session: Session, user: User, project: Project
    ):
        entry, created = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Use dataclasses.",
        )
        assert created is True
        assert entry.id is not None
        assert entry.message == "Use dataclasses."
        assert entry.status == GuidanceStatus.ACTIVE

    def test_deduplication_returns_existing_entry(
        self, db_session: Session, user: User, project: Project
    ):
        entry1, created1 = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable defaults.",
        )
        entry2, created2 = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable defaults.",
        )
        assert created1 is True
        assert created2 is False
        assert entry1.id == entry2.id

    def test_dedup_different_scope_creates_new(
        self, db_session: Session, user: User, project: Project
    ):
        _, c1 = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Same message.",
        )
        _, c2 = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="global",
            message="Same message.",
        )
        assert c1 is True
        assert c2 is True

    def test_update_message_creates_revision(
        self, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Original.",
        )
        updated = update_guidance(
            db_session,
            entry.id,
            message="Updated.",
            changed_by="op@example.com",
            change_reason="clarified",
        )
        assert updated.message == "Updated."
        assert updated.revision == 2

        revs = (
            db_session.query(HumanGuidanceRevision)
            .filter(HumanGuidanceRevision.guidance_id == entry.id)
            .all()
        )
        assert len(revs) == 1
        assert revs[0].message == "Original."
        assert revs[0].revision == 1

    def test_update_same_message_no_revision(
        self, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Stable.",
        )
        updated = update_guidance(db_session, entry.id, message="Stable.")
        assert updated.revision == 1
        count = (
            db_session.query(HumanGuidanceRevision)
            .filter(HumanGuidanceRevision.guidance_id == entry.id)
            .count()
        )
        assert count == 0

    def test_archive_preserves_record(
        self, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="To archive.",
        )
        archived = archive_guidance(db_session, entry.id)
        assert archived.status == GuidanceStatus.ARCHIVED
        assert archived.archived_at is not None

        still_there = (
            db_session.query(HumanGuidance).filter(HumanGuidance.id == entry.id).first()
        )
        assert still_there is not None

    def test_archive_idempotent(
        self, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Archive me twice.",
        )
        archived1 = archive_guidance(db_session, entry.id)
        archived2 = archive_guidance(db_session, entry.id)
        assert archived1.id == archived2.id
        assert archived2.status == GuidanceStatus.ARCHIVED

    def test_list_guidance_filters_by_status(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Active guidance.",
        )
        entry2, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Disabled guidance.",
        )
        update_guidance(db_session, entry2.id, status="disabled")

        active_items, active_total = list_guidance(
            db_session, project_id=project.id, status="active"
        )
        assert active_total == 1
        assert active_items[0].message == "Active guidance."

        all_items, all_total = list_guidance(
            db_session, project_id=project.id, status="all"
        )
        assert all_total == 2

    def test_list_guidance_filters_by_scope(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project guidance.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="global",
            message="Global guidance.",
        )

        project_items, project_total = list_guidance(
            db_session, project_id=project.id, scope="project"
        )
        assert project_total == 1
        assert project_items[0].scope.value == "project"

    def test_get_guidance_not_found(self, db_session: Session):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            get_guidance(db_session, 99999)
        assert exc.value.status_code == 404

    def test_update_archived_raises(
        self, db_session: Session, user: User, project: Project
    ):
        from fastapi import HTTPException

        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Will be archived.",
        )
        archive_guidance(db_session, entry.id)
        with pytest.raises(HTTPException) as exc:
            update_guidance(db_session, entry.id, message="Cannot update.")
        assert exc.value.status_code == 400


# ── API tests ─────────────────────────────────────────────────────────────────


class TestHumanGuidanceAPI:
    def test_create_guidance_201(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Use dataclasses for all records.", "scope": "project"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["message"] == "Use dataclasses for all records."
        assert data["scope"] == "project"
        assert data["status"] == "active"
        assert data["revision"] == 1

    def test_create_guidance_dedup_returns_200(
        self, authenticated_client: TestClient, project: Project
    ):
        body = {"message": "Idempotent message.", "scope": "project"}
        r1 = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance", json=body
        )
        r2 = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance", json=body
        )
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]

    def test_create_guidance_empty_message_400(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "   ", "scope": "project"},
        )
        assert resp.status_code == 400

    def test_create_guidance_invalid_scope_400(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Valid message.", "scope": "invalid_scope"},
        )
        assert resp.status_code == 400

    def test_list_guidance_empty(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
        assert data["project_id"] == project.id

    def test_list_guidance_returns_items(
        self, authenticated_client: TestClient, project: Project
    ):
        authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Rule A.", "scope": "project"},
        )
        authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Rule B.", "scope": "project"},
        )
        resp = authenticated_client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_get_guidance_by_id(
        self, authenticated_client: TestClient, project: Project
    ):
        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Fetchable rule.", "scope": "project"},
        )
        gid = create_resp.json()["id"]

        resp = authenticated_client.get(f"/api/v1/guidance/{gid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == gid
        assert data["message"] == "Fetchable rule."
        assert "conflict_warnings" in data

    def test_get_guidance_not_found(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/guidance/99999")
        assert resp.status_code == 404

    def test_patch_guidance_message(
        self, authenticated_client: TestClient, project: Project
    ):
        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Old message.", "scope": "project"},
        )
        gid = create_resp.json()["id"]

        patch_resp = authenticated_client.patch(
            f"/api/v1/guidance/{gid}",
            json={"message": "New message.", "change_reason": "Clarified"},
        )
        assert patch_resp.status_code == 200
        data = patch_resp.json()
        assert data["message"] == "New message."
        assert data["revision"] == 2

    def test_patch_guidance_status_disable(
        self, authenticated_client: TestClient, project: Project
    ):
        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Will be disabled.", "scope": "project"},
        )
        gid = create_resp.json()["id"]

        patch_resp = authenticated_client.patch(
            f"/api/v1/guidance/{gid}", json={"status": "disabled"}
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["status"] == "disabled"

    def test_patch_guidance_invalid_status_422(
        self, authenticated_client: TestClient, project: Project
    ):
        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Some rule.", "scope": "project"},
        )
        gid = create_resp.json()["id"]

        patch_resp = authenticated_client.patch(
            f"/api/v1/guidance/{gid}", json={"status": "archived"}
        )
        assert patch_resp.status_code == 422

    def test_delete_archives_guidance(
        self, authenticated_client: TestClient, project: Project
    ):
        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "To be archived.", "scope": "project"},
        )
        gid = create_resp.json()["id"]

        del_resp = authenticated_client.delete(f"/api/v1/guidance/{gid}")
        assert del_resp.status_code == 200
        data = del_resp.json()
        assert data["status"] == "archived"
        assert data["archived_at"] is not None

        get_resp = authenticated_client.get(f"/api/v1/guidance/{gid}")
        assert get_resp.json()["status"] == "archived"

    def test_delete_not_found(self, authenticated_client: TestClient):
        resp = authenticated_client.delete("/api/v1/guidance/99999")
        assert resp.status_code == 404

    def test_project_not_found_403_or_404(self, authenticated_client: TestClient):
        resp = authenticated_client.post(
            "/api/v1/projects/99999/guidance",
            json={"message": "Rule.", "scope": "project"},
        )
        assert resp.status_code in (403, 404)


# ── regression test ───────────────────────────────────────────────────────────


class TestOperatorGuidanceRegression:
    """Verify existing POST /sessions/{id}/operator-guidance is unchanged."""

    def test_operator_guidance_endpoint_still_works(
        self, authenticated_client: TestClient, session_model: SessionModel
    ):
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_checkpoint:
            mock_checkpoint.return_value = "operator_guidance_20260615_120000"
            resp = authenticated_client.post(
                f"/api/v1/sessions/{session_model.id}/operator-guidance",
                json={"guidance": "Always use stdout for output."},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_model.id
        assert data["non_blocking"] is True
        assert "message" in data

    def test_operator_guidance_empty_body_400(
        self, authenticated_client: TestClient, session_model: SessionModel
    ):
        with patch(
            "app.services.session.intervention_service._append_guidance_to_checkpoint"
        ) as mock_checkpoint:
            mock_checkpoint.return_value = None
            resp = authenticated_client.post(
                f"/api/v1/sessions/{session_model.id}/operator-guidance",
                json={"guidance": ""},
            )
        assert resp.status_code == 400
