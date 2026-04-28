"""Tests for human-in-the-loop (HITL) intervention system.

Covers:
- Service: create_intervention_request, submit_intervention_reply,
           approve_intervention, deny_intervention
- API: POST /request-intervention, GET /interventions, reply/approve/deny
- Session status transitions: running → waiting_for_human → paused
- Resume from waiting_for_human status
- Error cases: bad type, double-reply, approve non-approval-type
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    InterventionRequest,
    LogEntry,
    Project,
    Session as SessionModel,
    TaskStatus,
)
from app.services.session.intervention_service import (
    approve_intervention,
    create_intervention_request,
    deny_intervention,
    get_intervention_history,
    get_pending_interventions,
    submit_intervention_reply,
)

_REVOKE_PATH = (
    "app.services.session.session_runtime_service.revoke_session_celery_tasks"
)
_CHECKPOINT_PATH = "app.services.workspace.checkpoint_service.CheckpointService"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="hitl-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="hitl-session",
        status="running",
        is_active=True,
        instance_id="test-instance-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def pending_intervention(
    db_session: Session, running_session: SessionModel, project: Project
) -> InterventionRequest:
    with (
        patch(_REVOKE_PATH),
        patch(_CHECKPOINT_PATH) as mock_cs,
    ):
        mock_cs.return_value.load_checkpoint.return_value = None
        req = create_intervention_request(
            db_session,
            session_id=running_session.id,
            project_id=project.id,
            intervention_type="guidance",
            prompt="What should we do next?",
        )
    return req


# ── service unit tests ────────────────────────────────────────────────────────


class TestCreateInterventionRequest:
    def test_creates_record(
        self, db_session: Session, running_session: SessionModel, project: Project
    ):
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            req = create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="guidance",
                prompt="Need guidance here",
            )

        assert req.id is not None
        assert req.status == "pending"
        assert req.intervention_type == "guidance"
        assert req.prompt == "Need guidance here"

    def test_session_transitions_to_waiting_for_human(
        self, db_session: Session, running_session: SessionModel, project: Project
    ):
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="approval",
                prompt="Approve this?",
            )

        db_session.refresh(running_session)
        assert running_session.status == "waiting_for_human"

    def test_rejects_unknown_intervention_type(
        self, db_session: Session, running_session: SessionModel, project: Project
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="unknown_type",
                prompt="test",
            )
        assert exc_info.value.status_code == 400

    def test_rejects_stopped_session(self, db_session: Session, project: Project):
        from fastapi import HTTPException

        stopped = SessionModel(
            project_id=project.id,
            name="stopped-session",
            status="stopped",
            is_active=False,
        )
        db_session.add(stopped)
        db_session.commit()
        db_session.refresh(stopped)

        with pytest.raises(HTTPException) as exc_info:
            create_intervention_request(
                db_session,
                session_id=stopped.id,
                project_id=project.id,
                intervention_type="guidance",
                prompt="test",
            )
        assert exc_info.value.status_code == 400

    def test_emits_log_entry(
        self,
        db_session: Session,
        running_session: SessionModel,
        project: Project,
    ):
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            req = create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="information",
                prompt="What is the branch name?",
            )

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.session_id == running_session.id)
            .order_by(LogEntry.id.desc())
            .first()
        )
        assert log is not None
        meta = json.loads(log.log_metadata)
        assert meta["event_type"] == "human_intervention_requested"
        assert meta["intervention_id"] == req.id


class TestSubmitInterventionReply:
    def test_records_reply(
        self,
        db_session: Session,
        running_session: SessionModel,
        pending_intervention: InterventionRequest,
    ):
        db_session.refresh(running_session)
        # session is now waiting_for_human
        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            req = submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Continue with approach B",
                operator_id="operator@example.com",
            )

        assert req.status == "replied"
        assert req.operator_reply == "Continue with approach B"
        assert req.operator_id == "operator@example.com"
        assert req.replied_at is not None

    def test_transitions_session_to_paused(
        self,
        db_session: Session,
        running_session: SessionModel,
        pending_intervention: InterventionRequest,
    ):
        db_session.refresh(running_session)
        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Use the new approach",
            )

        db_session.refresh(running_session)
        assert running_session.status == "paused"

    def test_rejects_double_reply(
        self,
        db_session: Session,
        pending_intervention: InterventionRequest,
    ):
        from fastapi import HTTPException

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="First reply",
            )

        with pytest.raises(HTTPException) as exc_info:
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Second reply",
            )
        assert exc_info.value.status_code == 400

    def test_emits_reply_event_log(
        self,
        db_session: Session,
        running_session: SessionModel,
        pending_intervention: InterventionRequest,
    ):
        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Use fallback strategy",
            )

        logs = (
            db_session.query(LogEntry)
            .filter(LogEntry.session_id == running_session.id)
            .order_by(LogEntry.id.desc())
            .all()
        )
        meta_list = [
            json.loads(log_entry.log_metadata)
            for log_entry in logs
            if log_entry.log_metadata
        ]
        event_types = [m.get("event_type") for m in meta_list]
        assert "human_intervention_replied" in event_types


class TestApproveIntervention:
    def test_approve_sets_status(
        self,
        db_session: Session,
        running_session: SessionModel,
        project: Project,
    ):
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            req = create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="approval",
                prompt="Proceed with deployment?",
            )

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            result = approve_intervention(
                db_session,
                intervention_id=req.id,
                operator_id="admin@example.com",
            )

        assert result.status == "approved"
        assert result.operator_id == "admin@example.com"

    def test_approve_rejects_non_approval_type(
        self,
        db_session: Session,
        pending_intervention: InterventionRequest,
    ):
        from fastapi import HTTPException

        # pending_intervention is "guidance" type
        with pytest.raises(HTTPException) as exc_info:
            approve_intervention(db_session, intervention_id=pending_intervention.id)
        assert exc_info.value.status_code == 400


class TestDenyIntervention:
    def test_deny_sets_status(
        self,
        db_session: Session,
        running_session: SessionModel,
        project: Project,
    ):
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            req = create_intervention_request(
                db_session,
                session_id=running_session.id,
                project_id=project.id,
                intervention_type="approval",
                prompt="Delete production DB?",
            )

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            result = deny_intervention(
                db_session,
                intervention_id=req.id,
                reason="Too risky",
            )

        assert result.status == "denied"
        assert result.operator_reply == "Too risky"

    def test_deny_rejects_non_approval_type(
        self,
        db_session: Session,
        pending_intervention: InterventionRequest,
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            deny_intervention(db_session, intervention_id=pending_intervention.id)
        assert exc_info.value.status_code == 400


class TestGetInterventions:
    def test_get_pending_only(
        self,
        db_session: Session,
        running_session: SessionModel,
        project: Project,
        pending_intervention: InterventionRequest,
    ):
        pending = get_pending_interventions(db_session, session_id=running_session.id)
        assert any(r.id == pending_intervention.id for r in pending)

    def test_history_includes_all_statuses(
        self,
        db_session: Session,
        running_session: SessionModel,
        pending_intervention: InterventionRequest,
    ):
        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Noted",
            )

        history = get_intervention_history(db_session, session_id=running_session.id)
        statuses = {r.status for r in history}
        assert "replied" in statuses

    def test_pending_excludes_replied(
        self,
        db_session: Session,
        running_session: SessionModel,
        pending_intervention: InterventionRequest,
    ):
        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            submit_intervention_reply(
                db_session,
                intervention_id=pending_intervention.id,
                operator_reply="Done",
            )

        pending = get_pending_interventions(db_session, session_id=running_session.id)
        assert not any(r.id == pending_intervention.id for r in pending)


# ── API endpoint tests ────────────────────────────────────────────────────────


@pytest.fixture()
def api_project_and_session(authenticated_client: TestClient, db_session_factory):
    """Create project + running session via the DB directly, return ids."""
    db = db_session_factory()
    p = Project(name="api-hitl-project", workspace_path=None)
    db.add(p)
    db.commit()
    db.refresh(p)
    s = SessionModel(
        project_id=p.id,
        name="api-hitl-session",
        status="running",
        is_active=True,
        instance_id="api-test-uuid",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    ids = {"project_id": p.id, "session_id": s.id}
    db.close()
    return ids


class TestInterventionAPIEndpoints:
    def test_request_intervention_returns_intervention_payload(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        project_id = api_project_and_session["project_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={
                    "intervention_type": "guidance",
                    "prompt": "Should we skip the migration step?",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["project_id"] == project_id
        assert data["status"] == "pending"
        assert data["intervention_type"] == "guidance"
        assert data["initiated_by"] == "human"
        assert data["message"] == (
            "Session 'api-hitl-session' is now waiting for human input"
        )

    def test_request_intervention_bad_type(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        resp = authenticated_client.post(
            f"/api/v1/sessions/{session_id}/request-intervention",
            json={"intervention_type": "bad_type", "prompt": "test"},
        )
        assert resp.status_code == 400

    def test_list_interventions_empty(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        resp = authenticated_client.get(f"/api/v1/sessions/{session_id}/interventions")
        assert resp.status_code == 200
        assert resp.json()["interventions"] == []

    def test_reply_to_intervention(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={"intervention_type": "guidance", "prompt": "What next?"},
            )
        intervention_id = create_resp.json()["id"]

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            reply_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/interventions/{intervention_id}/reply",
                json={"reply": "Use the safe approach"},
            )

        assert reply_resp.status_code == 200
        assert reply_resp.json()["status"] == "replied"
        assert reply_resp.json()["session_id"] == session_id

    def test_list_interventions_after_create(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={"intervention_type": "information", "prompt": "What branch?"},
            )

        resp = authenticated_client.get(f"/api/v1/sessions/{session_id}/interventions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert len(resp.json()["interventions"]) == 1
        assert resp.json()["interventions"][0]["context_snapshot"] is None

    def test_approve_intervention_endpoint(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={"intervention_type": "approval", "prompt": "Proceed?"},
            )
        intervention_id = create_resp.json()["id"]

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            approve_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/interventions/{intervention_id}/approve"
            )

        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"
        assert approve_resp.json()["session_id"] == session_id

    def test_deny_intervention_endpoint(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={"intervention_type": "approval", "prompt": "Risky op?"},
            )
        intervention_id = create_resp.json()["id"]

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            deny_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/interventions/{intervention_id}/deny",
                json={"reason": "Too risky"},
            )

        assert deny_resp.status_code == 200
        assert deny_resp.json()["status"] == "denied"
        assert deny_resp.json()["session_id"] == session_id

    def test_pending_only_filter(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={"intervention_type": "guidance", "prompt": "Pending item"},
            )
        intervention_id = create_resp.json()["id"]

        with patch(_CHECKPOINT_PATH) as mock_cs:
            mock_cs.return_value.load_checkpoint.return_value = None
            authenticated_client.post(
                f"/api/v1/sessions/{session_id}/interventions/{intervention_id}/reply",
                json={"reply": "done"},
            )

        # pending_only=true should return 0
        pending_resp = authenticated_client.get(
            f"/api/v1/sessions/{session_id}/interventions?pending_only=true"
        )
        assert pending_resp.status_code == 200
        assert pending_resp.json()["interventions"] == []

        # Without filter should return 1
        all_resp = authenticated_client.get(
            f"/api/v1/sessions/{session_id}/interventions"
        )
        assert len(all_resp.json()["interventions"]) == 1

    def test_list_interventions_includes_context_and_initiator(
        self, authenticated_client: TestClient, api_project_and_session: dict
    ):
        session_id = api_project_and_session["session_id"]
        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            authenticated_client.post(
                f"/api/v1/sessions/{session_id}/request-intervention",
                json={
                    "intervention_type": "guidance",
                    "prompt": "Need project context",
                    "context_snapshot": {"branch": "main"},
                },
            )

        resp = authenticated_client.get(f"/api/v1/sessions/{session_id}/interventions")

        assert resp.status_code == 200
        item = resp.json()["interventions"][0]
        assert item["initiated_by"] == "human"
        assert json.loads(item["context_snapshot"]) == {"branch": "main"}

    def test_reply_endpoint_rejects_cross_session_intervention_id(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="cross-session-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()
        db_session.refresh(project)

        session_one = SessionModel(
            project_id=project.id,
            name="session-one",
            status="running",
            is_active=True,
            instance_id="cross-1",
        )
        session_two = SessionModel(
            project_id=project.id,
            name="session-two",
            status="running",
            is_active=True,
            instance_id="cross-2",
        )
        db_session.add_all([session_one, session_two])
        db_session.commit()
        db_session.refresh(session_one)
        db_session.refresh(session_two)

        with (
            patch(_REVOKE_PATH),
            patch(_CHECKPOINT_PATH) as mock_cs,
        ):
            mock_cs.return_value.load_checkpoint.return_value = None
            create_resp = authenticated_client.post(
                f"/api/v1/sessions/{session_one.id}/request-intervention",
                json={"intervention_type": "guidance", "prompt": "Session one only"},
            )

        intervention_id = create_resp.json()["id"]
        reply_resp = authenticated_client.post(
            f"/api/v1/sessions/{session_two.id}/interventions/{intervention_id}/reply",
            json={"reply": "wrong session"},
        )

        assert reply_resp.status_code == 404


# ── DB migration test ─────────────────────────────────────────────────────────


class TestInterventionMigration:
    def test_migration_creates_table(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from app.db_migrations import MIGRATIONS, run_schema_migrations

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from app.models import Base

        Base.metadata.create_all(bind=engine)
        run_schema_migrations(engine, MIGRATIONS)

        inspector = __import__("sqlalchemy").inspect(engine)
        assert "intervention_requests" in inspector.get_table_names()
        col_names = {c["name"] for c in inspector.get_columns("intervention_requests")}
        for expected in (
            "id",
            "session_id",
            "task_id",
            "project_id",
            "intervention_type",
            "prompt",
            "status",
            "operator_reply",
            "operator_id",
            "created_at",
            "replied_at",
            "expires_at",
        ):
            assert expected in col_names, f"Missing column: {expected}"

    def test_migration_idempotent(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from app.db_migrations import MIGRATIONS, run_schema_migrations

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from app.models import Base

        Base.metadata.create_all(bind=engine)
        run_schema_migrations(engine, MIGRATIONS)
        run_schema_migrations(engine, MIGRATIONS)  # must not raise
