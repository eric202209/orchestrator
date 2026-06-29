"""Tests for DecisionAnalyticsService — Phase 15F-1 / 15F-2."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    InterventionRequest,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.analytics.decision_analytics_service import DecisionAnalyticsService
from app.services.orchestration.events.event_types import EventType

_WINDOW_LABELS = ("7d", "30d", "all_time")

_WINDOW_KEYS = {
    "successful_recovery_strategies",
    "repeated_failures",
    "knowledge_effectiveness",
    "coordinator_reliability",
    "project_reliability",
    "improvement_opportunities",
}


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _project(db, name: str = "test-project") -> Project:
    p = Project(name=name, workspace_path="/tmp/test")
    db.add(p)
    db.flush()
    return p


def _session(
    db,
    project: Project,
    *,
    status: str = "completed",
    repair_churn_stopped: bool = False,
    created_at: datetime | None = None,
) -> SessionModel:
    now = created_at or datetime.now(UTC)
    s = SessionModel(
        project_id=project.id,
        name=f"session-{uuid.uuid4()}",
        status=status,
        started_at=now,
        created_at=now,
        repair_churn_stopped=repair_churn_stopped,
    )
    db.add(s)
    db.flush()
    return s


def _task(db, project: Project, *, error_message: str | None = None) -> Task:
    t = Task(
        project_id=project.id,
        title=f"task-{uuid.uuid4()}",
        description="x",
        error_message=error_message,
    )
    db.add(t)
    db.flush()
    return t


def _execution(
    db,
    session: SessionModel,
    task: Task,
    *,
    attempt: int = 1,
    status: TaskStatus = TaskStatus.FAILED,
    failure_category: str | None = "context_overflow",
    created_at: datetime | None = None,
) -> TaskExecution:
    now = created_at or datetime.now(UTC)
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=status,
        failure_category=failure_category,
        created_at=now,
    )
    db.add(ex)
    db.flush()
    return ex


def _knowledge_item(db, title: str = "Retry Format Guide") -> KnowledgeItem:
    item = KnowledgeItem(
        id=str(uuid.uuid4()),
        title=title,
        content="content",
        knowledge_type="pattern",
        checksum=str(uuid.uuid4()),
    )
    db.add(item)
    db.flush()
    return item


def _usage(
    db,
    session: SessionModel,
    item: KnowledgeItem,
    *,
    used_in_prompt: bool = True,
    was_effective: bool | None = False,
    confidence: float = 0.9,
) -> KnowledgeUsageLog:
    log = KnowledgeUsageLog(
        id=str(uuid.uuid4()),
        session_id=session.id,
        knowledge_item_id=item.id,
        trigger_phase="planning",
        retrieval_reason="test",
        confidence=confidence,
        rank=1,
        used_in_prompt=used_in_prompt,
        was_effective=was_effective,
        created_at=datetime.now(UTC),
    )
    db.add(log)
    db.flush()
    return log


def _event_record(
    event_type: str,
    *,
    ts: datetime | None = None,
    phase: str | None = None,
    details: dict | None = None,
) -> dict:
    event = {
        "event_type": event_type,
        "timestamp": (ts or datetime.now(UTC)).isoformat(),
    }
    if phase is not None:
        event["phase"] = phase
    if details is not None:
        event["details"] = details
    return {
        "project_id": 1,
        "session_id": 1,
        "task_id": 1,
        "timestamp": ts or datetime.now(UTC),
        "event": event,
    }


class TestEmptyDatabase:
    def test_contract_shape(self, mem_db):
        result = DecisionAnalyticsService(mem_db).compute()
        assert set(result) == {"windows", "generated_at", "metrics_version"}
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == set(_WINDOW_LABELS)
        for label in _WINDOW_LABELS:
            assert set(result["windows"][label]) == _WINDOW_KEYS

    def test_empty_collections(self, mem_db):
        result = DecisionAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            for key in _WINDOW_KEYS:
                assert result["windows"][label][key] == []


class TestDecisionSignals:
    def test_repeated_failures_and_project_reliability(self, mem_db):
        p = _project(mem_db)
        s1 = _session(mem_db, p, status="stopped", repair_churn_stopped=True)
        s2 = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(mem_db, s1, t, failure_category="context_overflow")
        _execution(mem_db, s2, t, failure_category="context_overflow")
        _execution(mem_db, s2, t, attempt=2, status=TaskStatus.DONE)
        mem_db.add(
            InterventionRequest(
                project_id=p.id,
                session_id=s1.id,
                intervention_type="guidance",
                prompt="help",
                status="pending",
            )
        )
        mem_db.commit()

        w = DecisionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        failure = w["repeated_failures"][0]
        assert failure["failure_signature"] == "context_overflow"
        assert failure["occurrences"] == 2
        assert failure["projects"] == 1
        assert failure["sessions"] == 2
        assert failure["affected_project_ids"] == [p.id]
        assert failure["affected_session_ids"] == [s1.id, s2.id]
        project = w["project_reliability"][0]
        assert project["project_name"] == "test-project"
        assert project["session_success_rate"] == 0.5
        assert project["intervention_rate"] == 0.5
        assert project["recovery_rate"] == 1.0
        assert project["repair_churn"] == 1

    def test_knowledge_leaderboard_and_rewrite_recommendation(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        for _ in range(4):
            _usage(mem_db, s, item, used_in_prompt=True, was_effective=False)
        mem_db.commit()

        w = DecisionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        entry = w["knowledge_effectiveness"][0]
        assert entry["title"] == "Retry Format Guide"
        assert entry["retrievals"] == 4
        assert entry["success_contribution"] == 0
        assert entry["effectiveness"] == 0.0
        assert any(
            item["kind"] == "knowledge"
            and item["target"] == "Retry Format Guide"
            and item["recommendation"] == "Candidate for rewrite."
            and item["confidence"] > 0
            and item["evidence"]["sample_size"] == 4
            and item["evidence"]["affected_projects"] == [p.id]
            and item["evidence"]["affected_sessions"] == [s.id]
            and item["evidence"]["supporting_metrics"]["effectiveness"] == 0.0
            for item in w["improvement_opportunities"]
        )

    def test_event_derived_recovery_and_coordinator_metrics(self, mem_db):
        now = datetime.now(UTC)
        events = [
            _event_record(
                EventType.PHASE_STARTED,
                ts=now,
                phase="planning",
            ),
            _event_record(
                EventType.EXECUTION_RECOVERY_ATTEMPTED,
                ts=now + timedelta(seconds=1),
                phase="planning",
                details={"repair_type": "planning_repair"},
            ),
            _event_record(
                EventType.EXECUTION_RECOVERY_SUCCEEDED,
                ts=now + timedelta(seconds=2),
                phase="planning",
                details={"repair_type": "planning_repair"},
            ),
            _event_record(
                EventType.TASK_FAILED,
                ts=now + timedelta(seconds=3),
                phase="planning",
            ),
            _event_record(
                EventType.PHASE_FINISHED,
                ts=now + timedelta(seconds=10),
                phase="planning",
            ),
        ]

        with patch.object(
            DecisionAnalyticsService,
            "_collect_event_records",
            return_value=events,
        ):
            w = DecisionAnalyticsService(mem_db).compute()["windows"]["all_time"]

        strategy = w["successful_recovery_strategies"][0]
        assert strategy["repair_type"] == "planning_repair"
        assert strategy["attempts"] == 1
        assert strategy["successes"] == 1
        assert strategy["success_rate"] == 1.0
        assert strategy["affected_project_ids"] == [1]
        assert strategy["affected_session_ids"] == [1]
        coordinator = w["coordinator_reliability"][0]
        assert coordinator["coordinator"] == "PlanningCoordinator"
        assert coordinator["invocations"] == 1
        assert coordinator["failures"] == 1
        assert coordinator["recovery_rate"] == 1.0
        assert coordinator["average_duration_seconds"] == 10.0
        assert coordinator["affected_project_ids"] == [1]
        assert coordinator["affected_session_ids"] == [1]

    def test_every_recommendation_includes_structured_evidence(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        for _ in range(4):
            _usage(mem_db, s, item, used_in_prompt=True, was_effective=False)
        mem_db.commit()

        w = DecisionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["improvement_opportunities"]
        for item in w["improvement_opportunities"]:
            assert "confidence" in item
            assert "rationale" in item
            evidence = item["evidence"]
            assert set(evidence) == {
                "sample_size",
                "affected_projects",
                "affected_sessions",
                "supporting_metrics",
            }
            assert isinstance(evidence["sample_size"], int)
            assert isinstance(evidence["affected_projects"], list)
            assert isinstance(evidence["affected_sessions"], list)
            assert isinstance(evidence["supporting_metrics"], dict)

    def test_drilldown_returns_deterministic_item_evidence(self, mem_db):
        p = _project(mem_db)
        s1 = _session(mem_db, p, status="stopped")
        s2 = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(mem_db, s1, t, failure_category="context_overflow")
        _execution(mem_db, s2, t, failure_category="context_overflow")
        mem_db.commit()

        service = DecisionAnalyticsService(mem_db)
        first = service.drilldown(
            kind="failure_signature",
            target="context_overflow",
            window="all_time",
        )
        second = service.drilldown(
            kind="failure_signature",
            target="context_overflow",
            window="all_time",
        )
        assert first == second
        assert first["found"] is True
        assert first["item"]["occurrences"] == 2
        assert first["evidence"]["sample_size"] == 2
        assert first["evidence"]["affected_projects"] == [p.id]
        assert first["evidence"]["affected_sessions"] == [s1.id, s2.id]

    def test_drilldown_missing_target_degrades_gracefully(self, mem_db):
        result = DecisionAnalyticsService(mem_db).drilldown(
            kind="knowledge",
            target="missing",
            window="7d",
        )
        assert result["found"] is False
        assert result["window"] == "7d"
        assert result["item"] is None
        assert result["evidence"] == {
            "sample_size": 0,
            "affected_projects": [],
            "affected_sessions": [],
            "supporting_metrics": {},
        }

    def test_json_serialization(self, mem_db):
        result = DecisionAnalyticsService(mem_db).compute()
        json.dumps(result)

    def test_endpoint_function_returns_service_result(self, mem_db):
        from app.api.v1.endpoints.analytics import get_decision_analytics

        result = get_decision_analytics(current_user=object(), db=mem_db)
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == set(_WINDOW_LABELS)

    def test_drilldown_endpoint_function_returns_service_result(self, mem_db):
        from app.api.v1.endpoints.analytics import get_decision_analytics_drilldown

        result = get_decision_analytics_drilldown(
            kind="knowledge",
            target="missing",
            window="all_time",
            current_user=object(),
            db=mem_db,
        )
        assert result["found"] is False
        assert result["kind"] == "knowledge"
