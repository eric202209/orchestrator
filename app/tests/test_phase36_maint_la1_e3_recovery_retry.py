"""Provider-free E3 recovery/retry writer integration tests."""

from __future__ import annotations

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
from app.services.orchestration.lifecycle.transitions import (
    ContinuationIdentity,
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    finalize_logical_success,
    revoke_autonomous_continuation,
    schedule_continuation,
)
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.run_state import mark_task_attempt_pending
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.session.session_runtime_service import queue_task_for_session
from app.tasks.worker_support.dispatch import (
    _claim_continuation_for_worker,
    _claim_queued_task_for_worker,
)
from app.services.orchestration.types import OrchestrationRunContext


class _RetrySignal(Exception):
    pass


class _RetryTask:
    max_retries = 3

    def __init__(self, retries: int = 0):
        self.request = SimpleNamespace(retries=retries, kwargs={})
        self.retry_kwargs = None

    def retry(self, exc, **kwargs):
        self.retry_kwargs = kwargs
        raise _RetrySignal(str(exc))


def _seed(
    db,
    tmp_path: Path,
    *,
    execution_mode: str = "manual",
    session_status: str = "running",
    task_status: TaskStatus = TaskStatus.RUNNING,
    execution_status: TaskStatus = TaskStatus.RUNNING,
    plan_position: int | None = None,
):
    project = Project(
        name="E3 Project",
        workspace_path=str(tmp_path / "project-workspace"),
    )
    session = SessionModel(
        project=project,
        name="E3 Session",
        status=session_status,
        execution_mode=execution_mode,
        is_active=session_status in {"running", "recovering", "retry_pending"},
        instance_id="e3-instance-1",
    )
    task = Task(
        project=project,
        title="E3 Task",
        description="Exercise the ordinary recovery writer",
        status=task_status,
        task_subfolder="task-e3",
        plan_position=plan_position,
        workspace_status="isolated",
    )
    link = SessionTask(session=session, task=task, status=task_status)
    execution = TaskExecution(
        session=session,
        task=task,
        attempt_number=1,
        status=execution_status,
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    db.refresh(session)
    db.refresh(task)
    db.refresh(link)
    db.refresh(execution)
    return project, session, task, link, execution


def _ctx(db, project, session, task, link, execution, *, should_retry):
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
        logger=__import__("logging").getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=SimpleNamespace(should_retry=lambda _exc, _scope: should_retry),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )


def _noop(*_args, **_kwargs):
    return None


def _prepare_clean_retry(monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._prepare_retry_workspace",
        lambda **_kwargs: (True, {}, False),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: False,
    )


def test_e3_t1_t2_failure_enters_recovering_before_reflection(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=True)
    _prepare_clean_retry(monkeypatch)
    observed = {}

    def held_reflection(*_args, **_kwargs):
        db_session.refresh(session)
        observed["authority"] = derive_lifecycle_authority(
            db_session, session, task_id=task.id
        )
        return SimpleNamespace(strategy="continue")

    monkeypatch.setattr(
        "app.services.orchestration.recovery.recovery_strategy_registry.RecoveryStrategyRegistry.route",
        held_reflection,
    )

    with pytest.raises(_RetrySignal):
        handle_task_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("transient failure"),
            get_latest_session_task_link_fn=lambda *_a, **_k: link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=_noop,
            record_live_log_fn=_noop,
        )

    during = observed["authority"]
    assert during.attempt_status == "failed"
    assert during.logical_terminal is False
    assert during.continuation_pending is True
    assert during.quiescent is False

    db_session.refresh(session)
    after_schedule = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "retry_pending"
    assert after_schedule.logical_terminal is False
    assert after_schedule.continuation_pending is True
    assert after_schedule.quiescent is False
    assert execution.status == TaskStatus.PENDING


def test_e3_t2_checkpoint_records_attempt_failure_without_aborted(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    state = OrchestrationState(
        session_id=str(session.id),
        task_description=task.description or "",
        project_name=project.name,
        task_id=task.id,
        _project_dir_override=str(tmp_path),
    )
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=True)
    object.__setattr__(ctx, "orchestration_state", state)
    _prepare_clean_retry(monkeypatch)
    checkpoint_observation = {}

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence.save_orchestration_checkpoint",
        lambda *_args, **_kwargs: checkpoint_observation.update(
            status=state.status,
            abort_reason=state.abort_reason,
            session_status=session.status,
        ),
    )

    with pytest.raises(_RetrySignal):
        handle_task_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("checkpointed retryable failure"),
            get_latest_session_task_link_fn=lambda *_a, **_k: link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=lambda *_a, **_k: checkpoint_observation.update(
                status=state.status,
                abort_reason=state.abort_reason,
                session_status=session.status,
            ),
            record_live_log_fn=_noop,
        )

    assert checkpoint_observation["status"] != "aborted"
    assert checkpoint_observation["abort_reason"] is None
    assert checkpoint_observation["session_status"] == "recovering"


def test_e3_t3_t5_retry_pending_keeps_generation_and_binds_attempt(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    original_instance = session.instance_id
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=True)
    _prepare_clean_retry(monkeypatch)
    retry_task = _RetryTask()

    with pytest.raises(_RetrySignal):
        handle_task_failure(
            self_task=retry_task,
            ctx=ctx,
            exc=RuntimeError("retryable"),
            get_latest_session_task_link_fn=lambda *_a, **_k: link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=_noop,
            record_live_log_fn=_noop,
        )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(execution)
    assert session.instance_id == original_instance
    assert session.continuation_kind == "celery_retry"
    assert session.continuation_task_id == task.id
    assert session.continuation_retry_count == 1
    assert execution.status == TaskStatus.PENDING
    assert retry_task.retry_kwargs["kwargs"]["task_execution_id"] == execution.id
    assert (
        retry_task.retry_kwargs["kwargs"]["expected_session_instance_id"]
        == original_instance
    )


def _scheduled(db, tmp_path):
    project, session, task, link, execution = _seed(db, tmp_path)
    entered = enter_recovering(
        db,
        session,
        task_execution=execution,
        continuation_kind="celery_retry",
        retry_count=0,
        failure_reason="failed attempt",
        commit=True,
    )
    mark_task_attempt_pending(
        task=task, session_task_link=link, task_execution=execution
    )
    identity = schedule_continuation(
        db,
        session,
        task_execution=execution,
        continuation_task_id=task.id,
        continuation_kind=entered.continuation_kind,
        retry_count=1,
        commit=True,
    )
    return project, session, task, link, execution, identity


def test_e3_t4_t6_valid_claim_and_exhaustion(db_session, tmp_path):
    _project, session, task, _link, execution, identity = _scheduled(
        db_session, tmp_path
    )
    result = _claim_continuation_for_worker(
        db=db_session,
        session_id=identity.session_id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    )
    assert result.accepted is True
    db_session.refresh(session)
    db_session.refresh(execution)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "running"
    assert execution.status == TaskStatus.RUNNING
    assert authority.logical_terminal is False
    assert authority.continuation_pending is False
    duplicate = _claim_continuation_for_worker(
        db=db_session,
        session_id=identity.session_id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    )
    assert duplicate.accepted is False

    # Exhaustion is a separate stable finalization and fences the running generation.
    finalize_logical_failure(
        db_session,
        session,
        task_execution=execution,
        failure_reason="retry budget exhausted",
        commit=True,
    )
    db_session.refresh(session)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False
    assert session.instance_id != identity.instance_id


def test_e3_t7_unrecoverable_failure_finalizes_without_continuation(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=False)
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: False,
    )

    with pytest.raises(RuntimeError):
        handle_task_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("unrecoverable"),
            get_latest_session_task_link_fn=lambda *_a, **_k: link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=_noop,
            record_live_log_fn=_noop,
        )

    db_session.refresh(session)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "failed"
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False


def test_e3_t8_operator_pause_revokes_autonomous_generation(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    old_instance = session.instance_id
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=True)
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: True,
    )

    with pytest.raises(RuntimeError):
        handle_task_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("known failure requires operator"),
            get_latest_session_task_link_fn=lambda *_a, **_k: link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=_noop,
            record_live_log_fn=_noop,
        )

    db_session.refresh(session)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "paused"
    assert session.instance_id != old_instance
    assert session.continuation_task_id is None
    assert authority.logical_terminal is False
    assert authority.continuation_pending is False
    assert authority.quiescent is True


@pytest.mark.parametrize("outcome", ["pause", "stop", "failure", "completion"])
def test_e3_t9_t12_stale_continuation_is_rejected_after_terminal_or_quiescent_outcome(
    db_session, tmp_path, outcome
):
    _project, session, task, _link, execution, identity = _scheduled(
        db_session, tmp_path
    )
    if outcome == "pause":
        revoke_autonomous_continuation(
            db_session, session, resulting_status="paused", commit=True
        )
    elif outcome == "stop":
        revoke_autonomous_continuation(
            db_session, session, resulting_status="stopped", commit=True
        )
    elif outcome == "failure":
        finalize_logical_failure(
            db_session, session, task_execution=execution, commit=True
        )
    else:
        finalize_logical_success(
            db_session, session, task_execution=execution, commit=True
        )

    result = claim_continuation(db_session, identity, commit=True)
    assert result.accepted is False
    db_session.refresh(session)
    assert session.status in {"paused", "stopped", "failed", "completed"}


def test_e3_t13_retry_pending_is_committed_before_broker_and_survives_failure(
    db_session, db_session_factory, tmp_path, monkeypatch
):
    _project, session, task, _link, execution = _seed(db_session, tmp_path)
    enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="celery_retry",
        retry_count=0,
        failure_reason="failed attempt",
        commit=True,
    )
    observed = {}

    class _Broker:
        @staticmethod
        def delay(**kwargs):
            broker_db = db_session_factory()
            try:
                broker_session = (
                    broker_db.query(SessionModel).filter_by(id=session.id).one()
                )
                broker_execution = (
                    broker_db.query(TaskExecution)
                    .filter_by(id=kwargs["task_execution_id"])
                    .one()
                )
                observed.update(
                    status=broker_session.status,
                    pending=broker_session.continuation_task_id is not None,
                    execution=broker_execution.status,
                )
            finally:
                broker_db.close()
            raise RuntimeError("broker publication failed")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)
    with pytest.raises(RuntimeError, match="broker publication failed"):
        queue_task_for_session(
            db_session,
            session,
            task.id,
            continuation_kind="celery_retry",
            continuation_retry_count=1,
        )

    assert observed == {
        "status": "retry_pending",
        "pending": True,
        "execution": TaskStatus.PENDING,
    }
    db_session.refresh(session)
    assert session.status == "retry_pending"
    assert session.continuation_task_id == task.id


def test_e3_failure_during_strict_claim_leaves_retry_pending_nonterminal(
    db_session, tmp_path, monkeypatch
):
    _project, session, task, _link, _execution, identity = _scheduled(
        db_session, tmp_path
    )

    def fail_claim(*_args, **_kwargs):
        raise RuntimeError("claim storage failure")

    monkeypatch.setattr(
        "app.tasks.worker_support.dispatch.claim_continuation", fail_claim
    )
    with pytest.raises(RuntimeError, match="claim storage failure"):
        _claim_continuation_for_worker(
            db=db_session,
            session_id=identity.session_id,
            task_id=task.id,
            instance_id=identity.instance_id,
            continuation_task_id=identity.continuation_task_id,
            continuation_kind=identity.continuation_kind,
            task_execution_id=identity.task_execution_id,
            retry_count=identity.retry_count,
        )

    db_session.refresh(session)
    authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
    assert session.status == "retry_pending"
    assert authority.logical_terminal is False
    assert authority.continuation_pending is True


def test_e3_t14_automatic_recovery_uses_fresh_strict_execution(
    db_session, db_session_factory, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(
        db_session,
        tmp_path,
        execution_mode="automatic",
        plan_position=1,
    )
    observed = {}

    class _Broker:
        @staticmethod
        def delay(**kwargs):
            broker_db = db_session_factory()
            try:
                broker_session = (
                    broker_db.query(SessionModel).filter_by(id=session.id).one()
                )
                broker_execution = (
                    broker_db.query(TaskExecution)
                    .filter_by(id=kwargs["task_execution_id"])
                    .one()
                )
                observed.update(
                    status=broker_session.status,
                    kind=broker_session.continuation_kind,
                    execution=broker_execution.status,
                    execution_id=kwargs["task_execution_id"],
                )
            finally:
                broker_db.close()
            return SimpleNamespace(id="auto-recovery-celery-id")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: False,
    )
    ctx = _ctx(db_session, project, session, task, link, execution, should_retry=False)

    handle_task_failure(
        self_task=_RetryTask(),
        ctx=ctx,
        exc=RuntimeError("ordered task needs recovery"),
        get_latest_session_task_link_fn=lambda *_a, **_k: link,
        queue_task_for_session_fn=queue_task_for_session,
        write_project_state_snapshot_fn=_noop,
        save_orchestration_checkpoint_fn=_noop,
        record_live_log_fn=_noop,
    )

    db_session.refresh(session)
    executions = db_session.query(TaskExecution).filter_by(session_id=session.id).all()
    new_execution = max(executions, key=lambda row: row.id)
    assert observed["status"] == "retry_pending"
    assert observed["kind"] == "automatic_recovery"
    assert observed["execution"] == TaskStatus.PENDING
    assert observed["execution_id"] == new_execution.id
    assert new_execution.id != execution.id
    assert execution.status == TaskStatus.FAILED
    assert session.status == "retry_pending"


def test_e3_t15_legacy_dispatch_remains_compatible_but_cannot_claim_marker(
    db_session, tmp_path
):
    _project, session, task, link, _execution = _seed(
        db_session,
        tmp_path,
        session_status="pending",
        task_status=TaskStatus.PENDING,
        execution_status=TaskStatus.FAILED,
    )
    ok, reason, _started, _link = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=None,
    )
    assert ok is True
    assert reason == "claimed"

    # Metadata is an opt-in strict route; a fabricated identity cannot use the
    # legacy helper to bypass the continuation fence.
    result = _claim_continuation_for_worker(
        db=db_session,
        session_id=session.id,
        instance_id=session.instance_id,
        continuation_task_id=task.id,
        continuation_kind="celery_retry",
        task_execution_id=999999,
        retry_count=1,
    )
    assert result.accepted is False
