"""Verify failure knowledge recording for stopped sessions.

Tests:
1. Manual stop (stop_session_lifecycle) does NOT trigger failure knowledge.
2. Orphan-recovery stop (runtime failure) triggers retrieve(trigger_phase="failure").
3. Orphan-recovery stop writes KnowledgeUsageLog rows.
4. Maintenance stale-session recovery records failure knowledge.
5. Maintenance recovery survives adapter failure.
6. record_failure_knowledge_for_stopped_session is callable and records usage.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeItem,
    KnowledgeUsageLog,
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
)
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.phases.failure_flow import (
    record_failure_knowledge_for_stopped_session,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Sess = sessionmaker(bind=engine)
    session = Sess()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed(db):
    project = Project(name="Stop Test", workspace_path="/tmp/stop_test")
    db.add(project)
    db.flush()

    session = SessionModel(
        project_id=project.id,
        name="Stop Session",
        status="running",
        is_active=True,
        execution_mode="automatic",
        instance_id="test-instance-001",
    )
    db.add(session)
    db.flush()

    task = Task(
        project_id=project.id,
        title="Running task",
        description="task in progress",
        status="running",
        current_step=0,
    )
    db.add(task)
    db.flush()

    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.add(link)

    # A stale log that looks like a planning terminal message
    db.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            session_instance_id=session.instance_id,
            level="INFO",
            message="[ORCHESTRATION] Planning response received; parsing and validating plan",
            created_at=datetime.now(UTC) - timedelta(seconds=150),
        )
    )
    db.commit()
    db.refresh(project)
    db.refresh(session)
    db.refresh(task)
    return project, session, task, link


def _empty_knowledge_ctx(session_id: int) -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=[],
        query="test",
        trigger_phase="failure",
        retrieval_reason="no_results",
        confidence=0.0,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )


def _failure_knowledge_ctx(item_id: str) -> KnowledgeContext:
    ref = KnowledgeItemRef(
        id=item_id,
        title="Worker Timeout",
        knowledge_type=KnowledgeType.failure_memory,
        content="SoftTimeLimitExceeded worker timed out",
        priority=10,
        confidence=0.9,
    )
    return KnowledgeContext(
        retrieved_items=[ref],
        query="stalled planning",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.9,
        matched_failure_memory=True,
        recommended_action=RecommendedAction.stop_retry,
    )


# ---------------------------------------------------------------------------
# 1. Manual stop does NOT trigger failure knowledge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_stop_does_not_trigger_failure_knowledge(db, monkeypatch):
    """stop_session_lifecycle must not call record_failure_knowledge_for_stopped_session."""
    from app.services.session.session_lifecycle_service import stop_session_lifecycle

    project, session, task, link = _seed(db)

    calls = []

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda db, session_id, terminate=True: [],
    )

    async def _fake_stop():
        pass

    fake_runtime = MagicMock()
    fake_runtime.stop_session = _fake_stop
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: fake_runtime,
    )

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        MagicMock(
            return_value=MagicMock(
                load_checkpoint=MagicMock(side_effect=Exception("no checkpoint")),
            )
        ),
    )

    def _track_record(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        _track_record,
    )

    result = await stop_session_lifecycle(
        db,
        session.id,
        initiated_by="test_user@example.com",
        source="api:POST /sessions/test/stop",
    )

    assert result["status"] == "stopped"
    assert (
        calls == []
    ), "Manual stop must not call record_failure_knowledge_for_stopped_session"


# ---------------------------------------------------------------------------
# 2 & 3. Orphan recovery triggers failure knowledge retrieval and KnowledgeUsageLog
# ---------------------------------------------------------------------------


def test_orphan_recovery_triggers_failure_knowledge(db, monkeypatch):
    """_recover_orphaned_running_session_if_needed calls record_failure_knowledge_for_stopped_session."""
    from app.services.session.session_lifecycle_service import (
        _recover_orphaned_running_session_if_needed,
    )

    project, session, task, link = _seed(db)

    calls = []

    def _track_record(**kwargs):
        calls.append(kwargs)

    # Function is imported locally inside the method body — patch at source module
    with patch(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        side_effect=_track_record,
    ):
        recovered = _recover_orphaned_running_session_if_needed(db, session=session)

    assert recovered is True
    db.refresh(session)
    assert session.status == "stopped"
    assert len(calls) == 1
    assert calls[0]["session_id"] == session.id
    assert calls[0]["task_id"] == task.id
    assert "orphan" in calls[0]["failure_reason"].lower()


def test_orphan_recovery_writes_knowledge_usage_log(db, monkeypatch):
    """After orphan recovery, KnowledgeUsageLog must have trigger_phase='failure'."""
    from app.services.session.session_lifecycle_service import (
        _recover_orphaned_running_session_if_needed,
    )
    from app.services.knowledge.knowledge_service import KnowledgeService

    project, session, task, link = _seed(db)

    content = "planning stall orphan timeout failure"
    item = KnowledgeItem(
        title="Orphaned Planning Stall",
        content=content,
        knowledge_type=KnowledgeType.failure_memory,
        applies_to=["failure"],
        tags=[],
        priority=10,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()

    _fake_vector = [0.0] * 1536

    with patch.object(KnowledgeService, "_embed", return_value=_fake_vector):
        svc = KnowledgeService(qdrant_url=":memory:")
        svc.ingest(item)

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService",
        return_value=svc,
    ), patch.object(svc, "_embed", return_value=_fake_vector):
        recovered = _recover_orphaned_running_session_if_needed(db, session=session)

    assert recovered is True
    logs = (
        db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, trigger_phase="failure")
        .all()
    )
    assert (
        len(logs) >= 1
    ), "Expected at least 1 KnowledgeUsageLog with trigger_phase='failure'"
    assert all(log.used_in_prompt is False for log in logs)


# ---------------------------------------------------------------------------
# 4. Maintenance stale-session recovery
# ---------------------------------------------------------------------------


def test_stale_running_session_recovery_records_failure_knowledge(db):
    """recover_stale_running_sessions records failure knowledge for stale runtime stops."""
    from app.services.session.session_lifecycle_service import (
        recover_stale_running_sessions,
    )

    project, session, task, link = _seed(db)
    task.current_step = 2
    db.commit()

    calls = []

    def _track_record(**kwargs):
        calls.append(kwargs)
        return True

    with patch(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        side_effect=_track_record,
    ):
        recovered = recover_stale_running_sessions(db, stale_after_seconds=60)

    assert len(recovered) == 1
    assert recovered[0]["session_id"] == session.id
    assert recovered[0]["task_id"] == task.id
    assert recovered[0]["stop_reason"] == "no_progress_timeout"
    assert recovered[0]["knowledge_recorded"] is True
    db.refresh(session)
    db.refresh(task)
    assert session.status == "stopped"
    assert task.status == TaskStatus.PENDING
    assert len(calls) == 1
    assert calls[0]["failure_reason"] == "no_progress_timeout"


def test_stale_running_session_recovery_survives_adapter_failure(db):
    """recover_stale_running_sessions must not fail when knowledge adapter raises."""
    from app.services.session.session_lifecycle_service import (
        recover_stale_running_sessions,
    )

    project, session, task, link = _seed(db)
    task.current_step = 3
    db.commit()

    with patch(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        side_effect=RuntimeError("knowledge unavailable"),
    ):
        recovered = recover_stale_running_sessions(db, stale_after_seconds=60)

    assert len(recovered) == 1
    assert recovered[0]["session_id"] == session.id
    assert recovered[0]["task_id"] == task.id
    assert recovered[0]["knowledge_recorded"] is False
    db.refresh(session)
    assert session.status == "stopped"


# 5. record_failure_knowledge_for_stopped_session standalone
# ---------------------------------------------------------------------------


def test_record_failure_knowledge_logs_usage(db, monkeypatch):
    """record_failure_knowledge_for_stopped_session writes KnowledgeUsageLog via mocked KnowledgeService."""
    project, session, task, _ = _seed(db)

    knowledge_ctx = _failure_knowledge_ctx(item_id="fake-uuid-stop-001")

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.usage_log_service.log_usage"
    ) as mock_log_usage, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="planning stall orphaned",
            signature_hash=lambda: "deadbeef" * 8,
        )
        MockSvc.return_value.retrieve.return_value = knowledge_ctx

        record_failure_knowledge_for_stopped_session(
            db=db,
            session_id=session.id,
            task_id=task.id,
            failure_reason="orphaned planning run stalled after planning-response handling",
            logger=logging.getLogger("test.stop"),
        )

    mock_log_usage.assert_called_once()
    kwargs = mock_log_usage.call_args[1]
    assert kwargs["session_id"] == session.id
    assert kwargs["task_id"] == task.id
    assert kwargs["used_in_prompt"] is False
    ctx_arg = kwargs["context"]
    assert ctx_arg.trigger_phase == "failure"


def test_record_failure_knowledge_survives_qdrant_failure(db, monkeypatch):
    """record_failure_knowledge_for_stopped_session must not raise when KnowledgeService fails."""
    project, session, task, _ = _seed(db)

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService",
        side_effect=Exception("Qdrant connection refused"),
    ):
        record_failure_knowledge_for_stopped_session(
            db=db,
            session_id=session.id,
            task_id=task.id,
            failure_reason="timeout",
            logger=logging.getLogger("test.stop"),
        )
    # No exception raised — function is defensive

    logs = (
        db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, trigger_phase="failure")
        .all()
    )
    assert logs == [], "No logs expected when KnowledgeService fails"
