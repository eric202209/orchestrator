"""Provider-free E4 backend-capacity continuation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import derive_lifecycle_authority
from app.services.orchestration.lifecycle.transitions import (
    LifecycleTransitionError,
    claim_continuation,
    finalize_logical_failure,
    finalize_logical_success,
    revoke_autonomous_continuation,
    schedule_continuation,
    validate_continuation,
)
from app.tasks.worker_support.dispatch import _claim_continuation_for_worker


def _seed_capacity_graph(
    db,
    tmp_path,
    *,
    session_status: str = "pending",
    execution_status: TaskStatus = TaskStatus.PENDING,
    label: str = "default",
):
    project = Project(
        name=f"E4 project {id(db)} {label}",
        workspace_path=str(tmp_path / "project-workspace"),
    )
    session = SessionModel(
        project=project,
        name=f"E4 session {id(db)} {label}",
        status=session_status,
        is_active=session_status in {"running", "recovering", "retry_pending"},
        instance_id="e4-generation-1",
    )
    task = Task(
        project=project,
        title="E4 task",
        description="capacity continuation",
        status=TaskStatus.PENDING,
        task_subfolder="task-e4",
        workspace_status="isolated",
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.PENDING)
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


def _schedule_capacity(db, session, task, execution, *, retry_count=1):
    return schedule_continuation(
        db,
        session,
        task_id=task.id,
        task_execution=execution,
        continuation_kind="backend_capacity",
        retry_count=retry_count,
        retry_eta=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def _authority(db, session, task):
    db.refresh(session)
    return derive_lifecycle_authority(db, session, task_id=task.id)


def test_e4_t1_t3_t11_worker_capacity_writer_commits_before_retry_publication(
    db_session, db_session_factory, tmp_path, monkeypatch
):
    """Exercise the production worker branch with a provider-free fake governor."""

    from app.tasks import worker as worker_module

    _project, session, task, _link, _unused_execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    worker_db = db_session_factory()
    observed = {}
    provider_contract_calls = []

    class FakeWorkerTask:
        request = SimpleNamespace(retries=0, kwargs={})

        def retry(self, **kwargs):
            broker_db = db_session_factory()
            try:
                broker_session = broker_db.get(SessionModel, session.id)
                broker_execution = (
                    broker_db.query(TaskExecution)
                    .filter(TaskExecution.session_id == session.id)
                    .order_by(TaskExecution.id.desc())
                    .first()
                )
                observed.update(
                    status=broker_session.status,
                    kind=broker_session.continuation_kind,
                    pending=broker_session.continuation_task_id is not None,
                    execution=broker_execution.status,
                    identity={
                        key: kwargs["kwargs"].get(key)
                        for key in (
                            "expected_session_instance_id",
                            "continuation_task_id",
                            "continuation_kind",
                            "continuation_retry_count",
                            "task_execution_id",
                        )
                    },
                )
            finally:
                broker_db.close()
            raise RuntimeError("fake capacity broker publication failure")

    descriptor = SimpleNamespace(
        name="local_openclaw",
        capabilities=SimpleNamespace(max_parallel_sessions=1),
    )
    configuration = SimpleNamespace(backend_name="local_openclaw")
    monkeypatch.setattr(worker_module, "get_db_session", lambda: worker_db)
    monkeypatch.setattr(
        worker_module,
        "resolve_runtime_configuration",
        lambda *_args, **_kwargs: configuration,
    )
    monkeypatch.setattr(
        worker_module,
        "validate_runtime_provider_contract",
        lambda *_args, **_kwargs: provider_contract_calls.append(True),
    )
    monkeypatch.setattr(
        worker_module,
        "build_runtime_identity_projection",
        lambda *_args, **_kwargs: SimpleNamespace(to_metadata=lambda: {}),
    )
    monkeypatch.setattr(
        worker_module, "_runtime_selection_details", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        worker_module, "_find_queued_event_for_dispatch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        worker_module, "_append_orchestration_event", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        worker_module, "_record_live_log", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        worker_module, "get_backend_descriptor", lambda *_args: descriptor
    )
    monkeypatch.setattr(
        worker_module,
        "_sync_task_execution_from_task_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(worker_module, "flush_langfuse", lambda: None)
    monkeypatch.setattr(
        "app.services.agents.backend_concurrency.make_redis_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "app.services.agents.backend_concurrency.acquire_backend_slot",
        lambda *_args, **_kwargs: False,
    )

    task_fn = worker_module.execute_orchestration_task.__wrapped__.__func__
    result = task_fn(
        FakeWorkerTask(),
        session.id,
        task.id,
        task.description,
    )

    assert result == {
        "status": "ignored",
        "reason": "backend_capacity_publication_failed",
    }
    assert observed["status"] == "retry_pending"
    assert observed["kind"] == "backend_capacity"
    assert observed["pending"] is True
    assert observed["execution"] == TaskStatus.PENDING
    assert observed["identity"]["continuation_kind"] == "backend_capacity"
    assert observed["identity"]["continuation_retry_count"] == 1
    assert observed["identity"]["continuation_task_id"] == task.id
    assert observed["identity"]["task_execution_id"] is not None
    assert len(provider_contract_calls) == 2
    db_session.expire_all()
    persisted_session = db_session.get(SessionModel, session.id)
    persisted_task = db_session.get(Task, task.id)
    assert persisted_session.status == "retry_pending"
    assert persisted_session.continuation_kind == "backend_capacity"
    assert persisted_session.continuation_retry_count == 1
    publication_log = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.message
            == "E4 backend capacity publication failed after retry_pending commit",
        )
        .first()
    )
    assert publication_log is not None
    assert (
        _authority(db_session, persisted_session, persisted_task).logical_terminal
        is False
    )


def test_e4_t1_t2_t5_capacity_wait_is_retry_pending_nonterminal(db_session, tmp_path):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    original_instance = session.instance_id

    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()
    authority = _authority(db_session, session, task)

    assert execution.status == TaskStatus.PENDING
    assert session.status == "retry_pending"
    assert session.continuation_kind == "backend_capacity"
    assert session.continuation_retry_count == 1
    assert session.continuation_retry_eta is not None
    assert identity.instance_id == original_instance
    assert authority.current_phase == "retry_pending"
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert authority.quiescent is False


def test_e4_decisive_contention_shape_releases_capacity_then_claims(
    db_session, tmp_path
):
    """Session B waits while the provider-free governor says A owns the slot."""

    _project_a, session_a, _task_a, _link_a, _execution_a = _seed_capacity_graph(
        db_session, tmp_path, label="a"
    )
    _project_b, session_b, task_b, _link_b, execution_b = _seed_capacity_graph(
        db_session, tmp_path, label="b"
    )
    held_sessions = {session_a.id}

    def fake_capacity_available(_session_id):
        return not held_sessions

    assert fake_capacity_available(session_a.id) is False
    assert fake_capacity_available(session_b.id) is False
    # A owns the only slot, so B's capacity result is the retry writer input.
    assert session_a.id in held_sessions
    identity = _schedule_capacity(db_session, session_b, task_b, execution_b)
    db_session.commit()
    waiting = _authority(db_session, session_b, task_b)
    assert waiting.continuation_kind == "backend_capacity"
    assert waiting.logical_terminal is False
    assert waiting.continuation_pending is True
    assert waiting.quiescent is False

    held_sessions.remove(session_a.id)
    assert fake_capacity_available(session_b.id) is True
    claim = _claim_continuation_for_worker(
        db=db_session,
        session_id=identity.session_id,
        task_id=task_b.id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    )
    assert claim.accepted is True
    assert session_b.instance_id == identity.instance_id
    assert _authority(db_session, session_b, task_b).logical_terminal is False


def test_e4_t3_transaction_is_committed_before_fake_broker(
    db_session, db_session_factory, tmp_path
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    observed = {}

    class FakeBroker:
        def publish(self, **_kwargs):
            broker_db = db_session_factory()
            try:
                broker_session = broker_db.get(SessionModel, session.id)
                broker_execution = broker_db.get(TaskExecution, execution.id)
                observed.update(
                    status=broker_session.status,
                    kind=broker_session.continuation_kind,
                    count=broker_session.continuation_retry_count,
                    execution=broker_execution.status,
                )
            finally:
                broker_db.close()
            raise RuntimeError("capacity broker unavailable")

    with pytest.raises(RuntimeError, match="capacity broker unavailable"):
        FakeBroker().publish(
            session_id=identity.session_id,
            continuation_kind=identity.continuation_kind,
        )

    assert observed == {
        "status": "retry_pending",
        "kind": "backend_capacity",
        "count": 1,
        "execution": TaskStatus.PENDING,
    }
    assert _authority(db_session, session, task).logical_terminal is False


def test_e4_t4_t5_t15_valid_capacity_delivery_uses_strict_claim(db_session, tmp_path):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    preflight = validate_continuation(db_session, identity)
    assert preflight.accepted is True
    claim = _claim_continuation_for_worker(
        db=db_session,
        session_id=identity.session_id,
        task_id=task.id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    )

    assert claim.accepted is True
    db_session.refresh(session)
    db_session.refresh(execution)
    authority = _authority(db_session, session, task)
    assert session.instance_id == identity.instance_id
    assert session.status == "running"
    assert execution.status == TaskStatus.RUNNING
    assert authority.continuation_pending is False
    assert authority.logical_terminal is False


def test_e4_t6_duplicate_capacity_delivery_has_one_winner(
    db_session, db_session_factory, tmp_path
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    first_db = db_session_factory()
    second_db = db_session_factory()
    try:
        first = _claim_continuation_for_worker(
            db=first_db,
            session_id=identity.session_id,
            task_id=task.id,
            instance_id=identity.instance_id,
            continuation_task_id=identity.continuation_task_id,
            continuation_kind=identity.continuation_kind,
            task_execution_id=identity.task_execution_id,
            retry_count=identity.retry_count,
        )
        second = _claim_continuation_for_worker(
            db=second_db,
            session_id=identity.session_id,
            task_id=task.id,
            instance_id=identity.instance_id,
            continuation_task_id=identity.continuation_task_id,
            continuation_kind=identity.continuation_kind,
            task_execution_id=identity.task_execution_id,
            retry_count=identity.retry_count,
        )
        assert [first.accepted, second.accepted].count(True) == 1
        assert second.accepted is False
        assert second.reason in {
            "continuation_attempt_not_pending",
            "stale_or_duplicate_continuation",
        }
    finally:
        first_db.close()
        second_db.close()


@pytest.mark.parametrize("resulting_status", ["paused", "stopped", "cancelled"])
def test_e4_t7_t8_stale_delivery_after_operator_outcome_is_rejected(
    db_session, tmp_path, resulting_status
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()
    old_instance = session.instance_id

    revoke_autonomous_continuation(
        db_session, session, resulting_status=resulting_status, commit=True
    )
    result = claim_continuation(db_session, identity, commit=True)
    authority = _authority(db_session, session, task)

    assert result.accepted is False
    assert session.instance_id != old_instance
    assert session.status == resulting_status
    assert authority.logical_terminal is (resulting_status != "paused")
    assert authority.quiescent is True


@pytest.mark.parametrize("terminalizer", ["failure", "success"])
def test_e4_t9_stale_delivery_after_completion_or_failure_is_rejected(
    db_session, tmp_path, terminalizer
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    if terminalizer == "failure":
        finalize_logical_failure(
            db_session,
            session,
            task_execution=execution,
            failure_reason="capacity retry exhausted",
            commit=True,
        )
    else:
        finalize_logical_success(
            db_session, session, task_execution=execution, commit=True
        )
    result = claim_continuation(db_session, identity, commit=True)
    authority = _authority(db_session, session, task)

    assert result.accepted is False
    assert session.status in {"failed", "completed"}
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False
    assert execution.status in {
        TaskStatus.FAILED,
        TaskStatus.DONE,
        TaskStatus.CANCELLED,
    }


def test_e4_t10_acquired_slot_is_released_when_pause_wins_claim_race(
    db_session, db_session_factory, tmp_path, monkeypatch
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()
    delivery_db = db_session_factory()
    released = []
    try:
        assert validate_continuation(delivery_db, identity).accepted is True
        fake_slot = object()
        operator_db = db_session_factory()
        try:
            revoke_autonomous_continuation(
                operator_db,
                operator_db.get(SessionModel, session.id),
                resulting_status="paused",
                commit=True,
            )
        finally:
            operator_db.close()
        result = claim_continuation(delivery_db, identity, commit=True)
        monkeypatch.setattr(
            "app.tasks.worker._release_backend_slot_safely",
            lambda *_args, **_kwargs: released.append(fake_slot),
        )
        if not result.accepted:
            from app.tasks.worker import _release_backend_slot_safely

            _release_backend_slot_safely(
                fake_slot,
                "local_openclaw",
                session_id=session.id,
                task_execution_id=execution.id,
            )
        assert result.accepted is False
        assert released == [fake_slot]
        assert delivery_db.get(SessionModel, session.id).status == "paused"
    finally:
        delivery_db.close()


def test_e4_claim_failure_retains_backend_capacity_continuation(
    db_session, tmp_path, monkeypatch
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    def fail_claim(*_args, **_kwargs):
        raise RuntimeError("strict claim storage failure")

    monkeypatch.setattr(
        "app.tasks.worker_support.dispatch.claim_continuation", fail_claim
    )
    with pytest.raises(RuntimeError, match="strict claim storage failure"):
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
    authority = _authority(db_session, session, task)
    assert session.status == "retry_pending"
    assert authority.logical_terminal is False
    assert authority.continuation_pending is True


def test_e4_t12_exhaustion_finalizes_and_fences_delayed_delivery(db_session, tmp_path):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    identity = _schedule_capacity(db_session, session, task, execution, retry_count=60)
    db_session.commit()
    old_instance = identity.instance_id

    finalize_logical_failure(
        db_session,
        session,
        task_execution=execution,
        failure_reason="backend capacity retry budget exhausted",
        commit=True,
    )
    delayed = claim_continuation(db_session, identity, commit=True)
    authority = _authority(db_session, session, task)

    assert delayed.accepted is False
    assert session.status == "failed"
    assert session.instance_id != old_instance
    assert session.continuation_task_id is None
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False


@pytest.mark.parametrize("kind", ["celery_retry", "automatic_recovery"])
def test_e4_t13_t14_e3_continuation_kinds_keep_distinct_strict_identity(
    db_session, tmp_path, kind
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path, session_status="recovering"
    )
    identity = schedule_continuation(
        db_session,
        session,
        task_id=task.id,
        task_execution=execution,
        continuation_kind=kind,
        retry_count=2,
    )
    db_session.commit()
    assert identity.continuation_kind == kind
    assert _claim_continuation_for_worker(
        db=db_session,
        session_id=identity.session_id,
        task_id=task.id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind=identity.continuation_kind,
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    ).accepted


def test_e4_t15_legacy_dispatch_cannot_claim_retry_pending_capacity_marker(
    db_session, tmp_path
):
    _project, session, task, link, execution = _seed_capacity_graph(
        db_session, tmp_path
    )
    _schedule_capacity(db_session, session, task, execution)
    db_session.commit()

    from app.tasks.worker_support.dispatch import _claim_queued_task_for_worker

    claimed, reason, _started, _latest_link = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=link,
        expected_session_instance_id=None,
    )
    assert claimed is False
    assert reason == "session_not_runnable:retry_pending"


def test_e4_capacity_wait_does_not_fabricate_failed_attempt(db_session, tmp_path):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path, execution_status=TaskStatus.PENDING
    )
    _schedule_capacity(db_session, session, task, execution)
    db_session.commit()
    authority = _authority(db_session, session, task)

    assert authority.attempt_status == TaskStatus.PENDING.value
    assert authority.attempt_failure_reason is None
    assert authority.logical_terminal is False


def test_e4_transition_rejects_capacity_wait_with_competing_active_execution(
    db_session, tmp_path
):
    _project, session, task, _link, execution = _seed_capacity_graph(
        db_session, tmp_path, session_status="running"
    )
    competing = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.RUNNING,
    )
    db_session.add(competing)
    db_session.commit()

    with pytest.raises(LifecycleTransitionError) as raised:
        _schedule_capacity(db_session, session, task, execution)
    assert raised.value.reason == "capacity_wait_active_execution"
