from __future__ import annotations

import logging

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.phases.planning_flow import (
    _finalize_planning_timeout_failure,
)
from app.services.orchestration.types import OrchestrationRunContext
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

    def retry(self, exc):
        raise self._RetrySignal(exc)


class _UnexpectedRetrySelfTask:
    max_retries = 3

    class request:
        retries = 0

    def retry(self, exc):
        raise AssertionError("planning lock timeout should not schedule Celery retry")


def test_planner_marks_architecture_inspection_as_review_only():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: Inspect current project architecture | Review the real files, tests, fixtures, and extension points before implementation.
"""
    )

    assert parsed
    assert parsed[0].execution_profile == "review_only"


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
