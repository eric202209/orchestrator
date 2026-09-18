"""Provider-free BR2 tests for lost continuation delivery reconciliation.

Every case holds one invariant: a durable continuation is the logical intent to
continue and the broker is only its transport.  Losing transport may never lose
the continuation or end the workflow, and reconciliation may only restore
transport under the same fenced durable identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from app.services.orchestration.lifecycle import continuation_recovery as cr
from app.services.orchestration.lifecycle.authority import (
    derive_lifecycle_authority,
)
from app.services.orchestration.lifecycle.transitions import (
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    resolve_continuation_identity,
    revoke_autonomous_continuation,
    schedule_continuation,
)

CONTINUATION_KINDS = ("celery_retry", "automatic_recovery", "backend_capacity")
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _seed(db, tmp_path: Path, *, instance_id: str = "br2-generation-1"):
    workspace = tmp_path / "project-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    project = Project(name="BR2 Project", workspace_path=str(workspace))
    session = SessionModel(
        project=project,
        name="BR2 Session",
        status="running",
        execution_mode="manual",
        is_active=True,
        instance_id=instance_id,
    )
    task = Task(
        project=project,
        title="BR2 Task",
        description="Exercise lost continuation delivery reconciliation",
        status=TaskStatus.RUNNING,
        task_subfolder="task-br2",
        workspace_status="isolated",
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session=session, task=task, attempt_number=1, status=TaskStatus.RUNNING
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    for row in (project, session, task, link, execution):
        db.refresh(row)
    return project, session, task, link, execution


def _strand(db, tmp_path, *, kind: str = "celery_retry", retry_count: int = 1, **kw):
    """Commit a durable continuation whose broker publication then fails."""

    project, session, task, link, execution = _seed(db, tmp_path, **kw)
    enter_recovering(
        db,
        session,
        task_execution=execution,
        continuation_kind=kind,
        retry_count=retry_count - 1,
        failure_reason="attempt failed",
        commit=True,
    )
    identity = schedule_continuation(
        db,
        session,
        continuation_kind=kind,
        retry_count=retry_count,
        retry_eta=T0 + timedelta(seconds=15),
        commit=True,
    )
    db.refresh(session)
    # The publication that should have followed this commit is lost here.
    return project, session, task, link, identity


class _Publisher:
    def __init__(self, task_id: str = "br2-restored-delivery"):
        self.calls: list[dict] = []
        self.task_id = task_id

    def __call__(self, db, identity):
        self.calls.append(
            {
                "session_id": identity.session_id,
                "instance_id": identity.instance_id,
                "continuation_task_id": identity.continuation_task_id,
                "continuation_kind": identity.continuation_kind,
                "task_execution_id": identity.task_execution_id,
                "retry_count": identity.retry_count,
            }
        )
        return self.task_id


def _probe(present, sources=(), error=None):
    return lambda _identity: cr.DeliveryProbeResult(
        present=present, sources=tuple(sources), error=error
    )


def _due(seconds: int = 600) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _reconcile(db, **kwargs):
    kwargs.setdefault("now", _due())
    kwargs.setdefault("delivery_probe", _probe(False))
    kwargs.setdefault("grace_seconds", 180)
    return cr.reconcile_stranded_continuations(db, **kwargs)


def _assert_nonterminal(db, session):
    db.refresh(session)
    authority = derive_lifecycle_authority(db, session)
    assert session.status == "retry_pending"
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert authority.quiescent is False


# --------------------------------------------------------------------------
# Durable identity reconstruction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", CONTINUATION_KINDS)
def test_br2_durable_identity_is_reconstructible_from_session_state(
    db_session, tmp_path, kind
):
    _project, session, task, _link, identity = _strand(
        db_session, tmp_path, kind=kind, retry_count=2
    )
    rebuilt = resolve_continuation_identity(db_session, session)
    assert rebuilt == identity
    assert rebuilt.continuation_task_id == task.id


def test_br2_identity_is_none_without_a_valid_marker(db_session, tmp_path):
    _project, session, _task, _link, _execution = _seed(db_session, tmp_path)
    assert resolve_continuation_identity(db_session, session) is None


# --------------------------------------------------------------------------
# ETA / grace policy
# --------------------------------------------------------------------------


def test_br2_continuation_is_not_due_before_its_eta_plus_grace(db_session, tmp_path):
    _project, session, _task, _link, identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    due_at = cr.continuation_due_at(session, grace_seconds=180)
    assert due_at == T0 + timedelta(seconds=15 + 180)

    result = _reconcile(
        db_session,
        now=due_at - timedelta(seconds=1),
        publish=publisher,
    )
    assert result["counts"][cr.NOT_DUE] == 1
    assert publisher.calls == []
    _assert_nonterminal(db_session, session)
    assert identity is not None


def test_br2_due_falls_back_to_the_lifecycle_transition_without_an_eta(
    db_session, tmp_path
):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    session.continuation_retry_eta = None
    session.lifecycle_updated_at = T0
    db_session.commit()
    assert cr.continuation_due_at(session, grace_seconds=180) == T0 + timedelta(
        seconds=180
    )


# --------------------------------------------------------------------------
# Section 20 — injected pre-publication failure, end to end
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", CONTINUATION_KINDS)
def test_br2_injected_publication_failure_is_reconciled_and_claimed(
    db_session, tmp_path, monkeypatch, kind
):
    _project, session, task, _link, identity = _strand(
        db_session, tmp_path, kind=kind, retry_count=2
    )

    class _BrokenBroker:
        @staticmethod
        def apply_async(**_kwargs):
            raise RuntimeError("broker publication failed")

    # The durable marker survives the lost publication.
    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _BrokenBroker)
    _assert_nonterminal(db_session, session)

    published: list[dict] = []

    class _Broker:
        @staticmethod
        def apply_async(*, kwargs, **options):
            published.append({"kwargs": kwargs, "options": options})
            return SimpleNamespace(id="br2-real-delivery")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)

    result = _reconcile(db_session)
    assert result["counts"][cr.REPUBLISHED] == 1
    assert result["republished_session_ids"] == [session.id]

    # The republished delivery carries the exact durable identity.
    assert len(published) == 1
    kwargs = published[0]["kwargs"]
    assert kwargs["session_id"] == identity.session_id
    assert kwargs["task_id"] == identity.continuation_task_id
    assert kwargs["expected_session_instance_id"] == identity.instance_id
    assert kwargs["task_execution_id"] == identity.task_execution_id
    assert kwargs["continuation_task_id"] == identity.continuation_task_id
    assert kwargs["continuation_kind"] == kind
    assert kwargs["continuation_retry_count"] == identity.retry_count
    assert kwargs["prompt"]

    # The Session is untouched by reconciliation itself.
    _assert_nonterminal(db_session, session)

    # The ordinary strict claim still owns the transition.
    claimed = claim_continuation(db_session, identity, commit=True)
    assert claimed.accepted is True
    db_session.refresh(session)
    assert session.status == "running"
    assert derive_lifecycle_authority(db_session, session).continuation_pending is False
    assert (
        db_session.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .count()
        == 1
    )
    assert task is not None


# --------------------------------------------------------------------------
# Section 21 — publication reported success but no claim ever happened
# --------------------------------------------------------------------------


def test_br2_apparent_publication_success_without_claim_is_reconciled(
    db_session, tmp_path
):
    _project, session, _task, _link, identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    # Publication returned normally, yet no worker ever claimed and the broker
    # no longer holds a matching delivery.
    result = _reconcile(db_session, publish=publisher, delivery_probe=_probe(False))

    assert result["counts"][cr.REPUBLISHED] == 1
    assert publisher.calls[0]["task_execution_id"] == identity.task_execution_id
    _assert_nonterminal(db_session, session)


# --------------------------------------------------------------------------
# Section 22 — delivery present
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ("active", "reserved", "scheduled"))
def test_br2_matching_delivery_present_is_never_republished(
    db_session, tmp_path, source
):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    result = _reconcile(
        db_session,
        publish=publisher,
        delivery_probe=_probe(True, sources=(source,)),
    )
    assert result["counts"][cr.DELIVERY_PRESENT] == 1
    assert result["decisions"][0]["delivery_sources"] == [source]
    assert publisher.calls == []
    _assert_nonterminal(db_session, session)


@pytest.mark.parametrize("source", ("active", "reserved", "scheduled"))
def test_br2_inspector_matches_the_exact_durable_identity(db_session, tmp_path, source):
    _project, _session, _task, _link, identity = _strand(db_session, tmp_path)
    request = {
        "name": cr.ORCHESTRATION_TASK_NAME,
        "kwargs": {
            "session_id": identity.session_id,
            "task_id": identity.continuation_task_id,
            "task_execution_id": identity.task_execution_id,
            "continuation_kind": identity.continuation_kind,
            "expected_session_instance_id": identity.instance_id,
        },
    }
    payload = {
        "worker@host": [
            {"request": request} if source == "scheduled" else request,
        ]
    }
    inspector = SimpleNamespace(
        **{
            name: (lambda p=payload, n=name: p if n == source else {})
            for name in ("active", "reserved", "scheduled")
        }
    )
    probed = cr.inspect_continuation_delivery(identity, inspector=inspector)
    assert probed.present is True
    assert probed.sources == (source,)


def test_br2_inspector_ignores_a_different_generation(db_session, tmp_path):
    _project, _session, _task, _link, identity = _strand(db_session, tmp_path)
    payload = {
        "worker@host": [
            {
                "name": cr.ORCHESTRATION_TASK_NAME,
                "kwargs": {
                    "session_id": identity.session_id,
                    "task_id": identity.continuation_task_id,
                    "task_execution_id": identity.task_execution_id,
                    "continuation_kind": identity.continuation_kind,
                    "expected_session_instance_id": "some-other-generation",
                },
            }
        ]
    }
    inspector = SimpleNamespace(
        active=lambda: payload, reserved=lambda: {}, scheduled=lambda: {}
    )
    assert cr.inspect_continuation_delivery(identity, inspector=inspector).present is (
        False
    )


# --------------------------------------------------------------------------
# Section 23 — physical inspection unavailable or ambiguous
# --------------------------------------------------------------------------


def test_br2_unreachable_workers_never_cause_publication(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    result = _reconcile(
        db_session,
        publish=publisher,
        delivery_probe=_probe(None, error="no_worker_inspection_response"),
    )
    assert result["counts"][cr.PHYSICAL_STATE_UNCERTAIN] == 1
    assert result["decisions"][0]["probe_error"] == "no_worker_inspection_response"
    assert publisher.calls == []
    _assert_nonterminal(db_session, session)


def test_br2_no_inspection_response_is_uncertain_not_empty(db_session, tmp_path):
    _project, _session, _task, _link, identity = _strand(db_session, tmp_path)
    silent = SimpleNamespace(
        active=lambda: None, reserved=lambda: None, scheduled=lambda: None
    )
    probed = cr.inspect_continuation_delivery(identity, inspector=silent)
    assert probed.present is None
    assert probed.uncertain is True
    assert probed.error == "no_worker_inspection_response"


def test_br2_raising_inspection_is_uncertain(db_session, tmp_path):
    _project, _session, _task, _link, identity = _strand(db_session, tmp_path)

    def _boom():
        raise ConnectionError("redis unreachable")

    broken = SimpleNamespace(active=_boom, reserved=_boom, scheduled=_boom)
    probed = cr.inspect_continuation_delivery(identity, inspector=broken)
    assert probed.present is None
    assert "redis unreachable" in (probed.error or "")


def test_br2_publication_failure_keeps_the_marker(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)

    def _explode(_db, _identity):
        raise RuntimeError("broker still unreachable")

    result = _reconcile(db_session, publish=_explode)
    assert result["counts"][cr.RECONCILIATION_ERROR] == 1
    _assert_nonterminal(db_session, session)


# --------------------------------------------------------------------------
# Section 24 — operator generation fencing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator_action",
    ("pause", "stop", "final_failure"),
)
def test_br2_operator_transitions_prevent_republication(
    db_session, tmp_path, operator_action
):
    _project, session, _task, _link, identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    if operator_action == "pause":
        revoke_autonomous_continuation(
            db_session, session, resulting_status="paused", commit=True
        )
    elif operator_action == "stop":
        revoke_autonomous_continuation(
            db_session, session, resulting_status="stopped", commit=True
        )
    else:
        finalize_logical_failure(
            db_session, session, failure_reason="max_attempts_reached", commit=True
        )
    db_session.refresh(session)

    result = _reconcile(db_session, publish=publisher)
    # The Session is no longer a retry_pending candidate at all.
    assert result["inspected_count"] == 0
    assert publisher.calls == []

    # A delivery from the old generation is still rejected by the strict claim.
    assert claim_continuation(db_session, identity).accepted is False


def test_br2_generation_rotated_mid_scan_is_revalidated_before_publication(
    db_session, tmp_path
):
    """A scan that began before rotation must not publish afterwards."""

    _project, session, _task, _link, identity = _strand(db_session, tmp_path)
    publisher = _Publisher()
    rotated: list[str] = []

    def _rotate_during_probe(_identity):
        # The operator pauses between eligibility and publication.
        revoke_autonomous_continuation(
            db_session, session, resulting_status="paused", commit=True
        )
        rotated.append(session.instance_id)
        return cr.DeliveryProbeResult(present=False)

    decision = cr.reconcile_session_continuation(
        db_session,
        session,
        now=_due(),
        grace_seconds=180,
        delivery_probe=_rotate_during_probe,
        publish=publisher,
    )

    assert rotated and rotated[0] != identity.instance_id
    assert decision.outcome == cr.STALE_GENERATION
    assert decision.details["revalidated"] is True
    assert publisher.calls == []

    # And the stale prior-generation delivery is rejected by the strict claim.
    assert claim_continuation(db_session, identity).accepted is False


# --------------------------------------------------------------------------
# Section 25 — duplicate reconciliation
# --------------------------------------------------------------------------


def test_br2_exactly_one_reconciler_republishes_the_same_identity(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    first = _reconcile(db_session, publish=publisher)
    second = _reconcile(db_session, publish=publisher)

    assert first["counts"][cr.REPUBLISHED] == 1
    assert second["counts"][cr.REPUBLISHED] == 0
    assert second["counts"][cr.DELIVERY_PRESENT] == 1
    assert second["decisions"][0]["reason"] == "republication_already_recorded"
    assert len(publisher.calls) == 1
    _assert_nonterminal(db_session, session)


def test_br2_duplicate_transport_still_yields_one_logical_execution(
    db_session, tmp_path
):
    """Even if transport were duplicated, only one strict claim can win."""

    _project, session, _task, _link, identity = _strand(db_session, tmp_path)

    first = claim_continuation(db_session, identity, commit=True)
    second = claim_continuation(db_session, identity, commit=True)

    assert [first.accepted, second.accepted].count(True) == 1
    db_session.refresh(session)
    assert session.status == "running"
    assert (
        db_session.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .count()
        == 1
    )


def test_br2_suppression_window_expiry_allows_a_later_republish(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    publisher = _Publisher()

    _reconcile(db_session, publish=publisher, suppression_seconds=900)
    later = _reconcile(
        db_session,
        now=_due() + timedelta(seconds=1200),
        publish=publisher,
        suppression_seconds=900,
    )
    assert later["counts"][cr.REPUBLISHED] == 1
    assert len(publisher.calls) == 2
    _assert_nonterminal(db_session, session)


# --------------------------------------------------------------------------
# Section 26 — capacity retry budget / Section 10-11 retry accounting
# --------------------------------------------------------------------------


@pytest.mark.parametrize("retry_count", (1, 7, 59))
def test_br2_capacity_republish_preserves_the_celery_retry_counter(
    db_session, tmp_path, monkeypatch, retry_count
):
    _project, session, _task, _link, identity = _strand(
        db_session, tmp_path, kind="backend_capacity", retry_count=retry_count
    )
    published: list[dict] = []

    class _Broker:
        @staticmethod
        def apply_async(*, kwargs, **options):
            published.append({"kwargs": kwargs, "options": options})
            return SimpleNamespace(id="br2-capacity-delivery")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)

    result = _reconcile(db_session)
    assert result["counts"][cr.REPUBLISHED] == 1

    # The worker rejects any capacity delivery whose Celery retry counter
    # disagrees with the durable marker, so both must stay at N.
    assert published[0]["options"]["retries"] == retry_count
    assert published[0]["kwargs"]["continuation_retry_count"] == retry_count

    # The logical marker is untouched: no budget reset, no extra attempt.
    db_session.refresh(session)
    assert session.continuation_retry_count == retry_count
    assert session.continuation_kind == "backend_capacity"
    assert identity.retry_count == retry_count


def test_br2_capacity_budget_exhaustion_bound_is_unchanged(db_session, tmp_path):
    from app.services.orchestration.lifecycle.worker_capacity import (
        BACKEND_CAPACITY_RETRY_MAX_RETRIES,
        backend_capacity_retry_state,
    )

    _project, _session, _task, _link, identity = _strand(
        db_session,
        tmp_path,
        kind="backend_capacity",
        retry_count=BACKEND_CAPACITY_RETRY_MAX_RETRIES - 1,
    )
    delivered = cr._celery_delivery_retries(identity)
    count, exhausted = backend_capacity_retry_state(
        SimpleNamespace(retries=delivered), BACKEND_CAPACITY_RETRY_MAX_RETRIES
    )
    assert count == BACKEND_CAPACITY_RETRY_MAX_RETRIES - 1
    assert exhausted is False

    _, next_exhausted = backend_capacity_retry_state(
        SimpleNamespace(retries=delivered + 1), BACKEND_CAPACITY_RETRY_MAX_RETRIES
    )
    assert next_exhausted is True


def test_br2_ordinary_retry_republish_preserves_its_logical_budget(
    db_session, tmp_path, monkeypatch
):
    _project, _session, _task, _link, _identity = _strand(
        db_session, tmp_path, kind="celery_retry", retry_count=3
    )
    published: list[dict] = []

    class _Broker:
        @staticmethod
        def apply_async(*, kwargs, **options):
            published.append({"kwargs": kwargs, "options": options})
            return SimpleNamespace(id="br2-retry-delivery")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)
    _reconcile(db_session)
    # celery_retry derives its logical retry count from request.retries, so the
    # restored delivery must not reset that budget to zero.
    assert published[0]["options"]["retries"] == 3


def test_br2_automatic_recovery_republish_matches_its_original_dispatch(
    db_session, tmp_path, monkeypatch
):
    _project, _session, _task, _link, _identity = _strand(
        db_session, tmp_path, kind="automatic_recovery", retry_count=2
    )
    published: list[dict] = []

    class _Broker:
        @staticmethod
        def apply_async(*, kwargs, **options):
            published.append({"kwargs": kwargs, "options": options})
            return SimpleNamespace(id="br2-recovery-delivery")

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Broker)
    _reconcile(db_session)
    # Automatic recovery was originally published by a fresh delay(), whose
    # Celery counter is zero; the logical retry count still travels in kwargs.
    assert published[0]["options"]["retries"] == 0
    assert published[0]["kwargs"]["continuation_retry_count"] == 2


# --------------------------------------------------------------------------
# Section 27 — automatic recovery identity
# --------------------------------------------------------------------------


def test_br2_reconciliation_creates_no_second_task_execution(db_session, tmp_path):
    _project, session, _task, _link, identity = _strand(
        db_session, tmp_path, kind="automatic_recovery", retry_count=1
    )
    before = db_session.query(TaskExecution).count()
    publisher = _Publisher()

    _reconcile(db_session, publish=publisher)

    assert db_session.query(TaskExecution).count() == before
    assert publisher.calls[0]["task_execution_id"] == identity.task_execution_id
    pending = (
        db_session.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.status == TaskStatus.PENDING,
        )
        .all()
    )
    assert [row.id for row in pending] == [identity.task_execution_id]


# --------------------------------------------------------------------------
# Section 19 — no false terminality, ever
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probe,publisher_factory",
    (
        (_probe(True, sources=("active",)), _Publisher),
        (_probe(None, error="unreachable"), _Publisher),
        (_probe(False), _Publisher),
    ),
)
def test_br2_reconciliation_never_terminalizes_the_session(
    db_session, tmp_path, probe, publisher_factory
):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    _reconcile(db_session, delivery_probe=probe, publish=publisher_factory())
    _assert_nonterminal(db_session, session)


def test_br2_reconciliation_emits_no_failure_events(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    _reconcile(db_session, delivery_probe=_probe(None, error="unreachable"))

    messages = [
        row.message
        for row in db_session.query(LogEntry)
        .filter(LogEntry.session_id == session.id)
        .all()
    ]
    assert not any("TASK_FAILED" in message for message in messages)
    assert any("continuation reconciliation" in message for message in messages)


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probe,expected",
    (
        (_probe(False), cr.REPUBLISHED),
        (_probe(True, sources=("reserved",)), cr.DELIVERY_PRESENT),
        (_probe(None, error="unreachable"), cr.PHYSICAL_STATE_UNCERTAIN),
    ),
)
def test_br2_every_outcome_writes_structured_evidence(
    db_session, tmp_path, probe, expected
):
    import json

    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    _reconcile(db_session, delivery_probe=probe, publish=_Publisher())

    rows = (
        db_session.query(LogEntry)
        .filter(LogEntry.session_id == session.id)
        .order_by(LogEntry.id.desc())
        .all()
    )
    payloads = [json.loads(row.log_metadata or "{}") for row in rows]
    reconciliation = [
        payload
        for payload in payloads
        if payload.get("event_type") == cr.RECONCILIATION_EVENT
    ]
    assert reconciliation
    assert reconciliation[0]["outcome"] == expected
    assert reconciliation[0]["session_instance_id"] == session.instance_id


def test_br2_invalid_marker_is_reported_not_repaired(db_session, tmp_path):
    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    # A pending attempt is required to rebuild the identity; remove it.
    db_session.query(TaskExecution).filter(
        TaskExecution.session_id == session.id,
        TaskExecution.status == TaskStatus.PENDING,
    ).delete()
    db_session.commit()

    result = _reconcile(db_session, publish=_Publisher())
    assert result["counts"][cr.INVALID_CONTINUATION] == 1
    _assert_nonterminal(db_session, session)


# --------------------------------------------------------------------------
# Maintenance integration and scheduling
# --------------------------------------------------------------------------


def test_br2_sweep_task_is_registered_and_scheduled():
    from app.celery_app import celery_app
    import app.tasks.maintenance  # noqa: F401
    from app.services.observability.maintenance_observability import (
        CONTINUATION_SWEEP_SCHEDULE_ID,
        CONTINUATION_SWEEP_TASK_NAME,
    )

    assert CONTINUATION_SWEEP_TASK_NAME in celery_app.tasks
    entry = celery_app.conf.beat_schedule[CONTINUATION_SWEEP_SCHEDULE_ID]
    assert entry["task"] == CONTINUATION_SWEEP_TASK_NAME
    assert entry["schedule"] == timedelta(minutes=5)


def test_br2_sweep_reports_counts_and_records_maintenance_evidence(
    db_session, tmp_path, monkeypatch
):
    import json

    from app.tasks import maintenance

    _project, session, _task, _link, _identity = _strand(db_session, tmp_path)
    monkeypatch.setattr(maintenance, "get_db_session", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        cr,
        "inspect_continuation_delivery",
        lambda _identity, **_kw: cr.DeliveryProbeResult(
            present=True, sources=("active",)
        ),
    )

    sweep = maintenance.sweep_stranded_continuation_deliveries
    result = sweep.run(grace_seconds=0)

    assert result["status"] == "completed"
    assert result["inspected_continuation_count"] == 1
    assert result["delivery_present_count"] == 1
    assert result["republished_session_ids"] == []

    maintenance_events = [
        json.loads(row.log_metadata or "{}")
        for row in db_session.query(LogEntry)
        .filter(LogEntry.session_id.is_(None))
        .all()
    ]
    completed = [
        payload
        for payload in maintenance_events
        if payload.get("event_type") == "MAINTENANCE_COMPLETED"
    ]
    assert completed
    assert completed[0]["schedule_identity"] == (
        "reconcile-stranded-continuation-deliveries"
    )
    assert completed[0]["decision_evidence"]
    _assert_nonterminal(db_session, session)


# --------------------------------------------------------------------------
# Section 31 — BR1 non-regression under a stranded continuation
# --------------------------------------------------------------------------


def test_br2_br1_protections_hold_while_a_continuation_is_stranded(
    db_session, tmp_path, monkeypatch
):
    import asyncio

    from fastapi import HTTPException

    from app.services.session.replan_service import trigger_replan
    from app.services.session.session_execution_service import start_session_payload
    from app.services.session.session_runtime_service import queue_task_for_session
    from app.services.workspace.baseline_promotion_service import (
        BaselinePromotionService,
    )
    from scripts.session_and_replay.failure_taxonomy import outcome_class

    project, session, task, _link, _identity = _strand(db_session, tmp_path)
    (Path(project.workspace_path) / task.task_subfolder).mkdir(
        parents=True, exist_ok=True
    )
    task.workspace_status = "blocked"
    db_session.commit()

    # The stranded marker is still a live continuation for every BR1 consumer.
    with pytest.raises(HTTPException) as fresh:
        queue_task_for_session(db=db_session, session=session, task_id=task.id)
    assert fresh.value.status_code == 409

    with pytest.raises(HTTPException) as direct:
        asyncio.run(start_session_payload(db_session, session.id, task_description="x"))
    assert direct.value.status_code == 409

    with pytest.raises(HTTPException) as replan:
        trigger_replan(db_session, session.id)
    assert replan.value.status_code == 409

    report = BaselinePromotionService(db_session).cleanup_retained_task_workspaces(
        project, dry_run=True, include_blocked=True
    )
    assert report["candidate_count"] == 0
    assert any(
        row["reason"] == "session_continuation_live" for row in report["skipped"]
    )

    row = {
        "id": session.id,
        "status": session.status,
        "started_at": None,
        "continuation_task_id": session.continuation_task_id,
        "continuation_kind": session.continuation_kind,
        "continuation_retry_count": session.continuation_retry_count,
        "continuation_retry_eta": session.continuation_retry_eta,
    }
    failed_attempt = [
        {"id": 1, "task_id": task.id, "attempt_number": 1, "status": "failed"}
    ]
    assert outcome_class(row, failed_attempt, []) == "in_progress"
