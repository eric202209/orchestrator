"""Provider-free ER2 lifecycle failure/recovery reconciliation regressions."""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from types import SimpleNamespace

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
from app.services.orchestration.lifecycle.authority import derive_lifecycle_authority
from app.services.orchestration.phases.planning_support import (
    _finalize_planning_terminal_failure,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
)
from app.services.orchestration.types import OrchestrationRunContext


class _RetrySignal(Exception):
    pass


class _RetryTask:
    max_retries = 3
    default_retry_delay = 0

    def __init__(self, retries: int = 0):
        self.request = SimpleNamespace(retries=retries, kwargs={})

    def retry(self, exc, **_kwargs):
        raise _RetrySignal(str(exc))


def _seed(db, tmp_path: Path):
    project = Project(
        name="ER2 Project",
        workspace_path=str(tmp_path / "project-workspace"),
    )
    session = SessionModel(
        project=project,
        name="ER2 Execution Session",
        status="running",
        execution_mode="manual",
        is_active=True,
        instance_id="er2-generation-1",
    )
    task = Task(
        project=project,
        title="ER2 Task",
        description="Reproduce CA1 lifecycle ordering",
        status=TaskStatus.RUNNING,
        task_subfolder="task-er2",
        workspace_status="in_progress",
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session=session,
        task=task,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        worker_pid=424242,
        worker_hostname=socket.gethostname(),
        worker_process_start_identity="historical-process-start",
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    return project, session, task, link, execution


def _ctx(db, project, session, task, link, execution, *, should_retry=True):
    return OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
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
        error_handler=SimpleNamespace(should_retry=lambda _exc, _scope: should_retry),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )


def _failure_kwargs(link):
    return {
        "get_latest_session_task_link_fn": lambda *_args, **_kwargs: link,
        "write_project_state_snapshot_fn": lambda *_args, **_kwargs: None,
        "save_orchestration_checkpoint_fn": lambda *_args, **_kwargs: None,
        "record_live_log_fn": lambda *_args, **_kwargs: None,
    }


def _provider_free_failure_setup(monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._prepare_retry_workspace",
        lambda **_kwargs: (True, {}, False),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.orchestration.recovery.recovery_strategy_registry.RecoveryStrategyRegistry.route",
        lambda *_args, **_kwargs: SimpleNamespace(strategy="continue"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        lambda **_kwargs: True,
    )


def test_er2_ca1_replay_planning_exhaustion_does_not_preempt_recovery(
    db_session, tmp_path, monkeypatch
):
    """CA1 replay: invalid Planning exhausts, then outer recovery remains legal."""

    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="root_cause_oscillation_no_progress",
        failure_reason="invalid_python: bounded Planning repair exhausted",
        planning_root_cause="root_cause_oscillation_no_progress",
    )
    db_session.refresh(session)
    db_session.refresh(execution)
    assert session.status == "running"
    assert execution.status == TaskStatus.FAILED

    with pytest.raises(_RetrySignal):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError(
                "Planning invalid_python root_cause_oscillation_no_progress"
            ),
            **_failure_kwargs(link),
        )

    db_session.refresh(session)
    db_session.refresh(execution)
    authority = derive_lifecycle_authority(
        db_session, session, task_id=task.id, latest_task_execution=execution
    )
    assert session.status == "retry_pending"
    assert execution.status == TaskStatus.PENDING
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert execution.worker_pid is None
    assert execution.worker_hostname is None
    assert execution.worker_process_start_identity is None


def test_er2_recovery_transition_rejection_is_branch_ending(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)
    schedule_called = False

    def reject_recovery(*_args, **_kwargs):
        from app.services.orchestration.lifecycle.transitions import (
            LifecycleTransitionError,
        )

        raise LifecycleTransitionError("injected_recovery_admission_failure")

    def observe_schedule(*_args, **_kwargs):
        nonlocal schedule_called
        schedule_called = True
        raise AssertionError("retry scheduling must not follow rejected recovery")

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator.enter_recovering",
        reject_recovery,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator.schedule_continuation",
        observe_schedule,
    )

    with pytest.raises(RuntimeError, match="retryable failure"):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("retryable failure"),
            **_failure_kwargs(link),
        )

    db_session.refresh(session)
    db_session.refresh(execution)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert schedule_called is False
    assert session.status == "failed"
    assert execution.status == TaskStatus.FAILED
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False


def test_er2_pre_marker_schedule_failure_rolls_back_pending_and_compensates(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    def reject_schedule(*_args, **_kwargs):
        from app.services.orchestration.lifecycle.transitions import (
            LifecycleTransitionError,
        )

        raise LifecycleTransitionError("injected_pre_marker_failure")

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator.schedule_continuation",
        reject_schedule,
    )

    with pytest.raises(Exception, match="injected_pre_marker_failure"):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("retryable Planning failure"),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "failed"
    assert execution.status == TaskStatus.FAILED
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False
    assert execution.worker_pid is None


@pytest.mark.parametrize(
    "transition",
    [
        mark_task_attempt_pending,
        mark_task_attempt_failed,
        mark_task_attempt_cancelled,
        mark_task_attempt_done,
    ],
)
def test_er2_non_running_attempt_state_releases_runtime_identity(
    db_session, tmp_path, transition
):
    _project, _session, task, link, execution = _seed(db_session, tmp_path)
    transition(task=task, session_task_link=link, task_execution=execution)
    assert execution.status != TaskStatus.RUNNING
    assert execution.worker_pid is None
    assert execution.worker_hostname is None
    assert execution.worker_process_start_identity is None
    assert execution.heartbeat_at is None
