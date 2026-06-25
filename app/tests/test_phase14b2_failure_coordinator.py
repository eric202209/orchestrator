"""Phase 14B-2: FailureCoordinator tests.

Tests call FailureCoordinator().handle_failure() directly and assert that the
coordinator routes to the correct outcome for each path. All external calls
(mark_task_attempt_failed, session state, DB) are verified through real DB
fixtures where the existing planner-recovery tests do it, or through mocks for
coordinator-boundary concerns.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.types import OrchestrationRunContext

_LOG = logging.getLogger(__name__)
_NOOP = lambda *a, **k: None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TerminalSelfTask:
    """Celery task stub with no retry capacity."""

    max_retries = 0

    class request:
        retries = 0

    def retry(self, exc, **kwargs):
        raise AssertionError("retry should not be called")


class _RetryCapableSelfTask:
    """Celery task stub on first attempt (retries=0, max_retries=3)."""

    max_retries = 3

    class request:
        retries = 0

    class _RetrySignal(Exception):
        pass

    def retry(self, exc, **kwargs):
        self.retry_kwargs = kwargs
        raise self._RetrySignal(exc)


def _seed_ctx(db_session, *, execution_mode="manual", plan_position=None):
    project = Project(name="FC Test Project", workspace_path="/tmp/fc-test")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id,
        name="FC Session",
        status="running",
        execution_mode=execution_mode,
        is_active=True,
    )
    task = Task(
        project_id=project.id,
        title="FC Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-fc",
        plan_position=plan_position,
    )
    db_session.add_all([session, task])
    db_session.flush()

    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="test prompt",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=_LOG,
        emit_live=_NOOP,
        error_handler=type(
            "EH",
            (),
            {"should_retry": staticmethod(lambda exc, ctx: False)},
        )(),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )
    return ctx, session, task, execution


# ---------------------------------------------------------------------------
# 1. Terminal failure path — task marked failed, session paused, exc re-raised
# ---------------------------------------------------------------------------


def test_terminal_failure_marks_task_failed_and_reraises(db_session):
    ctx, session, task, _ = _seed_ctx(db_session)
    exc = RuntimeError("something broke")

    with pytest.raises(RuntimeError, match="something broke"):
        FailureCoordinator().handle_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )

    db_session.refresh(task)
    db_session.refresh(session)
    assert task.status == TaskStatus.FAILED
    assert session.status == "paused"


# ---------------------------------------------------------------------------
# 2. Retry-eligible failure path — retry is raised
# ---------------------------------------------------------------------------


def test_retry_eligible_path_raises_retry_signal(db_session):
    ctx, session, task, _ = _seed_ctx(db_session)
    exc = RuntimeError("transient error")

    retry_task = _RetryCapableSelfTask()
    # Override error_handler to say yes to retry
    error_handler = type("EH", (), {"should_retry": staticmethod(lambda e, c: True)})()
    object.__setattr__(ctx, "error_handler", error_handler)

    with pytest.raises(_RetryCapableSelfTask._RetrySignal):
        FailureCoordinator().handle_failure(
            self_task=retry_task,
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )


# ---------------------------------------------------------------------------
# 3. Retry blocked by restore failure — task re-failed, session paused, return
# ---------------------------------------------------------------------------


def test_retry_blocked_by_restore_failure_pauses_session(db_session, monkeypatch):
    ctx, session, task, _ = _seed_ctx(db_session)
    exc = RuntimeError("transient error")

    error_handler = type("EH", (), {"should_retry": staticmethod(lambda e, c: True)})()
    object.__setattr__(ctx, "error_handler", error_handler)

    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._prepare_retry_workspace",
        lambda **kwargs: (False, None, True),  # restore_blocked=True
    )

    # Should return (not raise) when retry is blocked
    result = FailureCoordinator().handle_failure(
        self_task=_RetryCapableSelfTask(),
        ctx=ctx,
        exc=exc,
        get_latest_session_task_link_fn=lambda *a, **k: None,
        write_project_state_snapshot_fn=_NOOP,
        save_orchestration_checkpoint_fn=_NOOP,
        record_live_log_fn=_NOOP,
    )

    assert result is None
    db_session.refresh(task)
    db_session.refresh(session)
    assert task.status == TaskStatus.FAILED
    assert session.status == "paused"


# ---------------------------------------------------------------------------
# 4. Session paused path — other_active_execution=False -> mark_session_paused
# ---------------------------------------------------------------------------


def test_no_other_active_execution_pauses_session(db_session):
    ctx, session, task, _ = _seed_ctx(db_session)
    exc = ValueError("workflow error")

    with pytest.raises(ValueError):
        FailureCoordinator().handle_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )

    db_session.refresh(session)
    assert session.status == "paused"


# ---------------------------------------------------------------------------
# 5. Session kept running when another execution is active
# ---------------------------------------------------------------------------


def test_other_active_execution_keeps_session_running(db_session):
    ctx, session, task, first_execution = _seed_ctx(db_session)

    # Seed a second active execution so _session_has_other_active_execution -> True
    second_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.RUNNING,
    )
    db_session.add(second_execution)
    db_session.commit()

    exc = RuntimeError("error while another exec is active")

    with pytest.raises(RuntimeError):
        FailureCoordinator().handle_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )

    db_session.refresh(session)
    assert session.status == "running"


# ---------------------------------------------------------------------------
# 6. Pre-existing terminal state is not overwritten
# ---------------------------------------------------------------------------


def test_pre_existing_terminal_task_status_not_overwritten(db_session, monkeypatch):
    """If coordinator is called after task is already FAILED, it should not
    change it back to PENDING (no retry) or alter a done session."""
    ctx, session, task, _ = _seed_ctx(db_session)

    # Manually put task in terminal state before the call
    task.status = TaskStatus.FAILED
    task.error_message = "pre-existing failure"
    db_session.commit()

    exc = RuntimeError("another error on already-failed task")

    with pytest.raises(RuntimeError):
        FailureCoordinator().handle_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )

    db_session.refresh(task)
    # Task should remain FAILED (not be set to PENDING via retry path)
    assert task.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# 7. handle_task_failure shim still routes through coordinator
# ---------------------------------------------------------------------------


def test_handle_task_failure_shim_calls_coordinator(db_session):
    """handle_task_failure in failure_flow.py delegates to FailureCoordinator."""
    ctx, session, task, _ = _seed_ctx(db_session)
    exc = RuntimeError("shim test error")

    with pytest.raises(RuntimeError, match="shim test error"):
        handle_task_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=exc,
            get_latest_session_task_link_fn=lambda *a, **k: None,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
        )

    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
