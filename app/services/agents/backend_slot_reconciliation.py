"""Canonical reconciliation authority for Redis backend concurrency slots.

Phase 22B-1X1. A backend slot is valid only while its recorded runtime owner
is valid. This module is the single seam that decides that: worker boot,
periodic maintenance, deployment admission, and the operator health action
all call :func:`reconcile_backend_slots` rather than reasoning about slot
staleness themselves.

Ownership itself is *not* re-derived here. The execution-graph verdict comes
from ``app.services.session.orphan_ownership.evaluate_execution_ownership``,
the existing fail-safe authority, so slot capacity and orphan recovery can
never disagree about who owns a running execution.

Decision table for one slot member (a session id):

* no resolvable Session, or the Session is soft-deleted -> STALE
* every execution for the session is terminal, or none exists -> STALE
* any live execution classified LIVE_OWNER_CONFIRMED / LEASE_ACTIVE -> RETAIN
* any execution classified AMBIGUOUS_FAIL_SAFE -> RETAIN (ambiguous)
* all live executions RECOVERY_ELIGIBLE -> STALE

Age is never the sole proof of staleness: it is used only in the opposite,
fail-safe direction, as a grace window that retains a very recently acquired
slot whose dispatch has not yet reached the database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel, TaskExecution
from app.services.agents.backend_concurrency import (
    get_slot_owner_evidence,
    release_backend_slot_if_owner,
)
from app.services.session.orphan_ownership import (
    TERMINAL_EXECUTION_STATUSES,
    evaluate_execution_ownership,
)

logger = logging.getLogger(__name__)

RECONCILIATION_SCHEMA_VERSION = "backend-slot-reconciliation/1.0"

DECISION_RETAINED = "RETAINED"
DECISION_RELEASED_STALE = "RELEASED_STALE"
DECISION_AMBIGUOUS_RETAINED = "AMBIGUOUS_RETAINED"
DECISION_RELEASE_FENCED = "RELEASE_FENCED"
DECISION_RELEASE_FAILED = "RELEASE_FAILED"

# A slot acquired this recently is retained even without a resolvable
# execution: the dispatch that acquired it may not have committed its
# running-state rows yet. This never *proves* staleness, only withholds it.
DEFAULT_ACQUISITION_GRACE_SECONDS = 300
DEFAULT_STALE_AFTER_SECONDS = 2100

LIVE_CLASSIFICATIONS = {"LIVE_OWNER_CONFIRMED", "LEASE_ACTIVE"}
AMBIGUOUS_CLASSIFICATIONS = {"AMBIGUOUS_FAIL_SAFE"}


@dataclass(frozen=True)
class SlotDecision:
    backend_id: str
    session_id: Optional[int]
    member: str
    decision: str
    reason: str
    task_execution_ids: List[int] = field(default_factory=list)
    classifications: List[str] = field(default_factory=list)
    owner_evidence_present: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "session_id": self.session_id,
            "member": self.member,
            "decision": self.decision,
            "reason": self.reason,
            "task_execution_ids": list(self.task_execution_ids),
            "classifications": list(self.classifications),
            "owner_evidence_present": self.owner_evidence_present,
        }


def _utc(value: datetime | None) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _status(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value)).strip().lower() or None


def _parse_evidence(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _within_grace(evidence: Dict[str, Any], now: datetime, grace_seconds: int) -> bool:
    acquired_at = evidence.get("acquired_at")
    if not acquired_at:
        return False
    try:
        observed = _utc(datetime.fromisoformat(str(acquired_at)))
    except ValueError:
        return False
    if observed is None:
        return False
    return observed > now - timedelta(seconds=max(0, int(grace_seconds)))


def _slot_members(redis_client: Any, backend_id: str) -> List[str]:
    from app.services.agents.backend_concurrency import _slot_key

    members = redis_client.smembers(_slot_key(backend_id)) or set()
    resolved: List[str] = []
    for member in members:
        resolved.append(
            member.decode(errors="replace")
            if isinstance(member, bytes)
            else str(member)
        )
    return sorted(resolved)


def _evaluate_member(
    db: DbSession,
    *,
    backend_id: str,
    member: str,
    evidence: Dict[str, Any],
    evidence_present: bool,
    now: datetime,
    stale_after_seconds: int,
    grace_seconds: int,
) -> SlotDecision:
    """Classify one slot member without touching Redis."""

    try:
        session_id = int(member)
    except (TypeError, ValueError):
        # An unparseable member cannot be resolved to any owner, and the
        # normal acquire path never writes one. Removing it is safe only in
        # the sense that nothing can ever validate it; fail safe instead and
        # surface it as ambiguous for an operator.
        return SlotDecision(
            backend_id=backend_id,
            session_id=None,
            member=member,
            decision=DECISION_AMBIGUOUS_RETAINED,
            reason="slot member is not a resolvable session identity",
            owner_evidence_present=evidence_present,
        )

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    grace_active = _within_grace(evidence, now, grace_seconds)

    if session is None:
        if grace_active:
            return SlotDecision(
                backend_id=backend_id,
                session_id=session_id,
                member=member,
                decision=DECISION_RETAINED,
                reason="session not yet resolvable inside the acquisition grace window",
                owner_evidence_present=evidence_present,
            )
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_RELEASED_STALE,
            reason="slot owner session no longer exists",
            owner_evidence_present=evidence_present,
        )

    if getattr(session, "deleted_at", None) is not None:
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_RELEASED_STALE,
            reason="slot owner session is deleted",
            owner_evidence_present=evidence_present,
        )

    executions = (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id)
        .order_by(TaskExecution.id.desc())
        .all()
    )
    live_executions = [
        execution
        for execution in executions
        if _status(execution.status) not in TERMINAL_EXECUTION_STATUSES
    ]

    if not live_executions:
        if grace_active:
            return SlotDecision(
                backend_id=backend_id,
                session_id=session_id,
                member=member,
                decision=DECISION_RETAINED,
                reason="no live execution yet inside the acquisition grace window",
                task_execution_ids=[e.id for e in executions[:5]],
                owner_evidence_present=evidence_present,
            )
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_RELEASED_STALE,
            reason=(
                "slot owner has no live execution"
                if executions
                else "slot owner has no execution at all"
            ),
            task_execution_ids=[e.id for e in executions[:5]],
            owner_evidence_present=evidence_present,
        )

    classifications: List[str] = []
    for execution in live_executions:
        evaluation = evaluate_execution_ownership(
            execution,
            runtime_lease=getattr(execution, "runtime_lease", None),
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        classifications.append(evaluation.ownership_classification)

    execution_ids = [execution.id for execution in live_executions]

    if any(item in LIVE_CLASSIFICATIONS for item in classifications):
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_RETAINED,
            reason="slot owner has a live execution owner",
            task_execution_ids=execution_ids,
            classifications=classifications,
            owner_evidence_present=evidence_present,
        )

    if any(item in AMBIGUOUS_CLASSIFICATIONS for item in classifications):
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_AMBIGUOUS_RETAINED,
            reason="slot owner ownership is ambiguous; capacity is withheld fail-safe",
            task_execution_ids=execution_ids,
            classifications=classifications,
            owner_evidence_present=evidence_present,
        )

    if grace_active:
        return SlotDecision(
            backend_id=backend_id,
            session_id=session_id,
            member=member,
            decision=DECISION_RETAINED,
            reason="stale owner evidence is inside the acquisition grace window",
            task_execution_ids=execution_ids,
            classifications=classifications,
            owner_evidence_present=evidence_present,
        )

    return SlotDecision(
        backend_id=backend_id,
        session_id=session_id,
        member=member,
        decision=DECISION_RELEASED_STALE,
        reason="every live execution owner is proven absent",
        task_execution_ids=execution_ids,
        classifications=classifications,
        owner_evidence_present=evidence_present,
    )


def reconcile_backend_slots(
    db: DbSession,
    redis_client: Any,
    backend_id: str,
    *,
    now: Optional[datetime] = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    grace_seconds: int = DEFAULT_ACQUISITION_GRACE_SECONDS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reconcile one backend's slot set against its runtime owners.

    Idempotent: a second run over an already-reconciled set removes nothing
    and reports the same retained decisions. Every removal is fenced on the
    owner evidence observed during evaluation, so a slot re-acquired by a
    live worker mid-reconciliation is never dropped.
    """

    observed_at = _utc(now) or datetime.now(UTC)
    evidence_by_member = get_slot_owner_evidence(redis_client, backend_id)
    members = _slot_members(redis_client, backend_id)

    decisions: List[SlotDecision] = []
    for member in members:
        raw_evidence = evidence_by_member.get(member)
        decision = _evaluate_member(
            db,
            backend_id=backend_id,
            member=member,
            evidence=_parse_evidence(raw_evidence),
            evidence_present=raw_evidence is not None,
            now=observed_at,
            stale_after_seconds=stale_after_seconds,
            grace_seconds=grace_seconds,
        )
        if decision.decision == DECISION_RELEASED_STALE and not dry_run:
            try:
                released = release_backend_slot_if_owner(
                    redis_client, backend_id, int(member), raw_evidence
                )
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                logger.warning(
                    "[SLOT_RECONCILIATION] Release failed for backend=%s member=%s: %s",
                    backend_id,
                    member,
                    exc,
                )
                decision = SlotDecision(
                    backend_id=backend_id,
                    session_id=decision.session_id,
                    member=member,
                    decision=DECISION_RELEASE_FAILED,
                    reason=f"redis release failed: {str(exc)[:200]}",
                    task_execution_ids=decision.task_execution_ids,
                    classifications=decision.classifications,
                    owner_evidence_present=decision.owner_evidence_present,
                )
            else:
                if not released:
                    decision = SlotDecision(
                        backend_id=backend_id,
                        session_id=decision.session_id,
                        member=member,
                        decision=DECISION_RELEASE_FENCED,
                        reason="owner evidence changed during reconciliation; slot retained",
                        task_execution_ids=decision.task_execution_ids,
                        classifications=decision.classifications,
                        owner_evidence_present=decision.owner_evidence_present,
                    )
        decisions.append(decision)

    counts = {
        "evaluated": len(decisions),
        "retained": sum(
            1
            for d in decisions
            if d.decision in {DECISION_RETAINED, DECISION_RELEASE_FENCED}
        ),
        "released_stale": sum(
            1 for d in decisions if d.decision == DECISION_RELEASED_STALE
        ),
        "ambiguous": sum(
            1 for d in decisions if d.decision == DECISION_AMBIGUOUS_RETAINED
        ),
        "release_failed": sum(
            1 for d in decisions if d.decision == DECISION_RELEASE_FAILED
        ),
        "release_fenced": sum(
            1 for d in decisions if d.decision == DECISION_RELEASE_FENCED
        ),
    }

    return {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "backend_id": backend_id,
        "reconciled_at": observed_at.isoformat(),
        "dry_run": dry_run,
        "stale_after_seconds": stale_after_seconds,
        "grace_seconds": grace_seconds,
        **counts,
        "decisions": [decision.to_dict() for decision in decisions],
    }


def reconcile_all_backend_slots(
    db: DbSession,
    redis_client: Any,
    *,
    backend_ids: Optional[List[str]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reconcile every concurrency-governed backend in one idempotent pass."""

    if backend_ids is None:
        # Registry descriptors only: enumerating slot-governed backends must
        # not trigger the provider health probes list_supported_backends runs.
        from app.services.agents.agent_backends import _BACKEND_REGISTRY

        backend_ids = [
            registration.descriptor.name
            for registration in _BACKEND_REGISTRY.values()
            if registration.descriptor.capabilities.max_parallel_sessions is not None
        ]

    results = [
        reconcile_backend_slots(db, redis_client, backend_id, now=now, dry_run=dry_run)
        for backend_id in backend_ids
    ]
    return {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "backends": results,
        "released_stale": sum(result["released_stale"] for result in results),
        "ambiguous": sum(result["ambiguous"] for result in results),
        "evaluated": sum(result["evaluated"] for result in results),
    }
