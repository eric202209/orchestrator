"""Tests for Track 2 — Replan Flow.

Covers:
- Service: get_or_generate_failure_summary, store_operator_feedback, trigger_replan
- API: GET /failure-summary, POST /operator-feedback, POST /replan
- Summary idempotency (second GET reuses cached record)
- Replan creates a PlanningSession with summary + feedback as prompt
"""

from __future__ import annotations

from unittest.mock import patch
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    ExecutionFailureSummary,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
    SessionTask,
)
from app.services.session.replan_service import (
    get_or_generate_failure_summary,
    store_operator_feedback,
    trigger_replan,
)

_LLM_PATH = "app.services.session.replan_service._generate_summary_via_llm"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path / "projects"))
    monkeypatch.setattr(
        "app.services.planning.planning_session_service.PlanningSessionService.schedule_processing",
        lambda self, session_id: None,
    )


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="replan-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def stopped_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="replan-session",
        status="stopped",
        is_active=False,
        instance_id="replan-test-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def failed_task(
    db_session: Session, project: Project, stopped_session: SessionModel
) -> Task:
    t = Task(
        project_id=project.id,
        title="Migrate schema",
        description="Run DB migration",
        status=TaskStatus.FAILED,
        error_message="Column 'user_id' already exists",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    link = SessionTask(
        session_id=stopped_session.id, task_id=t.id, status=TaskStatus.FAILED
    )
    db_session.add(link)
    db_session.commit()
    return t


# ── service tests ─────────────────────────────────────────────────────────────


class TestGetOrGenerateFailureSummary:
    def test_generates_via_llm(
        self, db_session: Session, stopped_session: SessionModel
    ):
        with patch(_LLM_PATH, return_value="LLM-generated summary of failure."):
            record = get_or_generate_failure_summary(db_session, stopped_session.id)

        assert record.session_id == stopped_session.id
        assert record.summary == "LLM-generated summary of failure."
        assert record.operator_feedback is None

    def test_falls_back_when_llm_fails(
        self, db_session: Session, stopped_session: SessionModel, failed_task: Task
    ):
        with patch(_LLM_PATH, return_value=None):
            record = get_or_generate_failure_summary(db_session, stopped_session.id)

        assert record.summary != ""
        assert (
            "Migrate schema" in record.summary or "Column 'user_id'" in record.summary
        )

    def test_idempotent_second_call(
        self, db_session: Session, stopped_session: SessionModel
    ):
        with patch(_LLM_PATH, return_value="First summary."):
            r1 = get_or_generate_failure_summary(db_session, stopped_session.id)

        with patch(_LLM_PATH, return_value="Second summary.") as mock:
            r2 = get_or_generate_failure_summary(db_session, stopped_session.id)
            mock.assert_not_called()

        assert r1.id == r2.id
        assert r2.summary == "First summary."

    def test_404_for_missing_session(self, db_session: Session):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            get_or_generate_failure_summary(db_session, 99999)
        assert exc_info.value.status_code == 404


class TestStoreOperatorFeedback:
    def test_saves_feedback(self, db_session: Session, stopped_session: SessionModel):
        with patch(_LLM_PATH, return_value="summary"):
            record = store_operator_feedback(
                db_session, stopped_session.id, "Fix the migration."
            )

        assert record.operator_feedback == "Fix the migration."
        assert record.feedback_at is not None

    def test_strips_whitespace(
        self, db_session: Session, stopped_session: SessionModel
    ):
        with patch(_LLM_PATH, return_value="summary"):
            record = store_operator_feedback(
                db_session, stopped_session.id, "  padded  "
            )

        assert record.operator_feedback == "padded"


class TestTriggerReplan:
    def test_creates_planning_session(
        self, db_session: Session, stopped_session: SessionModel, project: Project
    ):
        with patch(
            _LLM_PATH, return_value="The migration failed due to duplicate column."
        ):
            result = trigger_replan(db_session, stopped_session.id)

        assert result["session_id"] == stopped_session.id
        assert result["planning_session_id"] is not None
        assert "Replan started" in result["message"]

        summary = (
            db_session.query(ExecutionFailureSummary)
            .filter(ExecutionFailureSummary.session_id == stopped_session.id)
            .first()
        )
        assert summary is not None
        assert summary.replan_planning_session_id == result["planning_session_id"]

    def test_includes_operator_feedback_in_prompt(
        self, db_session: Session, stopped_session: SessionModel
    ):
        with patch(_LLM_PATH, return_value="summary"):
            store_operator_feedback(
                db_session, stopped_session.id, "Focus on schema only."
            )

        from app.models import PlanningSession

        with patch(_LLM_PATH, return_value="summary"):
            result = trigger_replan(db_session, stopped_session.id)

        ps = (
            db_session.query(PlanningSession)
            .filter(PlanningSession.id == result["planning_session_id"])
            .first()
        )
        assert ps is not None
        assert "Focus on schema only." in ps.prompt


# ── API tests ─────────────────────────────────────────────────────────────────


class TestFailureSummaryEndpoints:
    def test_get_failure_summary(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="api-test-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()

        session = SessionModel(
            project_id=project.id,
            name="api-test-session",
            status="stopped",
            is_active=False,
            instance_id="api-test-uuid",
        )
        db_session.add(session)
        db_session.commit()

        with patch(_LLM_PATH, return_value="API test summary."):
            resp = authenticated_client.get(
                f"/api/v1/sessions/{session.id}/failure-summary"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session.id
        assert data["summary"] == "API test summary."

    def test_get_failure_summary_includes_latest_failure_diagnostics(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="diagnostic-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()

        session = SessionModel(
            project_id=project.id,
            name="diagnostic-session",
            status="stopped",
            is_active=False,
            instance_id="diagnostic-uuid",
        )
        task = Task(
            project_id=project.id,
            title="Build utility",
            status=TaskStatus.FAILED,
            error_message="Plan validation failed after repair",
        )
        db_session.add_all([session, task])
        db_session.commit()

        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
        )
        db_session.add(execution)
        db_session.commit()

        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="ERROR",
                message="[ORCHESTRATION] Plan validation failed after repair",
                log_metadata=json.dumps(
                    {
                        "reason": "planning_validation_failed_after_repair",
                        "brittle_command_subcodes": ["oversized_command_length"],
                        "brittle_command_step_details": {
                            "2": ["oversized_command_length"]
                        },
                        "max_command_length": 1456,
                    }
                ),
            )
        )
        db_session.commit()

        with patch(_LLM_PATH, return_value="API test summary."):
            resp = authenticated_client.get(
                f"/api/v1/sessions/{session.id}/failure-summary"
            )

        assert resp.status_code == 200
        diagnostics = resp.json()["diagnostics"]
        assert diagnostics["task_execution_id"] == execution.id
        assert diagnostics["reason"] == "planning_validation_failed_after_repair"
        assert diagnostics["brittle_command_subcodes"] == ["oversized_command_length"]
        assert diagnostics["brittle_command_step_details"] == {
            "2": ["oversized_command_length"]
        }

    def test_operator_feedback_saved(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="fb-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()

        session = SessionModel(
            project_id=project.id,
            name="fb-session",
            status="stopped",
            is_active=False,
            instance_id="fb-uuid",
        )
        db_session.add(session)
        db_session.commit()

        with patch(_LLM_PATH, return_value="summary"):
            resp = authenticated_client.post(
                f"/api/v1/sessions/{session.id}/operator-feedback",
                json={"feedback": "Please fix auth middleware."},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["operator_feedback"] == "Please fix auth middleware."
        assert "message" in data

    def test_operator_feedback_empty_rejected(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="empty-fb-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()

        session = SessionModel(
            project_id=project.id,
            name="empty-fb-session",
            status="stopped",
            is_active=False,
            instance_id="empty-fb-uuid",
        )
        db_session.add(session)
        db_session.commit()

        resp = authenticated_client.post(
            f"/api/v1/sessions/{session.id}/operator-feedback",
            json={"feedback": "   "},
        )
        assert resp.status_code == 400

    def test_replan_creates_planning_session(
        self, authenticated_client: TestClient, db_session: Session
    ):
        project = Project(name="replan-api-project", workspace_path=None)
        db_session.add(project)
        db_session.commit()

        session = SessionModel(
            project_id=project.id,
            name="replan-api-session",
            status="stopped",
            is_active=False,
            instance_id="replan-api-uuid",
        )
        db_session.add(session)
        db_session.commit()

        with patch(_LLM_PATH, return_value="Failure: auth broke."):
            resp = authenticated_client.post(f"/api/v1/sessions/{session.id}/replan")

        assert resp.status_code == 200
        data = resp.json()
        assert "planning_session_id" in data
        assert data["planning_session_id"] is not None
        assert "message" in data

    def test_summary_404_for_unknown_session(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/sessions/99999/failure-summary")
        assert resp.status_code == 404
