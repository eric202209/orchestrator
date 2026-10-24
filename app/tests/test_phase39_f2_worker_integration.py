"""Canonical Worker -> F1b claim -> F1c dispatch -> real F2 Supervisor.

Deliveries go through ``execute_orchestration_task`` (Celery ``apply``) and
the Worker's real configuration-based Supervisor selection, against a real
``python -m app.supervisor.server`` child on a disposable socket and ledger.
The Worker identity attestation is an explicit test-only fixture; no
production identity evidence is read or created. The Supervisor runs only
its fixed no-op operation; no executor exists.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import tempfile
import threading

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import run_schema_migrations
from app.models import Base
from app.services.execution.one_shot_dispatch import (
    AttemptStatus,
    UnavailableSupervisor,
)
from app.services.execution.one_shot_supervisor_client import (
    MisconfiguredSupervisor,
    UnixSocketSupervisor,
    configured_supervisor,
)
from app.tasks import worker
from app.tests.test_phase39_f1a_authorization import _graph
from app.tests.test_phase39_f1b_atomic_claim import (
    MIGRATION_057,
    MIGRATION_058,
    _engine,
    _issue,
)
from app.tests.test_phase39_f1c2_worker_routing import (
    PRODUCTION_POSTURE,
    VERIFIED,
    _auth_state,
    _deliver,
    _receipts,
)
from app.tests.test_phase39_f2_trusted_supervisor import (
    UID,
    SupervisorProcess,
    _start_request,
    _wait_status,
)


class _LegacyReached(Exception):
    """Raised where the legacy lifecycle would open its own session."""


@pytest.fixture
def supervisor_root():
    # Short path: Unix socket paths are limited to 108 bytes.
    root = Path(tempfile.mkdtemp(prefix="f2w-"))
    os.chmod(root, 0o750)
    processes = []
    yield root, processes
    for process in processes:
        if process.proc is not None and process.proc.poll() is None:
            process.proc.kill()
            process.proc.wait(timeout=10)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch, supervisor_root):
    """Disposable migrated SQLite and a real Supervisor wired into the Worker."""

    root, processes = supervisor_root
    supervisor = SupervisorProcess(root)
    processes.append(supervisor)
    supervisor.start()

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    monkeypatch.setattr(
        settings, "ONE_SHOT_SUPERVISOR_SOCKET", str(supervisor.socket_path)
    )
    monkeypatch.setattr(settings, "ONE_SHOT_SUPERVISOR_UID", str(UID))
    path = tmp_path / "f2w.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    factory = sessionmaker(autoflush=False, bind=engine)
    local = threading.local()
    legacy = []

    def get_db_session():
        # One session per delivery belongs to the routing hook; a second one
        # in the same delivery means the legacy lifecycle was entered.
        count = getattr(local, "count", 0)
        local.count = count + 1
        if count >= 1:
            legacy.append(threading.get_ident())
            raise _LegacyReached()
        return factory()

    state = {"identity": VERIFIED, "local": local, "legacy": legacy}
    monkeypatch.setattr(worker, "get_db_session", get_db_session)
    # Test-only identity fixture; the Supervisor selection stays real.
    monkeypatch.setattr(
        worker, "_one_shot_worker_identity_attestation", lambda: state["identity"]
    )
    db = factory()
    try:
        yield path, engine, db, state, supervisor
    finally:
        db.close()
        engine.dispose()


def _apply(stack, kwargs, **request):
    stack[3]["local"].count = 0
    result = worker.execute_orchestration_task.apply(
        kwargs=kwargs, throw=True, **request
    )
    # Eager result: already computed. Other suites boot a Celery worker
    # in-process, which leaves the global "inside a task" join guard set.
    return result.get(disable_sync_subtasks=False)["one_shot"]


def _client(supervisor):
    return UnixSocketSupervisor(supervisor.socket_path, supervisor_uid=UID)


# 1-3. Valid delivery reaches the real Supervisor -------------------------------


def test_worker_delivery_reaches_the_real_supervisor_once(stack):
    path, _, db, state, supervisor = stack
    identity, issued = _issue(db)
    assert isinstance(worker._one_shot_supervisor(), UnixSocketSupervisor)

    result = _apply(stack, _deliver(identity))

    assert (result["status"], result["mode"]) == ("HANDED_OFF", "START")
    assert result["claim_outcome"] == "CLAIMED_SUCCESSFULLY"
    assert result["dispatch_outcome"] == "DISPATCHED"
    assert result["start_submitted"] is True
    assert _auth_state(path, identity) == "CLAIMED"
    assert len(_receipts(path, identity)) == 1
    (row,) = supervisor.rows()
    assert row[0] == issued.attempt_idempotency_key
    receipt = _wait_status(
        _client(supervisor), issued.attempt_idempotency_key, {AttemptStatus.EXITED}
    )
    assert receipt.status is AttemptStatus.EXITED
    assert receipt.request_payload_sha256 == issued.request_payload_sha256
    assert receipt.authorization_identity_sha256 == issued.identity_sha256
    assert state["legacy"] == []


# 4. Duplicate Celery deliveries ------------------------------------------------


def test_duplicate_deliveries_do_not_create_attempts(stack):
    path, _, db, state, supervisor = stack
    identity, _ = _issue(db)
    _apply(stack, _deliver(identity))
    (first_row,) = supervisor.rows()

    for request in ({}, {"retries": 2}, {}, {"retries": 1}):
        again = _apply(stack, _deliver(identity), **request)
        assert (again["status"], again["mode"]) == ("RECONCILED", "RECONCILE_ONLY")
        assert again["start_submitted"] is False
        assert again["claim_outcome"] is None

    # Same single attempt (key, attempt id, request digest); status may advance.
    (row,) = supervisor.rows()
    assert (row[0], row[1], row[3]) == (first_row[0], first_row[1], first_row[3])
    assert len(_receipts(path, identity)) == 1
    assert state["legacy"] == []


def test_concurrent_worker_deliveries_produce_one_claim_and_one_attempt(stack):
    path, _, db, state, supervisor = stack
    identity, _ = _issue(db)
    barrier = threading.Barrier(4)
    results, errors = [], []

    def run():
        try:
            barrier.wait(timeout=10)
            results.append(_apply(stack, _deliver(identity)))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [] and len(results) == 4
    starters = [r for r in results if r["mode"] == "START"]
    assert len(starters) == 1 and starters[0]["status"] == "HANDED_OFF"
    assert all(
        r["mode"] == "RECONCILE_ONLY" and r["status"] in ("RECONCILED", "HELD")
        for r in results
        if r is not starters[0]
    )
    assert sum(r["start_submitted"] for r in results) == 1
    assert len(supervisor.rows()) == 1
    assert len(_receipts(path, identity)) == 1
    assert state["legacy"] == []


# 5. Supervisor restart -----------------------------------------------------------


def test_reconciliation_after_supervisor_restart_returns_the_original_attempt(stack):
    path, _, db, state, supervisor = stack
    identity, issued = _issue(db)
    _apply(stack, _deliver(identity))
    key = issued.attempt_idempotency_key
    _wait_status(_client(supervisor), key, {AttemptStatus.EXITED})
    (before,) = supervisor.rows()

    supervisor.crash()
    down = _apply(stack, _deliver(identity))
    assert down["mode"] == "RECONCILE_ONLY"
    assert down["reason"] == "SUPERVISOR_UNAVAILABLE"
    assert down["start_submitted"] is False

    supervisor.start()
    after = _apply(stack, _deliver(identity), retries=1)
    assert (after["status"], after["attempt_status"]) == ("RECONCILED", "EXITED")
    assert after["start_submitted"] is False
    assert supervisor.rows() == [before]
    assert len(_receipts(path, identity)) == 1
    assert state["legacy"] == []


# 6. Conflicting attempt ------------------------------------------------------------


def test_conflicting_attempt_is_refused_without_modifying_it(stack):
    path, _, db, state, supervisor = stack
    identity, issued = _issue(db)
    # A foreign request already holds this attempt key with other bindings.
    foreign = replace(
        _start_request("foreign"),
        attempt_idempotency_key=issued.attempt_idempotency_key,
    )
    _client(supervisor).start_attempt(foreign)
    _wait_status(
        _client(supervisor), issued.attempt_idempotency_key, {AttemptStatus.EXITED}
    )
    before = supervisor.rows()

    result = _apply(stack, _deliver(identity))

    assert result["status"] == "HELD"
    assert result["dispatch_outcome"] == "HOLD_RECEIPT_CONFLICT"
    assert result["start_submitted"] is False
    assert supervisor.rows() == before
    again = _apply(stack, _deliver(identity))
    assert again["status"] == "HELD" and again["start_submitted"] is False
    assert supervisor.rows() == before
    assert state["legacy"] == []


# 7. Unavailable, untrusted, or misconfigured Supervisor ---------------------------


@pytest.mark.parametrize(
    "socket_value, uid_value, reason",
    [
        ("missing", str(UID), "SUPERVISOR_UNAVAILABLE"),
        ("real", str(UID + 4242), "SUPERVISOR_UNTRUSTED"),
        ("real", None, "SUPERVISOR_UNAVAILABLE"),
        (None, str(UID), "SUPERVISOR_UNAVAILABLE"),
        ("relative.sock", str(UID), "SUPERVISOR_UNAVAILABLE"),
        ("real", "root", "SUPERVISOR_UNAVAILABLE"),
        (None, None, "SUPERVISOR_UNAVAILABLE"),
    ],
)
def test_unusable_supervisor_refuses_before_claim(
    stack, monkeypatch, socket_value, uid_value, reason
):
    path, _, db, state, supervisor = stack
    identity, _ = _issue(db)
    socket_path = {
        "real": str(supervisor.socket_path),
        "missing": str(supervisor.socket_path.with_name("absent.sock")),
    }.get(socket_value, socket_value)
    monkeypatch.setattr(settings, "ONE_SHOT_SUPERVISOR_SOCKET", socket_path)
    monkeypatch.setattr(settings, "ONE_SHOT_SUPERVISOR_UID", uid_value)

    result = _apply(stack, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", reason)
    assert result["start_submitted"] is False
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert supervisor.rows() == []
    assert state["legacy"] == []


def test_supervisor_refusing_the_worker_uid_is_untrusted(
    stack, supervisor_root, monkeypatch
):
    path, _, db, state, _ = stack
    root, processes = supervisor_root
    (root / "other").mkdir(mode=0o750)
    refusing = SupervisorProcess(root / "other", allowed_uid=UID + 4242)
    processes.append(refusing)
    refusing.start()
    monkeypatch.setattr(
        settings, "ONE_SHOT_SUPERVISOR_SOCKET", str(refusing.socket_path)
    )
    identity, _ = _issue(db)

    result = _apply(stack, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", "SUPERVISOR_UNTRUSTED")
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert refusing.rows() == []


# 8. Worker identity ------------------------------------------------------------------


@pytest.mark.parametrize(
    "attestation, reason",
    [
        (None, "WORKER_IDENTITY_UNAVAILABLE"),
        (PRODUCTION_POSTURE, "CANDIDATE_PIN_NOT_MATCHED"),
    ],
)
def test_missing_worker_pin_refuses_before_claim(stack, attestation, reason):
    path, _, db, state, supervisor = stack
    identity, _ = _issue(db)
    state["identity"] = attestation

    result = _apply(stack, _deliver(identity))

    assert (result["status"], result["reason"]) == ("REFUSED", reason)
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert supervisor.rows() == []
    assert state["legacy"] == []


# 9-10. Ordinary tasks and no legacy fallback -------------------------------------------


@pytest.mark.parametrize("gate", [True, False])
def test_ordinary_task_keeps_its_legacy_path(stack, monkeypatch, gate):
    _, _, db, state, supervisor = stack
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", gate)
    _, session, task, execution = _graph(db, execution_profile="full_lifecycle")
    state["local"].count = 0

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
    assert len(state["legacy"]) == 1
    assert supervisor.rows() == []


@pytest.mark.parametrize("variant", ["no_context", "gate_off"])
def test_one_shot_delivery_never_falls_back_to_legacy(stack, monkeypatch, variant):
    path, _, db, state, supervisor = stack
    identity, _ = _issue(db)
    kwargs = _deliver(identity)
    if variant == "no_context":
        kwargs = _deliver(identity, context=None)
    else:
        monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", False)

    result = _apply(stack, kwargs)

    assert result["status"] == "REFUSED"
    assert result["reason"] in ("PAYLOAD_INVALID", "FEATURE_DISABLED")
    assert _auth_state(path, identity) == "AUTHORIZED"
    assert supervisor.rows() == []
    assert state["legacy"] == []


# Adapter selection --------------------------------------------------------------------


def test_worker_supervisor_selection_fails_closed():
    assert isinstance(configured_supervisor(None, None), UnavailableSupervisor)
    assert isinstance(configured_supervisor("  ", ""), UnavailableSupervisor)
    for socket_path, uid, reason in (
        ("/run/s.sock", None, "supervisor_configuration_incomplete"),
        (None, "0", "supervisor_configuration_incomplete"),
        ("run/s.sock", "0", "supervisor_configuration_invalid"),
        ("/run/s.sock", "-1", "supervisor_configuration_invalid"),
        ("/run/s.sock", "1.5", "supervisor_configuration_invalid"),
        ("/run/s.sock", "²", "supervisor_configuration_invalid"),
    ):
        selected = configured_supervisor(socket_path, uid)
        assert isinstance(selected, MisconfiguredSupervisor), (socket_path, uid)
        assert selected.admission().reasons == (reason,)
    selected = configured_supervisor("/run/s.sock", "1001")
    assert isinstance(selected, UnixSocketSupervisor)
    assert (str(selected.socket_path), selected.supervisor_uid) == ("/run/s.sock", 1001)
