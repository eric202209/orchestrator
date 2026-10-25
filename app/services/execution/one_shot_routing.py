"""Provider-free F1c-2 routing of a one-shot Celery delivery.

The canonical Worker entry (``execute_orchestration_task``) asks this module
whether a delivery is one-shot before any legacy lifecycle work. A one-shot
delivery never falls through to the legacy path. Routing is decided only by
durable F1b state, never by process memory or Celery delivery flags:

- AUTHORIZED: preflight Worker identity and Supervisor admission, then claim.
  Only this invocation's own proven-committed claim may START.
- CLAIMED: some earlier invocation claimed; RECONCILE_ONLY, never START.
  A durable READY intent does not prove whether a start was submitted.
- Uncertain claim commit: F1b reconciles; anything unproven holds.

The module never writes the database except through the F1b claim, never
mints a key, and never retries. Outcomes are logged only; logs are not
execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Callable, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import OneShotExecutionAuthorization, Task
from app.services.execution.one_shot_authorization import (
    SUPPORTED_TASK_EXECUTION_PROFILE,
    _IDENTITY_FIELDS,
    OneShotAuthorizationError,
    OneShotAuthorizationIdentity,
    OneShotFeatureDisabled,
    _utc,
    canonical_identity_sha256,
)
from app.services.execution.one_shot_claim import (
    ClaimOutcome,
    _engine,
    _schema_outcome,
    claim_one_shot_authorization,
    reconcile_one_shot_claim,
)
from app.services.execution.one_shot_dispatch import (
    DispatchMode,
    DispatchOutcome,
    InvalidDispatchRequest,
    OneShotDispatchRequest,
    OneShotSupervisor,
    WorkerIdentityAttestation,
    _live_execution_reasons,
    _supervisor_outcome,
    _worker_outcome,
    dispatch_one_shot_intent,
)

logger = logging.getLogger(__name__)

ONE_SHOT_CONTEXT_KEY = "one_shot_authorization_uuid"
_DATETIME_FIELDS = {"operator_approved_at", "issued_at", "expires_at"}


class RoutingStatus(str, Enum):
    HANDED_OFF = "HANDED_OFF"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"
    HELD = "HELD"
    REFUSED = "REFUSED"


_DISPATCH_STATUS = {
    DispatchOutcome.DISPATCHED: RoutingStatus.HANDED_OFF,
    DispatchOutcome.RECONCILED: RoutingStatus.RECONCILED,
    DispatchOutcome.SUPERVISOR_REJECTED: RoutingStatus.REJECTED,
    # READY with no Supervisor attempt, seen by a non-claimant: whether the
    # claimant's start was ever submitted is not durably provable.
    DispatchOutcome.NOT_STARTED: RoutingStatus.HELD,
    DispatchOutcome.HOLD_UNKNOWN: RoutingStatus.HELD,
    DispatchOutcome.HOLD_RECEIPT_CONFLICT: RoutingStatus.HELD,
}


@dataclass(frozen=True)
class OneShotDelivery:
    """The fields of one ``execute_orchestration_task`` delivery."""

    session_id: int
    task_id: int
    task_execution_id: int | None
    expected_session_instance_id: str | None
    authorization_uuid: object
    continuation_present: bool = False
    redelivered: bool = False


@dataclass(frozen=True)
class OneShotRoutingResult:
    status: RoutingStatus
    reason: str
    authorization_uuid: str | None = None
    attempt_key_prefix: str | None = None
    mode: str | None = None
    claim_outcome: str | None = None
    claim_reconciled: bool = False
    dispatch_outcome: str | None = None
    attempt_status: str | None = None
    execution_result: dict[str, Any] | None = None
    start_submitted: bool = False
    redelivered: bool = False

    def as_task_result(self) -> dict[str, Any]:
        return {
            "status": "one_shot_" + self.status.value.lower(),
            "one_shot": {
                "status": self.status.value,
                "reason": self.reason,
                "authorization_uuid": self.authorization_uuid,
                "attempt_key_prefix": self.attempt_key_prefix,
                "mode": self.mode,
                "claim_outcome": self.claim_outcome,
                "claim_reconciled": self.claim_reconciled,
                "dispatch_outcome": self.dispatch_outcome,
                "attempt_status": self.attempt_status,
                "execution_result": self.execution_result,
                "start_submitted": self.start_submitted,
                "redelivered": self.redelivered,
            },
        }


def requested_authorization_uuid(context: object) -> object:
    if isinstance(context, Mapping):
        return context.get(ONE_SHOT_CONTEXT_KEY)
    return None


def is_one_shot_delivery(db: Session, task_id: int, context: object) -> bool:
    """True for an explicit authorization or a one-shot-profile Task.

    Database errors propagate: an unknown classification must not fall
    through to the legacy path.
    """

    if requested_authorization_uuid(context) is not None:
        return True
    profile = db.scalar(select(Task.execution_profile).where(Task.id == task_id))
    return profile == SUPPORTED_TASK_EXECUTION_PROFILE


def _identity_from_row(row: OneShotExecutionAuthorization):
    values = {}
    for field in _IDENTITY_FIELDS:
        value = getattr(row, field)
        values[field] = _utc(value) if field in _DATETIME_FIELDS else value
    return OneShotAuthorizationIdentity(**values)


def _valid_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _route(
    db: Session,
    delivery: OneShotDelivery,
    *,
    worker_identity: Callable[[], WorkerIdentityAttestation | None],
    supervisor: OneShotSupervisor,
    reconcile_only: bool,
) -> OneShotRoutingResult:
    def done(status, reason, **fields):
        return OneShotRoutingResult(
            status, reason, redelivered=delivery.redelivered, **fields
        )

    if settings.ONE_SHOT_EXECUTION_ENABLED is not True:
        return done(RoutingStatus.REFUSED, "FEATURE_DISABLED")
    auth_uuid = delivery.authorization_uuid
    if (
        not _valid_uuid(auth_uuid)
        or not isinstance(delivery.task_execution_id, int)
        or not _valid_uuid(delivery.expected_session_instance_id)
        or delivery.continuation_present
    ):
        return done(RoutingStatus.REFUSED, "PAYLOAD_INVALID")
    fields = {"authorization_uuid": auth_uuid}

    unavailable = _schema_outcome(_engine(db))
    if unavailable is not None:
        return done(RoutingStatus.REFUSED, unavailable.value, **fields)
    try:
        row = db.get(OneShotExecutionAuthorization, auth_uuid)
        if row is None:
            return done(RoutingStatus.REFUSED, "AUTHORITY_MISSING", **fields)
        try:
            identity = _identity_from_row(row)
        except OneShotAuthorizationError:
            return done(RoutingStatus.REFUSED, "AUTHORITY_MISMATCH", **fields)
        stored_sha, state = row.identity_sha256, row.state
        expires_at = _utc(row.expires_at)
    finally:
        # End the read before the claim takes its own write transaction.
        db.rollback()
    if canonical_identity_sha256(identity) != stored_sha or (
        identity.session_id,
        identity.task_id,
        identity.task_execution_id,
        identity.session_instance_id,
    ) != (
        delivery.session_id,
        delivery.task_id,
        delivery.task_execution_id,
        delivery.expected_session_instance_id,
    ):
        return done(RoutingStatus.REFUSED, "AUTHORITY_MISMATCH", **fields)
    if state == "REVOKED":
        return done(RoutingStatus.REFUSED, "AUTHORIZATION_REVOKED", **fields)
    if state == "EXPIRED" or (
        state == "AUTHORIZED" and expires_at <= datetime.now(timezone.utc)
    ):
        return done(RoutingStatus.REFUSED, "AUTHORIZATION_EXPIRED", **fields)

    attestation = worker_identity()
    claim = None
    if state == "AUTHORIZED":
        if reconcile_only:
            return done(RoutingStatus.REFUSED, "NOT_CLAIMED", **fields)
        # A claim is irreversible: claim only when a START could proceed.
        try:
            live_reasons = _live_execution_reasons(db, identity)
        finally:
            db.rollback()
        if live_reasons:
            return done(RoutingStatus.REFUSED, "EXECUTION_STATE_CONFLICT", **fields)
        # Short-circuit in order: no Supervisor contact on an identity refusal.
        for check in (
            lambda: _worker_outcome(attestation),
            lambda: _supervisor_outcome(supervisor),
        ):
            refused, _ = check()
            if refused is not None:
                return done(RoutingStatus.REFUSED, refused.value, **fields)
        claim = claim_one_shot_authorization(db, identity)
        fields.update(
            claim_outcome=claim.outcome.value, claim_reconciled=claim.reconciled
        )
        if claim.outcome is ClaimOutcome.CLAIMED_SUCCESSFULLY:
            request = OneShotDispatchRequest.from_claim(identity, claim)
            mode = DispatchMode.START
        elif claim.outcome is ClaimOutcome.COMMIT_UNCERTAIN:
            return done(RoutingStatus.HELD, claim.outcome.value, **fields)
        elif claim.outcome is not ClaimOutcome.ALREADY_CLAIMED:
            return done(RoutingStatus.REFUSED, claim.outcome.value, **fields)
    if claim is None or claim.outcome is ClaimOutcome.ALREADY_CLAIMED:
        # Another invocation (or an earlier delivery) owns the claim.
        try:
            request = OneShotDispatchRequest.from_reconciliation(
                identity, reconcile_one_shot_claim(db, identity)
            )
        except InvalidDispatchRequest:
            return done(RoutingStatus.HELD, "CLAIM_NOT_DURABLY_READY", **fields)
        mode = DispatchMode.RECONCILE_ONLY

    fields.update(
        attempt_key_prefix=request.attempt_idempotency_key[:16], mode=mode.value
    )
    dispatched = dispatch_one_shot_intent(
        db,
        request,
        worker_identity=attestation,
        supervisor=supervisor,
        mode=mode,
    )
    return done(
        _DISPATCH_STATUS.get(dispatched.outcome, RoutingStatus.REFUSED),
        dispatched.outcome.value,
        dispatch_outcome=dispatched.outcome.value,
        attempt_status=(
            dispatched.attempt_status.value if dispatched.attempt_status else None
        ),
        execution_result=dispatched.execution_result,
        start_submitted=dispatched.start_submitted,
        **fields,
    )


def route_one_shot_delivery(
    db: Session,
    delivery: OneShotDelivery,
    *,
    worker_identity: Callable[[], WorkerIdentityAttestation | None],
    supervisor: OneShotSupervisor,
    reconcile_only: bool = False,
) -> OneShotRoutingResult:
    """Route one recognized one-shot delivery; never raises an Exception.

    ``reconcile_only`` is for recovery callers: it never claims or starts.
    """

    try:
        result = _route(
            db,
            delivery,
            worker_identity=worker_identity,
            supervisor=supervisor,
            reconcile_only=reconcile_only,
        )
    except OneShotFeatureDisabled:
        result = OneShotRoutingResult(
            RoutingStatus.REFUSED, "FEATURE_DISABLED", redelivered=delivery.redelivered
        )
    except Exception as exc:
        # Fail closed: no fallback to the legacy path, no retry, no new key.
        logger.warning(
            "[ONE_SHOT] routing error session_id=%s task_id=%s error_type=%s",
            delivery.session_id,
            delivery.task_id,
            type(exc).__name__,
        )
        result = OneShotRoutingResult(
            RoutingStatus.HELD,
            "ROUTING_ERROR",
            authorization_uuid=(
                delivery.authorization_uuid
                if _valid_uuid(delivery.authorization_uuid)
                else None
            ),
            redelivered=delivery.redelivered,
        )
    logger.info(
        "[ONE_SHOT] routing status=%s reason=%s session_id=%s task_id=%s "
        "task_execution_id=%s authorization=%s attempt_key_prefix=%s mode=%s "
        "claim_outcome=%s claim_reconciled=%s dispatch_outcome=%s "
        "attempt_status=%s start_submitted=%s redelivered=%s",
        result.status.value,
        result.reason,
        delivery.session_id,
        delivery.task_id,
        delivery.task_execution_id,
        result.authorization_uuid,
        result.attempt_key_prefix,
        result.mode,
        result.claim_outcome,
        result.claim_reconciled,
        result.dispatch_outcome,
        result.attempt_status,
        result.start_submitted,
        result.redelivered,
    )
    return result
