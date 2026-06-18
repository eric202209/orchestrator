"""Tests for Pilot Evidence Dashboard backend endpoints.

Covers:
- GET /ops/pilot-summary
- GET /ops/pilot-guidance-stats
- GET /ops/pilot-token-stats
- GET /ops/pilot-permission-stats
- GET /ops/queue-latency (p95 field added)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    HumanGuidance,
    HumanGuidanceConflict,
    HumanGuidanceUsage,
    LogEntry,
    PermissionRequest,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)

try:
    from app.models import GuidanceScope, GuidanceStatus
except ImportError:
    GuidanceScope = None
    GuidanceStatus = None


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="pilot-dash-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="pilot-dash-session",
        status="running",
        is_active=True,
        instance_id="pilot-dash-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def task(db_session: Session, project: Project) -> Task:
    t = Task(
        project_id=project.id,
        title="pilot-dash-task",
        status=TaskStatus.RUNNING,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_execution(
    db: Session,
    session: SessionModel,
    task: Task,
    *,
    attempt: int = 1,
    status: TaskStatus = TaskStatus.DONE,
    failure_category: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    queue_latency_seconds: float | None = None,
) -> TaskExecution:
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=status,
        failure_category=failure_category,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        queue_latency_seconds=queue_latency_seconds,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


# ── pilot-summary ─────────────────────────────────────────────────────────────


class TestPilotSummaryEndpoint:
    def test_empty_project_returns_zero_counts(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == project.id
        assert body["task_executions"]["total"] == 0
        assert body["rates"]["success_rate"] is None

    def test_counts_done_and_failed(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(db_session, session, task, attempt=1, status=TaskStatus.DONE)
        _make_execution(
            db_session,
            session,
            task,
            attempt=2,
            status=TaskStatus.FAILED,
            failure_category="timeout",
        )
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        body = resp.json()
        assert body["task_executions"]["total"] == 2
        assert body["task_executions"]["done"] == 1
        assert body["task_executions"]["failed"] == 1

    def test_success_rate_computed(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(db_session, session, task, attempt=1, status=TaskStatus.DONE)
        _make_execution(db_session, session, task, attempt=2, status=TaskStatus.DONE)
        _make_execution(db_session, session, task, attempt=3, status=TaskStatus.FAILED)
        _make_execution(db_session, session, task, attempt=4, status=TaskStatus.FAILED)
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        body = resp.json()
        assert abs(body["rates"]["success_rate"] - 0.5) < 0.001

    def test_timeout_rate_uses_failure_category(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(
            db_session,
            session,
            task,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="timeout_exceeded",
        )
        _make_execution(db_session, session, task, attempt=2, status=TaskStatus.DONE)
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        body = resp.json()
        assert abs(body["rates"]["timeout_rate"] - 0.5) < 0.001

    def test_symbol_verification_failed_counted(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(db_session, session, task)
        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                level="WARNING",
                message="[COMPLETION_SYMBOL_VERIFICATION_FAILED] missing: foo",
            )
        )
        db_session.commit()
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        body = resp.json()
        assert body["symbol_verification"]["failed"] == 1
        assert body["symbol_verification"]["passed"] is None

    def test_project_id_required(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/ops/pilot-summary")
        assert resp.status_code == 422

    def test_response_has_computed_at(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-summary?project_id={project.id}"
        )
        assert "computed_at" in resp.json()


# ── pilot-guidance-stats ──────────────────────────────────────────────────────


class TestPilotGuidanceStatsEndpoint:
    def _make_guidance(self, db: Session, project: Project) -> HumanGuidance:
        kwargs: dict = {"message": "Test guidance entry", "project_id": project.id}
        if GuidanceScope is not None:
            kwargs["scope"] = GuidanceScope.PROJECT
        if GuidanceStatus is not None:
            kwargs["status"] = GuidanceStatus.ACTIVE
        g = HumanGuidance(**kwargs)
        db.add(g)
        db.commit()
        db.refresh(g)
        return g

    def test_empty_returns_zero_counts(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-guidance-stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["usage"]["total_injections"] == 0
        assert body["conflicts"]["total"] == 0

    def test_injection_counts_selected_rows(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        task: Task,
    ):
        g = self._make_guidance(db_session, project)
        for i in range(3):
            db_session.add(
                HumanGuidanceUsage(
                    guidance_id=g.id,
                    project_id=project.id,
                    task_id=task.id,
                    selected=True,
                    rendered=True,
                )
            )
        # One unselected — must not count
        db_session.add(
            HumanGuidanceUsage(
                guidance_id=g.id,
                project_id=project.id,
                task_id=task.id,
                selected=False,
                rendered=False,
            )
        )
        db_session.commit()
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-guidance-stats?project_id={project.id}"
        )
        body = resp.json()
        assert body["usage"]["total_injections"] == 3
        assert body["usage"]["total_rendered"] == 3

    def test_top_entries_sorted_by_count(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        task: Task,
    ):
        g1 = self._make_guidance(db_session, project)
        g2 = self._make_guidance(db_session, project)
        for _ in range(5):
            db_session.add(
                HumanGuidanceUsage(
                    guidance_id=g1.id,
                    project_id=project.id,
                    task_id=task.id,
                    selected=True,
                    rendered=True,
                )
            )
        for _ in range(2):
            db_session.add(
                HumanGuidanceUsage(
                    guidance_id=g2.id,
                    project_id=project.id,
                    task_id=task.id,
                    selected=True,
                    rendered=True,
                )
            )
        db_session.commit()
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-guidance-stats?project_id={project.id}"
        )
        body = resp.json()
        entries = body["usage"]["top_entries"]
        assert entries[0]["guidance_id"] == g1.id
        assert entries[0]["injection_count"] == 5

    def test_conflict_counts(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
    ):
        db_session.add(
            HumanGuidanceConflict(
                project_id=project.id,
                guidance_message="conflict msg",
                conflict_excerpt="some code",
                status="open",
            )
        )
        db_session.add(
            HumanGuidanceConflict(
                project_id=project.id,
                guidance_message="conflict msg 2",
                conflict_excerpt="other code",
                status="resolved",
            )
        )
        db_session.commit()
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-guidance-stats?project_id={project.id}"
        )
        body = resp.json()
        assert body["conflicts"]["total"] == 2
        assert body["conflicts"]["open"] == 1
        assert body["conflicts"]["resolved"] == 1


# ── pilot-token-stats ─────────────────────────────────────────────────────────


class TestPilotTokenStatsEndpoint:
    def test_empty_project_returns_nulls(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-token-stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tasks_with_tokens"] == 0
        assert body["avg_tokens_in"] is None
        assert body["top_consumers"] == []

    def test_aggregates_token_data(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(
            db_session,
            session,
            task,
            attempt=1,
            tokens_in=1000,
            tokens_out=800,
        )
        _make_execution(
            db_session,
            session,
            task,
            attempt=2,
            tokens_in=2000,
            tokens_out=1200,
        )
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-token-stats?project_id={project.id}"
        )
        body = resp.json()
        assert body["tasks_with_tokens"] == 2
        assert abs(body["avg_tokens_in"] - 1500.0) < 0.01
        assert body["total_tokens_in"] == 3000

    def test_null_token_rows_excluded_from_avg(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(
            db_session,
            session,
            task,
            attempt=1,
            tokens_in=1000,
            tokens_out=500,
        )
        _make_execution(db_session, session, task, attempt=2)  # no tokens
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-token-stats?project_id={project.id}"
        )
        body = resp.json()
        assert body["tasks_with_tokens"] == 1
        assert body["token_availability_rate"] == 0.5

    def test_top_consumers_sorted_by_total_tokens(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(
            db_session,
            session,
            task,
            attempt=1,
            tokens_in=500,
            tokens_out=300,
        )
        _make_execution(
            db_session,
            session,
            task,
            attempt=2,
            tokens_in=2000,
            tokens_out=1500,
        )
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-token-stats?project_id={project.id}"
        )
        body = resp.json()
        # Top consumer should be the one with more tokens (2000+1500=3500)
        assert body["top_consumers"][0]["tokens_in"] == 2000


# ── pilot-permission-stats ────────────────────────────────────────────────────


class TestPilotPermissionStatsEndpoint:
    def test_empty_project_returns_zero(
        self, authenticated_client: TestClient, project: Project
    ):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-permission-stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["approvals"] == 0
        assert body["pending"] == 0
        assert body["avg_response_seconds"] is None

    def test_counts_by_status(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        project: Project,
        session: SessionModel,
        task: Task,
    ):
        db_session.add(
            PermissionRequest(
                project_id=project.id,
                session_id=session.id,
                task_id=task.id,
                operation_type="file_write",
                status="approved",
            )
        )
        db_session.add(
            PermissionRequest(
                project_id=project.id,
                session_id=session.id,
                task_id=task.id,
                operation_type="file_write",
                status="pending",
            )
        )
        db_session.commit()
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-permission-stats?project_id={project.id}"
        )
        body = resp.json()
        assert body["approvals"] == 1
        assert body["pending"] == 1
        assert body["denials"] == 0

    def test_response_shape(self, authenticated_client: TestClient, project: Project):
        resp = authenticated_client.get(
            f"/api/v1/ops/pilot-permission-stats?project_id={project.id}"
        )
        body = resp.json()
        for key in (
            "computed_at",
            "project_id",
            "approvals",
            "denials",
            "pending",
            "avg_response_seconds",
            "max_response_seconds",
        ):
            assert key in body, f"Missing key: {key}"


# ── queue-latency p95 ─────────────────────────────────────────────────────────


class TestQueueLatencyP95Field:
    def test_p95_field_present_in_response(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/ops/queue-latency")
        assert resp.status_code == 200
        assert "p95_queue_latency_seconds" in resp.json()

    def test_p95_null_when_fewer_than_20_samples(
        self, authenticated_client: TestClient
    ):
        # No data → count is 0 → p95 is None
        resp = authenticated_client.get("/api/v1/ops/queue-latency?days=1")
        body = resp.json()
        assert body["p95_queue_latency_seconds"] is None
