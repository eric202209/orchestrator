"""E8 lost-continuation-delivery reconciliation.

A continuation is durably committed before it is published, so a broker
publication or delivery loss after that commit leaves a Session permanently
`retry_pending`: nonterminal, nonquiescent, and with nothing left to deliver
it.  This module restores the *transport* for such a marker.  It never
manufactures lifecycle truth: eligibility, generation fencing and the final
claim stay with the accepted E1/E2 authority, and a reconciled delivery must
still pass the ordinary strict `claim_continuation`.

The reconciler is deliberately conservative.  When it cannot establish that no
matching physical delivery already exists, it declines and leaves the marker
untouched rather than publishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.config import settings
from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
)
from app.services.orchestration.lifecycle.transitions import (
    ContinuationIdentity,
    resolve_continuation_identity,
    validate_continuation,
)

logger = logging.getLogger(__name__)


RECONCILIATION_EVENT = "CONTINUATION_DELIVERY_RECONCILIATION"

# Outcomes.  Every candidate receives exactly one.
NOT_ELIGIBLE = "NOT_ELIGIBLE"
NOT_DUE = "NOT_DUE"
DELIVERY_PRESENT = "DELIVERY_PRESENT"
REPUBLISHED = "REPUBLISHED"
STALE_GENERATION = "STALE_GENERATION"
ALREADY_CLAIMED = "ALREADY_CLAIMED"
PHYSICAL_STATE_UNCERTAIN = "PHYSICAL_STATE_UNCERTAIN"
INVALID_CONTINUATION = "INVALID_CONTINUATION"
RECONCILIATION_LOCKED = "RECONCILIATION_LOCKED"
RECONCILIATION_ERROR = "RECONCILIATION_ERROR"

RECONCILIATION_OUTCOMES = (
    NOT_ELIGIBLE,
    NOT_DUE,
    DELIVERY_PRESENT,
    REPUBLISHED,
    STALE_GENERATION,
    ALREADY_CLAIMED,
    PHYSICAL_STATE_UNCERTAIN,
    INVALID_CONTINUATION,
    RECONCILIATION_LOCKED,
    RECONCILIATION_ERROR,
)

# `validate_continuation` is the canonical eligibility authority; this maps its
# rejection reasons onto reconciliation outcomes without reinterpreting them.
_VALIDATION_REASON_OUTCOMES = {
    "session_missing": NOT_ELIGIBLE,
    "stale_or_duplicate_continuation": STALE_GENERATION,
    "continuation_attempt_not_pending": ALREADY_CLAIMED,
    "autonomous_execution_already_active": ALREADY_CLAIMED,
}

ORCHESTRATION_TASK_NAME = "app.tasks.worker.execute_orchestration_task"


@dataclass(frozen=True)
class DeliveryProbeResult:
    """Physical delivery diagnostics for one durable continuation identity.

    ``present`` is tri-state on purpose: ``None`` means the physical state
    could not be established and the caller must not publish.
    """

    present: bool | None
    sources: tuple[str, ...] = ()
    error: str | None = None

    @property
    def uncertain(self) -> bool:
        return self.present is None


@dataclass
class ReconciliationDecision:
    session_id: int
    outcome: str
    reason: str
    task_id: int | None = None
    task_execution_id: int | None = None
    continuation_kind: str | None = None
    retry_count: int | None = None
    instance_id: str | None = None
    observed_at: datetime | None = None
    due_at: datetime | None = None
    delivery_sources: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "session_id": self.session_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "task_id": self.task_id,
            "task_execution_id": self.task_execution_id,
            "continuation_kind": self.continuation_kind,
            "continuation_retry_count": self.retry_count,
            "session_instance_id": self.instance_id,
        }
        if self.observed_at is not None:
            evidence["observed_at"] = _utc(self.observed_at).isoformat()
        if self.due_at is not None:
            evidence["due_at"] = _utc(self.due_at).isoformat()
        if self.delivery_sources:
            evidence["delivery_sources"] = list(self.delivery_sources)
        if self.details:
            evidence.update(self.details)
        return evidence


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _now(value: datetime | None) -> datetime:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Physical delivery diagnostics
# ---------------------------------------------------------------------------


def _iter_inspected_requests(payload: Any) -> Iterable[dict[str, Any]]:
    """Yield task request dicts from a Celery inspect() payload."""

    if not isinstance(payload, dict):
        return
    for entries in payload.values():
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # scheduled() wraps the task under "request"; active()/reserved()
            # return the request shape directly.
            request = entry.get("request")
            yield request if isinstance(request, dict) else entry


def _request_matches(request: dict[str, Any], identity: ContinuationIdentity) -> bool:
    if str(request.get("name") or "") != ORCHESTRATION_TASK_NAME:
        return False
    kwargs = request.get("kwargs")
    if isinstance(kwargs, str):
        try:
            kwargs = json.loads(kwargs)
        except ValueError:
            kwargs = None
    if not isinstance(kwargs, dict):
        return False
    return (
        kwargs.get("session_id") == identity.session_id
        and kwargs.get("task_id") == identity.continuation_task_id
        and kwargs.get("task_execution_id") == identity.task_execution_id
        and kwargs.get("continuation_kind") == identity.continuation_kind
        and kwargs.get("expected_session_instance_id") == identity.instance_id
    )


def inspect_continuation_delivery(
    identity: ContinuationIdentity,
    *,
    inspector: Any | None = None,
    timeout: float | None = None,
) -> DeliveryProbeResult:
    """Return whether a matching physical delivery already exists.

    Celery and Redis stay physical diagnostics here: this answers only "is the
    transport still carrying this exact durable identity", never "what is the
    Session's lifecycle state".  A worker that cannot be reached, or a probe
    that raises, yields ``present=None`` so the caller fails closed.
    """

    try:
        if inspector is None:
            from app.celery_app import celery_app

            inspector = celery_app.control.inspect(
                timeout=(
                    settings.CONTINUATION_RECONCILIATION_INSPECT_TIMEOUT_SECONDS
                    if timeout is None
                    else timeout
                )
            )
        matched: list[str] = []
        responded = False
        for source in ("active", "reserved", "scheduled"):
            probe = getattr(inspector, source, None)
            payload = probe() if callable(probe) else None
            if payload is None:
                # No worker replied for this channel; the physical state for
                # it is unknown, not empty.
                continue
            responded = True
            for request in _iter_inspected_requests(payload):
                if _request_matches(request, identity):
                    matched.append(source)
                    break
        if matched:
            return DeliveryProbeResult(present=True, sources=tuple(matched))
        if not responded:
            return DeliveryProbeResult(
                present=None, error="no_worker_inspection_response"
            )
        return DeliveryProbeResult(present=False)
    except Exception as exc:  # noqa: BLE001 - never raise into the sweep
        return DeliveryProbeResult(present=None, error=str(exc)[:300])


# ---------------------------------------------------------------------------
# Republication
# ---------------------------------------------------------------------------


def _celery_delivery_retries(identity: ContinuationIdentity) -> int:
    """Reproduce the Celery delivery retry counter the original publication set.

    This is transport accounting, not logical retry accounting.  ``celery_retry``
    and ``backend_capacity`` were published by ``Task.retry``, which stamps the
    message with the incremented counter that the durable marker also stores;
    ``automatic_recovery`` was published by a fresh ``delay()`` whose counter is
    zero.  Reproducing each exactly keeps retry budgets identical, and the
    capacity path additionally rejects any delivery whose counter disagrees
    with the marker.
    """

    if identity.continuation_kind == "automatic_recovery":
        return 0
    return identity.retry_count


def publish_continuation_delivery(
    db: DbSession,
    identity: ContinuationIdentity,
    *,
    task: Task | None = None,
    timeout_seconds: int | None = None,
) -> str | None:
    """Re-publish the exact durable continuation identity; return the task id."""

    from app.services.session.session_runtime_service import (
        DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
        build_task_execution_prompt,
    )
    from app.tasks.worker import execute_orchestration_task

    if task is None:
        task = db.query(Task).filter(Task.id == identity.continuation_task_id).first()
    if task is None:
        raise LookupError("continuation_task_missing")

    result = execute_orchestration_task.apply_async(
        kwargs={
            "session_id": identity.session_id,
            "task_id": identity.continuation_task_id,
            "prompt": build_task_execution_prompt(task),
            "timeout_seconds": (
                DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS
                if timeout_seconds is None
                else timeout_seconds
            ),
            "expected_session_instance_id": identity.instance_id,
            "task_execution_id": identity.task_execution_id,
            "continuation_task_id": identity.continuation_task_id,
            "continuation_kind": identity.continuation_kind,
            "continuation_retry_count": identity.retry_count,
        },
        retries=_celery_delivery_retries(identity),
    )
    return getattr(result, "id", None)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def continuation_due_at(
    session: SessionModel,
    *,
    grace_seconds: int | None = None,
) -> datetime:
    """Return the instant a durable continuation becomes reconciliation-eligible.

    A legitimate delayed delivery keeps its intended time: eligibility starts
    at the marker's own ``continuation_retry_eta`` (or, when it has none, the
    lifecycle transition that committed it) plus one explicit grace interval.
    """

    grace = (
        settings.CONTINUATION_RECONCILIATION_GRACE_SECONDS
        if grace_seconds is None
        else grace_seconds
    )
    anchor = (
        getattr(session, "continuation_retry_eta", None)
        or getattr(session, "lifecycle_updated_at", None)
        or getattr(session, "updated_at", None)
        or getattr(session, "created_at", None)
        or datetime.now(timezone.utc)
    )
    return _utc(anchor) + timedelta(seconds=max(int(grace), 0))


def _recent_republish_record(
    db: DbSession,
    identity: ContinuationIdentity,
    *,
    now: datetime,
    suppression_seconds: int | None = None,
) -> LogEntry | None:
    """Return a durable republication record for this exact identity, if recent.

    Two reconcilers that serialize on the project lock must not both publish;
    the winner's durable record is what makes the loser decline.
    """

    window = (
        settings.CONTINUATION_RECONCILIATION_REPUBLISH_SUPPRESSION_SECONDS
        if suppression_seconds is None
        else suppression_seconds
    )
    cutoff = now - timedelta(seconds=max(int(window), 0))
    try:
        rows = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == identity.session_id,
                LogEntry.task_execution_id == identity.task_execution_id,
                LogEntry.message == _republish_message(),
            )
            .order_by(LogEntry.id.desc())
            .limit(20)
            .all()
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        return None
    for row in rows:
        try:
            metadata = json.loads(row.log_metadata or "{}")
        except ValueError:
            continue
        recorded_at = _parse_timestamp(metadata.get("observed_at")) or _optional_utc(
            getattr(row, "created_at", None)
        )
        if recorded_at is not None and recorded_at < cutoff:
            continue
        if (
            metadata.get("session_instance_id") == identity.instance_id
            and metadata.get("continuation_kind") == identity.continuation_kind
            and metadata.get("continuation_retry_count") == identity.retry_count
        ):
            return row
    return None


def _optional_utc(value: datetime | None) -> datetime | None:
    return _utc(value) if isinstance(value, datetime) else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _republish_message() -> str:
    return "E8 continuation delivery republished"


def _record_decision(db: DbSession, decision: ReconciliationDecision) -> None:
    """Persist structured reconciliation evidence.

    Reconciliation never emits TASK_FAILED or any final-outcome event: losing
    transport is not a logical outcome.
    """

    message = (
        _republish_message()
        if decision.outcome == REPUBLISHED
        else f"E8 continuation reconciliation {decision.outcome.lower()}"
    )
    metadata = {"event_type": RECONCILIATION_EVENT, **decision.as_evidence()}
    db.add(
        LogEntry(
            session_id=decision.session_id,
            session_instance_id=decision.instance_id,
            task_id=decision.task_id,
            task_execution_id=decision.task_execution_id,
            level=(
                "ERROR"
                if decision.outcome == RECONCILIATION_ERROR
                else (
                    "WARN"
                    if decision.outcome
                    in (PHYSICAL_STATE_UNCERTAIN, RECONCILIATION_LOCKED)
                    else "INFO"
                )
            ),
            message=message,
            log_metadata=json.dumps(metadata, sort_keys=True, default=str),
        )
    )


def _candidate_sessions(db: DbSession, *, limit: int) -> Sequence[SessionModel]:
    return (
        db.query(SessionModel)
        .filter(
            SessionModel.status == "retry_pending",
            SessionModel.deleted_at.is_(None),
            SessionModel.continuation_kind.isnot(None),
        )
        .order_by(SessionModel.id.asc())
        .limit(limit)
        .all()
    )


def _validation_outcome(reason: str) -> str:
    return _VALIDATION_REASON_OUTCOMES.get(reason, INVALID_CONTINUATION)


def _project_lock_context(db: DbSession, session: SessionModel):
    """Serialize reconciliation on the existing project mutation lock."""

    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )
    from app.services.workspace.project_mutation_lock import project_mutation_lock

    project = (
        db.query(Project).filter(Project.id == session.project_id).first()
        if session.project_id
        else None
    )
    if project is None or not project.workspace_path:
        return None
    project_root = Path(
        resolve_project_workspace_path(project.workspace_path, project.name, db=db)
    )
    return project_mutation_lock(
        project_id=project.id,
        project_root=project_root,
        operation="continuation_delivery_reconciliation",
        owner=f"session:{session.id}:generation:{session.instance_id}",
    )


def reconcile_session_continuation(
    db: DbSession,
    session: SessionModel,
    *,
    now: datetime | None = None,
    grace_seconds: int | None = None,
    suppression_seconds: int | None = None,
    delivery_probe: Callable[[ContinuationIdentity], DeliveryProbeResult] | None = None,
    publish: Callable[[DbSession, ContinuationIdentity], str | None] | None = None,
    use_project_lock: bool = True,
) -> ReconciliationDecision:
    """Reconcile one candidate Session, restoring delivery only when safe."""

    observed = _now(now)
    session_id = getattr(session, "id", None)
    probe = delivery_probe or inspect_continuation_delivery
    publisher = publish or publish_continuation_delivery

    identity = resolve_continuation_identity(db, session)
    if identity is None:
        return ReconciliationDecision(
            session_id=session_id,
            outcome=INVALID_CONTINUATION,
            reason="continuation_identity_unresolvable",
            instance_id=getattr(session, "instance_id", None),
        )

    def _decision(outcome: str, reason: str, **details: Any) -> ReconciliationDecision:
        return ReconciliationDecision(
            session_id=identity.session_id,
            outcome=outcome,
            reason=reason,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            continuation_kind=identity.continuation_kind,
            retry_count=identity.retry_count,
            instance_id=identity.instance_id,
            observed_at=observed,
            due_at=due_at,
            details=details,
        )

    due_at = continuation_due_at(session, grace_seconds=grace_seconds)
    if observed < due_at:
        return _decision(NOT_DUE, "continuation_not_yet_due")

    validation = validate_continuation(db, identity)
    if not validation.accepted:
        return _decision(_validation_outcome(validation.reason), validation.reason)

    lock_context = _project_lock_context(db, session) if use_project_lock else None
    if lock_context is None:
        return _reconcile_locked_section(
            db,
            session,
            identity,
            observed=observed,
            due_at=due_at,
            suppression_seconds=suppression_seconds,
            probe=probe,
            publisher=publisher,
        )

    from app.services.workspace.project_mutation_lock import ProjectMutationLockError

    try:
        with lock_context:
            return _reconcile_locked_section(
                db,
                session,
                identity,
                observed=observed,
                due_at=due_at,
                suppression_seconds=suppression_seconds,
                probe=probe,
                publisher=publisher,
            )
    except ProjectMutationLockError as exc:
        return _decision(
            RECONCILIATION_LOCKED,
            "project_reconciliation_lock_contended",
            lock_error=str(exc)[:200],
        )


def _reconcile_locked_section(
    db: DbSession,
    session: SessionModel,
    identity: ContinuationIdentity,
    *,
    observed: datetime,
    due_at: datetime,
    suppression_seconds: int | None,
    probe: Callable[[ContinuationIdentity], DeliveryProbeResult],
    publisher: Callable[[DbSession, ContinuationIdentity], str | None],
) -> ReconciliationDecision:
    def _decision(
        outcome: str,
        reason: str,
        *,
        sources: tuple[str, ...] = (),
        **details: Any,
    ) -> ReconciliationDecision:
        return ReconciliationDecision(
            session_id=identity.session_id,
            outcome=outcome,
            reason=reason,
            task_id=identity.continuation_task_id,
            task_execution_id=identity.task_execution_id,
            continuation_kind=identity.continuation_kind,
            retry_count=identity.retry_count,
            instance_id=identity.instance_id,
            observed_at=observed,
            due_at=due_at,
            delivery_sources=sources,
            details=details,
        )

    existing = _recent_republish_record(
        db, identity, now=observed, suppression_seconds=suppression_seconds
    )
    if existing is not None:
        return _decision(
            DELIVERY_PRESENT,
            "republication_already_recorded",
            prior_republish_log_id=existing.id,
        )

    result = probe(identity)
    if result.present:
        return _decision(
            DELIVERY_PRESENT, "matching_delivery_present", sources=result.sources
        )
    if result.uncertain:
        # Never publish because inspection failed.
        return _decision(
            PHYSICAL_STATE_UNCERTAIN,
            "physical_delivery_state_unknown",
            probe_error=result.error,
        )

    # Mandatory revalidation immediately before publication: an operator pause,
    # stop, resume or finalization may have rotated the generation after the
    # scan began.
    db.expire_all()
    revalidation = validate_continuation(db, identity)
    if not revalidation.accepted:
        return _decision(
            _validation_outcome(revalidation.reason),
            revalidation.reason,
            revalidated=True,
        )

    try:
        celery_task_id = publisher(db, identity)
    except Exception as exc:  # noqa: BLE001 - reported; marker deliberately kept
        logger.error(
            "[E8] Continuation republication failed for session %s: %s",
            identity.session_id,
            exc,
        )
        return _decision(
            RECONCILIATION_ERROR,
            "republication_failed",
            error=str(exc)[:300],
        )
    return _decision(
        REPUBLISHED,
        "delivery_restored",
        celery_task_id=celery_task_id,
        celery_delivery_retries=_celery_delivery_retries(identity),
    )


def reconcile_stranded_continuations(
    db: DbSession,
    *,
    now: datetime | None = None,
    grace_seconds: int | None = None,
    suppression_seconds: int | None = None,
    limit: int | None = None,
    delivery_probe: Callable[[ContinuationIdentity], DeliveryProbeResult] | None = None,
    publish: Callable[[DbSession, ContinuationIdentity], str | None] | None = None,
    use_project_lock: bool = True,
    decision_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Restore lost delivery for durable continuations that are past due.

    The Session's logical state is never mutated here.  A candidate that is not
    safely republishable is simply left as it is: still ``retry_pending``,
    still ``continuation_pending``, still nonterminal.
    """

    observed = _now(now)
    candidates = _candidate_sessions(
        db,
        limit=(
            settings.CONTINUATION_RECONCILIATION_MAX_CANDIDATES
            if limit is None
            else limit
        ),
    )
    counts = {outcome: 0 for outcome in RECONCILIATION_OUTCOMES}
    decisions: list[ReconciliationDecision] = []
    for session in candidates:
        try:
            decision = reconcile_session_continuation(
                db,
                session,
                now=observed,
                grace_seconds=grace_seconds,
                suppression_seconds=suppression_seconds,
                delivery_probe=delivery_probe,
                publish=publish,
                use_project_lock=use_project_lock,
            )
        except Exception as exc:  # noqa: BLE001 - one candidate never fails the sweep
            logger.error(
                "[E8] Continuation reconciliation raised for session %s: %s",
                getattr(session, "id", None),
                exc,
            )
            decision = ReconciliationDecision(
                session_id=getattr(session, "id", None),
                outcome=RECONCILIATION_ERROR,
                reason="reconciliation_exception",
                instance_id=getattr(session, "instance_id", None),
                details={"error": str(exc)[:300]},
            )
        counts[decision.outcome] = counts.get(decision.outcome, 0) + 1
        decisions.append(decision)
        _record_decision(db, decision)
    db.commit()

    evidence = [decision.as_evidence() for decision in decisions]
    if decision_records is not None:
        decision_records.extend(evidence)
    return {
        "inspected_count": len(candidates),
        "counts": counts,
        "republished_session_ids": [
            decision.session_id
            for decision in decisions
            if decision.outcome == REPUBLISHED
        ],
        "decisions": evidence,
    }
