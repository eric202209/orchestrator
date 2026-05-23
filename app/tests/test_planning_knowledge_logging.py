"""Prove that KnowledgeUsageLog rows are committed before the LLM call.

If the downstream LLM/OpenClaw call raises TimeoutError, the usage rows must
still exist because log_usage(db.commit) runs before execute_task is called.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    ExecutionFailureSummary,
    KnowledgeItem,
    KnowledgeUsageLog,
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
from app.services.orchestration.phases.planning_flow import (
    _retrieve_knowledge,
    execute_planning_phase,
)
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

    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db.add(link)
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
    return project, session, task, link, item


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


def _build_ctx(db, session, task, link, item) -> OrchestrationRunContext:
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
        session_task_link=link,
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


def test_validation_knowledge_retrieval_uses_failure_reason_query(mem_db):
    ctx = MagicMock()
    ctx.prompt = "Upgrade landing page verification commands"
    ctx.db = mem_db
    ctx.logger = logging.getLogger("test.validation_knowledge_retrieval")
    expected_ctx = KnowledgeContext(
        retrieved_items=[],
        query="Plan validation failed: missing workspace files",
        trigger_phase="validation",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=0.0,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as service_cls:
        service_cls.return_value.retrieve.return_value = expected_ctx

        result = _retrieve_knowledge(
            ctx,
            trigger_phase="validation",
            knowledge_types=["failure_memory", "format_guide", "debug_case"],
            query="Plan validation failed: missing workspace files",
            failure_signature="missing workspace files",
        )

    assert result is expected_ctx
    service_cls.return_value.retrieve.assert_called_once_with(
        query="Plan validation failed: missing workspace files",
        trigger_phase="validation",
        knowledge_types=["failure_memory", "format_guide", "debug_case"],
        failure_signature="missing workspace files",
        db=mem_db,
    )


def test_knowledge_usage_logged_before_planning_llm_timeout(mem_db, monkeypatch):
    """KnowledgeUsageLog rows must survive even if the LLM call raises TimeoutError.

    Ordering invariant:
      _retrieve_knowledge  →  assemble_planning_prompt  →  _log_knowledge_usage (db.commit)
      →  [LLM call — may raise]
    """
    project, session, task, link, item = _seed_db(mem_db)
    knowledge_ctx = _knowledge_ctx_for(item)
    ctx = _build_ctx(mem_db, session, task, link, item)

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
    project, session, task, link, item = _seed_db(mem_db)

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

    ctx = _build_ctx(mem_db, session, task, link, item)

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


def test_malformed_planning_output_repair_timeout_does_not_leave_session_running(
    mem_db, monkeypatch
):
    project, session, task, link, item = _seed_db(mem_db)
    fallback_ctx = KnowledgeContext(
        retrieved_items=[],
        query=None,
        trigger_phase="failure",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=0.0,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    ctx = _build_ctx(mem_db, session, task, link, item)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *a, **kw: _knowledge_ctx_for(item),
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
    monkeypatch.setattr(
        PlannerService,
        "should_retry_with_minimal_prompt",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *a, **kw: {"output": "still malformed"}),
    )

    def _raise_repair_timeout(*args, **kwargs):
        raise TimeoutError("repair timeout")

    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *a, **kw: _raise_repair_timeout()),
    )

    def _attempt_json_parsing(*args, **kwargs):
        return False, None, "json parse failed"

    ctx.error_handler.attempt_json_parsing = _attempt_json_parsing

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="malformed planning output timeout",
            signature_hash=lambda: "feedface" * 8,
        )
        MockSvc.return_value.retrieve.return_value = fallback_ctx

        result = execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda x: x,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: [],
            workspace_violation_error_cls=RuntimeError,
        )

    mem_db.refresh(session)
    mem_db.refresh(task)
    mem_db.refresh(link)
    assert result == {
        "status": "failed",
        "reason": "malformed_planning_output_repair_timeout",
    }
    assert session.status == "paused"
    assert session.is_active is False
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    summary = (
        mem_db.query(ExecutionFailureSummary).filter_by(session_id=session.id).first()
    )
    assert summary is not None


def test_planning_repair_timeout_records_failure_knowledge(mem_db, monkeypatch):
    project, session, task, link, item = _seed_db(mem_db)
    failure_ctx = KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id=item.id,
                title=item.title,
                knowledge_type=item.knowledge_type,
                content=item.content,
                priority=item.priority,
                confidence=0.91,
            )
        ],
        query="repair timeout",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.91,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    ctx = _build_ctx(mem_db, session, task, link, item)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *a, **kw: _knowledge_ctx_for(item),
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
    monkeypatch.setattr(
        PlannerService,
        "should_retry_with_minimal_prompt",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *a, **kw: {"output": "still malformed"}),
    )

    def _raise_repair_timeout(*args, **kwargs):
        raise TimeoutError("repair timeout")

    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *a, **kw: _raise_repair_timeout()),
    )

    def _attempt_json_parsing(*args, **kwargs):
        return False, None, "json parse failed"

    ctx.error_handler.attempt_json_parsing = _attempt_json_parsing

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="repair timeout",
            signature_hash=lambda: "deadbeef" * 8,
        )
        MockSvc.return_value.retrieve.return_value = failure_ctx

        result = execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda x: x,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: [],
            workspace_violation_error_cls=RuntimeError,
        )

    assert result["status"] == "failed"
    assert result["reason"] == "malformed_planning_output_repair_timeout"
    logs = (
        mem_db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, task_id=task.id, trigger_phase="failure")
        .all()
    )
    assert len(logs) == 1
    assert logs[0].retrieval_reason == "semantic_retrieval"
    assert logs[0].used_in_prompt is False
    assert logs[0].knowledge_item_id == item.id


def test_planning_validation_failure_records_planning_validation_and_failure_knowledge(
    mem_db, monkeypatch
):
    project, session, task, link, item = _seed_db(mem_db)
    plan = [
        {
            "step_number": 1,
            "description": "Create app output",
            "commands": ["printf 'ok\\n' > app.py"],
            "verification": "python3 - <<'PY'\nfrom pathlib import Path\nassert Path('app.py').read_text() == 'ok\\n'\nPY",
            "rollback": "rm -f app.py",
            "expected_files": ["app.py"],
        }
    ]
    planning_ctx = _knowledge_ctx_for(item)
    validation_ctx = KnowledgeContext(
        retrieved_items=planning_ctx.retrieved_items,
        query="validation failure",
        trigger_phase="validation",
        retrieval_reason="semantic_retrieval",
        confidence=0.89,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    failure_ctx = KnowledgeContext(
        retrieved_items=planning_ctx.retrieved_items,
        query="planning validation failed after repair",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.9,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    ctx = _build_ctx(mem_db, session, task, link, item)

    async def _execute_task(*args, **kwargs):
        return {"status": "completed", "output": json.dumps(plan)}

    ctx.runtime_service.execute_task = _execute_task
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    def _retrieve_by_phase(*args, **kwargs):
        if kwargs.get("trigger_phase") == "validation":
            return validation_ctx
        return planning_ctx

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        _retrieve_by_phase,
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
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.record_validation_verdict",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.maybe_emit_divergence_detected",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.session.replan_service.get_or_generate_failure_summary",
        lambda *a, **kw: None,
    )

    from app.services.orchestration.planning.planner import PlannerService
    from app.services.orchestration.validation.validator import ValidatorService

    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(
            lambda cls, *a, **kw: (_ for _ in ()).throw(
                AssertionError("hidden minimal-prompt retry should not run")
            )
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *a, **kw: {"output": json.dumps(plan)}),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_plan",
        staticmethod(
            lambda *a, **kw: type(
                "Verdict",
                (),
                {
                    "accepted": False,
                    "warning": False,
                    "status": "rejected",
                    "reasons": ["Plan contains placeholder-only implementation"],
                    "details": {},
                    "verdict": {"status": "rejected"},
                },
            )()
        ),
    )

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="planning validation failed after repair",
            signature_hash=lambda: "facefeed" * 8,
        )
        MockSvc.return_value.retrieve.return_value = failure_ctx

        result = execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda value: value if isinstance(value, list) else None,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: a[3],
            workspace_violation_error_cls=RuntimeError,
        )

    mem_db.refresh(session)
    mem_db.refresh(task)
    mem_db.refresh(link)
    assert result == {
        "status": "failed",
        "reason": "planning_validation_failed_after_repair",
    }
    assert session.status == "paused"
    assert session.is_active is False
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    phases = [
        row.trigger_phase
        for row in mem_db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, task_id=task.id)
        .order_by(KnowledgeUsageLog.id.asc())
        .all()
    ]
    assert sorted(phases) == ["failure", "planning", "validation"]


def test_immediate_stale_replace_repair_uses_validation_knowledge(
    mem_db, tmp_path, monkeypatch
):
    project, session, task, link, item = _seed_db(mem_db)
    (tmp_path / "index.html").write_text("<main><h1>Garden Story</h1></main>\n")
    stale_plan = [
        {
            "step_number": 1,
            "description": "Update existing image reference",
            "commands": [],
            "verification": 'python -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": ["index.html"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "index.html",
                    "old": '<img src="images/',
                    "new": '<img src="images/',
                }
            ],
        }
    ]
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Verify existing page",
            "commands": [
                "python -c \"import pathlib; assert pathlib.Path('index.html').exists()\""
            ],
            "verification": "python -c \"import pathlib; assert pathlib.Path('index.html').exists()\"",
            "rollback": None,
            "expected_files": ["index.html"],
        }
    ]
    planning_ctx = _knowledge_ctx_for(item)
    validation_ctx = KnowledgeContext(
        retrieved_items=planning_ctx.retrieved_items,
        query="Plan immediate repair issue",
        trigger_phase="validation",
        retrieval_reason="failure_signature_match",
        confidence=0.92,
        matched_failure_memory=True,
        recommended_action=RecommendedAction.adjust_format,
    )
    ctx = _build_ctx(mem_db, session, task, link, item)
    ctx.orchestration_state.project_dir = tmp_path

    async def _execute_task(*args, **kwargs):
        return {"status": "completed", "output": json.dumps(stale_plan)}

    ctx.runtime_service.execute_task = _execute_task
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    def _retrieve_by_phase(*args, **kwargs):
        if kwargs.get("trigger_phase") == "validation":
            return validation_ctx
        return planning_ctx

    captured_repair: dict[str, object] = {}

    def _repair_output(cls, *args, **kwargs):
        captured_repair.update(kwargs)
        return {"output": json.dumps(repaired_plan)}

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        _retrieve_by_phase,
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
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.record_validation_verdict",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.maybe_emit_divergence_detected",
        lambda *a, **kw: None,
    )

    from app.services.orchestration.planning.planner import PlannerService
    from app.services.orchestration.validation.validator import ValidatorService

    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(_repair_output),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_plan",
        staticmethod(
            lambda *a, **kw: type(
                "Verdict",
                (),
                {
                    "accepted": True,
                    "warning": False,
                    "status": "accepted",
                    "reasons": [],
                    "details": {},
                    "verdict": {"status": "accepted"},
                },
            )()
        ),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *a, **kw: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={},
        extract_structured_text=lambda x: str(x),
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *a, **kw: a[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert captured_repair["knowledge_context"] is validation_ctx
    assert captured_repair["reason"].startswith("plan_contains_immediate_repair_issues")
    logs = (
        mem_db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, task_id=task.id, trigger_phase="validation")
        .all()
    )
    assert len(logs) == 1
    assert logs[0].retrieval_reason == "failure_signature_match"
    assert logs[0].used_in_prompt is True


def test_oversized_planning_repair_prompt_skips_repair_and_records_failure_knowledge(
    mem_db, monkeypatch
):
    project, session, task, link, item = _seed_db(mem_db)
    failure_ctx = KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id=item.id,
                title=item.title,
                knowledge_type=item.knowledge_type,
                content=item.content,
                priority=item.priority,
                confidence=0.91,
            )
        ],
        query="repair prompt too large",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.91,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    ctx = _build_ctx(mem_db, session, task, link, item)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *a, **kw: _knowledge_ctx_for(item),
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
    monkeypatch.setattr(
        PlannerService,
        "should_retry_with_minimal_prompt",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *a, **kw: {"output": "still malformed"}),
    )
    monkeypatch.setattr(
        "app.services.orchestration.planning.planner.REPAIR_PROMPT_MAX_CHARS",
        200,
    )
    monkeypatch.setattr(
        "app.services.orchestration.planning.planner.PLANNING_REPAIR_PROMPT_MAX_CHARS",
        200,
    )

    def _attempt_json_parsing(*args, **kwargs):
        return False, None, "json parse failed"

    ctx.error_handler.attempt_json_parsing = _attempt_json_parsing

    with patch(
        "app.services.knowledge.knowledge_service.KnowledgeService"
    ) as MockSvc, patch(
        "app.services.knowledge.failure_signature_service.extract"
    ) as mock_extract:
        mock_extract.return_value = MagicMock(
            normalized_message="repair prompt too large",
            signature_hash=lambda: "beadfeed" * 8,
        )
        MockSvc.return_value.retrieve.return_value = failure_ctx

        result = execute_planning_phase(
            ctx=ctx,
            workspace_review={},
            extract_structured_text=lambda x: str(x),
            extract_plan_steps=lambda x: x,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *a, **kw: [],
            workspace_violation_error_cls=RuntimeError,
        )

    mem_db.refresh(session)
    mem_db.refresh(task)
    mem_db.refresh(link)
    assert result == {
        "status": "failed",
        "reason": "planning_repair_prompt_too_large",
    }
    assert session.status == "paused"
    assert session.is_active is False
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    logs = (
        mem_db.query(KnowledgeUsageLog)
        .filter_by(session_id=session.id, task_id=task.id, trigger_phase="failure")
        .all()
    )
    assert len(logs) == 1
    assert logs[0].retrieval_reason == "semantic_retrieval"
    assert logs[0].used_in_prompt is False
    assert logs[0].knowledge_item_id == item.id
