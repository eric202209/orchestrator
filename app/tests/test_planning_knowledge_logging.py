"""Prove that KnowledgeUsageLog rows are committed before the LLM call.

If the downstream LLM/OpenClaw call raises TimeoutError, the usage rows must
still exist because log_usage(db.commit) runs before execute_task is called.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
    Task,
)
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.phases.planning_flow import execute_planning_phase
from app.services.orchestration.types import OrchestrationRunContext


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_db(db):
    project = Project(name="Logging Test Project", workspace_path="/tmp/kl_test")
    db.add(project)
    db.flush()

    session = SessionModel(
        project_id=project.id,
        name="Logging Test Session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db.add(session)
    db.flush()

    task = Task(
        project_id=project.id,
        title="Build a test page",
        description="Simple page task",
        status="running",
    )
    db.add(task)
    db.flush()

    content = "planning format guide content"
    item = KnowledgeItem(
        title="Planning Format Guide",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=5,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.commit()
    db.refresh(project)
    db.refresh(session)
    db.refresh(task)
    db.refresh(item)
    return project, session, task, item


def _knowledge_ctx_for(item: KnowledgeItem) -> KnowledgeContext:
    ref = KnowledgeItemRef(
        id=item.id,
        title=item.title,
        knowledge_type=item.knowledge_type,
        content=item.content,
        priority=item.priority,
        confidence=0.88,
    )
    return KnowledgeContext(
        retrieved_items=[ref],
        query="Build a test page",
        trigger_phase="planning",
        retrieval_reason="semantic_retrieval",
        confidence=0.88,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )


def _build_ctx(db, session, task, item) -> OrchestrationRunContext:
    orchestration_state = MagicMock()
    orchestration_state.project_dir = Path("/tmp/kl_project")
    orchestration_state.project_context = ""
    orchestration_state.plan = []

    runtime = MagicMock()
    runtime.get_backend_metadata.return_value = {}

    return OrchestrationRunContext(
        db=db,
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=session.id,
        task_id=task.id,
        prompt="Build a test page",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=runtime,
        task_service=MagicMock(),
        logger=logging.getLogger("test.planning_knowledge_logging"),
        emit_live=lambda *a, **kw: None,
        error_handler=MagicMock(),
    )


def test_knowledge_usage_logged_before_planning_llm_timeout(mem_db, monkeypatch):
    """KnowledgeUsageLog rows must survive even if the LLM call raises TimeoutError.

    Ordering invariant:
      _retrieve_knowledge  →  assemble_planning_prompt  →  _log_knowledge_usage (db.commit)
      →  [LLM call — may raise]
    """
    project, session, task, item = _seed_db(mem_db)
    knowledge_ctx = _knowledge_ctx_for(item)
    ctx = _build_ctx(mem_db, session, task, item)

    # Suppress Qdrant/OpenAI retrieval — return a known context instead.
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *a, **kw: knowledge_ctx,
    )

    # Return a real prompt so used_in_prompt=True.
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.assemble_planning_prompt",
        lambda *a, **kw: "mock planning prompt",
    )

    # Suppress filesystem / event side-effects.
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.append_orchestration_event",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.write_orchestration_state_snapshot",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.emit_phase_event",
        lambda *a, **kw: None,
    )

    # Force the minimal-prompt LLM path and make it simulate a timeout.
    from app.services.orchestration.planning.planner import PlannerService

    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *a, **kw: True),
    )

    def _raise_timeout(*a, **kw):
        raise TimeoutError("LLM timed out (simulated)")

    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *a, **kw: _raise_timeout()),
    )

    # execute_planning_phase raises TimeoutError when the LLM path fails before
    # the inner try block — the caller is expected to handle it.
    with pytest.raises(TimeoutError):
        execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda x: x,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: [],
            workspace_violation_error_cls=RuntimeError,
        )

    # The usage rows were committed by log_usage BEFORE the LLM call — they must
    # still be present despite the timeout.
    logs = mem_db.query(KnowledgeUsageLog).filter_by(session_id=session.id).all()
    assert len(logs) == 1, f"Expected 1 usage log, got {len(logs)}"
    assert logs[0].trigger_phase == "planning"
    assert logs[0].used_in_prompt is True
    assert logs[0].knowledge_item_id == item.id


def test_knowledge_usage_logged_with_sqlite_fallback_context(mem_db, monkeypatch):
    """KnowledgeUsageLog rows are written even when retrieval used sqlite_fallback path."""
    project, session, task, item = _seed_db(mem_db)

    fallback_ref = KnowledgeItemRef(
        id=item.id,
        title=item.title,
        knowledge_type=item.knowledge_type,
        content=item.content,
        priority=item.priority,
        confidence=0.3,
    )
    fallback_ctx = KnowledgeContext(
        retrieved_items=[fallback_ref],
        query=None,
        trigger_phase="planning",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=0.3,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )

    ctx = _build_ctx(mem_db, session, task, item)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *a, **kw: fallback_ctx,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.assemble_planning_prompt",
        lambda *a, **kw: "mock planning prompt",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.append_orchestration_event",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.write_orchestration_state_snapshot",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.emit_phase_event",
        lambda *a, **kw: None,
    )

    from app.services.orchestration.planning.planner import PlannerService

    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *a, **kw: True),
    )

    def _raise_timeout(*a, **kw):
        raise TimeoutError("LLM timed out (simulated)")

    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *a, **kw: _raise_timeout()),
    )

    with pytest.raises(TimeoutError):
        execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda x: x,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: [],
            workspace_violation_error_cls=RuntimeError,
        )

    logs = mem_db.query(KnowledgeUsageLog).filter_by(session_id=session.id).all()
    assert len(logs) == 1, f"Expected 1 usage log, got {len(logs)}"
    assert logs[0].retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"
    assert logs[0].trigger_phase == "planning"
    assert logs[0].used_in_prompt is True
    assert logs[0].knowledge_item_id == item.id
