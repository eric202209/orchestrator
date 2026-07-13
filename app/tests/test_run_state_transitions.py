from __future__ import annotations

from datetime import UTC, datetime

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.run_state import (
    cancel_attempt_for_session_pause_stop,
    finalize_attempt_completion_validation_failure,
    finalize_attempt_execution_failure,
    finalize_attempt_planning_failure,
    finalize_attempt_successful_completion,
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
    refresh_task_execution_lease,
    read_run_state_snapshot,
    reset_active_attempts_for_session_stop,
    task_execution_id_from_context,
)


def _make_task_attempt(db_session):
    project = Project(name="Run State Project", workspace_path="/tmp/run-state")
    db_session.add(project)
    db_session.commit()

    session = SessionModel(
        id=1,
        project_id=project.id,
        name="Run state session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()

    task = Task(
        project_id=project.id,
        title="Run state task",
        description="Exercise transitions",
        status=TaskStatus.PENDING,
        task_subfolder="task-run-state",
    )
    db_session.add(task)
    db_session.commit()

    link = SessionTask(
        session_id=1,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    execution = TaskExecution(
        session_id=1,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db_session.add_all([link, execution])
    db_session.commit()
    return task, link, execution


def test_mark_task_attempt_running_updates_task_link_and_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    started_at = datetime(2026, 5, 14, 1, 2, 3, tzinfo=UTC)

    result = mark_task_attempt_running(
        task=task,
        session_task_link=link,
        task_execution=execution,
        started_at=started_at,
    )

    assert result == started_at
    assert task.status == TaskStatus.RUNNING
    assert task.started_at == started_at
    assert task.completed_at is None
    assert task.error_message is None
    assert task.current_step == 0
    assert task.workspace_status == "in_progress"
    assert link.status == TaskStatus.RUNNING
    assert link.started_at == started_at
    assert link.completed_at is None
    assert execution.status == TaskStatus.RUNNING
    assert execution.started_at == started_at
    assert execution.completed_at is None


def test_refresh_task_execution_lease_updates_only_running_attempt(db_session):
    _, _, execution = _make_task_attempt(db_session)
    execution.status = TaskStatus.RUNNING
    heartbeat_at = datetime(2026, 5, 14, 2, 3, 4, tzinfo=UTC)

    assert (
        refresh_task_execution_lease(execution, heartbeat_at=heartbeat_at)
        == heartbeat_at
    )
    assert execution.heartbeat_at == heartbeat_at

    execution.status = TaskStatus.DONE
    assert refresh_task_execution_lease(execution, heartbeat_at=heartbeat_at) is None


def test_mark_task_attempt_pending_resets_task_link_and_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    started_at = datetime(2026, 5, 14, 1, 2, 3, tzinfo=UTC)
    completed_at = datetime(2026, 5, 14, 2, 3, 4, tzinfo=UTC)
    task.status = TaskStatus.FAILED
    task.started_at = started_at
    task.completed_at = completed_at
    task.current_step = 4
    task.steps = [{"step": "old"}]
    task.workspace_status = "blocked"
    task.error_message = "old error"
    link.status = TaskStatus.FAILED
    link.started_at = started_at
    link.completed_at = completed_at
    execution.status = TaskStatus.FAILED
    execution.started_at = started_at
    execution.completed_at = completed_at

    mark_task_attempt_pending(
        task=task,
        session_task_link=link,
        task_execution=execution,
        reset_started_at=True,
        reset_steps=True,
        workspace_status="changes_requested",
        error_message="retry with repair context",
    )

    assert task.status == TaskStatus.PENDING
    assert task.started_at is None
    assert task.completed_at is None
    assert task.current_step == 0
    assert task.steps is None
    assert task.workspace_status == "changes_requested"
    assert task.error_message == "retry with repair context"
    assert link.status == TaskStatus.PENDING
    assert link.started_at is None
    assert link.completed_at is None
    assert execution.status == TaskStatus.PENDING
    assert execution.started_at is None
    assert execution.completed_at is None


def test_mark_task_attempt_failed_updates_task_link_and_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 2, 3, 4, tzinfo=UTC)

    result = mark_task_attempt_failed(
        task=task,
        session_task_link=link,
        task_execution=execution,
        error_message="planner unavailable",
        completed_at=completed_at,
        workspace_status="blocked",
    )

    assert result == completed_at
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "planner unavailable"
    assert task.completed_at == completed_at
    assert task.workspace_status == "blocked"
    assert link.status == TaskStatus.FAILED
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at == completed_at


def test_mark_task_attempt_cancelled_updates_task_link_and_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 3, 4, 5, tzinfo=UTC)

    mark_task_attempt_cancelled(
        task=task,
        session_task_link=link,
        task_execution=execution,
        completed_at=completed_at,
    )

    assert task.status == TaskStatus.CANCELLED
    assert task.completed_at == completed_at
    assert link.status == TaskStatus.CANCELLED
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.CANCELLED
    assert execution.completed_at == completed_at


def test_mark_task_attempt_done_updates_task_link_and_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    task.error_message = "old error"
    completed_at = datetime(2026, 5, 14, 4, 5, 6, tzinfo=UTC)

    mark_task_attempt_done(
        task=task,
        session_task_link=link,
        task_execution=execution,
        completed_at=completed_at,
    )

    assert task.status == TaskStatus.DONE
    assert task.error_message is None
    assert task.completed_at == completed_at
    assert link.status == TaskStatus.DONE
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.DONE
    assert execution.completed_at == completed_at


def test_attempt_planning_failure_wrapper_preserves_failed_semantics(db_session):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 5, 6, 7, tzinfo=UTC)

    result = finalize_attempt_planning_failure(
        task=task,
        session_task_link=link,
        task_execution=execution,
        error_message="planning validation failed",
        completed_at=completed_at,
    )

    assert result == completed_at
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "planning validation failed"
    assert task.completed_at == completed_at
    assert task.workspace_status == "blocked"
    assert link.status == TaskStatus.FAILED
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at == completed_at


def test_attempt_execution_failure_wrapper_preserves_failed_semantics(db_session):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 6, 7, 8, tzinfo=UTC)

    result = finalize_attempt_execution_failure(
        task=task,
        session_task_link=link,
        task_execution=execution,
        error_message="verification failed",
        completed_at=completed_at,
    )

    assert result == completed_at
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "verification failed"
    assert task.workspace_status == "blocked"
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED


def test_attempt_completion_validation_failure_wrapper_preserves_failed_semantics(
    db_session,
):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 7, 8, 9, tzinfo=UTC)

    result = finalize_attempt_completion_validation_failure(
        task=task,
        session_task_link=link,
        task_execution=execution,
        error_message="completion validation failed",
        completed_at=completed_at,
    )

    assert result == completed_at
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "completion validation failed"
    assert task.workspace_status == "blocked"
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED


def test_attempt_pause_stop_cancellation_wrapper_preserves_cancelled_semantics(
    db_session,
):
    task, link, execution = _make_task_attempt(db_session)
    completed_at = datetime(2026, 5, 14, 8, 9, 10, tzinfo=UTC)

    result = cancel_attempt_for_session_pause_stop(
        task=task,
        session_task_link=link,
        task_execution=execution,
        completed_at=completed_at,
    )

    assert result == completed_at
    assert task.status == TaskStatus.CANCELLED
    assert task.completed_at == completed_at
    assert link.status == TaskStatus.CANCELLED
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.CANCELLED
    assert execution.completed_at == completed_at


def test_attempt_successful_completion_wrapper_preserves_done_semantics(db_session):
    task, link, execution = _make_task_attempt(db_session)
    task.error_message = "old error"
    completed_at = datetime(2026, 5, 14, 9, 10, 11, tzinfo=UTC)

    result = finalize_attempt_successful_completion(
        task=task,
        session_task_link=link,
        task_execution=execution,
        completed_at=completed_at,
    )

    assert result == completed_at
    assert task.status == TaskStatus.DONE
    assert task.error_message is None
    assert task.completed_at == completed_at
    assert link.status == TaskStatus.DONE
    assert link.completed_at == completed_at
    assert execution.status == TaskStatus.DONE
    assert execution.completed_at == completed_at


def test_reset_active_attempts_cancels_pending_and_running_executions(db_session):
    task, link, execution = _make_task_attempt(db_session)
    task.status = TaskStatus.RUNNING
    link.status = TaskStatus.RUNNING
    execution.status = TaskStatus.PENDING
    db_session.commit()

    reset_count = reset_active_attempts_for_session_stop(db_session, session_id=1)

    assert reset_count == 1
    assert task.status == TaskStatus.PENDING
    assert task.completed_at is None
    assert task.workspace_status == "isolated"
    assert link.status == TaskStatus.PENDING
    assert link.completed_at is None
    assert execution.status == TaskStatus.CANCELLED
    assert execution.completed_at is not None
    assert execution.failure_category == "manual_stop"


def test_reset_active_attempts_terminalizes_explicit_operator_stop(db_session):
    task, link, execution = _make_task_attempt(db_session)
    task.status = TaskStatus.RUNNING
    link.status = TaskStatus.RUNNING
    execution.status = TaskStatus.RUNNING
    db_session.commit()

    reset_count = reset_active_attempts_for_session_stop(
        db_session,
        session_id=1,
        terminalize=True,
        stop_reason="Operator requested stop",
    )

    assert reset_count == 1
    assert task.status == TaskStatus.CANCELLED
    assert task.error_message == "Operator requested stop"
    assert task.completed_at is not None
    assert link.status == TaskStatus.CANCELLED
    assert link.completed_at == task.completed_at
    assert execution.status == TaskStatus.CANCELLED
    assert execution.completed_at == task.completed_at
    assert execution.failure_category == "manual_stop"


def test_task_execution_id_from_context_rejects_bool_and_non_int():
    class Context:
        def __init__(self, task_execution_id):
            self.task_execution_id = task_execution_id

    assert task_execution_id_from_context(Context(42)) == 42
    assert task_execution_id_from_context(Context(True)) is None
    assert task_execution_id_from_context(Context("42")) is None
    assert task_execution_id_from_context(None) is None


def test_read_run_state_snapshot_flags_stopped_session_active_execution(db_session):
    task, link, execution = _make_task_attempt(db_session)
    session = db_session.query(SessionModel).filter(SessionModel.id == 1).one()
    session.status = "stopped"
    session.is_active = False
    task.status = TaskStatus.PENDING
    link.status = TaskStatus.PENDING
    execution.status = TaskStatus.PENDING
    db_session.commit()

    snapshot = read_run_state_snapshot(
        db_session,
        session_id=1,
        task_id=task.id,
    )

    assert snapshot.session_status == "stopped"
    assert snapshot.session_is_active is False
    assert snapshot.task_status == TaskStatus.PENDING.value
    assert snapshot.session_task_status == TaskStatus.PENDING.value
    assert snapshot.task_execution_status == TaskStatus.PENDING.value
    assert snapshot.has_active_execution is True
    assert snapshot.stopped_with_active_execution is True
