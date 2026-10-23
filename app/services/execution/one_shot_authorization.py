"""Provider-free F1a authorization issuance and pre-claim state handling.

This module does not create TaskExecution rows, dispatch work, or transition
launch intents. Its sole issuance operation binds a complete caller-supplied
identity to an already-existing legacy TaskExecution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ExecutorLaunchIntent,
    OneShotExecutionAuthorization,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
)

IDENTITY_SCHEMA_VERSION = "one-shot-authorization-identity/v1"
POLICY_VERSION = "one-shot-authorization-policy/v1"
SUPPORTED_PROFILE_ID = "executor-contained-planning"
SUPPORTED_PROFILE_VERSION = "v1"
SUPPORTED_TASK_EXECUTION_PROFILE = f"{SUPPORTED_PROFILE_ID}/{SUPPORTED_PROFILE_VERSION}"
LAUNCH_REQUEST_SCHEMA_VERSION = "executor-launch-request/v1"
MIGRATION_VERSION = "057_one_shot_execution_authorization"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_CONSTRAINT_UNIQUE = 2067
_SQLITE_CONSTRAINT_PRIMARYKEY = 1555
_SQLITE_CONSTRAINT_FOREIGNKEY = 787
_IDENTITY_FIELDS = (
    "identity_schema_version",
    "policy_version",
    "authorization_uuid",
    "project_id",
    "session_id",
    "session_instance_id",
    "task_id",
    "task_execution_id",
    "source_revision_identity",
    "source_snapshot_digest",
    "candidate_uuid",
    "candidate_initial_digest",
    "execution_profile_id",
    "execution_profile_version",
    "operation_scope_id",
    "operation_scope_version",
    "operation_scope_digest",
    "operator_approval_id",
    "operator_identity",
    "operator_approval_decision",
    "operator_approved_at",
    "issued_at",
    "expires_at",
)
_TEXT_LIMITS = {
    "identity_schema_version": 64,
    "policy_version": 64,
    "source_revision_identity": 255,
    "execution_profile_id": 128,
    "execution_profile_version": 64,
    "operation_scope_id": 128,
    "operation_scope_version": 64,
    "operator_approval_id": 255,
    "operator_identity": 255,
}


class OneShotAuthorizationError(RuntimeError):
    """Base class for fail-closed F1a authorization errors."""


class OneShotFeatureDisabled(OneShotAuthorizationError):
    pass


class InvalidAuthorizationIdentity(OneShotAuthorizationError, ValueError):
    pass


class AuthorizationAssociationMismatch(OneShotAuthorizationError):
    pass


class UnsupportedExecutionProfile(OneShotAuthorizationError):
    pass


class AmbiguousExecutionMapping(OneShotAuthorizationError):
    pass


class AuthorizationAlreadyExists(OneShotAuthorizationError):
    pass


class AuthorizationNotFound(OneShotAuthorizationError):
    pass


class AuthorizationStateConflict(OneShotAuthorizationError):
    pass


class AuthorizationSchemaUnavailable(OneShotAuthorizationError):
    pass


class AuthorizationIntegrityError(OneShotAuthorizationError):
    """A database guard other than uniqueness or a reference refused issuance."""


@dataclass(frozen=True)
class OneShotAuthorizationIdentity:
    """Complete, precomputed immutable identity accepted by F1a issuance."""

    identity_schema_version: str
    policy_version: str
    authorization_uuid: str
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
    operator_approval_id: str
    operator_identity: str
    operator_approval_decision: str
    operator_approved_at: datetime
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        validate_identity(self)


@dataclass(frozen=True)
class OneShotAuthorizationResult:
    authorization_uuid: str
    task_execution_id: int
    identity_sha256: str
    state: str
    launch_intent_uuid: str
    launch_state: str
    attempt_idempotency_key: str
    request_payload_sha256: str


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidAuthorizationIdentity(f"{field} must be a non-empty exact string")
    if unicodedata.normalize("NFC", value) != value:
        raise InvalidAuthorizationIdentity(f"{field} must use NFC Unicode")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise InvalidAuthorizationIdentity(f"{field} contains a control character")


def _validate_uuid(value: str, field: str) -> None:
    _validate_text(value, field)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidAuthorizationIdentity(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidAuthorizationIdentity(
            f"{field} must be a lowercase canonical UUID"
        )


def _validate_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise InvalidAuthorizationIdentity(
            f"{field} must be a lowercase SHA-256 digest"
        )


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidAuthorizationIdentity(f"{field} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (ValueError, OverflowError) as exc:
        raise InvalidAuthorizationIdentity(f"{field} has an invalid timezone") from exc
    if offset is None:
        raise InvalidAuthorizationIdentity(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime, field: str) -> str:
    return (
        _require_aware(value, field)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive DateTime readback to UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_identity(identity: OneShotAuthorizationIdentity) -> None:
    for field in (
        "identity_schema_version",
        "policy_version",
        "source_revision_identity",
        "execution_profile_id",
        "execution_profile_version",
        "operation_scope_id",
        "operation_scope_version",
        "operator_approval_id",
        "operator_identity",
    ):
        value = getattr(identity, field)
        _validate_text(value, field)
        if len(value) > _TEXT_LIMITS[field]:
            raise InvalidAuthorizationIdentity(f"{field} exceeds its schema limit")

    if identity.identity_schema_version != IDENTITY_SCHEMA_VERSION:
        raise InvalidAuthorizationIdentity("unsupported identity schema version")
    if identity.policy_version != POLICY_VERSION:
        raise InvalidAuthorizationIdentity("unsupported authorization policy version")
    _validate_uuid(identity.authorization_uuid, "authorization_uuid")
    _validate_uuid(identity.session_instance_id, "session_instance_id")
    _validate_uuid(identity.candidate_uuid, "candidate_uuid")

    for field in ("project_id", "session_id", "task_id", "task_execution_id"):
        value = getattr(identity, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidAuthorizationIdentity(f"{field} must be a positive integer")

    _validate_digest(identity.source_snapshot_digest, "source_snapshot_digest")
    _validate_digest(identity.candidate_initial_digest, "candidate_initial_digest")
    _validate_digest(identity.operation_scope_digest, "operation_scope_digest")

    if (
        identity.execution_profile_id != SUPPORTED_PROFILE_ID
        or identity.execution_profile_version != SUPPORTED_PROFILE_VERSION
    ):
        raise UnsupportedExecutionProfile(
            "only executor-contained-planning/v1 is supported"
        )
    if identity.operator_approval_decision != "APPROVED":
        raise InvalidAuthorizationIdentity(
            "explicit APPROVED operator provenance is required"
        )

    approved_at = _require_aware(identity.operator_approved_at, "operator_approved_at")
    issued_at = _require_aware(identity.issued_at, "issued_at")
    expires_at = _require_aware(identity.expires_at, "expires_at")
    if approved_at > issued_at:
        raise InvalidAuthorizationIdentity("approval must precede or equal issuance")
    if expires_at <= issued_at:
        raise InvalidAuthorizationIdentity("expiry must be after issuance")


def canonical_identity_payload(
    identity: OneShotAuthorizationIdentity,
) -> dict[str, object]:
    """Return the explicit versioned canonical field mapping."""

    validate_identity(identity)
    payload: dict[str, object] = {}
    for field in _IDENTITY_FIELDS:
        value = getattr(identity, field)
        if field in {"operator_approved_at", "issued_at", "expires_at"}:
            value = _utc_text(value, field)
        payload[field] = value
    return payload


def canonical_identity_bytes(identity: OneShotAuthorizationIdentity) -> bytes:
    return json.dumps(
        canonical_identity_payload(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_identity_sha256(identity: OneShotAuthorizationIdentity) -> str:
    return hashlib.sha256(canonical_identity_bytes(identity)).hexdigest()


def _require_sqlite_schema(bind) -> None:
    if bind.dialect.name != "sqlite":
        raise AuthorizationSchemaUnavailable(
            "F1a issuance is disabled for database engines without reviewed immutable guards"
        )
    try:
        from app.db_migrations import _verify_057_one_shot_execution_authorization

        _verify_057_one_shot_execution_authorization(bind)
        with bind.connect() as connection:
            applied = connection.exec_driver_sql(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).first()
            trigger_rows = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN (?, ?, ?, ?)",
                (
                    "trg_one_shot_auth_immutable_state_guard",
                    "trg_one_shot_auth_no_delete",
                    "trg_executor_launch_intent_immutable",
                    "trg_executor_launch_intent_no_delete",
                ),
            ).fetchall()
    except SQLAlchemyError as exc:
        raise AuthorizationSchemaUnavailable(
            "F1a schema migration and immutable guards are unavailable"
        ) from exc
    if applied is None or len(trigger_rows) != 4:
        raise AuthorizationSchemaUnavailable(
            "F1a schema migration and immutable guards are not verified"
        )


def _identity_sha(identity: OneShotAuthorizationIdentity) -> str:
    return canonical_identity_sha256(identity)


def _intent_identity(
    identity: OneShotAuthorizationIdentity, identity_sha: str
) -> tuple[str, str, str]:
    auth_uuid = uuid.UUID(identity.authorization_uuid)
    intent_uuid = str(uuid.uuid5(auth_uuid, "executor-launch-intent/v1"))
    idempotency_key = hashlib.sha256(
        b"orchestrator-executor-attempt/v1\0"
        + identity.authorization_uuid.encode("ascii")
    ).hexdigest()
    request_payload = {
        "schema_version": LAUNCH_REQUEST_SCHEMA_VERSION,
        "authorization_uuid": identity.authorization_uuid,
        "authorization_identity_sha256": identity_sha,
        "attempt_idempotency_key": idempotency_key,
        "project_id": identity.project_id,
        "session_id": identity.session_id,
        "session_instance_id": identity.session_instance_id,
        "task_id": identity.task_id,
        "task_execution_id": identity.task_execution_id,
    }
    request_bytes = json.dumps(
        request_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return intent_uuid, idempotency_key, hashlib.sha256(request_bytes).hexdigest()


def _verify_existing_associations(
    db: Session, identity: OneShotAuthorizationIdentity
) -> None:
    project = db.get(Project, identity.project_id)
    session = db.get(SessionModel, identity.session_id)
    task = db.get(Task, identity.task_id)
    execution = db.get(TaskExecution, identity.task_execution_id)
    if project is None or session is None or task is None or execution is None:
        raise AuthorizationAssociationMismatch(
            "Project, Session, Task, and pre-existing TaskExecution are all required"
        )
    if session.project_id != project.id or task.project_id != project.id:
        raise AuthorizationAssociationMismatch("Project/Session/Task mapping conflicts")
    if execution.session_id != session.id or execution.task_id != task.id:
        raise AuthorizationAssociationMismatch("TaskExecution mapping conflicts")
    if (
        session.instance_id is None
        or session.instance_id != identity.session_instance_id
    ):
        raise AuthorizationAssociationMismatch(
            "Session generation is missing or mismatched"
        )
    if execution.runtime_lease_id is not None:
        raise AmbiguousExecutionMapping(
            "legacy TaskExecution has a Phase 29 runtime lease mapping"
        )
    if task.execution_profile != SUPPORTED_TASK_EXECUTION_PROFILE:
        raise UnsupportedExecutionProfile(
            "Task execution_profile does not match executor-contained-planning/v1"
        )


def _result(
    authorization: OneShotExecutionAuthorization,
    intent: ExecutorLaunchIntent,
) -> OneShotAuthorizationResult:
    return OneShotAuthorizationResult(
        authorization_uuid=authorization.authorization_uuid,
        task_execution_id=authorization.task_execution_id,
        identity_sha256=authorization.identity_sha256,
        state=authorization.state,
        launch_intent_uuid=intent.intent_uuid,
        launch_state=intent.state,
        attempt_idempotency_key=intent.attempt_idempotency_key,
        request_payload_sha256=intent.request_payload_sha256,
    )


def issue_one_shot_authorization(
    db: Session,
    identity: OneShotAuthorizationIdentity,
) -> OneShotAuthorizationResult:
    """Atomically issue one authorization and one non-dispatchable intent.

    The feature gate is checked before touching the SQLAlchemy Session. This
    function never creates a TaskExecution or invokes execution infrastructure.
    """

    if settings.ONE_SHOT_EXECUTION_ENABLED is not True:
        raise OneShotFeatureDisabled("one-shot execution is disabled")
    validate_identity(identity)
    bind = db.get_bind()
    _require_sqlite_schema(bind)

    identity_sha = _identity_sha(identity)
    intent_uuid, idempotency_key, request_sha = _intent_identity(identity, identity_sha)
    now_utc = datetime.now(timezone.utc)
    if _require_aware(identity.expires_at, "expires_at") <= now_utc:
        raise InvalidAuthorizationIdentity(
            "cannot issue an already-expired authorization"
        )

    authorization = OneShotExecutionAuthorization(
        authorization_uuid=identity.authorization_uuid,
        identity_schema_version=identity.identity_schema_version,
        policy_version=identity.policy_version,
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
        operator_approval_id=identity.operator_approval_id,
        operator_identity=identity.operator_identity,
        operator_approval_decision=identity.operator_approval_decision,
        operator_approved_at=_require_aware(
            identity.operator_approved_at, "operator_approved_at"
        ),
        issued_at=_require_aware(identity.issued_at, "issued_at"),
        expires_at=_require_aware(identity.expires_at, "expires_at"),
        identity_sha256=identity_sha,
        state="AUTHORIZED",
        state_changed_at=None,
        revoked_by=None,
        created_at=_require_aware(identity.issued_at, "issued_at"),
    )
    intent = ExecutorLaunchIntent(
        intent_uuid=intent_uuid,
        authorization_uuid=identity.authorization_uuid,
        project_id=identity.project_id,
        session_id=identity.session_id,
        session_instance_id=identity.session_instance_id,
        task_id=identity.task_id,
        task_execution_id=identity.task_execution_id,
        attempt_idempotency_key=idempotency_key,
        request_payload_sha256=request_sha,
        state="NOT_DISPATCHABLE",
        created_at=_require_aware(identity.issued_at, "issued_at"),
    )

    try:
        with Session(bind=bind, autoflush=False, expire_on_commit=False) as issue_db:
            with issue_db.begin():
                _verify_existing_associations(issue_db, identity)
                # No relationship() orders these mappers, so flush the parent
                # first; under PRAGMA foreign_keys=ON the intent FK needs it.
                issue_db.add(authorization)
                issue_db.flush()
                issue_db.add(intent)
                issue_db.flush()
    except IntegrityError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", None)
        if code in (_SQLITE_CONSTRAINT_UNIQUE, _SQLITE_CONSTRAINT_PRIMARYKEY):
            raise AuthorizationAlreadyExists(
                "an authorization or launch intent already exists for this identity"
            ) from exc
        if code == _SQLITE_CONSTRAINT_FOREIGNKEY:
            raise AuthorizationAssociationMismatch(
                "a referenced Project, Session, Task, or TaskExecution is missing"
            ) from exc
        raise AuthorizationIntegrityError(
            "authorization issuance violated a database integrity guard"
        ) from exc
    return _result(authorization, intent)


def inspect_authorization(
    db: Session,
    authorization_uuid: str,
    *,
    now: datetime | None = None,
) -> OneShotAuthorizationResult:
    """Inspect state and persist AUTHORIZED→EXPIRED when its deadline passed."""

    _validate_uuid(authorization_uuid, "authorization_uuid")
    bind = db.get_bind()
    _require_sqlite_schema(bind)
    observed_at = _require_aware(now or datetime.now(timezone.utc), "now")
    with Session(bind=bind, autoflush=False, expire_on_commit=False) as state_db:
        with state_db.begin():
            authorization = state_db.get(
                OneShotExecutionAuthorization, authorization_uuid
            )
            if authorization is None:
                raise AuthorizationNotFound(authorization_uuid)
            if (
                authorization.state == "AUTHORIZED"
                and _utc(authorization.expires_at) <= observed_at
            ):
                authorization.state = "EXPIRED"
                authorization.state_changed_at = observed_at
                authorization.revoked_by = None
                state_db.flush()
            intent = state_db.scalar(
                select(ExecutorLaunchIntent).where(
                    ExecutorLaunchIntent.authorization_uuid == authorization_uuid
                )
            )
            if intent is None:
                raise AuthorizationSchemaUnavailable(
                    "authorization has no durable launch intent"
                )
            return _result(authorization, intent)


def revoke_authorization(
    db: Session,
    authorization_uuid: str,
    *,
    operator_identity: str,
    now: datetime | None = None,
) -> OneShotAuthorizationResult:
    """Revoke an unexpired authorization; terminal states remain terminal."""

    _validate_uuid(authorization_uuid, "authorization_uuid")
    _validate_text(operator_identity, "operator_identity")
    bind = db.get_bind()
    _require_sqlite_schema(bind)
    observed_at = _require_aware(now or datetime.now(timezone.utc), "now")
    with Session(bind=bind, autoflush=False, expire_on_commit=False) as state_db:
        with state_db.begin():
            authorization = state_db.get(
                OneShotExecutionAuthorization, authorization_uuid
            )
            if authorization is None:
                raise AuthorizationNotFound(authorization_uuid)
            if authorization.state == "AUTHORIZED":
                if _utc(authorization.expires_at) <= observed_at:
                    authorization.state = "EXPIRED"
                    authorization.revoked_by = None
                else:
                    authorization.state = "REVOKED"
                    authorization.revoked_by = operator_identity
                authorization.state_changed_at = observed_at
                state_db.flush()
            elif authorization.state not in {"REVOKED", "EXPIRED"}:
                raise AuthorizationStateConflict(
                    f"authorization in state {authorization.state} cannot be revoked"
                )

            intent = state_db.scalar(
                select(ExecutorLaunchIntent).where(
                    ExecutorLaunchIntent.authorization_uuid == authorization_uuid
                )
            )
            if intent is None:
                raise AuthorizationSchemaUnavailable(
                    "authorization has no durable launch intent"
                )
            return _result(authorization, intent)
