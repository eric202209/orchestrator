"""Provider-free F1c-1 fail-closed dispatch boundary for a claimed intent.

The boundary admits exactly one READY executor launch intent, already made
durable by the F1b atomic claim, for handoff to a trusted Supervisor. It
never writes the database: the F1b CLAIMED authorization, READY intent, and
insert-only claim receipt are the durable outbox, and the Supervisor's
receipt keyed by the stable attempt idempotency key is authoritative for the
process. Every handoff reuses that one key, and an ambiguous outcome holds.

No production Supervisor exists. ``UnavailableSupervisor`` is the only
non-test adapter, it refuses admission, and no API or Worker route calls this
module. The feature gate is checked before any database or Supervisor access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.execution.one_shot_authorization import (
    SUPPORTED_TASK_EXECUTION_PROFILE,
    InvalidAuthorizationIdentity,
    OneShotAuthorizationIdentity,
    OneShotFeatureDisabled,
    _utc,
    _validate_digest,
    _validate_uuid,
    validate_identity,
)
from app.services.execution.one_shot_claim import (
    ClaimOutcome,
    OneShotClaimReconciliation,
    OneShotClaimResult,
    _db_error_outcome,
    _engine,
    _expected,
    _read_snapshot,
    _reconciliation,
    _rollback_quietly,
    _schema_outcome,
    _trusted_utc_now,
)
from app.services.orchestration.worker_identity import (
    IDENTITY_VERIFIED,
    PIN_MATCHED,
    WorkerBootIdentityManifest,
)

START_REQUEST_SCHEMA_VERSION = "executor-start-attempt/v1"
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class InvalidDispatchRequest(InvalidAuthorizationIdentity):
    """The request is not derivable from its own authorization identity."""


class DispatchMode(str, Enum):
    # May submit StartAttempt once, with the stable key, if none exists yet.
    START = "START"
    # Duplicate delivery or recovery: status lookup only, never StartAttempt.
    RECONCILE_ONLY = "RECONCILE_ONLY"


class DispatchOutcome(str, Enum):
    # Handoff results; only these follow a Supervisor call.
    DISPATCHED = "DISPATCHED"
    RECONCILED = "RECONCILED"
    NOT_STARTED = "NOT_STARTED"
    SUPERVISOR_REJECTED = "SUPERVISOR_REJECTED"
    HOLD_UNKNOWN = "HOLD_UNKNOWN"
    HOLD_RECEIPT_CONFLICT = "HOLD_RECEIPT_CONFLICT"
    # Refusals; no Supervisor operation was invoked.
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    AUTHORITY_MISMATCH = "AUTHORITY_MISMATCH"
    AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    NOT_CLAIMED = "NOT_CLAIMED"
    EXECUTION_STATE_CONFLICT = "EXECUTION_STATE_CONFLICT"
    DATABASE_BUSY = "DATABASE_BUSY"
    SCHEMA_UNAVAILABLE = "SCHEMA_UNAVAILABLE"
    UNSUPPORTED_DATABASE = "UNSUPPORTED_DATABASE"
    WORKER_IDENTITY_UNAVAILABLE = "WORKER_IDENTITY_UNAVAILABLE"
    CANDIDATE_PIN_NOT_MATCHED = "CANDIDATE_PIN_NOT_MATCHED"
    WORKER_IDENTITY_UNVERIFIED = "WORKER_IDENTITY_UNVERIFIED"
    SUPERVISOR_UNAVAILABLE = "SUPERVISOR_UNAVAILABLE"
    SUPERVISOR_UNTRUSTED = "SUPERVISOR_UNTRUSTED"


_CLAIM_DB_OUTCOMES = {
    ClaimOutcome.DATABASE_BUSY: DispatchOutcome.DATABASE_BUSY,
    ClaimOutcome.SCHEMA_UNAVAILABLE: DispatchOutcome.SCHEMA_UNAVAILABLE,
    ClaimOutcome.UNSUPPORTED_DATABASE: DispatchOutcome.UNSUPPORTED_DATABASE,
}


@dataclass(frozen=True)
class OneShotDispatchRequest:
    """Admission request bound to one authorization and its claim receipt.

    The intent UUID, attempt key, and payload digest must equal the values
    F1a derives from ``identity``; the durable rows are compared at dispatch.
    """

    identity: OneShotAuthorizationIdentity
    claim_uuid: str
    intent_uuid: str
    attempt_idempotency_key: str
    request_payload_sha256: str

    def __post_init__(self) -> None:
        validate_identity(self.identity)
        _validate_uuid(self.claim_uuid, "claim_uuid")
        _validate_uuid(self.intent_uuid, "intent_uuid")
        _validate_digest(self.attempt_idempotency_key, "attempt_idempotency_key")
        _validate_digest(self.request_payload_sha256, "request_payload_sha256")
        expected = _expected(self.identity)
        if (
            self.intent_uuid != expected.intent_uuid
            or self.attempt_idempotency_key != expected.attempt_idempotency_key
            or self.request_payload_sha256 != expected.request_payload_sha256
        ):
            raise InvalidDispatchRequest(
                "launch intent binding does not derive from the authorization"
            )

    @classmethod
    def from_claim(
        cls, identity: OneShotAuthorizationIdentity, claim: OneShotClaimResult
    ) -> "OneShotDispatchRequest":
        """Build from this claimant's own proven-committed F1b claim."""

        if not claim.handoff_permitted or claim.claim_uuid is None:
            raise InvalidDispatchRequest("only a committed own claim may hand off")
        if claim.authorization_uuid != identity.authorization_uuid:
            raise InvalidDispatchRequest("claim belongs to another authorization")
        return cls(
            identity=identity,
            claim_uuid=claim.claim_uuid,
            intent_uuid=_expected(identity).intent_uuid,
            attempt_idempotency_key=claim.attempt_idempotency_key or "",
            request_payload_sha256=claim.request_payload_sha256 or "",
        )

    @classmethod
    def from_reconciliation(
        cls,
        identity: OneShotAuthorizationIdentity,
        state: OneShotClaimReconciliation,
    ) -> "OneShotDispatchRequest":
        """Build from durable F1b state, e.g. after the claimant crashed."""

        if not state.durable_ready or state.claim_uuid is None:
            raise InvalidDispatchRequest("claim is not durably READY")
        if state.authorization_uuid != identity.authorization_uuid:
            raise InvalidDispatchRequest("state belongs to another authorization")
        return cls(
            identity=identity,
            claim_uuid=state.claim_uuid,
            intent_uuid=_expected(identity).intent_uuid,
            attempt_idempotency_key=state.attempt_idempotency_key or "",
            request_payload_sha256=state.request_payload_sha256 or "",
        )


@dataclass(frozen=True)
class ExecutorStartRequest:
    """Fixed StartAttempt payload; no command, mount, or network selection."""

    schema_version: str
    attempt_idempotency_key: str
    request_payload_sha256: str
    authorization_uuid: str
    authorization_identity_sha256: str
    claim_uuid: str
    project_id: int
    session_id: int
    session_instance_id: str
    task_id: int
    task_execution_id: int
    source_revision_identity: str
    source_snapshot_digest: str
    candidate_uuid: str
    candidate_initial_digest: str
    execution_profile_id: str
    execution_profile_version: str
    operation_scope_id: str
    operation_scope_version: str
    operation_scope_digest: str


@dataclass(frozen=True)
class WorkerIdentityAttestation:
    """Worker boot identity classification as recorded by worker_identity."""

    identity_status: str
    candidate_pin_status: str | None
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_boot_manifest(
        cls, manifest: WorkerBootIdentityManifest
    ) -> "WorkerIdentityAttestation":
        return cls(
            identity_status=manifest.identity_status,
            candidate_pin_status=manifest.candidate_pin_status,
            reasons=tuple(manifest.identity_status_reasons),
        )


class SupervisorAdmissionStatus(str, Enum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SupervisorAdmission:
    status: SupervisorAdmissionStatus
    durable_deduplication: bool = False
    reasons: tuple[str, ...] = ()


class AttemptStatus(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SupervisorReceipt:
    """Supervisor-owned attempt state for one stable attempt key."""

    attempt_idempotency_key: str
    status: AttemptStatus
    request_payload_sha256: str | None = None
    authorization_identity_sha256: str | None = None
    supervisor_attempt_id: str | None = None
    # True when StartAttempt returned an attempt that already existed.
    deduplicated: bool = False


class SupervisorTransportError(RuntimeError):
    """The Supervisor call's outcome is unknown (unreachable or lost reply)."""


class OneShotSupervisor(Protocol):
    def admission(self) -> SupervisorAdmission: ...

    def get_attempt_status(self, attempt_idempotency_key: str) -> SupervisorReceipt: ...

    def start_attempt(self, request: ExecutorStartRequest) -> SupervisorReceipt: ...


class UnavailableSupervisor:
    """The only non-test adapter: no trusted Supervisor is deployed."""

    def admission(self) -> SupervisorAdmission:
        return SupervisorAdmission(
            SupervisorAdmissionStatus.UNAVAILABLE,
            reasons=("trusted_supervisor_not_deployed",),
        )

    def get_attempt_status(self, attempt_idempotency_key: str) -> SupervisorReceipt:
        raise SupervisorTransportError("trusted Supervisor is not deployed")

    def start_attempt(self, request: ExecutorStartRequest) -> SupervisorReceipt:
        raise SupervisorTransportError("trusted Supervisor is not deployed")


@dataclass(frozen=True)
class OneShotDispatchResult:
    outcome: DispatchOutcome
    authorization_uuid: str
    attempt_idempotency_key: str
    mode: DispatchMode
    supervisor_contacted: bool = False
    start_submitted: bool = False
    supervisor_attempt_id: str | None = None
    attempt_status: AttemptStatus | None = None
    reasons: tuple[str, ...] = ()


def _start_request(
    request: OneShotDispatchRequest, identity_sha256: str
) -> ExecutorStartRequest:
    identity = request.identity
    return ExecutorStartRequest(
        schema_version=START_REQUEST_SCHEMA_VERSION,
        attempt_idempotency_key=request.attempt_idempotency_key,
        request_payload_sha256=request.request_payload_sha256,
        authorization_uuid=identity.authorization_uuid,
        authorization_identity_sha256=identity_sha256,
        claim_uuid=request.claim_uuid,
        project_id=identity.project_id,
        session_id=identity.session_id,
        session_instance_id=identity.session_instance_id,
        task_id=identity.task_id,
        task_execution_id=identity.task_execution_id,
        source_revision_identity=identity.source_revision_identity,
        source_snapshot_digest=identity.source_snapshot_digest,
        candidate_uuid=identity.candidate_uuid,
        candidate_initial_digest=identity.candidate_initial_digest,
        execution_profile_id=identity.execution_profile_id,
        execution_profile_version=identity.execution_profile_version,
        operation_scope_id=identity.operation_scope_id,
        operation_scope_version=identity.operation_scope_version,
        operation_scope_digest=identity.operation_scope_digest,
    )


def _live_execution_reasons(connection, identity: OneShotAuthorizationIdentity):
    """START needs the claimed generation, profile, and a non-terminal TE."""

    session = connection.execute(
        select(SessionModel.project_id, SessionModel.instance_id).where(
            SessionModel.id == identity.session_id
        )
    ).first()
    task = connection.execute(
        select(Task.project_id, Task.execution_profile).where(
            Task.id == identity.task_id
        )
    ).first()
    execution = connection.execute(
        select(
            TaskExecution.session_id,
            TaskExecution.task_id,
            TaskExecution.runtime_lease_id,
            TaskExecution.status,
        ).where(TaskExecution.id == identity.task_execution_id)
    ).first()
    reasons = []
    if (
        session is None
        or session.project_id != identity.project_id
        or session.instance_id != identity.session_instance_id
    ):
        reasons.append("session_generation_changed")
    if (
        task is None
        or task.project_id != identity.project_id
        or task.execution_profile != SUPPORTED_TASK_EXECUTION_PROFILE
    ):
        reasons.append("task_profile_changed")
    if (
        execution is None
        or execution.session_id != identity.session_id
        or execution.task_id != identity.task_id
        or execution.runtime_lease_id is not None
    ):
        reasons.append("task_execution_mapping_changed")
    elif execution.status in _TERMINAL_EXECUTION_STATUSES:
        reasons.append("task_execution_terminal")
    return tuple(reasons)


def _authority_outcome(
    db: Session, request: OneShotDispatchRequest, mode: DispatchMode
) -> tuple[DispatchOutcome | None, tuple[str, ...]]:
    """Compare the request with durable F1b state in one read snapshot."""

    identity = request.identity
    expected = _expected(identity)
    engine = _engine(db)
    unavailable = _schema_outcome(engine)
    if unavailable is not None:
        return _CLAIM_DB_OUTCOMES[unavailable], ()
    try:
        with engine.connect() as connection:
            # pysqlite issues no BEGIN before SELECT; an explicit read
            # transaction keeps both reads on one WAL snapshot.
            connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.exec_driver_sql("BEGIN")
            try:
                row = _read_snapshot(connection, identity.authorization_uuid)
                live_reasons = _live_execution_reasons(connection, identity)
            finally:
                _rollback_quietly(connection)
    except SQLAlchemyError as exc:
        outcome = _db_error_outcome(exc)
        if outcome is None:
            raise
        return _CLAIM_DB_OUTCOMES[outcome], ()

    if row is None:
        return DispatchOutcome.AUTHORITY_MISSING, ()
    state = _reconciliation(row, expected)
    if not state.identity_verified or not state.intent_verified:
        return DispatchOutcome.AUTHORITY_MISMATCH, ("durable_identity_mismatch",)
    if row.state == "REVOKED":
        return DispatchOutcome.AUTHORIZATION_REVOKED, ()
    if row.state == "EXPIRED" or (
        row.state == "AUTHORIZED" and _utc(row.expires_at) <= _trusted_utc_now()
    ):
        return DispatchOutcome.AUTHORIZATION_EXPIRED, ()
    if row.state != "CLAIMED" or row.claim_uuid is None:
        return DispatchOutcome.NOT_CLAIMED, ()
    if not state.durable_ready:
        return DispatchOutcome.AUTHORITY_MISMATCH, ("claim_receipt_mismatch",)
    if row.claim_uuid != request.claim_uuid:
        return DispatchOutcome.AUTHORITY_MISMATCH, ("claim_uuid_mismatch",)
    if mode is DispatchMode.START and live_reasons:
        return DispatchOutcome.EXECUTION_STATE_CONFLICT, live_reasons
    return None, ()


def _worker_outcome(
    worker_identity: WorkerIdentityAttestation | None,
) -> tuple[DispatchOutcome | None, tuple[str, ...]]:
    if worker_identity is None:
        return DispatchOutcome.WORKER_IDENTITY_UNAVAILABLE, ()
    reasons = (
        f"identity_status={worker_identity.identity_status}",
        f"candidate_pin_status={worker_identity.candidate_pin_status}",
    ) + worker_identity.reasons
    if worker_identity.candidate_pin_status != PIN_MATCHED:
        return DispatchOutcome.CANDIDATE_PIN_NOT_MATCHED, reasons
    if worker_identity.identity_status != IDENTITY_VERIFIED:
        return DispatchOutcome.WORKER_IDENTITY_UNVERIFIED, reasons
    return None, ()


def _supervisor_outcome(
    supervisor: OneShotSupervisor,
) -> tuple[DispatchOutcome | None, tuple[str, ...]]:
    try:
        admission = supervisor.admission()
    except SupervisorTransportError as exc:
        return DispatchOutcome.SUPERVISOR_UNAVAILABLE, (str(exc),)
    if admission.status is SupervisorAdmissionStatus.UNAVAILABLE:
        return DispatchOutcome.SUPERVISOR_UNAVAILABLE, admission.reasons
    if admission.status is not SupervisorAdmissionStatus.TRUSTED:
        return DispatchOutcome.SUPERVISOR_UNTRUSTED, admission.reasons
    if admission.durable_deduplication is not True:
        return (
            DispatchOutcome.SUPERVISOR_UNTRUSTED,
            ("durable_deduplication_not_guaranteed",) + admission.reasons,
        )
    return None, ()


def _receipt_bound(receipt: SupervisorReceipt, start: ExecutorStartRequest) -> bool:
    return (
        receipt.attempt_idempotency_key == start.attempt_idempotency_key
        and receipt.request_payload_sha256 == start.request_payload_sha256
        and receipt.authorization_identity_sha256 == start.authorization_identity_sha256
    )


def _receipt_outcome(
    receipt: SupervisorReceipt, start: ExecutorStartRequest, *, started: bool
) -> DispatchOutcome:
    """Classify an existing attempt; NOT_FOUND is handled by the caller."""

    if receipt.attempt_idempotency_key != start.attempt_idempotency_key:
        return DispatchOutcome.HOLD_RECEIPT_CONFLICT
    if receipt.status in (AttemptStatus.UNKNOWN, AttemptStatus.NOT_FOUND):
        return DispatchOutcome.HOLD_UNKNOWN
    if not _receipt_bound(receipt, start):
        return DispatchOutcome.HOLD_RECEIPT_CONFLICT
    if receipt.status is AttemptStatus.REJECTED:
        return DispatchOutcome.SUPERVISOR_REJECTED
    if started and not receipt.deduplicated:
        return DispatchOutcome.DISPATCHED
    return DispatchOutcome.RECONCILED


def dispatch_one_shot_intent(
    db: Session,
    request: OneShotDispatchRequest,
    *,
    worker_identity: WorkerIdentityAttestation | None,
    supervisor: OneShotSupervisor,
    mode: DispatchMode = DispatchMode.START,
) -> OneShotDispatchResult:
    """Admit one durably READY intent and hand it off under its stable key.

    Order: feature gate, durable authority, Worker identity, Supervisor
    admission, then Supervisor status lookup. Any refusal returns before a
    Supervisor operation. StartAttempt is submitted at most once per call,
    only in START mode, and only when the Supervisor reports NOT_FOUND for
    the key. A lost StartAttempt reply is resolved by status lookup only;
    it is never resubmitted here. ``db`` only supplies the engine.
    """

    if settings.ONE_SHOT_EXECUTION_ENABLED is not True:
        raise OneShotFeatureDisabled("one-shot execution is disabled")
    if not isinstance(request, OneShotDispatchRequest):
        raise InvalidDispatchRequest("a typed OneShotDispatchRequest is required")
    mode = DispatchMode(mode)
    key = request.attempt_idempotency_key

    def result(outcome, reasons=(), receipt=None, *, contacted, started=False):
        return OneShotDispatchResult(
            outcome=outcome,
            authorization_uuid=request.identity.authorization_uuid,
            attempt_idempotency_key=key,
            mode=mode,
            supervisor_contacted=contacted,
            start_submitted=started,
            supervisor_attempt_id=receipt.supervisor_attempt_id if receipt else None,
            attempt_status=receipt.status if receipt else None,
            reasons=tuple(reasons),
        )

    for check in (
        lambda: _authority_outcome(db, request, mode),
        lambda: _worker_outcome(worker_identity),
        lambda: _supervisor_outcome(supervisor),
    ):
        refused, reasons = check()
        if refused is not None:
            return result(refused, reasons, contacted=False)

    start = _start_request(request, _expected(request.identity).identity_sha256)
    try:
        receipt = supervisor.get_attempt_status(key)
    except SupervisorTransportError as exc:
        return result(DispatchOutcome.HOLD_UNKNOWN, (str(exc),), contacted=True)
    if receipt.status is not AttemptStatus.NOT_FOUND or (
        receipt.attempt_idempotency_key != key
    ):
        return result(
            _receipt_outcome(receipt, start, started=False), (), receipt, contacted=True
        )
    if mode is DispatchMode.RECONCILE_ONLY:
        return result(DispatchOutcome.NOT_STARTED, (), receipt, contacted=True)

    try:
        receipt = supervisor.start_attempt(start)
    except SupervisorTransportError as exc:
        # The Supervisor may have accepted the attempt; look it up, never resend.
        try:
            receipt = supervisor.get_attempt_status(key)
        except SupervisorTransportError as lookup_exc:
            return result(
                DispatchOutcome.HOLD_UNKNOWN,
                (str(exc), str(lookup_exc)),
                contacted=True,
                started=True,
            )
        return result(
            _receipt_outcome(receipt, start, started=False),
            (str(exc),),
            receipt,
            contacted=True,
            started=True,
        )
    return result(
        _receipt_outcome(receipt, start, started=True),
        (),
        receipt,
        contacted=True,
        started=True,
    )
