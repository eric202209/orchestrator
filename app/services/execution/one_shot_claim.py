"""Provider-free F1b durable at-most-once claim of an F1a authorization.

The claim atomically moves one authorization AUTHORIZED→CLAIMED and its
existing launch intent NOT_DISPATCHABLE→READY on a dedicated SQLite
connection whose first transactional statement is ``BEGIN IMMEDIATE``.
READY is durable handoff eligibility only: nothing here dispatches, calls a
supervisor, publishes Celery, or invokes a provider, and no API or Worker
route calls this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import sqlite3
import uuid

from sqlalchemy import exists, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ExecutorLaunchIntent,
    OneShotAuthorizationClaim,
    OneShotExecutionAuthorization,
    Session as SessionModel,
    Task,
    TaskExecution,
)
from app.services.execution.one_shot_authorization import (
    MIGRATION_VERSION as AUTHORIZATION_MIGRATION_VERSION,
    SUPPORTED_TASK_EXECUTION_PROFILE,
    _IDENTITY_FIELDS,
    AuthorizationSchemaUnavailable,
    OneShotAuthorizationIdentity,
    OneShotFeatureDisabled,
    _intent_identity,
    _require_aware,
    _utc,
    canonical_identity_sha256,
    validate_identity,
)

CLAIM_MIGRATION_VERSION = "058_one_shot_atomic_claim"

_AUTH = OneShotExecutionAuthorization.__table__
_INTENT = ExecutorLaunchIntent.__table__
_CLAIM = OneShotAuthorizationClaim.__table__
_DATETIME_FIELDS = {"operator_approved_at", "issued_at", "expires_at"}
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


class ClaimOutcome(str, Enum):
    CLAIMED_SUCCESSFULLY = "CLAIMED_SUCCESSFULLY"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    DATABASE_BUSY = "DATABASE_BUSY"
    COMMIT_UNCERTAIN = "COMMIT_UNCERTAIN"
    SCHEMA_UNAVAILABLE = "SCHEMA_UNAVAILABLE"
    UNSUPPORTED_DATABASE = "UNSUPPORTED_DATABASE"


@dataclass(frozen=True)
class OneShotClaimResult:
    outcome: ClaimOutcome
    authorization_uuid: str
    claim_uuid: str | None = None
    authorization_state: str | None = None
    launch_state: str | None = None
    attempt_idempotency_key: str | None = None
    request_payload_sha256: str | None = None
    reconciled: bool = False

    @property
    def handoff_permitted(self) -> bool:
        """Only the claimant whose own claim is proven committed may hand off."""

        return self.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY


@dataclass(frozen=True)
class OneShotClaimReconciliation:
    """Read-only durable claim state; READY never implies external dispatch."""

    authorization_uuid: str
    authorization_state: str | None
    launch_state: str | None
    claim_uuid: str | None
    attempt_idempotency_key: str | None
    request_payload_sha256: str | None
    identity_verified: bool
    intent_verified: bool
    durable_ready: bool
    externally_dispatched: bool = False


@dataclass(frozen=True)
class _Expected:
    identity: OneShotAuthorizationIdentity
    identity_sha256: str
    intent_uuid: str
    attempt_idempotency_key: str
    request_payload_sha256: str


def _expected(identity: OneShotAuthorizationIdentity) -> _Expected:
    identity_sha = canonical_identity_sha256(identity)
    intent_uuid, key, request_sha = _intent_identity(identity, identity_sha)
    return _Expected(identity, identity_sha, intent_uuid, key, request_sha)


def _identity_value(identity: OneShotAuthorizationIdentity, field: str):
    value = getattr(identity, field)
    if field in _DATETIME_FIELDS:
        return _require_aware(value, field)
    return value


def _trusted_utc_now() -> datetime:
    """The claim's only time source.

    Trust boundary: no caller-supplied time reaches the claim. Tests replace
    this function in-process; production code has no parameter to backdate.
    """

    return datetime.now(timezone.utc)


def _db_error_outcome(exc: BaseException) -> ClaimOutcome | None:
    """Map expected operational failures; anything else stays an exception."""

    if isinstance(exc, PoolTimeoutError):
        return ClaimOutcome.DATABASE_BUSY
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "sqlite_errorcode", None)
    message = str(orig).lower()
    if code is not None:
        if code & 0xFF in (_SQLITE_BUSY, _SQLITE_LOCKED):
            return ClaimOutcome.DATABASE_BUSY
    elif "database is locked" in message or "database is busy" in message:
        return ClaimOutcome.DATABASE_BUSY
    if "no such table" in message or "no such column" in message:
        return ClaimOutcome.SCHEMA_UNAVAILABLE
    return None


def _engine(db: Session) -> Engine:
    return db.get_bind().engine


def _schema_outcome(engine: Engine) -> ClaimOutcome | None:
    """Fail closed unless this is a file-backed SQLite with verified 057/058."""

    if engine.dialect.name != "sqlite":
        return ClaimOutcome.UNSUPPORTED_DATABASE
    if engine.url.database in (None, "", ":memory:"):
        return ClaimOutcome.UNSUPPORTED_DATABASE
    try:
        from app.db_migrations import _verify_058_one_shot_atomic_claim

        _verify_058_one_shot_atomic_claim(engine)
        with engine.connect() as connection:
            applied = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version IN (?, ?)",
                (AUTHORIZATION_MIGRATION_VERSION, CLAIM_MIGRATION_VERSION),
            ).scalar_one()
    except SQLAlchemyError as exc:
        if _db_error_outcome(exc) is ClaimOutcome.DATABASE_BUSY:
            return ClaimOutcome.DATABASE_BUSY
        return ClaimOutcome.SCHEMA_UNAVAILABLE
    except RuntimeError:
        return ClaimOutcome.SCHEMA_UNAVAILABLE
    if applied != 2:
        return ClaimOutcome.SCHEMA_UNAVAILABLE
    return None


def _claim_authorization(
    connection: Connection, expected: _Expected, observed_at: datetime
) -> int:
    identity = expected.identity
    return connection.execute(
        update(_AUTH)
        .where(
            *(
                _AUTH.c[field] == _identity_value(identity, field)
                for field in _IDENTITY_FIELDS
            ),
            _AUTH.c.identity_sha256 == expected.identity_sha256,
            _AUTH.c.state == "AUTHORIZED",
            _AUTH.c.expires_at > observed_at,
            exists().where(
                SessionModel.id == identity.session_id,
                SessionModel.project_id == identity.project_id,
                SessionModel.instance_id == identity.session_instance_id,
            ),
            exists().where(
                Task.id == identity.task_id,
                Task.project_id == identity.project_id,
                Task.execution_profile == SUPPORTED_TASK_EXECUTION_PROFILE,
            ),
            exists().where(
                TaskExecution.id == identity.task_execution_id,
                TaskExecution.session_id == identity.session_id,
                TaskExecution.task_id == identity.task_id,
                TaskExecution.runtime_lease_id.is_(None),
            ),
        )
        .values(state="CLAIMED", state_changed_at=observed_at, revoked_by=None)
    ).rowcount


def _transition_intent(connection: Connection, expected: _Expected) -> int:
    identity = expected.identity
    return connection.execute(
        update(_INTENT)
        .where(
            _INTENT.c.intent_uuid == expected.intent_uuid,
            _INTENT.c.authorization_uuid == identity.authorization_uuid,
            _INTENT.c.project_id == identity.project_id,
            _INTENT.c.session_id == identity.session_id,
            _INTENT.c.session_instance_id == identity.session_instance_id,
            _INTENT.c.task_id == identity.task_id,
            _INTENT.c.task_execution_id == identity.task_execution_id,
            _INTENT.c.attempt_idempotency_key == expected.attempt_idempotency_key,
            _INTENT.c.request_payload_sha256 == expected.request_payload_sha256,
            _INTENT.c.state == "NOT_DISPATCHABLE",
        )
        .values(state="READY")
    ).rowcount


def _insert_claim_receipt(
    connection: Connection,
    expected: _Expected,
    claim_uuid: str,
    observed_at: datetime,
) -> None:
    connection.execute(
        insert(_CLAIM).values(
            claim_uuid=claim_uuid,
            authorization_uuid=expected.identity.authorization_uuid,
            intent_uuid=expected.intent_uuid,
            task_execution_id=expected.identity.task_execution_id,
            identity_sha256=expected.identity_sha256,
            attempt_idempotency_key=expected.attempt_idempotency_key,
            request_payload_sha256=expected.request_payload_sha256,
            claimed_at=observed_at,
        )
    )


def _commit_claim(connection: Connection) -> None:
    connection.exec_driver_sql("COMMIT")


def _rollback_quietly(connection: Connection) -> None:
    try:
        connection.exec_driver_sql("ROLLBACK")
    except (SQLAlchemyError, sqlite3.Error):
        # Closing/invalidating the DBAPI connection rolls back as well.
        connection.invalidate()


def _read_snapshot(connection: Connection, authorization_uuid: str):
    """One statement, so authorization/intent/receipt come from one snapshot."""

    return connection.execute(
        select(
            _AUTH.c.identity_sha256,
            _AUTH.c.state,
            _AUTH.c.expires_at,
            _INTENT.c.intent_uuid,
            _INTENT.c.state.label("launch_state"),
            _INTENT.c.attempt_idempotency_key,
            _INTENT.c.request_payload_sha256,
            _CLAIM.c.claim_uuid,
            _CLAIM.c.attempt_idempotency_key.label("claim_key"),
            _CLAIM.c.request_payload_sha256.label("claim_request_sha256"),
        )
        .select_from(
            _AUTH.outerjoin(
                _INTENT, _INTENT.c.authorization_uuid == _AUTH.c.authorization_uuid
            ).outerjoin(
                _CLAIM, _CLAIM.c.authorization_uuid == _AUTH.c.authorization_uuid
            )
        )
        .where(_AUTH.c.authorization_uuid == authorization_uuid)
    ).first()


def _reconciliation(row, expected: _Expected) -> OneShotClaimReconciliation:
    authorization_uuid = expected.identity.authorization_uuid
    if row is None:
        return OneShotClaimReconciliation(
            authorization_uuid, None, None, None, None, None, False, False, False
        )
    identity_verified = row.identity_sha256 == expected.identity_sha256
    intent_verified = (
        row.intent_uuid == expected.intent_uuid
        and row.attempt_idempotency_key == expected.attempt_idempotency_key
        and row.request_payload_sha256 == expected.request_payload_sha256
    )
    durable_ready = (
        identity_verified
        and intent_verified
        and row.state == "CLAIMED"
        and row.launch_state == "READY"
        and row.claim_uuid is not None
        and row.claim_key == expected.attempt_idempotency_key
        and row.claim_request_sha256 == expected.request_payload_sha256
    )
    return OneShotClaimReconciliation(
        authorization_uuid=authorization_uuid,
        authorization_state=row.state,
        launch_state=row.launch_state,
        claim_uuid=row.claim_uuid,
        attempt_idempotency_key=row.attempt_idempotency_key,
        request_payload_sha256=row.request_payload_sha256,
        identity_verified=identity_verified,
        intent_verified=intent_verified,
        durable_ready=durable_ready,
    )


def _rejection(row, expected: _Expected, observed_at: datetime) -> ClaimOutcome:
    """Classify a conditional-UPDATE miss; the caller rolls back either way."""

    state = _reconciliation(row, expected)
    if row is None or not state.identity_verified:
        return ClaimOutcome.IDENTITY_CONFLICT
    if row.state == "CLAIMED":
        if state.durable_ready:
            return ClaimOutcome.ALREADY_CLAIMED
        return ClaimOutcome.INTENT_CONFLICT
    if row.state == "REVOKED":
        return ClaimOutcome.REVOKED
    if row.state == "EXPIRED" or _utc(row.expires_at) <= observed_at:
        return ClaimOutcome.EXPIRED
    # AUTHORIZED and unexpired: a stored identity column or the live
    # Session generation / Task / TaskExecution mapping no longer matches.
    return ClaimOutcome.IDENTITY_CONFLICT


def _result(
    outcome: ClaimOutcome,
    expected: _Expected,
    state: OneShotClaimReconciliation | None = None,
    *,
    claim_uuid: str | None = None,
    reconciled: bool = False,
) -> OneShotClaimResult:
    return OneShotClaimResult(
        outcome=outcome,
        authorization_uuid=expected.identity.authorization_uuid,
        claim_uuid=claim_uuid if claim_uuid else (state.claim_uuid if state else None),
        authorization_state=state.authorization_state if state else None,
        launch_state=state.launch_state if state else None,
        attempt_idempotency_key=state.attempt_idempotency_key if state else None,
        request_payload_sha256=state.request_payload_sha256 if state else None,
        reconciled=reconciled,
    )


def _reconcile_after_commit_failure(
    engine: Engine, expected: _Expected, claim_uuid: str
) -> OneShotClaimResult:
    """Return success only when this claimant's own receipt is proven durable."""

    try:
        with engine.connect() as connection:
            state = _reconciliation(
                _read_snapshot(connection, expected.identity.authorization_uuid),
                expected,
            )
    except (SQLAlchemyError, sqlite3.Error):
        return _result(ClaimOutcome.COMMIT_UNCERTAIN, expected, reconciled=False)
    if state.durable_ready and state.claim_uuid == claim_uuid:
        outcome = ClaimOutcome.CLAIMED_SUCCESSFULLY
    elif state.durable_ready:
        outcome = ClaimOutcome.ALREADY_CLAIMED
    else:
        # Includes a proven rollback: F1b holds instead of retrying itself.
        outcome = ClaimOutcome.COMMIT_UNCERTAIN
    return _result(outcome, expected, state, reconciled=True)


def claim_one_shot_authorization(
    db: Session,
    identity: OneShotAuthorizationIdentity,
) -> OneShotClaimResult:
    """Atomically claim one authorization and mark its existing intent READY.

    ``db`` only supplies the engine; the claim never uses or flushes it. The
    feature gate is checked before any database access.

    Expiry rule: the claim is eligible only if the trusted clock, sampled
    after ``BEGIN IMMEDIATE`` has acquired the write lock, is strictly before
    ``expires_at``; a second sample immediately before COMMIT must also be.
    This bounds, but does not prove, that COMMIT itself completed in time.
    """

    if settings.ONE_SHOT_EXECUTION_ENABLED is not True:
        raise OneShotFeatureDisabled("one-shot execution is disabled")
    validate_identity(identity)
    expires_at = _require_aware(identity.expires_at, "expires_at")
    expected = _expected(identity)
    engine = _engine(db)
    unavailable = _schema_outcome(engine)
    if unavailable is not None:
        return _result(unavailable, expected)

    claim_uuid = str(uuid.uuid4())
    try:
        connection = engine.connect()
    except SQLAlchemyError as exc:
        outcome = _db_error_outcome(exc)
        if outcome is None:
            raise
        return _result(outcome, expected)

    with connection:
        if connection.connection.dbapi_connection.in_transaction:
            # A pooled connection that is not clean cannot give BEGIN IMMEDIATE
            # first position; refuse rather than join someone else's work.
            connection.invalidate()
            return _result(ClaimOutcome.UNSUPPORTED_DATABASE, expected)
        connection.execution_options(isolation_level="AUTOCOMMIT")
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except SQLAlchemyError as exc:
            outcome = _db_error_outcome(exc)
            if outcome is None:
                raise
            return _result(outcome, expected)

        try:
            # Sampled only once the write lock is held, so lock waiting can
            # never carry a stale pre-expiry timestamp into the transition.
            observed_at = _require_aware(_trusted_utc_now(), "now")
            try:
                claimed = _claim_authorization(connection, expected, observed_at)
            except IntegrityError:
                # A database guard refused this exact identity's transition.
                _rollback_quietly(connection)
                return _result(ClaimOutcome.IDENTITY_CONFLICT, expected)
            if claimed != 1:
                row = _read_snapshot(connection, identity.authorization_uuid)
                _rollback_quietly(connection)
                return _result(
                    _rejection(row, expected, observed_at),
                    expected,
                    _reconciliation(row, expected),
                )
            try:
                transitioned = _transition_intent(connection, expected)
                if transitioned == 1:
                    _insert_claim_receipt(connection, expected, claim_uuid, observed_at)
            except IntegrityError:
                transitioned = 0
            if transitioned != 1:
                _rollback_quietly(connection)
                return _result(ClaimOutcome.INTENT_CONFLICT, expected)
            if _require_aware(_trusted_utc_now(), "now") >= expires_at:
                _rollback_quietly(connection)
                return _result(ClaimOutcome.EXPIRED, expected)
        except SQLAlchemyError as exc:
            _rollback_quietly(connection)
            outcome = _db_error_outcome(exc)
            if outcome is None:
                raise
            return _result(outcome, expected)
        except BaseException:
            _rollback_quietly(connection)
            raise

        try:
            _commit_claim(connection)
        except (SQLAlchemyError, sqlite3.Error):
            # Discard the connection (rolling back anything uncommitted), then
            # prove the durable state from a fresh connection.
            connection.invalidate()
            return _reconcile_after_commit_failure(engine, expected, claim_uuid)

    return OneShotClaimResult(
        outcome=ClaimOutcome.CLAIMED_SUCCESSFULLY,
        authorization_uuid=identity.authorization_uuid,
        claim_uuid=claim_uuid,
        authorization_state="CLAIMED",
        launch_state="READY",
        attempt_idempotency_key=expected.attempt_idempotency_key,
        request_payload_sha256=expected.request_payload_sha256,
    )


def reconcile_one_shot_claim(
    db: Session, identity: OneShotAuthorizationIdentity
) -> OneShotClaimReconciliation:
    """Read-only claim-state reconciliation for one exact identity.

    It never creates, retries, or dispatches anything; a durable READY intent
    is reported as eligible for handoff but not externally dispatched.
    """

    validate_identity(identity)
    expected = _expected(identity)
    engine = _engine(db)
    if _schema_outcome(engine) is not None:
        raise AuthorizationSchemaUnavailable(
            "F1b claim schema is unavailable or unsupported"
        )
    with engine.connect() as connection:
        return _reconciliation(
            _read_snapshot(connection, identity.authorization_uuid), expected
        )
