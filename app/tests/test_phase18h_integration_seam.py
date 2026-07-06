"""Phase 18H certification seam test.

Exercises the real, wired path with no stubbed verdicts and no replay/fixture
corpus:

    Planning (validator failure) -> Validator -> Candidate Recovery
    -> Recovery Registry -> Audit Events (file journal) -> SQLite

using the actual `ValidatorService.validate_plan`, the actual
`execute_single_sibling_candidate_recovery` / `RecoveryStrategyRegistry`
production code, a real in-memory SQLite database, and a real event journal
under `tmp_path`. `CANDIDATE_RECOVERY_ENABLED` is monkeypatched True for this
test only; the repository default in `app/config.py` is unchanged.

Certification-only per Phase 18H
(`docs/roadmap/done/phase18/phase18h-cleanup-certification-report.md`). No
behavioral changes.

Note on the "-> Knowledge" hop: `app/services/orchestration/recovery/` does
not read or write Knowledge today (confirmed by grep during the Phase 18H-0
audit; see `docs/roadmap/openclaw-platform-architecture-review.md` section
11 and `docs/roadmap/repository-wide-strategic-risk-review.md` section 8).
Building that link is Phase 19A scope, not 18H. This test therefore
validates the Knowledge touchpoint as it genuinely exists today -- usage-log
persistence to SQLite, exercised directly via `_log_knowledge_usage` -- and
does not claim Candidate Recovery consumes or produces Knowledge.
"""

from __future__ import annotations

import hashlib
import logging
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
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
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases.planning_knowledge import _log_knowledge_usage
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.planning.candidate_recovery import (
    CandidateRecoveryRequest,
    execute_single_sibling_candidate_recovery,
)
from app.services.orchestration.prompt_templates import OrchestrationState


# ---------------------------------------------------------------------------
# Real plan fixtures (not stubbed verdicts). The "bad" plan is the same
# missing-verification shape used in test_validator_rule_telemetry.py; the
# "good" sibling is the plan proven accepted in test_minimum_valid_plan_size.py.
# ---------------------------------------------------------------------------

_BAD_PLAN = [
    {
        "step_number": 1,
        "description": "Implement source",
        "commands": [],
        "verification": "",
        "rollback": "",
        "expected_files": ["src/app.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "src/app.py",
                "content": "print('hello')\n",
            }
        ],
    }
]

_GOOD_PLAN_PATH = "src/tiny_money/money.py"
_GOOD_PLAN_VERIFY = "python3 -m pytest -q"
_GOOD_PLAN_CONTENT = '''\
"""Money formatting helpers for the tiny money fixture."""


def format_cents(cents: int) -> str:
    """Render integer cents as a dollar amount with two decimal places."""
    sign = "-" if cents < 0 else ""
    abs_cents = abs(cents)
    dollars = abs_cents // 100
    remainder = abs_cents % 100
    return f"{sign}${dollars}.{remainder:02d}"
'''
_GOOD_PLAN = [
    {
        "step_number": 1,
        "description": "Rewrite format_cents to render integer cents as dollar amounts",
        "commands": [_GOOD_PLAN_VERIFY],
        "verification": _GOOD_PLAN_VERIFY,
        "rollback": f"git checkout -- {_GOOD_PLAN_PATH}",
        "expected_files": [_GOOD_PLAN_PATH],
        "ops": [
            {
                "op": "write_file",
                "path": _GOOD_PLAN_PATH,
                "content": _GOOD_PLAN_CONTENT,
            }
        ],
    }
]


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


def _seed_session_and_task(db):
    project = Project(name="18H Seam Test Project", workspace_path="/tmp/18h_seam")
    db.add(project)
    db.flush()

    session = SessionModel(
        project_id=project.id,
        name="18H Seam Test Session",
        status="running",
        is_active=True,
        execution_mode="automatic",
    )
    db.add(session)
    db.flush()

    task = Task(
        project_id=project.id,
        title="Fix money formatter",
        description="Certification seam task",
        status="running",
    )
    db.add(task)
    db.flush()
    db.commit()
    db.refresh(project)
    db.refresh(session)
    db.refresh(task)
    return project, session, task


def _seed_knowledge_item(db):
    content = "Money-formatting fix pattern"
    item = KnowledgeItem(
        title="Money Formatter Fix Pattern",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=5,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_planning_failure_routes_through_candidate_recovery_to_sqlite_and_knowledge(
    mem_db, tmp_path, monkeypatch
):
    (
        project,
        session,
        task,
    ) = _seed_session_and_task(mem_db)
    knowledge_item = _seed_knowledge_item(mem_db)

    # --- Validator: real rejection on a genuinely incomplete plan ----------
    original_verdict = ValidatorService.validate_plan(
        _BAD_PLAN,
        output_text="",
        task_prompt="Write a small Python implementation",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )
    assert original_verdict.status == "repair_required"
    assert any("verification" in reason for reason in original_verdict.reasons)

    # --- Candidate Recovery: real sibling generation + real re-validation ---
    def _generate_sibling():
        import json

        return _GOOD_PLAN, json.dumps(_GOOD_PLAN)

    def _validate_candidate(plan, output_text):
        return ValidatorService.validate_plan(
            plan,
            output_text=output_text,
            task_prompt="Fix the money formatter so existing tests pass",
            execution_profile="medium_cli_single_file",
            project_dir=tmp_path,
        )

    monkeypatch.setattr(settings, "CANDIDATE_RECOVERY_ENABLED", True)
    assert (
        settings.CANDIDATE_RECOVERY_ENABLED is True
    )  # this test's local override only

    def _candidate_executor():
        return execute_single_sibling_candidate_recovery(
            CandidateRecoveryRequest(
                project_dir=tmp_path,
                session_id=session.id,
                task_id=task.id,
                original_plan=_BAD_PLAN,
                original_output_text="",
                original_verdict=original_verdict,
                runtime_profile="standard",
                parent_event_id=None,
                generate_sibling=_generate_sibling,
                validate_candidate=_validate_candidate,
            )
        )

    evidence = ExecutionRecoveryEvidence(
        task_title=task.title,
        task_description=task.description,
        failed_command="planning_validation",
        exit_code=None,
        stdout_excerpt="",
        stderr_excerpt="validator rejected plan",
        traceback_excerpt="",
        validator_rejection_reason=original_verdict.reasons[0],
        failure_class="planning_validation_failed",
    )

    # --- Recovery Registry: real routing decision ---------------------------
    from app.services.orchestration.recovery.failure_event import make_failure_event

    decision = RecoveryStrategyRegistry.route(
        make_failure_event(
            failure_class="planning_validation_failed",
            source="planning",
            error_message=original_verdict.reasons[0],
            session_id=session.id,
            task_id=task.id,
        ),
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
    )
    assert decision.strategy == "candidate_planning"

    context = RecoveryContext(
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
        scope="planning",
        evidence=evidence,
        orchestration_state=OrchestrationState(
            session_id=str(session.id), task_description=task.description
        ),
        runtime_profile="standard",
        recovery_metadata={"candidate_executor": _candidate_executor},
    )

    outcome = RecoveryStrategyRegistry.execute_candidate_planning(context=context)

    # --- Real outcome: sibling plan (accepted) beats the rejected original --
    assert outcome.succeeded is True
    assert outcome.strategy_result["status"] == "success"
    candidate_outcome = outcome.strategy_result["candidate_outcome"]
    assert candidate_outcome["outcome"] == "selected"
    assert candidate_outcome["candidate_count"] == 2

    # --- Audit Events: real file journal, not mocked ------------------------
    # Two RECOVERY_DECISION_ROUTED events are expected: one from the
    # standalone `route()` call above (routing decision only) and one from
    # `execute_candidate_planning()`'s own dispatch emit below.
    routed_events = read_orchestration_events(
        tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type_filter=EventType.RECOVERY_DECISION_ROUTED,
    )
    assert len(routed_events) == 2
    assert all(e["details"]["strategy"] == "candidate_planning" for e in routed_events)

    selected_events = read_orchestration_events(
        tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type_filter=EventType.PLAN_CANDIDATE_SELECTED,
    )
    assert len(selected_events) == 1

    # --- SQLite: real DB round-trip for session/task rows -------------------
    reloaded_task = mem_db.get(Task, task.id)
    assert reloaded_task is not None
    assert reloaded_task.project_id == project.id

    # --- Knowledge: real usage-log persistence (the genuine touchpoint) -----
    # This models Planning's own knowledge retrieval, not a Recovery -> Knowledge
    # link (which does not exist yet; see module docstring).
    knowledge_ctx = KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id=str(knowledge_item.id),
                title=knowledge_item.title,
                knowledge_type=knowledge_item.knowledge_type,
                content=knowledge_item.content,
                priority=knowledge_item.priority,
                confidence=0.9,
            )
        ],
        query="money formatter fix",
        trigger_phase="planning",
        retrieval_reason="semantic_retrieval",
        confidence=0.9,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    runtime = MagicMock()
    runtime.get_backend_metadata.return_value = {}

    run_ctx = OrchestrationRunContext(
        db=mem_db,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt="Fix the money formatter",
        timeout_seconds=300,
        execution_profile="medium_cli_single_file",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=runtime,
        task_service=MagicMock(),
        logger=logging.getLogger("test.phase18h_integration_seam"),
        emit_live=lambda *a, **kw: None,
        error_handler=MagicMock(),
    )
    _log_knowledge_usage(run_ctx, knowledge_ctx, used_in_prompt=True)

    usage_rows = (
        mem_db.query(KnowledgeUsageLog)
        .filter(KnowledgeUsageLog.session_id == session.id)
        .all()
    )
    assert len(usage_rows) == 1
    assert usage_rows[0].task_id == task.id
