"""Provider-free coverage for F1c-2 one-shot routing in the canonical Worker.

Celery runs eagerly through ``Task.apply`` (in-process, nothing published);
every database is a disposable migrated SQLite file.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import run_schema_migrations
from app.models import Base, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.execution import one_shot_claim, one_shot_routing
from app.services.execution.one_shot_authorization import (
    inspect_authorization,
    revoke_authorization,
)
from app.services.execution.one_shot_dispatch import (
    AttemptStatus,
    SupervisorAdmission,
    SupervisorAdmissionStatus,
    SupervisorTransportError,
    UnavailableSupervisor,
    WorkerIdentityAttestation,
)
from app.services.execution.one_shot_routing import (
    ONE_SHOT_CONTEXT_KEY,
    OneShotDelivery,
    RoutingStatus,
    route_one_shot_delivery,
)
from app.services.orchestration.worker_identity import (
    IDENTITY_INCONCLUSIVE,
    IDENTITY_VERIFIED,
    PIN_ABSENT,
    PIN_MATCHED,
)
from app.tasks import worker
from app.tests.test_phase39_f1a_authorization import _graph
from app.tests.test_phase39_f1b_atomic_claim import (
    MIGRATION_057,
    MIGRATION_058,
    _SimulatedCrash,
    _commit_error,
    _engine,
    _issue,
    _state,
)
from app.tests.test_phase39_f1c1_dispatch_boundary import FakeSupervisor

VERIFIED = WorkerIdentityAttestation(IDENTITY_VERIFIED, PIN_MATCHED)
PRODUCTION_POSTURE = WorkerIdentityAttestation(
    IDENTITY_INCONCLUSIVE, PIN_ABSENT, ("candidate_pin_unavailable",)
)


class _LegacyReached(Exception):
    """Raised where the legacy lifecycle opens its own database session."""


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Disposable migrated SQLite wired into the Worker; gate enabled."""

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f1c2.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    factory = sessionmaker(autoflush=False, bind=engine)
    sessions = []

    def get_db_session():
        # The first session per delivery is the routing hook's; a second
        # one means the legacy lifecycle was entered.
        if len(sessions) % 2 == 1:
            sessions.append(None)
            raise _LegacyReached()
        db = factory()
        sessions.append(db)
        return db

    supervisor = FakeSupervisor()
    state = {"identity": VERIFIED, "supervisor": supervisor}
    monkeypatch.setattr(worker, "get_db_session", get_db_session)
    monkeypatch.setattr(
        worker, "_one_shot_worker_identity_attestation", lambda: state["identity"]
    )
    monkeypatch.setattr(worker, "_one_shot_supervisor", lambda: state["supervisor"])
    db = factory()
    try:
        yield path, engine, db, state, sessions
    finally:
        db.close()
        engine.dispose()


def _deliver(identity, **overrides):
    kwargs = {
        "session_id": identity.session_id,
        "task_id": identity.task_id,
        "prompt": "one-shot fixture",
        "task_execution_id": identity.task_execution_id,
        "expected_session_instance_id": identity.session_instance_id,
        "context": {ONE_SHOT_CONTEXT_KEY: identity.authorization_uuid},
    }
    kwargs.update(overrides)
    return kwargs


def _apply(env, kwargs, **request):
    # A fresh routing session per delivery keeps the legacy sentinel aligned.
    env[4].clear()
    result = worker.execute_orchestration_task.apply(
        kwargs=kwargs, throw=True, **request
    )
    return result.get()["one_shot"]


def _route(db, identity, supervisor, *, attestation=VERIFIED, **delivery):
    values = {
        "session_id": identity.session_id,
        "task_id": identity.task_id,
        "task_execution_id": identity.task_execution_id,
        "expected_session_instance_id": identity.session_instance_id,
        "authorization_uuid": identity.authorization_uuid,
    }
    reconcile_only = delivery.pop("reconcile_only", False)
    values.update(delivery)
    return route_one_shot_delivery(
        db,
        OneShotDelivery(**values),
        worker_identity=lambda: attestation,
        supervisor=supervisor,
        reconcile_only=reconcile_only,
    )


def _receipts(path, identity):
    return _state(path, identity.authorization_uuid)[2]


def _auth_state(path, identity):
    return _state(path, identity.authorization_uuid)[0][0]


# 1. Ordinary execution ------------------------------------------------------


@pytest.mark.parametrize("gate", [True, False])
def test_ordinary_task_reaches_the_legacy_path_unchanged(env, monkeypatch, gate):
    _, _, db, state, sessions = env
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", gate)
    _, session, task, execution = _graph(db, execution_profile="full_lifecycle")
    state["supervisor"] = None  # any one-shot handling would fail loudly

    with pytest.raises(_LegacyReached):
        worker.execute_orchestration_task.apply(
            kwargs={
                "session_id": session.id,
                "task_id": task.id,
                "prompt": "ordinary",
                "task_execution_id": execution.id,
            },
            throw=True,
        )
    assert sessions[-1] is None


def test_one_shot_profile_never_reaches_the_legacy_path(env):
    _, _, db, _, sessions = env
    identity, _ = _issue(db)

    result = _apply(env, _deliver(identity, context=None))

    assert result["status"] == "REFUSED" and result["reason"] == "PAYLOAD_INVALID"
    assert None not in sessions


# 2. Feature gate -------------------------------------------------------------


def test_disabled_gate_refuses_without_claim_or_supervisor(env, monkeypatch):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", False)

    result = _apply(env, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", "FEATURE_DISABLED")
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert state["supervisor"].calls == []


# 3, 4, 5. First claim, committed claim reuse, redelivery ---------------------


def test_first_delivery_claims_and_starts_once(env, caplog):
    path, _, db, state, sessions = env
    identity, issued = _issue(db)

    with caplog.at_level(logging.INFO, logger=one_shot_routing.__name__):
        result = _apply(env, _deliver(identity))

    assert result["status"] == "HANDED_OFF" and result["mode"] == "START"
    assert result["claim_outcome"] == "CLAIMED_SUCCESSFULLY"
    assert result["start_submitted"] is True
    assert result["attempt_key_prefix"] == issued.attempt_idempotency_key[:16]
    (start,) = state["supervisor"].spawned
    assert start.attempt_idempotency_key == issued.attempt_idempotency_key
    assert len(_receipts(path, identity)) == 1
    assert None not in sessions
    (line,) = [r.getMessage() for r in caplog.records if "[ONE_SHOT]" in r.message]
    assert "status=HANDED_OFF" in line and "mode=START" in line
    assert issued.attempt_idempotency_key not in line


@pytest.mark.parametrize(
    "request_options",
    [{}, {"retries": 2}],
)
def test_later_delivery_reuses_the_committed_claim_reconcile_only(env, request_options):
    path, _, db, state, _ = env
    identity, issued = _issue(db)
    first = _apply(env, _deliver(identity))

    again = _apply(env, _deliver(identity), **request_options)

    assert again["mode"] == "RECONCILE_ONLY" and again["claim_outcome"] is None
    assert again["status"] == "RECONCILED" and again["start_submitted"] is False
    assert again["redelivered"] is bool(request_options)
    assert again["attempt_key_prefix"] == first["attempt_key_prefix"]
    assert len(state["supervisor"].spawned) == 1
    assert len(_receipts(path, identity)) == 1


def test_redelivery_after_crash_before_claim_commit_may_claim(env, monkeypatch):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    monkeypatch.setattr(
        one_shot_claim,
        "_commit_claim",
        lambda connection: (_ for _ in ()).throw(_SimulatedCrash()),
    )
    with pytest.raises(_SimulatedCrash):
        _route(db, identity, state["supervisor"])
    assert _auth_state(path, identity) == "AUTHORIZED"
    monkeypatch.undo()
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)

    result = _route(db, identity, state["supervisor"], redelivered=True)

    assert result.status is RoutingStatus.HANDED_OFF and result.mode == "START"
    assert len(state["supervisor"].spawned) == 1


def test_explicit_recovery_is_reconcile_only(env):
    path, _, db, state, _ = env
    identity, _ = _issue(db)

    unclaimed = _route(db, identity, state["supervisor"], reconcile_only=True)
    _route(db, identity, state["supervisor"])
    claimed = _route(db, identity, state["supervisor"], reconcile_only=True)

    assert (unclaimed.status, unclaimed.reason) == (
        RoutingStatus.REFUSED,
        "NOT_CLAIMED",
    )
    assert claimed.status is RoutingStatus.RECONCILED
    assert claimed.mode == "RECONCILE_ONLY"
    assert len(state["supervisor"].spawned) == 1


# 6. Worker restart -----------------------------------------------------------


def test_restart_after_supervisor_accepted_reconciles_same_key(env):
    path, engine, db, state, _ = env
    identity, issued = _issue(db)
    supervisor = state["supervisor"]
    original = supervisor.start_attempt

    def accept_then_die(request):
        original(request)
        raise _SimulatedCrash()

    supervisor.start_attempt = accept_then_die
    with pytest.raises(_SimulatedCrash):
        _route(db, identity, supervisor)
    supervisor.start_attempt = original

    with sessionmaker(bind=engine)() as restarted:
        result = _route(restarted, identity, supervisor)

    assert result.status is RoutingStatus.RECONCILED
    assert result.attempt_key_prefix == issued.attempt_idempotency_key[:16]
    assert list(supervisor.attempts) == [issued.attempt_idempotency_key]
    assert len(supervisor.spawned) == 1 and len(_receipts(path, identity)) == 1


def test_restart_after_claim_before_start_holds_without_new_key(env):
    path, engine, db, state, _ = env
    identity, issued = _issue(db)
    supervisor = state["supervisor"]

    def die(request):
        raise _SimulatedCrash()

    supervisor.start_attempt = die
    with pytest.raises(_SimulatedCrash):
        _route(db, identity, supervisor)

    with sessionmaker(bind=engine)() as restarted:
        result = _route(restarted, identity, FakeSupervisor())

    # READY + no Supervisor attempt does not prove no start was submitted.
    assert (result.status, result.reason) == (RoutingStatus.HELD, "NOT_STARTED")
    assert result.mode == "RECONCILE_ONLY" and not result.start_submitted
    assert result.attempt_key_prefix == issued.attempt_idempotency_key[:16]
    assert len(_receipts(path, identity)) == 1


# 7. Commit uncertainty ------------------------------------------------------


def test_uncertain_commit_proven_durable_by_reconciliation_starts(env, monkeypatch):
    path, _, db, state, _ = env
    identity, _ = _issue(db)

    def commit_then_lose_reply(connection):
        connection.exec_driver_sql("COMMIT")
        raise _commit_error()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", commit_then_lose_reply)

    result = _route(db, identity, state["supervisor"])

    assert result.claim_outcome == "CLAIMED_SUCCESSFULLY" and result.claim_reconciled
    assert result.status is RoutingStatus.HANDED_OFF and result.mode == "START"
    assert len(state["supervisor"].spawned) == 1


def test_unprovable_commit_holds_without_dispatch(env, monkeypatch):
    path, _, db, state, _ = env
    identity, _ = _issue(db)

    def lose_commit(connection):
        raise _commit_error()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", lose_commit)

    result = _route(db, identity, state["supervisor"])

    assert (result.status, result.reason) == (RoutingStatus.HELD, "COMMIT_UNCERTAIN")
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert "status" not in state["supervisor"].calls
    assert "start" not in state["supervisor"].calls


# 8, 9. Missing, mismatched, expired, revoked authority ----------------------


def test_missing_authorization_fails_closed(env):
    _, _, db, state, _ = env
    identity, _ = _issue(db)
    kwargs = _deliver(identity, context={ONE_SHOT_CONTEXT_KEY: str(uuid.uuid4())})

    result = _apply(env, kwargs)

    assert (result["status"], result["reason"]) == ("REFUSED", "AUTHORITY_MISSING")
    assert state["supervisor"].calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_execution_id": None},
        {"expected_session_instance_id": None},
        {"context": {ONE_SHOT_CONTEXT_KEY: "not-a-uuid"}},
        {"continuation_kind": "backend_capacity"},
    ],
)
def test_incomplete_payload_fails_closed(env, overrides):
    _, _, db, state, sessions = env
    identity, _ = _issue(db)

    result = _apply(env, _deliver(identity, **overrides))

    assert (result["status"], result["reason"]) == ("REFUSED", "PAYLOAD_INVALID")
    assert None not in sessions and state["supervisor"].calls == []


def test_payload_bound_to_another_execution_is_a_mismatch(env):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    other_identity, _ = _issue(db)
    kwargs = _deliver(
        identity, context={ONE_SHOT_CONTEXT_KEY: other_identity.authorization_uuid}
    )

    result = _apply(env, kwargs)

    assert (result["status"], result["reason"]) == ("REFUSED", "AUTHORITY_MISMATCH")
    assert _auth_state(path, other_identity) == "AUTHORIZED"


def test_revoked_and_expired_authorizations_fail_closed(env):
    _, _, db, state, _ = env
    revoked, revoked_issued = _issue(db)
    revoke_authorization(
        db, revoked_issued.authorization_uuid, operator_identity="op@example.test"
    )
    expired, expired_issued = _issue(db)
    inspect_authorization(
        db,
        expired_issued.authorization_uuid,
        now=expired.expires_at + timedelta(seconds=1),
    )

    assert _route(db, revoked, state["supervisor"]).reason == "AUTHORIZATION_REVOKED"
    assert _route(db, expired, state["supervisor"]).reason == "AUTHORIZATION_EXPIRED"
    assert state["supervisor"].calls == []


# 10, 11. Generation and terminal execution ----------------------------------


def test_rotated_generation_fails_closed_before_and_after_claim(env):
    path, _, db, state, _ = env
    before, _ = _issue(db)
    after, _ = _issue(db)
    assert _route(db, after, state["supervisor"]).status is RoutingStatus.HANDED_OFF
    for identity in (before, after):
        db.get(SessionModel, identity.session_id).instance_id = str(uuid.uuid4())
    db.commit()
    supervisor = FakeSupervisor()

    unclaimed = _route(db, before, supervisor)
    claimed = _route(db, after, supervisor)

    assert unclaimed.reason == "EXECUTION_STATE_CONFLICT"
    assert _auth_state(path, before) == "AUTHORIZED"
    # Already claimed: lookup only, never a new start in a stale generation.
    assert claimed.mode == "RECONCILE_ONLY" and not claimed.start_submitted
    assert "start" not in supervisor.calls


def test_terminal_task_execution_cannot_initiate_an_attempt(env):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    db.get(TaskExecution, identity.task_execution_id).status = TaskStatus.DONE
    db.commit()

    result = _route(db, identity, state["supervisor"])

    assert (result.status, result.reason) == (
        RoutingStatus.REFUSED,
        "EXECUTION_STATE_CONFLICT",
    )
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert state["supervisor"].calls == []


# 12, 13. Worker identity and Supervisor --------------------------------------


@pytest.mark.parametrize(
    "attestation, reason",
    [
        (None, "WORKER_IDENTITY_UNAVAILABLE"),
        (PRODUCTION_POSTURE, "CANDIDATE_PIN_NOT_MATCHED"),
        (
            WorkerIdentityAttestation(IDENTITY_INCONCLUSIVE, PIN_MATCHED),
            "WORKER_IDENTITY_UNVERIFIED",
        ),
    ],
)
def test_unverified_worker_identity_refuses_before_claim(env, attestation, reason):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    state["identity"] = attestation

    result = _apply(env, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", reason)
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert state["supervisor"].calls == []


@pytest.mark.parametrize(
    "supervisor, reason",
    [
        (UnavailableSupervisor(), "SUPERVISOR_UNAVAILABLE"),
        (
            FakeSupervisor(
                admission=SupervisorAdmission(SupervisorAdmissionStatus.UNTRUSTED)
            ),
            "SUPERVISOR_UNTRUSTED",
        ),
        (
            FakeSupervisor(
                admission=SupervisorAdmission(
                    SupervisorAdmissionStatus.TRUSTED, durable_deduplication=False
                )
            ),
            "SUPERVISOR_UNTRUSTED",
        ),
    ],
)
def test_unavailable_or_untrusted_supervisor_refuses_before_claim(
    env, supervisor, reason
):
    path, _, db, state, _ = env
    identity, _ = _issue(db)
    state["supervisor"] = supervisor

    result = _apply(env, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", reason)
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert getattr(supervisor, "spawned", []) == []


def test_default_worker_adapters_refuse_dispatch(monkeypatch, tmp_path):
    assert isinstance(worker._one_shot_supervisor(), UnavailableSupervisor)
    # Never read a real identity directory or /proc from this test.
    monkeypatch.setattr(
        worker,
        "_resolve_worker_identity_paths",
        lambda **_kwargs: type("Paths", (), {"identity_dir": tmp_path})(),
    )
    monkeypatch.setattr(worker, "_capture_process_identity", lambda: object())
    monkeypatch.setattr(
        worker, "_matching_worker_boot_manifest", lambda *_args: (None, None)
    )
    assert worker._one_shot_worker_identity_attestation() is None

    class _Manifest:
        identity_status = IDENTITY_INCONCLUSIVE
        candidate_pin_status = PIN_ABSENT
        identity_status_reasons = ("candidate_pin_unavailable",)

    monkeypatch.setattr(
        worker, "_matching_worker_boot_manifest", lambda *_args: ({}, "digest")
    )
    monkeypatch.setattr(
        worker._WorkerBootIdentityManifest, "from_dict", lambda _value: _Manifest()
    )
    # The recorded classification passes through unchanged, never upgraded.
    assert worker._one_shot_worker_identity_attestation() == PRODUCTION_POSTURE

    def _broken(*_args):
        raise OSError("identity directory unreadable")

    monkeypatch.setattr(worker, "_matching_worker_boot_manifest", _broken)
    assert worker._one_shot_worker_identity_attestation() is None


# 14. Unknown Supervisor outcome ----------------------------------------------


def test_unknown_outcome_never_triggers_a_second_start(env):
    _, _, db, state, _ = env
    identity, _ = _issue(db)
    supervisor = FakeSupervisor(lose_start_reply=True)
    supervisor.status_override = None
    lookups = []
    original_status = supervisor.get_attempt_status

    def status(key):
        lookups.append(key)
        if len(lookups) > 1:
            raise SupervisorTransportError("status lookup timed out")
        return original_status(key)

    supervisor.get_attempt_status = status

    lost = _route(db, identity, supervisor)
    supervisor.status_override = AttemptStatus.UNKNOWN
    supervisor.get_attempt_status = original_status
    redelivered = _route(db, identity, supervisor, redelivered=True)

    assert (lost.status, lost.reason) == (RoutingStatus.HELD, "HOLD_UNKNOWN")
    assert (redelivered.status, redelivered.mode) == (
        RoutingStatus.HELD,
        "RECONCILE_ONLY",
    )
    assert supervisor.calls.count("start") == 1


def test_unexpected_error_holds_without_legacy_fallback(env, monkeypatch):
    _, _, db, state, sessions = env
    identity, _ = _issue(db)

    def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(one_shot_routing, "dispatch_one_shot_intent", broken)

    result = _apply(env, _deliver(identity))

    assert (result["status"], result["reason"]) == ("HELD", "ROUTING_ERROR")
    assert None not in sessions


# 15. Concurrency -------------------------------------------------------------


def test_concurrent_deliveries_produce_one_claim_and_one_attempt(env):
    path, engine, db, state, _ = env
    identity, _ = _issue(db)
    supervisor = state["supervisor"]
    factory = sessionmaker(autoflush=False, bind=engine)
    barrier = threading.Barrier(3)
    results = []

    def run():
        with factory() as own_db:
            barrier.wait(timeout=5)
            results.append(_route(own_db, identity, supervisor))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(results) == 3
    starters = [r for r in results if r.mode == "START"]
    assert len(starters) == 1 and starters[0].status is RoutingStatus.HANDED_OFF
    assert all(
        r.mode == "RECONCILE_ONLY"
        and r.status in (RoutingStatus.RECONCILED, RoutingStatus.HELD)
        for r in results
        if r is not starters[0]
    )
    assert len(supervisor.spawned) == 1
    assert len(_receipts(path, identity)) == 1
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM executor_launch_intents").fetchone()[
            0
        ] == len({identity.authorization_uuid})


def test_one_shot_profile_task_row_is_recognised_without_context(env):
    _, _, db, _, _ = env
    identity, _ = _issue(db)

    assert one_shot_routing.is_one_shot_delivery(db, identity.task_id, None)
    plain = Task(project_id=identity.project_id, title="plain")
    db.add(plain)
    db.commit()
    assert not one_shot_routing.is_one_shot_delivery(db, plain.id, {})
