from __future__ import annotations

import logging

from app.models import (
    InterventionRequest,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.events.event_types import EventType
import app.services.orchestration.phases.failure_flow as failure_flow
from app.services.orchestration.phases.planning_flow import (
    _finalize_planning_timeout_failure,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.schemas.knowledge import KnowledgeContext, KnowledgeItemRef, RecommendedAction
from app.services.planning.planner_service import PlannerService
from app.services.session.session_runtime_service import build_task_execution_prompt
from app.services.workspace.project_mutation_lock import ProjectMutationLockError


class _FakeSelfTask:
    max_retries = 3

    class request:
        retries = 3

    def retry(self, exc):
        raise AssertionError(
            "retry should not be called in terminal auto-recovery path"
        )


class _RetryCapableSelfTask:
    """Simulates a Celery task on its first attempt (retries=0, max_retries=3)."""

    max_retries = 3

    class request:
        retries = 0

    class _RetrySignal(Exception):
        pass

    def retry(self, exc, **kwargs):
        self.retry_kwargs = kwargs
        raise self._RetrySignal(exc)


class _RetryRequestWithKwargsSelfTask(_RetryCapableSelfTask):
    class request:
        retries = 0
        kwargs = {
            "session_id": 1,
            "task_id": 1,
            "prompt": "original prompt",
            "timeout_seconds": 300,
            "resume_checkpoint_name": None,
            "expected_session_instance_id": "instance-1",
            "task_execution_id": 1,
            "queued_event_id": "queued-1",
        }


class _FakeOrchestrationState:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.status = None
        self.abort_reason = ""
        self.phase_history = []


class _UnexpectedRetrySelfTask:
    max_retries = 3

    class request:
        retries = 0

    def retry(self, exc):
        raise AssertionError("planning lock timeout should not schedule Celery retry")


def _knowledge_context(
    *,
    knowledge_type: str = "failure_memory",
    confidence: float,
    recommended_action: RecommendedAction,
) -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id="knowledge-item-1",
                title="Known failure memory",
                knowledge_type=knowledge_type,
                content="Known halt guidance.",
                priority=10,
                confidence=confidence,
            )
        ],
        query="runtime failure",
        trigger_phase="failure",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=confidence,
        matched_failure_memory=knowledge_type == "failure_memory",
        recommended_action=recommended_action,
    )


def _install_fake_knowledge_context(monkeypatch, knowledge_ctx: KnowledgeContext):
    usage_calls = []

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, **kwargs):
            return knowledge_ctx

    monkeypatch.setattr(
        "app.services.knowledge.knowledge_service.KnowledgeService",
        _FakeKnowledgeService,
    )
    monkeypatch.setattr(
        "app.services.knowledge.usage_log_service.log_usage",
        lambda **kwargs: usage_calls.append(kwargs),
    )
    return usage_calls


def _make_knowledge_halt_fixture(db_session):
    project = Project(name="Knowledge Halt Predicate Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Knowledge Halt Predicate Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Retryable task",
        description="Exercise knowledge halt predicate",
        status=TaskStatus.RUNNING,
        execution_profile="full_lifecycle",
        plan_position=1,
        workspace_status="isolated",
        task_subfolder="task-retryable-task",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    ctx = type(
        "KnowledgeHaltCtx",
        (),
        {
            "db": db_session,
            "project": project,
            "task": task,
            "session_task_link": None,
            "orchestration_state": type("State", (), {"current_phase": "execution"})(),
        },
    )()
    return project, session, task, ctx


def test_knowledge_context_low_confidence_failure_memory_cannot_halt():
    ctx = _knowledge_context(
        confidence=0.3,
        recommended_action=RecommendedAction.stop_retry,
    )

    assert failure_flow._knowledge_context_can_halt(ctx) is False


def test_low_confidence_failure_memory_does_not_halt_after_retries(
    db_session, monkeypatch
):
    _project, session, task, ctx = _make_knowledge_halt_fixture(db_session)
    usage_calls = _install_fake_knowledge_context(
        monkeypatch,
        _knowledge_context(
            confidence=0.3,
            recommended_action=RecommendedAction.stop_retry,
        ),
    )

    halted = failure_flow._apply_knowledge_halt(
        ctx=ctx,
        exc=RuntimeError("generic transient backend failure"),
        retry_count=3,
        session_id=session.id,
        task_id=task.id,
        logger=logging.getLogger(__name__),
    )

    db_session.refresh(task)
    assert halted is False
    assert task.status == TaskStatus.RUNNING
    assert usage_calls and usage_calls[0]["used_in_prompt"] is False
    assert db_session.query(InterventionRequest).count() == 0


def test_knowledge_context_high_confidence_stop_retry_failure_memory_can_halt():
    ctx = _knowledge_context(
        confidence=1.0,
        recommended_action=RecommendedAction.stop_retry,
    )

    assert failure_flow._knowledge_context_can_halt(ctx) is True


def test_high_confidence_stop_retry_failure_memory_halts_after_retries(
    db_session, monkeypatch
):
    _project, session, task, ctx = _make_knowledge_halt_fixture(db_session)
    usage_calls = _install_fake_knowledge_context(
        monkeypatch,
        _knowledge_context(
            confidence=1.0,
            recommended_action=RecommendedAction.stop_retry,
        ),
    )

    halted = failure_flow._apply_knowledge_halt(
        ctx=ctx,
        exc=RuntimeError("known exact backend failure"),
        retry_count=3,
        session_id=session.id,
        task_id=task.id,
        logger=logging.getLogger(__name__),
    )

    db_session.refresh(task)
    intervention = db_session.query(InterventionRequest).one_or_none()
    assert halted is True
    assert task.status == TaskStatus.FAILED
    assert intervention is not None
    assert "matched known failure memory" in intervention.prompt
    assert usage_calls and usage_calls[0]["used_in_prompt"] is False


def test_knowledge_context_failure_memory_without_stop_retry_cannot_halt():
    ctx = _knowledge_context(
        confidence=1.0,
        recommended_action=RecommendedAction.review_failure,
    )

    assert failure_flow._knowledge_context_can_halt(ctx) is False


def test_failure_memory_without_stop_retry_does_not_halt_after_retries(
    db_session, monkeypatch
):
    _project, session, task, ctx = _make_knowledge_halt_fixture(db_session)
    usage_calls = _install_fake_knowledge_context(
        monkeypatch,
        _knowledge_context(
            confidence=1.0,
            recommended_action=RecommendedAction.review_failure,
        ),
    )

    halted = failure_flow._apply_knowledge_halt(
        ctx=ctx,
        exc=RuntimeError("known failure that should be reviewed"),
        retry_count=3,
        session_id=session.id,
        task_id=task.id,
        logger=logging.getLogger(__name__),
    )

    db_session.refresh(task)
    assert halted is False
    assert task.status == TaskStatus.RUNNING
    assert usage_calls and usage_calls[0]["used_in_prompt"] is False
    assert db_session.query(InterventionRequest).count() == 0


def test_planner_marks_architecture_inspection_as_review_only():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: Inspect current project architecture | Review the real files, tests, fixtures, and extension points before implementation.
"""
    )

    assert parsed
    assert parsed[0].execution_profile == "review_only"


def test_planner_canonicalizes_partial_scope_aliases():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: Plan recovery | Decide the bounded repair approach only. | order=1 | profile=plan_only
- [ ] TASK_START: Validate recovery | Run focused verification only. | order=2 | profile=validate_only
"""
    )

    assert [task.execution_profile for task in parsed] == [
        "review_only",
        "test_only",
    ]
    assert [task.workflow_stage for task in parsed] == [
        "plan",
        "validate",
    ]


def test_planner_preserves_explicit_workflow_stage_separate_from_execution_profile():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: Plan bounded recovery approach | Decide the repair shape only. | order=1 | stage=plan | profile=review_only
- [ ] TASK_START: Review recovery outcome | Audit evidence before continuing. | order=2 | stage=complete | profile=review_only
"""
    )

    assert [task.execution_profile for task in parsed] == [
        "review_only",
        "review_only",
    ]
    assert [task.workflow_stage for task in parsed] == [
        "plan",
        "complete",
    ]


def test_planner_extracts_equals_style_order_metadata():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: First task | Start here | order=1 | profile=review_only
- [ ] TASK_START: Third task | Preserve explicit ordering | order=3 | profile=test_only
"""
    )

    assert [task.plan_position for task in parsed] == [1, 3]


def test_build_task_execution_prompt_includes_failure_recovery_context():
    task = Task(
        title="Inspect current project architecture",
        description="Review the current codebase and identify extension points.",
        workspace_status="changes_requested",
        error_message="Previous run guessed the wrong file layout and missed the existing tests.",
    )

    prompt = build_task_execution_prompt(task)

    assert "Recovery instructions:" in prompt
    assert "inspect the real current workspace" in prompt
    assert "Previous failure details:" in prompt


def test_handle_task_failure_queues_one_automatic_recovery_for_failed_ordered_task(
    db_session,
):
    project = Project(name="Recovery Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Recovery Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Inspect current project architecture",
        description="Inspect the existing workspace and identify the real structure.",
        status=TaskStatus.RUNNING,
        execution_profile="review_only",
        plan_position=1,
        workspace_status="isolated",
        task_subfolder="task-inspect-current-project-architecture",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    queued: list[int] = []

    def fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        queued.append(task_id)
        return {"task_id": task_id}

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="review_only",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: False)},
        )(),
        restore_workspace_snapshot_if_needed=None,
    )

    handle_task_failure(
        self_task=_FakeSelfTask(),
        ctx=ctx,
        exc=RuntimeError("Inspection guessed the wrong workspace structure"),
        get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
        queue_task_for_session_fn=fake_queue_task_for_session,
        write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
        save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
        record_live_log_fn=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(task)
    db_session.refresh(session)

    assert queued == [task.id]
    assert task.status == TaskStatus.PENDING
    assert task.workspace_status == "changes_requested"
    assert "Automatic recovery requested" in (task.error_message or "")
    assert session.status == "running"
    assert session.is_active is True


def test_knowledge_halt_does_not_queue_automatic_recovery(db_session, monkeypatch):
    project = Project(name="Knowledge Halt Recovery Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Knowledge Halt Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Backend gateway unavailable",
        description="Run through planner",
        status=TaskStatus.RUNNING,
        execution_profile="full_lifecycle",
        plan_position=1,
        workspace_status="isolated",
        task_subfolder="task-backend-gateway-unavailable",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: True,
    )
    queued: list[int] = []

    def fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        queued.append(task_id)
        return {"task_id": task_id}

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: False)},
        )(),
        restore_workspace_snapshot_if_needed=None,
    )

    try:
        handle_task_failure(
            self_task=_FakeSelfTask(),
            ctx=ctx,
            exc=RuntimeError(
                "OpenAI Responses request failed: All connection attempts failed"
            ),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            queue_task_for_session_fn=fake_queue_task_for_session,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *_args, **_kwargs: None,
        )
    except RuntimeError as exc:
        assert "OpenAI Responses request failed" in str(exc)
    else:
        raise AssertionError("knowledge-halted failure should remain terminal")

    db_session.refresh(task)
    db_session.refresh(session)

    assert queued == []
    assert task.status == TaskStatus.FAILED
    assert session.status == "paused"


def test_auto_recovery_queue_failure_preserves_original_error_and_execution(
    db_session,
):
    project = Project(name="Recovery Queue Failure Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Recovery Queue Failure Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Inspect current project architecture",
        description="Inspect the existing workspace and identify the real structure.",
        status=TaskStatus.RUNNING,
        execution_profile="review_only",
        plan_position=1,
        workspace_status="isolated",
        task_subfolder="task-inspect-current-project-architecture",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    def failing_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        raise RuntimeError("broker unavailable")

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="review_only",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: False)},
        )(),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )

    try:
        handle_task_failure(
            self_task=_FakeSelfTask(),
            ctx=ctx,
            exc=RuntimeError("original workspace failure"),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            queue_task_for_session_fn=failing_queue_task_for_session,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *_args, **_kwargs: None,
        )
    except RuntimeError as exc:
        assert str(exc) == "original workspace failure"
    else:
        raise AssertionError("handle_task_failure should re-raise the original error")

    db_session.refresh(task)
    db_session.refresh(execution)
    db_session.refresh(session)

    assert task.status == TaskStatus.FAILED
    assert "original workspace failure" in (task.error_message or "")
    assert "broker unavailable" in (task.error_message or "")
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at is not None
    assert session.status == "paused"


def test_celery_retry_leaves_task_pending_so_claim_can_succeed(db_session):
    """When handle_task_failure schedules a Celery retry, task.status must be PENDING.

    The claim guard in _claim_queued_task_for_worker requires task.status == PENDING.
    If failure_flow sets it to RUNNING before raising self_task.retry(), the retry
    fires but immediately returns task_not_claimable:running — session stuck forever.
    """
    project = Project(name="Retry Claim Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Retry Claim Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Add existing page content section",
        description="Add a content section to the existing page",
        status=TaskStatus.RUNNING,
        task_subfolder="task-add-existing-page-content-section",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: True)},
        )(),
        restore_workspace_snapshot_if_needed=None,
    )

    try:
        handle_task_failure(
            self_task=_RetryCapableSelfTask(),
            ctx=ctx,
            exc=TimeoutError("Planning timed out or exceeded context after 360s"),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *_args, **_kwargs: None,
        )
    except _RetryCapableSelfTask._RetrySignal:
        pass

    db_session.refresh(task)
    db_session.refresh(session)

    # Task must be PENDING so the retry's claim guard can succeed.
    # If RUNNING, _claim_queued_task_for_worker returns task_not_claimable:running
    # and the session stays stuck forever.
    assert (
        task.status == TaskStatus.PENDING
    ), f"task.status={task.status!r} — retry will fail with task_not_claimable:running"
    assert session.status == "running"
    assert session.is_active is True


def test_retryable_failure_restores_workspace_before_celery_retry(db_session, tmp_path):
    project = Project(name="Retry Restore Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Retry Restore Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
        instance_id="instance-1",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Mutating task",
        description="Mutates a file then hits a retryable runtime error",
        status=TaskStatus.RUNNING,
        task_subfolder="task-mutating",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(execution)

    mutated_file = tmp_path / "dirty.txt"
    mutated_file.write_text("partial mutation", encoding="utf-8")
    restore_calls = []
    live_logs = []

    def restore_workspace(reason, *, force_restore=False):
        restore_calls.append((reason, force_restore))
        mutated_file.unlink()
        return {"restored": True, "files_restored": 1}

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=_FakeOrchestrationState(tmp_path),
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: True)},
        )(),
        restore_workspace_snapshot_if_needed=restore_workspace,
        task_execution_id=execution.id,
    )

    retry_task = _RetryCapableSelfTask()
    try:
        handle_task_failure(
            self_task=retry_task,
            ctx=ctx,
            exc=RuntimeError("retryable failure after file mutation"),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *args, **kwargs: live_logs.append((args, kwargs)),
        )
    except _RetryCapableSelfTask._RetrySignal:
        pass

    assert not mutated_file.exists()
    assert restore_calls == [("retryable task failure", True)]
    assert any(
        "Restored workspace snapshot before retrying failed task" in args[4]
        for args, _kwargs in live_logs
    )
    assert getattr(retry_task, "retry_kwargs", {}) == {}


def test_retryable_failure_marks_dirty_checkpoint_resume_when_restore_unavailable(
    db_session, tmp_path, monkeypatch
):
    project = Project(name="Retry Dirty Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Retry Dirty Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
        instance_id="instance-1",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Dirty retry task",
        description="Fails after mutating the workspace",
        status=TaskStatus.RUNNING,
        task_subfolder="task-dirty-retry",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    captured_events = []
    live_logs = []

    def capture_event(**kwargs):
        captured_events.append(kwargs)
        return {"event_id": "event-1"}

    monkeypatch.setattr(failure_flow, "append_orchestration_event", capture_event)

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=_FakeOrchestrationState(tmp_path),
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: True)},
        )(),
        restore_workspace_snapshot_if_needed=lambda _reason, **_kwargs: {
            "restored": False,
            "reason": "snapshot_missing",
        },
    )

    retry_task = _RetryRequestWithKwargsSelfTask()
    retry_task.request.kwargs = {
        **retry_task.request.kwargs,
        "session_id": session.id,
        "task_id": task.id,
    }

    try:
        handle_task_failure(
            self_task=retry_task,
            ctx=ctx,
            exc=RuntimeError("retryable failure after dirty mutation"),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *args, **kwargs: live_logs.append((args, kwargs)),
        )
    except _RetryCapableSelfTask._RetrySignal:
        pass

    retry_kwargs = retry_task.retry_kwargs["kwargs"]
    assert retry_kwargs["resume_checkpoint_name"] == "autosave_error"
    assert retry_kwargs["queued_event_id"] is None
    assert any(
        event["event_type"] == EventType.WORKSPACE_RETRY_DIRTY
        for event in captured_events
    )
    assert any(
        kwargs.get("metadata", {}).get("retry_mode") == "checkpoint_resume_required"
        for _args, kwargs in live_logs
    )

    db_session.refresh(task)
    assert task.status == TaskStatus.PENDING
    assert "checkpoint resume" in (task.error_message or "")


def test_planning_lock_wait_timeout_terminalizes_execution_without_retry(db_session):
    project = Project(name="Planning Lock Timeout Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Planning Lock Timeout Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Config merge CLI",
        description="Create config merge CLI",
        status=TaskStatus.RUNNING,
        task_subfolder="task-config-merge-cli",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    task_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(task_execution)
    db_session.commit()
    db_session.refresh(task_execution)

    timeout = TimeoutError(
        "OpenClaw planning lock wait timed out after 30s: "
        "/tmp/orchestrator-openclaw-planning.lock"
    )
    timeout.runtime_diagnostics = {
        "timeout_boundary": "planning_lock_wait",
        "planning_lock_wait_seconds": 30.0,
    }

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: True)},
        )(),
        task_execution_id=task_execution.id,
        restore_workspace_snapshot_if_needed=None,
    )

    try:
        handle_task_failure(
            self_task=_UnexpectedRetrySelfTask(),
            ctx=ctx,
            exc=timeout,
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *_args, **_kwargs: None,
        )
    except TimeoutError:
        pass

    db_session.refresh(task)
    db_session.refresh(session)
    db_session.refresh(task_execution)

    assert task.status == TaskStatus.FAILED
    assert task.completed_at is not None
    assert session.status == "paused"
    assert session.is_active is False
    assert task_execution.status == TaskStatus.FAILED
    assert task_execution.completed_at is not None


def test_project_mutation_lock_conflict_terminalizes_without_pausing_active_session(
    db_session, tmp_path
):
    project = Project(name="Project Mutation Lock Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Project Mutation Lock Session",
        status="running",
        execution_mode="manual",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    current_task = Task(
        project_id=project.id,
        title="Task blocked by project writer",
        description="Attempt canonical-root write",
        status=TaskStatus.RUNNING,
        task_subfolder="task-blocked",
    )
    other_task = Task(
        project_id=project.id,
        title="Task holding project writer",
        description="Already writing canonical root",
        status=TaskStatus.RUNNING,
        task_subfolder="task-active",
    )
    db_session.add_all([current_task, other_task])
    db_session.commit()
    db_session.refresh(current_task)
    db_session.refresh(other_task)

    current_execution = TaskExecution(
        session_id=session.id,
        task_id=current_task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    other_execution = TaskExecution(
        session_id=session.id,
        task_id=other_task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([current_execution, other_execution])
    db_session.commit()
    db_session.refresh(current_execution)
    db_session.refresh(other_execution)

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=current_task,
        session_task_link=None,
        session_id=session.id,
        task_id=current_task.id,
        prompt=current_task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: True)},
        )(),
        task_execution_id=current_execution.id,
        restore_workspace_snapshot_if_needed=None,
    )

    try:
        handle_task_failure(
            self_task=_UnexpectedRetrySelfTask(),
            ctx=ctx,
            exc=ProjectMutationLockError(
                project_id=project.id,
                operation="execute_canonical_root_task",
                lock_path=tmp_path / "project.lock",
            ),
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
            write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
            save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
            record_live_log_fn=lambda *_args, **_kwargs: None,
        )
    except ProjectMutationLockError:
        pass

    db_session.refresh(current_task)
    db_session.refresh(current_execution)
    db_session.refresh(other_execution)
    db_session.refresh(session)

    assert current_task.status == TaskStatus.FAILED
    assert current_execution.status == TaskStatus.FAILED
    assert current_execution.completed_at is not None
    assert other_execution.status == TaskStatus.RUNNING
    assert session.status == "running"
    assert session.is_active is True
    terminal_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.session_id == session.id, LogEntry.level == "ERROR")
        .order_by(LogEntry.id.desc())
        .first()
    )
    assert terminal_log is not None
    assert "project_mutation_lock_conflict" in (terminal_log.log_metadata or "")


def test_initial_planning_timeout_terminalizes_task_execution(db_session, monkeypatch):
    project = Project(name="Planning Timeout Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Planning Timeout Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Node line statistics CLI",
        description="Create node line stats CLI",
        status=TaskStatus.RUNNING,
        task_subfolder="task-node-line-statistics-cli",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    task_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(task_execution)
    db_session.commit()
    db_session.refresh(task_execution)

    monkeypatch.setattr(
        "app.services.session.replan_service.get_or_generate_failure_summary",
        lambda *_args, **_kwargs: "summary",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        lambda **_kwargs: True,
    )

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=None,
        task_execution_id=task_execution.id,
        restore_workspace_snapshot_if_needed=None,
    )

    _finalize_planning_timeout_failure(
        ctx=ctx,
        failure_type="planning_context_overflow",
        failure_reason="Planning timed out or exceeded context after 300s",
    )

    db_session.refresh(task)
    db_session.refresh(session)
    db_session.refresh(task_execution)

    assert task.status == TaskStatus.FAILED
    assert task.completed_at is not None
    assert session.status == "paused"
    assert session.is_active is False
    assert task_execution.status == TaskStatus.FAILED
    assert task_execution.completed_at is not None
