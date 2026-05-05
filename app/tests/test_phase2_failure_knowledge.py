"""Phase 2: real failure knowledge docs — retrieval and usage logging.

Verifies:
1. All 7 failure docs exist with required frontmatter.
2. failure_memory items are retrieved for the failure phase.
3. debug_case items are retrieved for the failure phase.
4. _apply_knowledge_halt logs usage when knowledge is matched.
5. _apply_knowledge_halt creates InterventionRequest when matched_failure_memory=True
   and retry_count >= 2.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    InterventionRequest,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
)
from app.models import Session as SessionModel
from app.models import Task, TaskStatus
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.knowledge.knowledge_service import KnowledgeService
from app.services.orchestration.phases.failure_flow import _apply_knowledge_halt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAILURE_DOCS_DIR = _REPO_ROOT / "knowledge" / "failure"

_EXPECTED_DOCS = [
    "openai_401.md",
    "qdrant_connection_refused.md",
    "openclaw_timeout.md",
    "backend_timeout.md",
    "worker_timeout.md",
    "websocket_disconnect.md",
    "invalid_planning_output.md",
]

_FAKE_VECTOR = [0.0] * 1536


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---"), f"{path.name}: no frontmatter"
    end = text.find("\n---", 3)
    assert end != -1, f"{path.name}: frontmatter not closed"
    return yaml.safe_load(text[3:end].strip()) or {}


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


@pytest.fixture()
def svc():
    with patch.object(KnowledgeService, "_embed", return_value=_FAKE_VECTOR):
        yield KnowledgeService(qdrant_url=":memory:")


def _make_failure_item(
    db, *, title: str, knowledge_type: str, content: str = "failure content"
) -> KnowledgeItem:
    item = KnowledgeItem(
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        applies_to=["failure"],
        tags=[],
        priority=10,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()
    return item


def _seed_session_context(db):
    project = Project(name="Phase2 Test", workspace_path="/tmp/p2")
    db.add(project)
    db.flush()

    session = SessionModel(
        project_id=project.id,
        name="P2 Session",
        status="running",
        is_active=True,
        execution_mode="automatic",
    )
    db.add(session)
    db.flush()

    task = Task(
        project_id=project.id,
        title="Phase2 task",
        description="test task",
        status="running",
        plan_position=0,
    )
    db.add(task)
    db.flush()
    db.commit()
    db.refresh(project)
    db.refresh(session)
    db.refresh(task)
    return project, session, task


# ---------------------------------------------------------------------------
# 1. Doc existence and frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _EXPECTED_DOCS)
def test_failure_doc_exists(filename):
    path = _FAILURE_DOCS_DIR / filename
    assert path.exists(), f"Missing failure doc: {filename}"


@pytest.mark.parametrize("filename", _EXPECTED_DOCS)
def test_failure_doc_frontmatter(filename):
    path = _FAILURE_DOCS_DIR / filename
    fm = _parse_frontmatter(path)
    assert fm.get("title"), f"{filename}: missing 'title'"
    assert fm.get("type") in (
        "failure_memory",
        "debug_case",
    ), f"{filename}: type must be failure_memory or debug_case, got {fm.get('type')!r}"
    applies = fm.get("applies_to")
    assert (
        applies and "failure" in applies
    ), f"{filename}: applies_to must include 'failure'"


# ---------------------------------------------------------------------------
# 2. Retrieval — failure_memory items returned for failure phase
# ---------------------------------------------------------------------------


def test_failure_memory_retrieved_for_failure_phase(svc, db):
    item = _make_failure_item(
        db, title="OpenAI 401", knowledge_type=KnowledgeType.failure_memory
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        ctx = svc.retrieve(
            query="AuthenticationError 401 incorrect api key",
            trigger_phase="failure",
            knowledge_types=[KnowledgeType.failure_memory, KnowledgeType.debug_case],
            db=db,
        )
    assert any(r.title == "OpenAI 401" for r in ctx.retrieved_items)
    assert ctx.matched_failure_memory is True
    assert ctx.recommended_action == RecommendedAction.stop_retry


def test_debug_case_retrieved_for_failure_phase(svc, db):
    item = _make_failure_item(
        db, title="Invalid Planning Output", knowledge_type=KnowledgeType.debug_case
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        ctx = svc.retrieve(
            query="json.JSONDecodeError invalid planning output",
            trigger_phase="failure",
            knowledge_types=[KnowledgeType.failure_memory, KnowledgeType.debug_case],
            db=db,
        )
    assert any(r.title == "Invalid Planning Output" for r in ctx.retrieved_items)
    assert ctx.recommended_action == RecommendedAction.review_failure


def test_failure_items_not_returned_for_planning_phase(svc, db):
    item = _make_failure_item(
        db, title="Worker Timeout", knowledge_type=KnowledgeType.failure_memory
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        ctx = svc.retrieve(
            query="task execution timeout",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )
    assert not any(r.title == "Worker Timeout" for r in ctx.retrieved_items)


# ---------------------------------------------------------------------------
# 3. _apply_knowledge_halt logs usage
# ---------------------------------------------------------------------------


def _build_ctx(db, session, task, project):
    ctx = MagicMock()
    ctx.db = db
    ctx.task = task
    ctx.project = project
    ctx.session = session
    ctx.orchestration_state = MagicMock()
    ctx.orchestration_state.current_phase = "execution"
    return ctx


def _matched_failure_ctx(item_id: str) -> KnowledgeContext:
    ref = KnowledgeItemRef(
        id=item_id,
        title="Worker Timeout",
        knowledge_type=KnowledgeType.failure_memory,
        content="SoftTimeLimitExceeded worker timed out",
        priority=10,
        confidence=0.95,
    )
    return KnowledgeContext(
        retrieved_items=[ref],
        query="SoftTimeLimitExceeded",
        trigger_phase="failure",
        retrieval_reason="failure_signature_match",
        confidence=0.95,
        matched_failure_memory=True,
        recommended_action=RecommendedAction.stop_retry,
    )


def test_apply_knowledge_halt_logs_usage(db):
    project, session, task = _seed_session_context(db)
    ctx = _build_ctx(db, session, task, project)

    knowledge_ctx = _matched_failure_ctx(item_id="fake-uuid-0001")

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.usage_log_service.log_usage"
    ) as mock_log_usage, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="softtimelimitexceeded worker timed out",
            signature_hash=lambda: "deadbeef" * 8,
        )
        MockSvc.return_value.retrieve.return_value = knowledge_ctx

        _apply_knowledge_halt(
            ctx=ctx,
            exc=Exception("SoftTimeLimitExceeded"),
            retry_count=0,
            session_id=session.id,
            task_id=task.id,
            logger=logging.getLogger("test"),
        )

    mock_log_usage.assert_called_once()
    call_kwargs = mock_log_usage.call_args[1]
    assert call_kwargs["session_id"] == session.id
    assert call_kwargs["task_id"] == task.id
    assert call_kwargs["used_in_prompt"] is False


def test_apply_knowledge_halt_creates_intervention_on_known_failure(db):
    project, session, task = _seed_session_context(db)
    ctx = _build_ctx(db, session, task, project)

    knowledge_ctx = _matched_failure_ctx(item_id="fake-uuid-0002")

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.usage_log_service.log_usage"
    ) as mock_log_usage, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="softtimelimitexceeded",
            signature_hash=lambda: "deadbeef" * 8,
        )
        MockSvc.return_value.retrieve.return_value = knowledge_ctx

        result = _apply_knowledge_halt(
            ctx=ctx,
            exc=Exception("SoftTimeLimitExceeded"),
            retry_count=2,
            session_id=session.id,
            task_id=task.id,
            logger=logging.getLogger("test"),
        )

    assert result is True, "_apply_knowledge_halt must return True when halting"
    mock_log_usage.assert_called_once()
    requests = db.query(InterventionRequest).filter_by(session_id=session.id).all()
    assert len(requests) == 1
    assert "Worker Timeout" in requests[0].prompt
    assert requests[0].intervention_type == "guidance"
    assert requests[0].initiated_by == "ai"
    assert task.status == TaskStatus.FAILED
