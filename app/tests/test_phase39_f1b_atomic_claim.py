"""Provider-free temporary-SQLite coverage for the Phase 39 F1b atomic claim."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import inspect
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.config import Settings, settings
from app.db_migrations import (
    MIGRATIONS,
    _SQL_TOKEN,
    _sql_tokens,
    _verify_058_exact_sqlite_schema,
    run_schema_migrations,
)
from app.models import (
    Base,
    ExecutorLaunchIntent,
    OneShotExecutionAuthorization,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.execution import one_shot_authorization, one_shot_claim
from app.services.execution.one_shot_authorization import (
    AuthorizationStateConflict,
    OneShotFeatureDisabled,
    issue_one_shot_authorization,
    revoke_authorization,
)
from app.services.execution.one_shot_claim import (
    ClaimOutcome,
    claim_one_shot_authorization,
    reconcile_one_shot_claim,
)
from app.tests.test_phase39_f1a_authorization import _graph, _identity

MIGRATION_057, MIGRATION_058 = (
    next(migration for migration in MIGRATIONS if migration.version == version)
    for version in (
        "057_one_shot_execution_authorization",
        "058_one_shot_atomic_claim",
    )
)
ONE_SHOT_TABLES = {
    "one_shot_execution_authorizations",
    "executor_launch_intents",
    "one_shot_authorization_claims",
}


class _SimulatedCrash(BaseException):
    """Stands in for process death: not an Exception, so nothing handles it."""


def _engine(path: Path, *, timeout: float = 5.0):
    """Mirror the production SQLite connection policy on a temporary file."""

    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        cursor.close()

    return engine


@pytest.fixture
def f1b(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f1b.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    factory = sessionmaker(autoflush=False, bind=engine)
    db = factory()
    try:
        yield path, engine, db
    finally:
        db.close()
        engine.dispose()


def _issue(db, **overrides):
    identity = _identity(*_graph(db), **overrides)
    issued = issue_one_shot_authorization(db, identity)
    return identity, issued


def _state(path: Path, authorization_uuid: str):
    """Read durable rows through an independent raw sqlite3 connection."""

    with sqlite3.connect(path) as raw:
        auth = raw.execute(
            "SELECT state, state_changed_at, revoked_by "
            "FROM one_shot_execution_authorizations WHERE authorization_uuid = ?",
            (authorization_uuid,),
        ).fetchone()
        intents = raw.execute(
            "SELECT state, attempt_idempotency_key, request_payload_sha256 "
            "FROM executor_launch_intents WHERE authorization_uuid = ?",
            (authorization_uuid,),
        ).fetchall()
        receipts = raw.execute(
            "SELECT claim_uuid, attempt_idempotency_key "
            "FROM one_shot_authorization_claims WHERE authorization_uuid = ?",
            (authorization_uuid,),
        ).fetchall()
    return auth, intents, receipts


def _assert_unclaimed(path, issued):
    auth, intents, receipts = _state(path, issued.authorization_uuid)
    assert auth == ("AUTHORIZED", None, None)
    assert intents == [
        (
            "NOT_DISPATCHABLE",
            issued.attempt_idempotency_key,
            issued.request_payload_sha256,
        )
    ]
    assert receipts == []


def _assert_claimed(path, issued, claim_uuid):
    auth, intents, receipts = _state(path, issued.authorization_uuid)
    assert auth[0] == "CLAIMED" and auth[1] is not None and auth[2] is None
    assert intents == [
        ("READY", issued.attempt_idempotency_key, issued.request_payload_sha256)
    ]
    assert receipts == [(claim_uuid, issued.attempt_idempotency_key)]


def _clock(monkeypatch, *instants):
    """Replace the claim's trusted clock; the last instant repeats."""

    values = list(instants)
    calls: list[datetime] = []

    def _now():
        calls.append(values.pop(0) if len(values) > 1 else values[0])
        return calls[-1]

    monkeypatch.setattr(one_shot_claim, "_trusted_utc_now", _now)
    return calls


def _commit_error():
    return OperationalError("COMMIT", {}, sqlite3.OperationalError("disk I/O error"))


# 1, 20 -----------------------------------------------------------------------


def test_successful_claim_transitions_both_rows_and_preserves_stable_key(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    assert result.handoff_permitted and not result.reconciled
    assert result.authorization_state == "CLAIMED"
    assert result.launch_state == "READY"
    assert result.attempt_idempotency_key == issued.attempt_idempotency_key
    assert result.request_payload_sha256 == issued.request_payload_sha256
    _assert_claimed(path, issued, result.claim_uuid)
    assert not db.new and not db.dirty


def test_begin_immediate_is_first_statement_of_the_claim_connection(f1b):
    _, engine, db = f1b
    identity, _ = _issue(db)
    # Hold the Connection objects themselves: id() of a discarded schema-check
    # connection can be reused by the claim connection (F1b-R3 R3-01).
    statements: list[tuple[object, str]] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append((conn, statement.strip().upper()))

    try:
        result = claim_one_shot_authorization(db, identity)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    claim_connection = next(
        conn for conn, statement in statements if statement == "BEGIN IMMEDIATE"
    )
    own = [statement for conn, statement in statements if conn is claim_connection]
    assert len(own) == 5
    assert own[0] == "BEGIN IMMEDIATE"
    assert own[1].startswith("UPDATE ONE_SHOT_EXECUTION_AUTHORIZATIONS")
    assert own[2].startswith("UPDATE EXECUTOR_LAUNCH_INTENTS")
    assert own[3].startswith("INSERT INTO ONE_SHOT_AUTHORIZATION_CLAIMS")
    assert own[4] == "COMMIT"
    # The schema inspection that preceded the claim ran on other connections.
    assert any(
        statement.startswith("PRAGMA")
        for conn, statement in statements
        if conn is not claim_connection
    )


# 2 ---------------------------------------------------------------------------


def test_duplicate_claim_performs_no_second_transition(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    first = claim_one_shot_authorization(db, identity)
    before = _state(path, issued.authorization_uuid)

    second = claim_one_shot_authorization(db, identity)

    assert second.outcome is ClaimOutcome.ALREADY_CLAIMED
    assert not second.handoff_permitted
    assert second.claim_uuid == first.claim_uuid
    assert second.attempt_idempotency_key == issued.attempt_idempotency_key
    assert _state(path, issued.authorization_uuid) == before


# 3, 8 (concurrency) ----------------------------------------------------------


@pytest.mark.parametrize("claimants", [2, 8])
def test_concurrent_independent_connections_claim_exactly_once(f1b, claimants):
    path, _, db = f1b
    identity, issued = _issue(db)
    barrier = threading.Barrier(claimants)
    results = []
    errors = []

    def _claimant():
        engine = _engine(path)
        session = sessionmaker(bind=engine)()
        try:
            barrier.wait()
            results.append(claim_one_shot_authorization(session, identity))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            session.close()
            engine.dispose()

    threads = [threading.Thread(target=_claimant) for _ in range(claimants)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(results) == claimants
    winners = [r for r in results if r.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY]
    losers = [r for r in results if r not in winners]
    assert len(winners) == 1
    assert all(
        r.outcome in {ClaimOutcome.ALREADY_CLAIMED, ClaimOutcome.DATABASE_BUSY}
        for r in losers
    )
    assert not any(r.handoff_permitted for r in losers)
    _assert_claimed(path, issued, winners[0].claim_uuid)


def test_concurrent_revocation_and_claim_end_in_one_legal_pair(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    barrier = threading.Barrier(2)
    outcomes = {}

    def _claim():
        engine = _engine(path)
        session = sessionmaker(bind=engine)()
        try:
            barrier.wait()
            outcomes["claim"] = claim_one_shot_authorization(session, identity)
        finally:
            session.close()
            engine.dispose()

    def _revoke():
        engine = _engine(path)
        session = sessionmaker(bind=engine)()
        try:
            barrier.wait()
            outcomes["revoke"] = revoke_authorization(
                session,
                identity.authorization_uuid,
                operator_identity="operator@example.test",
            )
        except (AuthorizationStateConflict, SQLAlchemyError) as exc:
            outcomes["revoke"] = exc
        finally:
            session.close()
            engine.dispose()

    threads = [threading.Thread(target=_claim), threading.Thread(target=_revoke)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    auth, intents, receipts = _state(path, issued.authorization_uuid)
    claim = outcomes["claim"]
    if auth[0] == "CLAIMED":
        assert claim.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
        assert intents[0][0] == "READY" and len(receipts) == 1
        assert not hasattr(outcomes["revoke"], "state")
    else:
        assert auth[0] == "REVOKED"
        assert claim.outcome in {ClaimOutcome.REVOKED, ClaimOutcome.DATABASE_BUSY}
        assert intents[0][0] == "NOT_DISPATCHABLE" and receipts == []


# 4, 5 ------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=1)])
def test_expired_authorization_is_not_claimed(f1b, monkeypatch, offset):
    path, _, db = f1b
    identity, issued = _issue(db)
    _clock(monkeypatch, identity.expires_at + offset)

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.EXPIRED
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)


def test_claim_just_before_expiry_succeeds(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    _clock(monkeypatch, identity.expires_at - timedelta(microseconds=1))

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    _assert_claimed(path, issued, result.claim_uuid)


def test_expiry_reached_before_commit_rolls_back_everything(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    calls = _clock(
        monkeypatch,
        identity.expires_at - timedelta(microseconds=1),
        identity.expires_at,
    )

    result = claim_one_shot_authorization(db, identity)

    assert len(calls) == 2
    assert result.outcome is ClaimOutcome.EXPIRED
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)


def test_clock_is_sampled_only_after_write_lock_is_held(f1b, monkeypatch):
    _, engine, db = f1b
    identity, _ = _issue(db)
    events: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        events.append(statement.strip().split()[0].upper() + " " + statement[:40])

    def _now():
        events.append("CLOCK")
        return datetime.now(timezone.utc)

    monkeypatch.setattr(one_shot_claim, "_trusted_utc_now", _now)
    try:
        result = claim_one_shot_authorization(db, identity)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    begin = next(i for i, e in enumerate(events) if "BEGIN IMMEDIATE" in e)
    first_clock = events.index("CLOCK")
    claim_update = next(
        i for i, e in enumerate(events) if "UPDATE one_shot_execution_auth" in e
    )
    commit = next(i for i, e in enumerate(events) if e.startswith("COMMIT"))
    assert begin < first_clock < claim_update
    assert (
        events.count("CLOCK") == 2 and events.index("CLOCK", first_clock + 1) < commit
    )


def test_waiting_for_the_write_lock_past_expiry_returns_expired(f1b):
    path, _, db = f1b
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    identity = _identity(*_graph(db), expires_at=expires_at)
    issued = issue_one_shot_authorization(db, identity)
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    assert holder.in_transaction
    released = threading.Event()
    release_started: list[datetime] = []

    def _release():
        release_started.append(datetime.now(timezone.utc))
        holder.execute("ROLLBACK")
        released.set()

    started = datetime.now(timezone.utc)
    # The claimant must enter before expiry, or a stale pre-lock clock (the
    # original defect) would also yield EXPIRED and this test would not catch it.
    assert started < expires_at
    timer = threading.Timer((expires_at - started).total_seconds() + 0.5, _release)
    timer.start()
    try:
        result = claim_one_shot_authorization(db, identity)
        # Sampled before joining the timer, so it proves the claim itself
        # returned only after the holder began releasing the lock past expiry.
        finished = datetime.now(timezone.utc)
    finally:
        timer.join()
        holder.close()

    assert released.is_set()
    assert release_started[0] > expires_at
    assert finished > release_started[0]
    assert result.outcome is ClaimOutcome.EXPIRED
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)


def test_claim_exposes_no_caller_supplied_clock(f1b):
    _, _, db = f1b
    identity, _ = _issue(db)
    assert list(inspect.signature(claim_one_shot_authorization).parameters) == [
        "db",
        "identity",
    ]
    with pytest.raises(TypeError):
        claim_one_shot_authorization(
            db, identity, now=identity.issued_at  # type: ignore[call-arg]
        )


def test_revoked_authorization_is_not_claimed(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    revoke_authorization(
        db, issued.authorization_uuid, operator_identity="operator@example.test"
    )

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.REVOKED
    auth, intents, receipts = _state(path, issued.authorization_uuid)
    assert auth[0] == "REVOKED" and intents[0][0] == "NOT_DISPATCHABLE"
    assert receipts == []


def test_claimed_authorization_cannot_be_revoked_or_expired(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    result = claim_one_shot_authorization(db, identity)

    with pytest.raises(AuthorizationStateConflict):
        revoke_authorization(
            db, issued.authorization_uuid, operator_identity="operator@example.test"
        )
    inspected = one_shot_authorization.inspect_authorization(
        db, issued.authorization_uuid, now=identity.expires_at + timedelta(days=1)
    )
    assert inspected.state == "CLAIMED" and inspected.launch_state == "READY"
    _assert_claimed(path, issued, result.claim_uuid)


# 6-9 identity mismatches ------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("session_instance_id", "00000000-0000-4000-8000-000000000001"),
        ("source_snapshot_digest", "d" * 64),
        ("candidate_initial_digest", "d" * 64),
        ("candidate_uuid", "00000000-0000-4000-8000-000000000002"),
        ("source_revision_identity", "git:other"),
        ("operation_scope_digest", "d" * 64),
        ("operation_scope_id", "other-scope"),
        ("operation_scope_version", "2"),
        ("operator_identity", "other@example.test"),
    ],
)
def test_mismatched_identity_field_is_identity_conflict(f1b, field, value):
    path, _, db = f1b
    identity, issued = _issue(db)

    result = claim_one_shot_authorization(db, replace(identity, **{field: value}))

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)


def test_wrong_task_execution_is_identity_conflict(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    other = TaskExecution(
        session_id=identity.session_id,
        task_id=identity.task_id,
        attempt_number=2,
        status=TaskStatus.PENDING,
    )
    db.add(other)
    db.commit()

    result = claim_one_shot_authorization(
        db, replace(identity, task_execution_id=other.id)
    )

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    _assert_unclaimed(path, issued)


def test_live_session_generation_change_blocks_claim(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    session = db.get(SessionModel, identity.session_id)
    session.instance_id = "00000000-0000-4000-8000-000000000003"
    db.commit()

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    _assert_unclaimed(path, issued)


def test_live_task_profile_change_blocks_claim(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    task = db.get(Task, identity.task_id)
    task.execution_profile = "default"
    db.commit()

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    _assert_unclaimed(path, issued)


def test_unknown_authorization_is_identity_conflict(f1b):
    _, _, db = f1b
    identity = _identity(*_graph(db))

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    assert result.authorization_state is None


# 10, 11 intent conflicts -------------------------------------------------------


def test_wrong_stored_request_hash_is_intent_conflict(f1b, monkeypatch):
    path, _, db = f1b
    original = one_shot_authorization._intent_identity

    def _tampered(identity, identity_sha):
        intent_uuid, key, _ = original(identity, identity_sha)
        return intent_uuid, key, "e" * 64

    monkeypatch.setattr(one_shot_authorization, "_intent_identity", _tampered)
    identity, issued = _issue(db)
    monkeypatch.setattr(one_shot_authorization, "_intent_identity", original)
    assert issued.request_payload_sha256 == "e" * 64

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.INTENT_CONFLICT
    _assert_unclaimed(path, issued)


def test_missing_launch_intent_is_intent_conflict(f1b):
    path, _, db = f1b
    identity = _identity(*_graph(db))
    values = {
        field: getattr(identity, field)
        for field in one_shot_authorization._IDENTITY_FIELDS
    }
    db.add(
        OneShotExecutionAuthorization(
            **values,
            identity_sha256=one_shot_authorization.canonical_identity_sha256(identity),
            state="AUTHORIZED",
            created_at=identity.issued_at,
        )
    )
    db.commit()

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.INTENT_CONFLICT
    auth, intents, receipts = _state(path, identity.authorization_uuid)
    assert auth == ("AUTHORIZED", None, None)
    assert intents == [] and receipts == []


# 12 busy ---------------------------------------------------------------------


def test_locked_database_fails_closed_as_busy(f1b, tmp_path):
    path, _, db = f1b
    identity, issued = _issue(db)
    short = _engine(path, timeout=0.2)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        result = claim_one_shot_authorization(sessionmaker(bind=short)(), identity)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert result.outcome is ClaimOutcome.DATABASE_BUSY
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)
    retried = claim_one_shot_authorization(sessionmaker(bind=short)(), identity)
    short.dispose()
    assert retried.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    _assert_claimed(path, issued, retried.claim_uuid)


# F-04 typed operational outcomes ----------------------------------------------


def test_exhausted_connection_pool_is_database_busy(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    small = create_engine(
        f"sqlite:///{path}", pool_size=1, max_overflow=0, pool_timeout=0.2
    )
    held = small.connect()
    try:
        result = claim_one_shot_authorization(sessionmaker(bind=small)(), identity)
    finally:
        held.close()
        small.dispose()

    assert result.outcome is ClaimOutcome.DATABASE_BUSY
    assert not result.handoff_permitted
    _assert_unclaimed(path, issued)


def test_database_guard_refusing_the_claim_is_identity_conflict(f1b, monkeypatch):
    path, engine, db = f1b
    identity, issued = _issue(db)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER test_refuse_claim BEFORE UPDATE ON "
            "one_shot_execution_authorizations WHEN NEW.state = 'CLAIMED' "
            "BEGIN SELECT RAISE(ABORT, 'test guard'); END"
        )
    # F-05: the exact 058 verifier refuses the unverified trigger before the
    # claim starts; bypass only that pre-check to reach the in-transaction guard.
    assert (
        claim_one_shot_authorization(db, identity).outcome
        is ClaimOutcome.SCHEMA_UNAVAILABLE
    )
    monkeypatch.setattr(one_shot_claim, "_schema_outcome", lambda engine: None)

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.IDENTITY_CONFLICT
    _assert_unclaimed(path, issued)


def test_unexpected_database_error_stays_visible_and_rolls_back(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)

    def _io_error(*_args):
        raise OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(one_shot_claim, "_transition_intent", _io_error)
    with pytest.raises(OperationalError, match="disk I/O error"):
        claim_one_shot_authorization(db, identity)
    _assert_unclaimed(path, issued)


# 13-16 rollback and crash points ----------------------------------------------


def test_rollback_before_commit_leaves_both_rows_unchanged(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    monkeypatch.setattr(one_shot_claim, "_transition_intent", lambda *_: 0)

    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.INTENT_CONFLICT
    _assert_unclaimed(path, issued)


@pytest.mark.parametrize(
    "crash_point", ["_transition_intent", "_insert_claim_receipt", "_commit_claim"]
)
def test_crash_before_commit_rolls_back_and_same_claim_can_proceed(
    f1b, monkeypatch, crash_point
):
    path, _, db = f1b
    identity, issued = _issue(db)
    original = getattr(one_shot_claim, crash_point)

    def _crash(*_args, **_kwargs):
        raise _SimulatedCrash(crash_point)

    monkeypatch.setattr(one_shot_claim, crash_point, _crash)
    with pytest.raises(_SimulatedCrash):
        claim_one_shot_authorization(db, identity)
    _assert_unclaimed(path, issued)

    monkeypatch.setattr(one_shot_claim, crash_point, original)
    result = claim_one_shot_authorization(db, identity)
    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    _assert_claimed(path, issued, result.claim_uuid)


def test_crash_after_commit_is_reconciled_without_new_identity(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    original = one_shot_claim._commit_claim

    def _commit_then_crash(connection):
        original(connection)
        raise _SimulatedCrash("after commit")

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _commit_then_crash)
    with pytest.raises(_SimulatedCrash):
        claim_one_shot_authorization(db, identity)
    monkeypatch.setattr(one_shot_claim, "_commit_claim", original)

    state = reconcile_one_shot_claim(db, identity)
    assert state.durable_ready and state.identity_verified and state.intent_verified
    assert state.externally_dispatched is False
    assert state.attempt_idempotency_key == issued.attempt_idempotency_key
    _assert_claimed(path, issued, state.claim_uuid)

    again = claim_one_shot_authorization(db, identity)
    assert again.outcome is ClaimOutcome.ALREADY_CLAIMED
    assert again.claim_uuid == state.claim_uuid
    _assert_claimed(path, issued, state.claim_uuid)


# 17-19 commit exceptions ----------------------------------------------------


def test_commit_exception_with_committed_state_reconciles_success(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    original = one_shot_claim._commit_claim

    def _commit_then_raise(connection):
        original(connection)
        raise _commit_error()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _commit_then_raise)
    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    assert result.reconciled and result.handoff_permitted
    assert result.attempt_idempotency_key == issued.attempt_idempotency_key
    _assert_claimed(path, issued, result.claim_uuid)


def test_commit_exception_with_rolled_back_state_holds(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)

    def _raise(connection):
        raise _commit_error()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _raise)
    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.COMMIT_UNCERTAIN
    assert result.reconciled and not result.handoff_permitted
    assert result.authorization_state == "AUTHORIZED"
    assert result.launch_state == "NOT_DISPATCHABLE"
    _assert_unclaimed(path, issued)


def test_commit_exception_never_adopts_a_rival_claim(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    original_commit = one_shot_claim._commit_claim
    original_reconcile = one_shot_claim._reconcile_after_commit_failure
    rival = {}

    def _raise(connection):
        raise _commit_error()

    def _rival_then_reconcile(engine, expected, claim_uuid):
        monkeypatch.setattr(one_shot_claim, "_commit_claim", original_commit)
        rival["result"] = claim_one_shot_authorization(db, identity)
        return original_reconcile(engine, expected, claim_uuid)

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _raise)
    monkeypatch.setattr(
        one_shot_claim, "_reconcile_after_commit_failure", _rival_then_reconcile
    )
    result = claim_one_shot_authorization(db, identity)

    assert rival["result"].outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    assert result.outcome is ClaimOutcome.ALREADY_CLAIMED
    assert result.reconciled and not result.handoff_permitted
    _assert_claimed(path, issued, rival["result"].claim_uuid)


def test_commit_exception_with_unavailable_database_holds(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)

    def _raise(*_args):
        raise _commit_error()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _raise)
    monkeypatch.setattr(one_shot_claim, "_read_snapshot", _raise)
    result = claim_one_shot_authorization(db, identity)

    assert result.outcome is ClaimOutcome.COMMIT_UNCERTAIN
    assert not result.reconciled and not result.handoff_permitted
    _assert_unclaimed(path, issued)


# 21-23 migration --------------------------------------------------------------


def _f1a_shaped_database(path: Path):
    """Create the exact F1a (057) intent CHECK and no 058 receipt table."""

    engine = _engine(path)
    Base.metadata.create_all(
        engine,
        tables=[
            table
            for table in Base.metadata.sorted_tables
            if table.name not in ONE_SHOT_TABLES
        ],
    )
    OneShotExecutionAuthorization.__table__.create(engine)
    intent_table = ExecutorLaunchIntent.__table__
    ddl = str(CreateTable(intent_table).compile(engine))
    widened = "CHECK (state IN ('NOT_DISPATCHABLE', 'READY'))"
    assert widened in ddl
    with engine.begin() as connection:
        connection.exec_driver_sql(
            ddl.replace(widened, "CHECK (state = 'NOT_DISPATCHABLE')")
        )
    for index in intent_table.indexes:
        index.create(engine)
    run_schema_migrations(engine, (MIGRATION_057,))
    return engine


def _intent_check_sql(engine) -> str:
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'executor_launch_intents'"
        ).scalar_one()


def test_migration_from_f1a_preserves_intent_and_enables_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f1a.db"
    engine = _f1a_shaped_database(path)
    db = sessionmaker(bind=engine)()
    try:
        identity, issued = _issue(db)
        assert "'READY'" not in _intent_check_sql(engine)
        assert (
            claim_one_shot_authorization(db, identity).outcome
            is ClaimOutcome.SCHEMA_UNAVAILABLE
        )

        run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))

        assert "'READY'" in _intent_check_sql(engine)
        _assert_unclaimed(path, issued)
        result = claim_one_shot_authorization(db, identity)
        assert result.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
        _assert_claimed(path, issued, result.claim_uuid)
    finally:
        db.close()
        engine.dispose()


def test_failed_058_upgrade_rolls_back_rebuild_without_recording(tmp_path, monkeypatch):
    import app.db_migrations as db_migrations

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f1a-failed.db"
    engine = _f1a_shaped_database(path)
    db = sessionmaker(bind=engine)()
    try:
        _, issued = _issue(db)
        original = db_migrations._058_trigger_statements
        monkeypatch.setattr(
            db_migrations,
            "_058_trigger_statements",
            lambda: original() + ("SELECT * FROM no_such_table_058",),
        )
        with pytest.raises(SQLAlchemyError):
            run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))

        assert "'READY'" not in _intent_check_sql(engine)
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            recorded = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (MIGRATION_058.version,),
            ).scalar_one()
        assert "executor_launch_intents_058_staging" not in tables
        assert "one_shot_authorization_claims" not in tables
        assert recorded == 0
        with sqlite3.connect(path) as raw:
            assert raw.execute(
                "SELECT state, attempt_idempotency_key FROM executor_launch_intents"
            ).fetchall() == [("NOT_DISPATCHABLE", issued.attempt_idempotency_key)]
    finally:
        db.close()
        engine.dispose()


def test_failed_058_keeps_original_error_when_transaction_already_ended(
    tmp_path, monkeypatch
):
    import app.db_migrations as db_migrations

    engine = _f1a_shaped_database(tmp_path / "f1a-ended.db")
    original = db_migrations._058_trigger_statements
    monkeypatch.setattr(
        db_migrations,
        "_058_trigger_statements",
        lambda: original() + ("COMMIT", "SELECT * FROM no_such_table_058"),
    )
    try:
        with pytest.raises(SQLAlchemyError, match="no_such_table_058"):
            run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
        with engine.connect() as connection:
            recorded = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (MIGRATION_058.version,),
            ).scalar_one()
        assert recorded == 0
    finally:
        engine.dispose()


def test_fresh_create_all_then_full_migration_chain_verifies(tmp_path):
    engine = _engine(tmp_path / "fresh.db")
    try:
        Base.metadata.create_all(bind=engine)
        run_schema_migrations(engine)
        with engine.connect() as connection:
            versions = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT version FROM schema_migrations"
                )
            }
        assert {MIGRATION_057.version, MIGRATION_058.version} <= versions
        assert "'READY'" in _intent_check_sql(engine)
    finally:
        engine.dispose()


def test_migration_replay_reverifies_and_rejects_a_missing_guard(f1b):
    _, engine, _ = f1b
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    with engine.begin() as connection:
        recorded = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (MIGRATION_058.version,),
        ).scalar_one()
        connection.exec_driver_sql("DROP TRIGGER trg_one_shot_claim_requires_ready")
    assert recorded == 1
    with pytest.raises(RuntimeError, match="058 one-shot claim trigger"):
        run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))


# F-05 exact schema verification -----------------------------------------------


def test_sql_tokenizer_covers_unicode_and_preserves_literals_and_identifiers():
    sql = (
        "alpha \t\u00a0beta\n\u0085gamma\u2028delta "
        "'s\u00a0t' \"q\u0085r\" `b\u2028c` [br\u00a0acket]"
    )
    matches = list(_SQL_TOKEN.finditer(sql))
    cursor = 0
    for match in matches:
        assert set(sql[cursor : match.start()]) <= set(" \t\n\f\r")
        cursor = match.end()
    assert set(sql[cursor:]) <= set(" \t\n\f\r")
    assert _sql_tokens(sql) == [
        "alpha",
        "\u00a0",
        "beta",
        "\u0085",
        "gamma",
        "\u2028",
        "delta",
        "'s\u00a0t'",
        '"q\u0085r"',
        "`b\u2028c`",
        "[br\u00a0acket]",
    ]


def _init_db(engine, monkeypatch):
    """Run the real app.database.init_db() against a temporary engine."""

    import app.database as database

    monkeypatch.setattr(database, "engine", engine)
    database.init_db()


def _one_shot_master(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name IN (?, ?, ?) ORDER BY type, name",
            tuple(sorted(ONE_SHOT_TABLES)),
        ).fetchall()


def _one_shot_rows(path: Path):
    raw = sqlite3.connect(path)
    try:
        return {
            table: sorted(raw.execute(f"SELECT * FROM {table}").fetchall())
            for table in sorted(ONE_SHOT_TABLES)
        }
    finally:
        raw.close()


def _schema_shape(path: Path, monkeypatch, shape: str):
    """Build the fresh-create or the 057-rebuilt 058 schema through init_db().

    Both shapes hold one issued authorization; the migrated shape issues it
    before 058 runs, so the rebuild has a real intent row to preserve.
    """

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    if shape == "fresh":
        engine = _engine(path)
        _init_db(engine, monkeypatch)
    else:
        engine = _f1a_shaped_database(path)
    db = sessionmaker(bind=engine)()
    try:
        _issue(db)
    finally:
        db.close()
    if shape == "migrated":
        _init_db(engine, monkeypatch)
    return engine


@pytest.mark.parametrize("shape", ["fresh", "migrated"])
def test_init_db_schema_verifies_exactly_and_replays_stably(
    tmp_path, monkeypatch, shape
):
    path = tmp_path / f"{shape}.db"
    engine = _schema_shape(path, monkeypatch, shape)
    try:
        _verify_058_exact_sqlite_schema(engine)
        schema, rows = _one_shot_master(engine), _one_shot_rows(path)
        assert len(rows["executor_launch_intents"]) == 1

        _init_db(engine, monkeypatch)

        assert _one_shot_master(engine) == schema
        assert _one_shot_rows(path) == rows
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                    (MIGRATION_058.version,),
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_migrated_schema_differs_from_fresh_only_by_m1_02_forms(tmp_path, monkeypatch):
    fresh = _schema_shape(tmp_path / "fresh.db", monkeypatch, "fresh")
    migrated = _schema_shape(tmp_path / "migrated.db", monkeypatch, "migrated")
    try:
        f = {(row[0], row[1]): row for row in _one_shot_master(fresh)}
        m = {(row[0], row[1]): row for row in _one_shot_master(migrated)}
    finally:
        fresh.dispose()
        migrated.dispose()
    table = ("table", "executor_launch_intents")
    trigger = ("trigger", "trg_executor_launch_intent_no_delete")
    assert set(f) == set(m)
    assert {key for key in f if f[key] != m[key]} == {table, trigger}
    # SQLite quotes the name when 058 renames the staging table.
    assert f[table][3].startswith("CREATE TABLE executor_launch_intents (")
    assert m[table][3].startswith('CREATE TABLE "executor_launch_intents" (')
    assert f[table][3][len("CREATE TABLE executor_launch_intents") :] == (
        m[table][3][len('CREATE TABLE "executor_launch_intents"') :]
    )
    # 057's text survives a fresh build; the rebuild recreates 058's text.
    assert f[trigger][3] != m[trigger][3]
    assert f[trigger][3].split() == m[trigger][3].split()


def test_exact_verification_is_read_only(tmp_path, monkeypatch):
    path = tmp_path / "ro.db"
    _schema_shape(path, monkeypatch, "migrated").dispose()
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA query_only=ON")

    try:
        MIGRATION_057.verify(engine)
        MIGRATION_058.verify(engine)
        with pytest.raises(OperationalError, match="readonly"):
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE f05_write_probe (x)")
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def one_shot_schema_templates(tmp_path_factory):
    """One disposable database per schema shape; each mutant copies one."""

    root = tmp_path_factory.mktemp("f05-templates")
    templates = {}
    with pytest.MonkeyPatch.context() as monkeypatch:
        for shape in ("fresh", "migrated"):
            path = root / f"{shape}.db"
            engine = _schema_shape(path, monkeypatch, shape)
            _verify_058_exact_sqlite_schema(engine)
            engine.dispose()
            assert not Path(f"{path}-wal").exists()
            templates[shape] = path
    return templates


_TRIGGER = "058 one-shot claim trigger is missing or invalid: "
_TABLE = "058 one-shot claim table is missing or invalid: "
_INDEX = "058 one-shot claim index is missing or invalid: "
_UNEXPECTED = "058 one-shot claim schema has an unexpected "
# (label, object, old text or None to drop, new text, exact-verifier error).
# With object None, the new text is executed to create an extra object.
F05_MUTANTS = (
    (
        "when0-receipt-requires-ready",
        "trg_one_shot_claim_requires_ready",
        "WHEN NOT EXISTS",
        "WHEN 0 AND NOT EXISTS",
        _TRIGGER + "trg_one_shot_claim_requires_ready",
    ),
    (
        "when0-receipt-no-delete",
        "trg_one_shot_claim_no_delete",
        "BEGIN",
        "WHEN 0 BEGIN",
        _TRIGGER + "trg_one_shot_claim_no_delete",
    ),
    (
        "when0-057-authorization-no-delete",
        "trg_one_shot_auth_no_delete",
        "BEGIN",
        "WHEN 0 BEGIN",
        _TRIGGER + "trg_one_shot_auth_no_delete",
    ),
    (
        "trigger-removed",
        "trg_executor_launch_intent_no_delete",
        None,
        None,
        _TRIGGER + "trg_executor_launch_intent_no_delete",
    ),
    (
        "raise-removed",
        "trg_one_shot_claim_immutable",
        "SELECT RAISE(ABORT, 'one-shot claim receipt is immutable');",
        "SELECT 1;",
        _TRIGGER + "trg_one_shot_claim_immutable",
    ),
    (
        "raise-abort-to-ignore",
        "trg_executor_launch_intent_no_delete",
        "RAISE(ABORT, 'executor launch intent evidence is retained')",
        "RAISE(IGNORE)",
        _TRIGGER + "trg_executor_launch_intent_no_delete",
    ),
    (
        "protected-column-unguarded",
        "trg_executor_launch_intent_immutable",
        "NEW.task_execution_id IS NOT OLD.task_execution_id OR",
        "",
        _TRIGGER + "trg_executor_launch_intent_immutable",
    ),
    (
        "transition-guard-and-to-or",
        "trg_executor_launch_intent_immutable",
        "OLD.state = 'NOT_DISPATCHABLE' AND NEW.state = 'READY'",
        "OLD.state = 'NOT_DISPATCHABLE' OR NEW.state = 'READY'",
        _TRIGGER + "trg_executor_launch_intent_immutable",
    ),
    (
        # Keeps every substring the pre-F-05 verifier looked for.
        "expiry-guard-bypassed-substring-preserving",
        "trg_one_shot_auth_immutable_state_guard",
        "NEW.state_changed_at < OLD.expires_at",
        "NEW.state_changed_at < OLD.expires_at OR 1",
        _TRIGGER + "trg_one_shot_auth_immutable_state_guard",
    ),
    (
        "string-literal-to-quoted-identifier",
        "trg_one_shot_claim_requires_ready",
        "auth.state = 'CLAIMED'",
        'auth.state = "CLAIMED"',
        _TRIGGER + "trg_one_shot_claim_requires_ready",
    ),
    (
        # Equivalent SQL, but quoting is accepted only for the renamed table.
        "quoted-identifier-outside-allowance",
        "trg_one_shot_claim_immutable",
        "ON one_shot_authorization_claims",
        'ON "one_shot_authorization_claims"',
        _TRIGGER + "trg_one_shot_claim_immutable",
    ),
    (
        # Keeps the 'READY' substring the pre-F-05 verifier looked for.
        "intent-state-check-widened",
        "executor_launch_intents",
        "CHECK (state IN ('NOT_DISPATCHABLE', 'READY'))",
        "CHECK (state IN ('NOT_DISPATCHABLE', 'READY', 'DISPATCHED'))",
        _TABLE + "executor_launch_intents",
    ),
    (
        "receipt-digest-check-weakened",
        "one_shot_authorization_claims",
        "CHECK (length(identity_sha256) = 64 AND identity_sha256 = lower(identity_sha256))",
        "CHECK (length(identity_sha256) = 64)",
        _TABLE + "one_shot_authorization_claims",
    ),
    (
        "authorization-terminal-shape-weakened",
        "one_shot_execution_authorizations",
        "(state = 'CLAIMED' AND state_changed_at IS NOT NULL)",
        "(state = 'CLAIMED')",
        _TABLE + "one_shot_execution_authorizations",
    ),
    (
        "receipt-foreign-key-cascade",
        "one_shot_authorization_claims",
        "REFERENCES executor_launch_intents (intent_uuid) ON DELETE RESTRICT",
        "REFERENCES executor_launch_intents (intent_uuid) ON DELETE CASCADE",
        _TABLE + "one_shot_authorization_claims",
    ),
    (
        "intent-index-removed",
        "ix_executor_launch_intents_state_created",
        None,
        None,
        _INDEX + "ix_executor_launch_intents_state_created",
    ),
    (
        "authorization-index-removed",
        "ix_one_shot_auth_candidate",
        None,
        None,
        _INDEX + "ix_one_shot_auth_candidate",
    ),
    (
        "intent-index-column-order",
        "ix_executor_launch_intents_state_created",
        "(state, created_at)",
        "(created_at, state)",
        _INDEX + "ix_executor_launch_intents_state_created",
    ),
    (
        "intent-index-made-partial",
        "ix_executor_launch_intents_state_created",
        "(state, created_at)",
        "(state, created_at) WHERE state = 'READY'",
        _INDEX + "ix_executor_launch_intents_state_created",
    ),
    (
        # Would silently drop receipt inserts while the claim still commits.
        "unexpected-receipt-trigger",
        None,
        None,
        "CREATE TRIGGER trg_f05_extra BEFORE INSERT ON one_shot_authorization_claims "
        "BEGIN SELECT RAISE(IGNORE); END",
        _UNEXPECTED + "trigger: trg_f05_extra",
    ),
    (
        "unexpected-intent-index",
        None,
        None,
        "CREATE INDEX ix_f05_extra ON executor_launch_intents (created_at)",
        _UNEXPECTED + "index: ix_f05_extra",
    ),
    (
        "unicode-nbsp-inside-authorization-state-check",
        "one_shot_execution_authorizations",
        "state IN",
        "state\u00a0IN",
        _TABLE + "one_shot_execution_authorizations",
    ),
    (
        "unicode-nbsp-between-as-and-alias",
        "trg_one_shot_claim_requires_ready",
        "AS auth",
        "AS\u00a0auth",
        _TRIGGER + "trg_one_shot_claim_requires_ready",
    ),
    (
        "unicode-nel-inside-intent-state-check",
        "executor_launch_intents",
        "state IN",
        "state\u0085IN",
        _TABLE + "executor_launch_intents",
    ),
    (
        "unicode-line-separator-inside-authorization-state-check",
        "one_shot_execution_authorizations",
        "state IN",
        "state\u2028IN",
        _TABLE + "one_shot_execution_authorizations",
    ),
)


def _mutate_one_shot_schema(path: Path, name, old, new) -> None:
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        if name is None:
            raw.execute(new)
            return
        kind, sql = raw.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        if old is None:
            raw.execute(f"DROP {kind.upper()} {name}")
            return
        assert sql.count(old) == 1, (name, old)
        mutated = sql.replace(old, new)
        if kind == "table":
            raw.execute("PRAGMA writable_schema=ON")
            raw.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
                (mutated, name),
            )
            raw.execute("PRAGMA writable_schema=OFF")
        else:
            raw.execute(f"DROP {kind.upper()} {name}")
            raw.execute(mutated)
    finally:
        raw.close()


@pytest.mark.parametrize("shape", ["fresh", "migrated"])
@pytest.mark.parametrize("mutant", F05_MUTANTS, ids=[m[0] for m in F05_MUTANTS])
def test_exact_verifier_rejects_weakened_one_shot_schema(
    one_shot_schema_templates, tmp_path, shape, mutant
):
    _, name, old, new, error = mutant
    path = tmp_path / "mutant.db"
    shutil.copyfile(one_shot_schema_templates[shape], path)
    before = _one_shot_master_raw(path)
    _mutate_one_shot_schema(path, name, old, new)
    assert _one_shot_master_raw(path) != before
    engine = _engine(path)
    try:
        with pytest.raises(RuntimeError) as rejected:
            _verify_058_exact_sqlite_schema(engine)
        assert str(rejected.value) == error
        # The startup replay path refuses it too (057 or 058 verification).
        with pytest.raises(RuntimeError, match=r"^05[78] one-shot"):
            run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    finally:
        engine.dispose()


def _one_shot_master_raw(path: Path):
    raw = sqlite3.connect(path)
    try:
        return raw.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        raw.close()


def test_claim_refuses_unverified_or_unsupported_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    memory = create_engine("sqlite://")
    Base.metadata.create_all(memory)
    db = sessionmaker(bind=memory)()
    identity = _identity(*_graph(db))
    try:
        result = claim_one_shot_authorization(db, identity)
        assert result.outcome is ClaimOutcome.UNSUPPORTED_DATABASE
    finally:
        db.close()
        memory.dispose()

    unmigrated = _engine(tmp_path / "unmigrated.db")
    Base.metadata.create_all(unmigrated)
    db = sessionmaker(bind=unmigrated)()
    try:
        result = claim_one_shot_authorization(db, identity)
        assert result.outcome is ClaimOutcome.SCHEMA_UNAVAILABLE
    finally:
        db.close()
        unmigrated.dispose()


# 24, 25 database guards ---------------------------------------------------------


def _raw_fails(path: Path, statement: str, parameters=()):
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(statement, parameters)


def test_claimed_ready_and_receipt_rows_are_immutable(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    result = claim_one_shot_authorization(db, identity)
    uuid_ = issued.authorization_uuid

    for statement in (
        "UPDATE one_shot_execution_authorizations SET state = 'AUTHORIZED', "
        "state_changed_at = NULL WHERE authorization_uuid = ?",
        "UPDATE one_shot_execution_authorizations SET state = 'REVOKED', "
        "revoked_by = 'x' WHERE authorization_uuid = ?",
        "UPDATE one_shot_execution_authorizations SET candidate_initial_digest = "
        "'" + "d" * 64 + "' WHERE authorization_uuid = ?",
        "UPDATE executor_launch_intents SET state = 'NOT_DISPATCHABLE' "
        "WHERE authorization_uuid = ?",
        "UPDATE executor_launch_intents SET attempt_idempotency_key = "
        "'" + "d" * 64 + "' WHERE authorization_uuid = ?",
        "UPDATE executor_launch_intents SET state = 'READY' "
        "WHERE authorization_uuid = ?",
        "DELETE FROM executor_launch_intents WHERE authorization_uuid = ?",
        "DELETE FROM one_shot_execution_authorizations WHERE authorization_uuid = ?",
        "UPDATE one_shot_authorization_claims SET claimed_at = claimed_at "
        "WHERE authorization_uuid = ?",
        "DELETE FROM one_shot_authorization_claims WHERE authorization_uuid = ?",
    ):
        _raw_fails(path, statement, (uuid_,))
    _assert_claimed(path, issued, result.claim_uuid)


def test_illegal_transitions_are_rejected_by_sqlite_guards(f1b):
    path, _, db = f1b
    identity, issued = _issue(db)
    uuid_ = issued.authorization_uuid
    late = (identity.expires_at + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    on_time = identity.issued_at.strftime("%Y-%m-%d %H:%M:%S.%f")

    # READY before CLAIMED, CLAIMED after expiry or with a revoker, and a
    # receipt without CLAIMED/READY are all refused.
    _raw_fails(
        path,
        "UPDATE executor_launch_intents SET state = 'READY' "
        "WHERE authorization_uuid = ?",
        (uuid_,),
    )
    _raw_fails(
        path,
        "UPDATE one_shot_execution_authorizations SET state = 'CLAIMED', "
        "state_changed_at = ? WHERE authorization_uuid = ?",
        (late, uuid_),
    )
    _raw_fails(
        path,
        "UPDATE one_shot_execution_authorizations SET state = 'CLAIMED', "
        "state_changed_at = ?, revoked_by = 'x' WHERE authorization_uuid = ?",
        (on_time, uuid_),
    )
    _raw_fails(
        path,
        "UPDATE executor_launch_intents SET state = 'SENDING' "
        "WHERE authorization_uuid = ?",
        (uuid_,),
    )
    _raw_fails(
        path,
        "INSERT INTO one_shot_authorization_claims (claim_uuid, authorization_uuid, "
        "intent_uuid, task_execution_id, identity_sha256, attempt_idempotency_key, "
        "request_payload_sha256, claimed_at) SELECT 'forged', authorization_uuid, "
        "intent_uuid, task_execution_id, ?, attempt_idempotency_key, "
        "request_payload_sha256, ? FROM executor_launch_intents "
        "WHERE authorization_uuid = ?",
        (one_shot_authorization.canonical_identity_sha256(identity), on_time, uuid_),
    )
    _assert_unclaimed(path, issued)


# 26, 27 gate and isolation ------------------------------------------------------


class _UntouchableSession:
    def get_bind(self):
        raise AssertionError("disabled claim must not touch the database")


def test_gate_is_disabled_by_default_and_blocks_before_database_access(
    monkeypatch,
):
    monkeypatch.delenv("ONE_SHOT_EXECUTION_ENABLED", raising=False)
    assert Settings(_env_file=None).ONE_SHOT_EXECUTION_ENABLED is False
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", False)
    identity = _identity(
        type("P", (), {"id": 1})(),
        type(
            "S", (), {"id": 1, "instance_id": "00000000-0000-4000-8000-0000000000aa"}
        )(),
        type("T", (), {"id": 1})(),
        type("E", (), {"id": 1})(),
    )
    with pytest.raises(OneShotFeatureDisabled):
        claim_one_shot_authorization(_UntouchableSession(), identity)


def test_disabled_gate_leaves_issued_authorization_unclaimed(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", False)

    with pytest.raises(OneShotFeatureDisabled):
        claim_one_shot_authorization(db, identity)
    _assert_unclaimed(path, issued)


def test_claim_module_has_no_external_launch_imports_or_callers(f1b, monkeypatch):
    module = importlib.import_module("app.services.execution.one_shot_claim")
    syntax = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(syntax)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name.split(".")[0] in {"celery", "subprocess", "requests", "httpx", "socket"}
        or name.startswith(("app.tasks", "app.providers", "app.supervisor", "app.api"))
        for name in imported
    )

    app_root = Path(module.__file__).resolve().parents[2]
    # F1c-1 dispatch and F1c-2 routing are the only claim importers; the
    # canonical Worker reaches the claim only through routing (F1c-2).
    callers = [
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if "tests" not in path.parts
        and path.name
        not in {"one_shot_claim.py", "one_shot_dispatch.py", "one_shot_routing.py"}
        and re.search(
            r"execution\.one_shot_claim|import[^\n]*\bone_shot_claim\b|"
            r"claim_one_shot_authorization|reconcile_one_shot_claim|"
            r"execution\.one_shot_dispatch|import[^\n]*\bone_shot_dispatch\b|"
            r"execution\.one_shot_routing|import[^\n]*\bone_shot_routing\b",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert sorted(callers) == [
        "services/execution/one_shot_supervisor_client.py",
        "services/orchestration/worker_identity.py",
        "tasks/worker.py",
    ]
    # F2: the Supervisor adapter uses dispatch protocol types, never the claim.
    adapter_source = (
        app_root / "services/execution/one_shot_supervisor_client.py"
    ).read_text(encoding="utf-8")
    assert not re.search(
        r"one_shot_claim|claim_one_shot_authorization|reconcile_one_shot_claim|"
        r"dispatch_one_shot_intent|one_shot_routing",
        adapter_source,
    )
    # worker_identity only names the modules in its reviewed identity boundary.
    identity_source = ast.parse(
        (app_root / "services/orchestration/worker_identity.py").read_text(
            encoding="utf-8"
        )
    )
    assert not any(
        "one_shot" in (getattr(node, "module", None) or "")
        or any("one_shot" in alias.name for alias in node.names)
        for node in ast.walk(identity_source)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("claim must not start a process")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    _, _, db = f1b
    identity, _ = _issue(db)
    assert (
        claim_one_shot_authorization(db, identity).outcome
        is ClaimOutcome.CLAIMED_SUCCESSFULLY
    )
