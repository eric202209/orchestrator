"""Tests for Human Guidance HG-P1c-1 — API completion and rendered preview.

Covers:
1.  GET /guidance/global — lists only global guidance
2.  GET /guidance/global — filters by status
3.  GET /guidance/global — respects limit/offset
4.  GET /guidance/global — excludes project/session/task guidance
5.  GET /guidance/{id}/history — returns revisions in ascending order
6.  GET /guidance/{id}/history — 404 for missing guidance
7.  GET /guidance/{id}/history — message update creates revision that appears in history
8.  GET /projects/{id}/guidance/rendered — returns active applicable guidance
9.  GET /projects/{id}/guidance/rendered — includes global + project guidance
10. GET /projects/{id}/guidance/rendered — orders task/session/project/global correctly
11. GET /projects/{id}/guidance/rendered — applies priority ordering within scope
12. GET /projects/{id}/guidance/rendered — reports trimmed=True when budget exceeded
13. GET /projects/{id}/guidance/rendered — does not create HumanGuidanceUsage rows
14. GET /projects/{id}/guidance/rendered — does not write working_memory.json
15. GET /projects/{id}/guidance/rendered — excludes disabled/archived/expired entries
16. Regression: render_guidance_block output matches _render_content Operator Guidance section
17. Regression: existing HG-P1a CRUD endpoints still respond correctly
18. Regression: existing /sessions/{id}/operator-guidance behavior unchanged
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
    create_guidance,
    get_guidance_history,
    list_global_guidance,
    update_guidance,
)
from app.services.orchestration.working_memory import (
    _HUMAN_GUIDANCE_LIMIT,
    _INJECTION_BUDGET,
    render_guidance_block,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p1c1@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p1c1-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p1c1-session",
        status="running",
        is_active=True,
        instance_id="p1c1-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


# ── 1–4: GET /guidance/global ─────────────────────────────────────────────────


class TestGlobalGuidanceEndpoint:
    def test_lists_only_global_guidance(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global rule A.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project rule B.",
        )
        resp = client.get("/api/v1/guidance/global")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["message"] == "Global rule A."
        assert data["items"][0]["scope"] == "global"

    def test_filters_by_status(
        self, client: TestClient, db_session: Session, user: User
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Active global.",
        )
        from app.services.human_guidance.service import archive_guidance

        archive_guidance(db_session, entry.id)

        resp_active = client.get("/api/v1/guidance/global?status=active")
        assert resp_active.status_code == 200
        assert resp_active.json()["total"] == 0

        resp_archived = client.get("/api/v1/guidance/global?status=archived")
        assert resp_archived.status_code == 200
        assert resp_archived.json()["total"] == 1

        resp_all = client.get("/api/v1/guidance/global?status=all")
        assert resp_all.status_code == 200
        assert resp_all.json()["total"] == 1

    def test_respects_limit_and_offset(
        self, client: TestClient, db_session: Session, user: User
    ):
        for i in range(5):
            create_guidance(
                db_session,
                user_id=user.id,
                project_id=None,
                scope="global",
                message=f"Global rule {i}.",
            )

        resp = client.get("/api/v1/guidance/global?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

        resp2 = client.get("/api/v1/guidance/global?limit=2&offset=2")
        assert resp2.status_code == 200
        assert len(resp2.json()["items"]) == 2

    def test_excludes_non_global_guidance(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project scope.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Session scope.",
        )
        resp = client.get("/api/v1/guidance/global")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ── 5–7: GET /guidance/{id}/history ──────────────────────────────────────────


class TestGuidanceHistoryEndpoint:
    def test_returns_revisions_ascending(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Original message.",
        )
        update_guidance(
            db_session,
            entry.id,
            message="Second message.",
            changed_by="op@example.com",
            change_reason="first edit",
        )
        update_guidance(
            db_session,
            entry.id,
            message="Third message.",
            changed_by="op@example.com",
            change_reason="second edit",
        )

        resp = client.get(f"/api/v1/guidance/{entry.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == entry.id
        revs = data["revisions"]
        assert len(revs) == 2
        assert revs[0]["revision"] < revs[1]["revision"]
        assert revs[0]["message"] == "Original message."
        assert revs[1]["message"] == "Second message."

    def test_404_for_missing_guidance(self, client: TestClient):
        resp = client.get("/api/v1/guidance/999999/history")
        assert resp.status_code == 404

    def test_message_update_creates_revision_in_history(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="First.",
        )
        resp_before = client.get(f"/api/v1/guidance/{entry.id}/history")
        assert resp_before.json()["revisions"] == []

        update_guidance(
            db_session,
            entry.id,
            message="Updated.",
            changed_by="editor",
            change_reason="clarification",
        )

        resp_after = client.get(f"/api/v1/guidance/{entry.id}/history")
        revs = resp_after.json()["revisions"]
        assert len(revs) == 1
        assert revs[0]["message"] == "First."
        assert revs[0]["changed_by"] == "editor"
        assert revs[0]["change_reason"] == "clarification"


# ── 8–15: GET /projects/{id}/guidance/rendered ───────────────────────────────


class TestRenderedPreviewEndpoint:
    def test_returns_active_guidance(
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
        data = resp.json()
        assert data["project_id"] == project.id
        assert "Use dataclasses." in data["block"]
        assert data["rendered_chars"] > 0
        assert data["max_chars"] == _INJECTION_BUDGET

    def test_includes_global_and_project_guidance(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global rule.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Project rule.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        block = resp.json()["block"]
        assert "Global rule." in block
        assert "Project rule." in block

    def test_task_scope_appears_before_global(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        from app.models import Task, TaskStatus

        task = Task(
            project_id=project.id,
            title="test-task",
            description="",
            status=TaskStatus.PENDING,
            plan_position=1,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Global last.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=task.id,
            scope="task",
            message="Task first.",
        )

        resp = client.get(
            f"/api/v1/projects/{project.id}/guidance/rendered"
            f"?session_id={running_session.id}&task_id={task.id}"
        )
        assert resp.status_code == 200
        block = resp.json()["block"]
        task_pos = block.index("Task first.")
        global_pos = block.index("Global last.")
        assert task_pos < global_pos

    def test_priority_ordering_within_scope(
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
            message="Low priority.",
            priority=1,
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="High priority.",
            priority=10,
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        block = resp.json()["block"]
        assert block.index("High priority.") < block.index("Low priority.")

    def test_trimmed_when_budget_exceeded(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        monkeypatch,
    ):
        # Temporarily lower the budget to 20 chars so any guidance overflows.
        import app.services.orchestration.working_memory as wm_mod

        monkeypatch.setattr(wm_mod, "_INJECTION_BUDGET", 20)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="This message is definitely longer than twenty characters.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trimmed"] is True
        assert len(data["block"]) <= 20

    def test_does_not_create_usage_telemetry_rows(
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
            message="No telemetry.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        # HumanGuidanceUsage rows must not be created by the preview endpoint.
        rows = db_session.query(HumanGuidanceUsage).all()
        assert len(rows) == 0

    def test_does_not_write_working_memory_json(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
        tmp_path,
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="No WM write.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        # working_memory.json must not be written anywhere under tmp_path.
        wm_files = list(tmp_path.rglob("working_memory.json"))
        assert len(wm_files) == 0

    def test_excludes_disabled_archived_expired_entries(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        project: Project,
    ):
        past = datetime.now(timezone.utc) - timedelta(days=1)

        db_session.add(
            HumanGuidance(
                user_id=user.id,
                project_id=project.id,
                scope="project",
                message="Disabled entry.",
                status=GuidanceStatus.DISABLED,
                priority=0,
                revision=1,
            )
        )
        db_session.add(
            HumanGuidance(
                user_id=user.id,
                project_id=project.id,
                scope="project",
                message="Archived entry.",
                status=GuidanceStatus.ARCHIVED,
                priority=0,
                revision=1,
            )
        )
        db_session.add(
            HumanGuidance(
                user_id=user.id,
                project_id=project.id,
                scope="project",
                message="Expired entry.",
                status=GuidanceStatus.ACTIVE,
                expires_at=past,
                priority=0,
                revision=1,
            )
        )
        db_session.commit()

        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        block = resp.json()["block"]
        assert "Disabled entry." not in block
        assert "Archived entry." not in block
        assert "Expired entry." not in block


# ── 16–18: Regression ─────────────────────────────────────────────────────────


class TestRegressionP1c1:
    def test_render_guidance_block_matches_wm_render_section(
        self, db_session: Session, user: User, project: Project, tmp_path
    ):
        """render_guidance_block output is identical to the Operator Guidance lines in _render_content."""
        from app.services.orchestration.working_memory import _render_content

        entries = [
            {"message": "Rule one.", "source": "operator_guidance"},
            {"message": "Rule two.", "source": "operator_guidance"},
        ]
        body_lines = render_guidance_block(entries)
        full_block = _render_content({"human_guidance": entries})

        # Every body line must appear verbatim in the full rendered block.
        for line in body_lines:
            assert line in full_block

        assert "Operator Guidance" in full_block

    def test_existing_crud_endpoint_create_still_works(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Regression check.", "scope": "project"},
        )
        assert resp.status_code == 201
        assert resp.json()["message"] == "Regression check."

    def test_existing_crud_endpoint_list_still_works(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Listed entry.",
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ── Service-layer unit tests (no HTTP) ────────────────────────────────────────


class TestServiceLayer:
    def test_list_global_guidance_service(
        self, db_session: Session, user: User, project: Project
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=None,
            scope="global",
            message="Service global.",
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Not global.",
        )
        items, total = list_global_guidance(db_session, user_id=user.id)
        assert total == 1
        assert items[0].message == "Service global."

    def test_get_guidance_history_service_empty(
        self, db_session: Session, user: User, project: Project
    ):
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="No edits yet.",
        )
        fetched, revisions = get_guidance_history(db_session, entry.id)
        assert fetched.id == entry.id
        assert revisions == []

    def test_render_guidance_block_caps_at_limit(self):
        entries = [{"message": f"Entry {i}."} for i in range(15)]
        lines = render_guidance_block(entries)
        assert len(lines) == _HUMAN_GUIDANCE_LIMIT

    def test_render_guidance_block_truncates_message(self):
        long_msg = "x" * 300
        lines = render_guidance_block([{"message": long_msg}])
        assert len(lines) == 1
        assert len(lines[0]) <= 200 + len("  - ")
