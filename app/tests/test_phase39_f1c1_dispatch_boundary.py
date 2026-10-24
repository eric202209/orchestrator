"""Provider-free temporary-SQLite coverage for the Phase 39 F1c-1 boundary."""

from __future__ import annotations

from dataclasses import replace
import ast
from datetime import timedelta
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import run_schema_migrations
from app.models import Base, Session as SessionModel, TaskExecution, TaskStatus
from app.services.execution import one_shot_claim
from app.services.execution.one_shot_authorization import (
    OneShotFeatureDisabled,
    inspect_authorization,
    revoke_authorization,
)
from app.services.execution.one_shot_claim import (
    ClaimOutcome,
    claim_one_shot_authorization,
    reconcile_one_shot_claim,
)
from app.services.execution.one_shot_dispatch import (
    AttemptStatus,
    DispatchMode,
    DispatchOutcome,
    ExecutorStartRequest,
    InvalidDispatchRequest,
    OneShotDispatchRequest,
    SupervisorAdmission,
    SupervisorAdmissionStatus,
    SupervisorReceipt,
    SupervisorTransportError,
    UnavailableSupervisor,
    WorkerIdentityAttestation,
    dispatch_one_shot_intent,
)
from app.services.orchestration.worker_identity import (
    IDENTITY_INCONCLUSIVE,
    IDENTITY_VERIFIED,
    PIN_ABSENT,
    PIN_MATCHED,
    WorkerBootIdentityManifest,
)
from app.tests.test_phase39_f1b_atomic_claim import (
    MIGRATION_057,
    MIGRATION_058,
    _SimulatedCrash,
    _engine,
    _issue,
    _state,
)

VERIFIED = WorkerIdentityAttestation(IDENTITY_VERIFIED, PIN_MATCHED)
# The accepted production posture after F1b runtime recovery.
PRODUCTION_POSTURE = WorkerIdentityAttestation(
    IDENTITY_INCONCLUSIVE, PIN_ABSENT, ("candidate_pin_unavailable",)
)


@pytest.fixture
def f1b(tmp_path: Path, monkeypatch):
    """Disposable migrated SQLite, as in the F1b suite; gate enabled in-test."""

    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)
    path = tmp_path / "f1c1.db"
    engine = _engine(path)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057, MIGRATION_058))
    db = sessionmaker(autoflush=False, bind=engine)()
    try:
        yield path, engine, db
    finally:
        db.close()
        engine.dispose()


class FakeSupervisor:
    """In-memory Supervisor with durable same-key deduplication.

    ``spawned`` counts processes that would exist; a real executor is never
    involved. Hooks inject lost replies and status failures.
    """

    def __init__(self, *, admission=None, lose_start_reply=False):
        self._admission = admission or SupervisorAdmission(
            SupervisorAdmissionStatus.TRUSTED, durable_deduplication=True
        )
        self.lose_start_reply = lose_start_reply
        self.status_error = False
        self.status_override: AttemptStatus | None = None
        self.before_start = None
        self.lock = threading.Lock()
        self.attempts: dict[str, SupervisorReceipt] = {}
        self.spawned: list[ExecutorStartRequest] = []
        self.calls: list[str] = []

    def admission(self):
        self.calls.append("admission")
        return self._admission

    def get_attempt_status(self, key):
        self.calls.append("status")
        if self.status_error:
            raise SupervisorTransportError("status lookup timed out")
        with self.lock:
            receipt = self.attempts.get(key)
        if self.status_override is not None:
            return SupervisorReceipt(key, self.status_override)
        return receipt or SupervisorReceipt(key, AttemptStatus.NOT_FOUND)

    def start_attempt(self, request):
        self.calls.append("start")
        if self.before_start is not None:
            self.before_start()
        with self.lock:
            existing = self.attempts.get(request.attempt_idempotency_key)
            if existing is not None:
                if (
                    existing.request_payload_sha256 != request.request_payload_sha256
                    or existing.authorization_identity_sha256
                    != request.authorization_identity_sha256
                ):
                    return replace(existing, status=AttemptStatus.REJECTED)
                return replace(existing, deduplicated=True)
            receipt = SupervisorReceipt(
                request.attempt_idempotency_key,
                AttemptStatus.ACCEPTED,
                request.request_payload_sha256,
                request.authorization_identity_sha256,
                f"attempt-{len(self.spawned) + 1}",
            )
            self.attempts[request.attempt_idempotency_key] = receipt
            self.spawned.append(request)
        if self.lose_start_reply:
            raise SupervisorTransportError("start reply lost")
        return receipt


class ForbiddenSupervisor:
    """Fails the test if any refusal path reaches a Supervisor."""

    def admission(self):
        raise AssertionError("Supervisor admission must not be reached")

    def get_attempt_status(self, key):
        raise AssertionError("Supervisor must not be contacted")

    def start_attempt(self, request):
        raise AssertionError("Supervisor must not be contacted")


def _claimed(db):
    identity, issued = _issue(db)
    claim = claim_one_shot_authorization(db, identity)
    assert claim.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY
    return identity, issued, OneShotDispatchRequest.from_claim(identity, claim)


def _dispatch(db, request, supervisor, *, identity=VERIFIED, mode=DispatchMode.START):
    return dispatch_one_shot_intent(
        db, request, worker_identity=identity, supervisor=supervisor, mode=mode
    )


def _forged(identity, claim_uuid=None):
    expected = one_shot_claim._expected(identity)
    return OneShotDispatchRequest(
        identity=identity,
        claim_uuid=claim_uuid or str(uuid.uuid4()),
        intent_uuid=expected.intent_uuid,
        attempt_idempotency_key=expected.attempt_idempotency_key,
        request_payload_sha256=expected.request_payload_sha256,
    )


def _snapshot_rows(path: Path):
    with sqlite3.connect(path) as raw:
        return tuple(
            raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "one_shot_execution_authorizations",
                "executor_launch_intents",
                "one_shot_authorization_claims",
                "task_executions",
            )
        )


# Valid authority -------------------------------------------------------------


def test_valid_claim_dispatches_once_with_bound_start_request(f1b):
    path, _, db = f1b
    identity, issued, request = _claimed(db)
    before = _snapshot_rows(path)
    supervisor = FakeSupervisor()

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.DISPATCHED
    assert result.start_submitted and result.supervisor_contacted
    assert result.attempt_status is AttemptStatus.ACCEPTED
    assert supervisor.calls == ["admission", "status", "start"]
    (start,) = supervisor.spawned
    assert start.attempt_idempotency_key == issued.attempt_idempotency_key
    assert start.request_payload_sha256 == issued.request_payload_sha256
    assert start.authorization_identity_sha256 == issued.identity_sha256
    assert start.claim_uuid == request.claim_uuid
    assert start.task_execution_id == identity.task_execution_id
    assert start.session_instance_id == identity.session_instance_id
    assert start.source_snapshot_digest == identity.source_snapshot_digest
    assert start.candidate_uuid == identity.candidate_uuid
    assert (start.execution_profile_id, start.execution_profile_version) == (
        identity.execution_profile_id,
        identity.execution_profile_version,
    )
    assert start.operation_scope_digest == identity.operation_scope_digest
    # The boundary never writes: F1b rows and the TaskExecution are unchanged.
    assert _snapshot_rows(path) == before


def test_request_from_durable_reconciliation_matches_claim_request(f1b):
    _, _, db = f1b
    identity, _, request = _claimed(db)

    rebuilt = OneShotDispatchRequest.from_reconciliation(
        identity, reconcile_one_shot_claim(db, identity)
    )

    assert rebuilt == request


# Invalid or mismatched authority ---------------------------------------------


def test_request_must_derive_from_its_authorization(f1b):
    _, _, db = f1b
    identity, _, request = _claimed(db)

    for field, value in (
        ("intent_uuid", str(uuid.uuid4())),
        ("attempt_idempotency_key", "d" * 64),
        ("request_payload_sha256", "e" * 64),
    ):
        with pytest.raises(InvalidDispatchRequest):
            replace(request, **{field: value})
    with pytest.raises(InvalidDispatchRequest):
        dispatch_one_shot_intent(
            db, object(), worker_identity=VERIFIED, supervisor=ForbiddenSupervisor()
        )


def test_losing_or_unclaimed_claim_results_cannot_build_a_request(f1b):
    _, _, db = f1b
    identity, issued = _issue(db)
    with pytest.raises(InvalidDispatchRequest):
        OneShotDispatchRequest.from_reconciliation(
            identity, reconcile_one_shot_claim(db, identity)
        )
    claim_one_shot_authorization(db, identity)
    duplicate = claim_one_shot_authorization(db, identity)
    assert duplicate.outcome is ClaimOutcome.ALREADY_CLAIMED
    with pytest.raises(InvalidDispatchRequest):
        OneShotDispatchRequest.from_claim(identity, duplicate)


def test_unclaimed_authorization_is_not_admitted(f1b):
    _, _, db = f1b
    identity, _ = _issue(db)

    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.NOT_CLAIMED
    assert not result.supervisor_contacted


def test_missing_authorization_is_not_admitted(f1b):
    _, _, db = f1b
    identity, _, _ = _claimed(db)
    other = replace(identity, authorization_uuid=str(uuid.uuid4()))

    result = _dispatch(db, _forged(other), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.AUTHORITY_MISSING


@pytest.mark.parametrize(
    "field, value",
    [
        ("source_snapshot_digest", "f" * 64),
        ("candidate_initial_digest", "f" * 64),
        ("operation_scope_digest", "f" * 64),
        ("source_revision_identity", "git:other"),
        ("candidate_uuid", "00000000-0000-4000-8000-000000000000"),
    ],
)
def test_identity_substitution_is_an_authority_mismatch(f1b, field, value):
    _, _, db = f1b
    identity, _, request = _claimed(db)
    substituted = replace(identity, **{field: value})

    result = _dispatch(
        db, _forged(substituted, request.claim_uuid), ForbiddenSupervisor()
    )

    assert result.outcome is DispatchOutcome.AUTHORITY_MISMATCH
    assert result.reasons == ("durable_identity_mismatch",)


def test_wrong_claim_receipt_is_an_authority_mismatch(f1b):
    _, _, db = f1b
    identity, _, _ = _claimed(db)

    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.AUTHORITY_MISMATCH
    assert result.reasons == ("claim_uuid_mismatch",)


def test_changed_generation_or_terminal_execution_refuses_start_only(f1b):
    _, engine, db = f1b
    identity, _, request = _claimed(db)
    supervisor = FakeSupervisor()
    session = db.get(SessionModel, identity.session_id)
    session.instance_id = str(uuid.uuid4())
    db.commit()

    refused = _dispatch(db, request, ForbiddenSupervisor())
    reconciled = _dispatch(db, request, supervisor, mode=DispatchMode.RECONCILE_ONLY)

    assert refused.outcome is DispatchOutcome.EXECUTION_STATE_CONFLICT
    assert refused.reasons == ("session_generation_changed",)
    assert reconciled.outcome is DispatchOutcome.NOT_STARTED
    assert supervisor.spawned == []

    session.instance_id = identity.session_instance_id
    db.get(TaskExecution, identity.task_execution_id).status = TaskStatus.CANCELLED
    db.commit()
    terminal = _dispatch(db, request, ForbiddenSupervisor())
    assert terminal.outcome is DispatchOutcome.EXECUTION_STATE_CONFLICT
    assert terminal.reasons == ("task_execution_terminal",)


# Expired or revoked ----------------------------------------------------------


def test_revoked_authorization_is_not_admitted(f1b):
    _, _, db = f1b
    identity, issued = _issue(db)
    revoke_authorization(
        db, issued.authorization_uuid, operator_identity="operator@example.test"
    )
    assert claim_one_shot_authorization(db, identity).outcome is ClaimOutcome.REVOKED

    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.AUTHORIZATION_REVOKED


def test_expired_authorization_is_not_admitted(f1b):
    _, _, db = f1b
    identity, issued = _issue(db)
    inspect_authorization(
        db, issued.authorization_uuid, now=identity.expires_at + timedelta(seconds=1)
    )

    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.AUTHORIZATION_EXPIRED


def test_lapsed_but_unpersisted_expiry_is_reported_expired(f1b, monkeypatch):
    _, _, db = f1b
    identity, _ = _issue(db)
    monkeypatch.setattr(
        "app.services.execution.one_shot_dispatch._trusted_utc_now",
        lambda: identity.expires_at,
    )

    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())

    assert result.outcome is DispatchOutcome.AUTHORIZATION_EXPIRED


# Worker identity and Supervisor prerequisites -------------------------------


def test_missing_candidate_pin_fails_closed_before_supervisor(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)

    result = _dispatch(db, request, ForbiddenSupervisor(), identity=PRODUCTION_POSTURE)

    assert result.outcome is DispatchOutcome.CANDIDATE_PIN_NOT_MATCHED
    assert "candidate_pin_status=PIN_ABSENT" in result.reasons
    assert not result.supervisor_contacted


def test_inconclusive_worker_identity_with_matched_pin_fails_closed(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    inconclusive = WorkerIdentityAttestation(IDENTITY_INCONCLUSIVE, PIN_MATCHED)

    result = _dispatch(db, request, ForbiddenSupervisor(), identity=inconclusive)

    assert result.outcome is DispatchOutcome.WORKER_IDENTITY_UNVERIFIED


def test_absent_worker_identity_fails_closed(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)

    result = _dispatch(db, request, ForbiddenSupervisor(), identity=None)

    assert result.outcome is DispatchOutcome.WORKER_IDENTITY_UNAVAILABLE


def test_attestation_reads_the_boot_manifest_classification():
    manifest = WorkerBootIdentityManifest.__new__(WorkerBootIdentityManifest)
    object.__setattr__(manifest, "identity_status", IDENTITY_INCONCLUSIVE)
    object.__setattr__(manifest, "candidate_pin_status", PIN_ABSENT)
    object.__setattr__(
        manifest, "identity_status_reasons", ("candidate_pin_unavailable",)
    )

    assert WorkerIdentityAttestation.from_boot_manifest(manifest) == (
        PRODUCTION_POSTURE
    )


def test_unavailable_supervisor_fails_closed_without_operations(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)

    result = _dispatch(db, request, UnavailableSupervisor())

    assert result.outcome is DispatchOutcome.SUPERVISOR_UNAVAILABLE
    assert result.reasons == ("trusted_supervisor_not_deployed",)
    assert not result.supervisor_contacted and not result.start_submitted


@pytest.mark.parametrize(
    "admission",
    [
        SupervisorAdmission(SupervisorAdmissionStatus.UNTRUSTED, True),
        SupervisorAdmission(SupervisorAdmissionStatus.TRUSTED, False),
    ],
)
def test_untrusted_or_non_deduplicating_supervisor_is_refused(f1b, admission):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor(admission=admission)

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.SUPERVISOR_UNTRUSTED
    assert supervisor.calls == ["admission"]


def test_feature_gate_refuses_before_database_or_supervisor(f1b, monkeypatch):
    _, _, db = f1b
    _, _, request = _claimed(db)
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(
        "app.services.execution.one_shot_dispatch._engine",
        lambda _db: pytest.fail("database must not be reached"),
    )

    with pytest.raises(OneShotFeatureDisabled):
        _dispatch(db, request, ForbiddenSupervisor())


# Duplicate and concurrent admission ------------------------------------------


def test_duplicate_admission_reuses_the_single_attempt(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor()

    first = _dispatch(db, request, supervisor)
    second = _dispatch(db, request, supervisor)
    reconciled = _dispatch(db, request, supervisor, mode=DispatchMode.RECONCILE_ONLY)

    assert first.outcome is DispatchOutcome.DISPATCHED
    assert second.outcome is DispatchOutcome.RECONCILED
    assert not second.start_submitted
    assert reconciled.outcome is DispatchOutcome.RECONCILED
    assert len(supervisor.spawned) == 1
    assert supervisor.calls.count("start") == 1


def test_reconcile_only_never_starts(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor()

    result = _dispatch(db, request, supervisor, mode=DispatchMode.RECONCILE_ONLY)

    assert result.outcome is DispatchOutcome.NOT_STARTED
    assert "start" not in supervisor.calls and supervisor.spawned == []


def test_concurrent_dispatchers_produce_one_attempt(f1b):
    path, engine, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor()
    barrier = threading.Barrier(2)
    # Both dispatchers observe NOT_FOUND before either starts.
    supervisor.before_start = lambda: barrier.wait(timeout=5)
    factory = sessionmaker(autoflush=False, bind=engine)
    results = []

    def run():
        with factory() as own_db:
            results.append(_dispatch(own_db, request, supervisor))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(result.outcome.value for result in results) == [
        DispatchOutcome.DISPATCHED.value,
        DispatchOutcome.RECONCILED.value,
    ]
    assert len(supervisor.spawned) == 1


def test_concurrent_claim_and_dispatch_admits_only_the_claimant(f1b):
    path, engine, db = f1b
    identity, _ = _issue(db)
    supervisor = FakeSupervisor()
    factory = sessionmaker(autoflush=False, bind=engine)
    barrier = threading.Barrier(2)
    outcomes = []

    def run():
        with factory() as own_db:
            barrier.wait(timeout=5)
            claim = claim_one_shot_authorization(own_db, identity)
            if not claim.handoff_permitted:
                outcomes.append(claim.outcome)
                return
            request = OneShotDispatchRequest.from_claim(identity, claim)
            outcomes.append(_dispatch(own_db, request, supervisor).outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(outcome.value for outcome in outcomes) == [
        ClaimOutcome.ALREADY_CLAIMED.value,
        DispatchOutcome.DISPATCHED.value,
    ]
    assert len(supervisor.spawned) == 1
    _, intents, receipts = _state(path, identity.authorization_uuid)
    assert [intent[0] for intent in intents] == ["READY"] and len(receipts) == 1


# Crash and reconciliation boundaries -----------------------------------------


def test_crash_before_claim_commit_leaves_nothing_dispatchable(f1b, monkeypatch):
    path, _, db = f1b
    identity, issued = _issue(db)

    def _crash(connection):
        raise _SimulatedCrash()

    monkeypatch.setattr(one_shot_claim, "_commit_claim", _crash)
    with pytest.raises(_SimulatedCrash):
        claim_one_shot_authorization(db, identity)
    monkeypatch.undo()
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)

    with pytest.raises(InvalidDispatchRequest):
        OneShotDispatchRequest.from_reconciliation(
            identity, reconcile_one_shot_claim(db, identity)
        )
    result = _dispatch(db, _forged(identity), ForbiddenSupervisor())
    assert result.outcome is DispatchOutcome.NOT_CLAIMED
    assert _state(path, issued.authorization_uuid)[0][0] == "AUTHORIZED"


def test_crash_after_claim_commit_recovers_with_the_same_key(f1b):
    _, _, db = f1b
    identity, issued = _issue(db)
    claim_one_shot_authorization(db, identity)  # claimant dies after COMMIT
    supervisor = FakeSupervisor()

    recovered = OneShotDispatchRequest.from_reconciliation(
        identity, reconcile_one_shot_claim(db, identity)
    )
    result = _dispatch(db, recovered, supervisor)

    assert result.outcome is DispatchOutcome.DISPATCHED
    (start,) = supervisor.spawned
    assert start.attempt_idempotency_key == issued.attempt_idempotency_key


def test_lost_start_reply_reconciles_by_lookup_without_resubmitting(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor(lose_start_reply=True)

    lost = _dispatch(db, request, supervisor)
    supervisor.lose_start_reply = False
    redelivered = _dispatch(db, request, supervisor)

    assert lost.outcome is DispatchOutcome.RECONCILED and lost.start_submitted
    assert lost.reasons == ("start reply lost",)
    assert redelivered.outcome is DispatchOutcome.RECONCILED
    assert supervisor.calls.count("start") == 1 and len(supervisor.spawned) == 1


def test_lost_start_reply_with_failed_lookup_holds(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor(lose_start_reply=True)
    original = supervisor.get_attempt_status
    lookups = []

    def status(key):
        lookups.append(key)
        if len(lookups) > 1:
            raise SupervisorTransportError("status lookup timed out")
        return original(key)

    supervisor.get_attempt_status = status

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.HOLD_UNKNOWN
    assert supervisor.calls.count("start") == 1


def test_lost_start_reply_with_not_found_lookup_holds(f1b):
    _, _, db = f1b
    _, _, request = _claimed(db)
    supervisor = FakeSupervisor()

    def lose(request):
        supervisor.calls.append("start")
        raise SupervisorTransportError("connection reset")

    supervisor.start_attempt = lose

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.HOLD_UNKNOWN
    assert supervisor.calls.count("start") == 1


@pytest.mark.parametrize("mode", list(DispatchMode))
def test_unknown_or_unreachable_status_holds_without_start(f1b, mode):
    _, _, db = f1b
    _, _, request = _claimed(db)
    unknown = FakeSupervisor()
    unknown.status_override = AttemptStatus.UNKNOWN
    unreachable = FakeSupervisor()
    unreachable.status_error = True

    for supervisor in (unknown, unreachable):
        result = _dispatch(db, request, supervisor, mode=mode)
        assert result.outcome is DispatchOutcome.HOLD_UNKNOWN
        assert "start" not in supervisor.calls


def test_receipt_bound_to_another_payload_holds(f1b):
    _, _, db = f1b
    _, issued, request = _claimed(db)
    supervisor = FakeSupervisor()
    supervisor.attempts[issued.attempt_idempotency_key] = SupervisorReceipt(
        issued.attempt_idempotency_key,
        AttemptStatus.RUNNING,
        "0" * 64,
        issued.identity_sha256,
        "foreign",
    )

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.HOLD_RECEIPT_CONFLICT
    assert "start" not in supervisor.calls


def test_supervisor_rejection_is_reported_and_not_retried(f1b):
    _, _, db = f1b
    _, issued, request = _claimed(db)
    supervisor = FakeSupervisor()

    def reject(start):
        supervisor.calls.append("start")
        return SupervisorReceipt(
            start.attempt_idempotency_key,
            AttemptStatus.REJECTED,
            start.request_payload_sha256,
            start.authorization_identity_sha256,
        )

    supervisor.start_attempt = reject

    result = _dispatch(db, request, supervisor)

    assert result.outcome is DispatchOutcome.SUPERVISOR_REJECTED
    assert supervisor.calls.count("start") == 1


# Isolation of the boundary ---------------------------------------------------


def _importers(root: Path, module_name: str) -> set[str]:
    found = set()
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [getattr(node, "module", None) or ""] + [
                    alias.name for alias in node.names
                ]
                if any(module_name in name for name in names):
                    found.add(str(path.relative_to(root)))
    return found


def test_dispatch_boundary_is_reached_only_through_worker_routing():
    root = Path(__file__).resolve().parents[1]
    # F1c-2: the only chain is worker.py -> one_shot_routing -> dispatch.
    # F2: the Supervisor adapter imports only the protocol types.
    assert _importers(root, "one_shot_dispatch") == {
        "services/execution/one_shot_routing.py",
        "services/execution/one_shot_supervisor_client.py",
        "tasks/worker.py",
    }
    adapter = root / "services/execution/one_shot_supervisor_client.py"
    adapter_names = {
        alias.name
        for node in ast.walk(ast.parse(adapter.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and "one_shot" in node.module
        for alias in node.names
    }
    assert adapter_names == {
        "AttemptStatus",
        "ExecutorStartRequest",
        "SupervisorAdmission",
        "SupervisorAdmissionStatus",
        "SupervisorReceipt",
        "SupervisorTransportError",
        "UnavailableSupervisor",
    }
    # worker_identity names it only as a string in the reviewed boundary.
    assert _importers(root, "one_shot_routing") == {"tasks/worker.py"}
    module = Path(__file__).resolve().parents[1] / (
        "services/execution/one_shot_dispatch.py"
    )
    imported = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(
        name.split(".")[0] in {"celery", "subprocess", "requests", "httpx", "socket"}
        or name.startswith(("app.tasks", "app.providers", "app.supervisor", "app.api"))
        for name in imported
    )
    assert type(settings).model_fields["ONE_SHOT_EXECUTION_ENABLED"].default is False
