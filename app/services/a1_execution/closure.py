"""Positive-closure semantics for A1 Attempts (contract §12; F-06).

Overall Attempt closure requires positive, evidence-referenced closure of
both the local process domain and the provider/session domain, plus durable
closure evidence.  Weak signals (parent exit, timeout, cancellation request,
...) never establish closure: on their own they leave a domain ``UNKNOWN``.
Any failure or still-running signal makes the domain ``NOT_CLOSED``.

The verdict carries no HOLD or capacity-release decision; the future SQLite
Attempt ledger owns those transitions (contract §§8–9).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable, Optional

from app.services.a1_execution.identity import (
    A1AttemptIdentity,
    require_same_attempt,
    validate_opaque_ref,
)


class ClosureDomain(str, enum.Enum):
    LOCAL_PROCESS = "local_process_closure"
    PROVIDER_SESSION = "provider_session_closure"


# Both domains are required for every A1 Attempt in the initial scope.
REQUIRED_CLOSURE_DOMAINS: tuple[ClosureDomain, ...] = tuple(ClosureDomain)


class ClosureStatus(str, enum.Enum):
    POSITIVELY_VERIFIED = "positively_verified"
    NOT_CLOSED = "not_closed"
    UNKNOWN = "unknown"


class ClosureSignal(str, enum.Enum):
    # Positive witnesses. Each requires an evidence reference.
    EMPTY_SCOPE_OBSERVED = "empty_scope_observed"
    AUTHORITATIVE_PROVIDER_COMPLETION = "authoritative_provider_completion"
    AUTHORITATIVE_PROVIDER_CANCELLATION = "authoritative_provider_cancellation"
    # Insufficient signals: observable, but never proof of closure.
    PARENT_EXIT = "parent_exit"
    CLI_EXIT = "cli_exit"
    PROCESS_GROUP_KILLED = "process_group_killed"
    HTTP_DISCONNECT = "http_disconnect"
    TIMEOUT = "timeout"
    CANCELLATION_REQUESTED = "cancellation_requested"
    HEARTBEAT_MISSING = "heartbeat_missing"
    SESSION_NOT_FOUND = "session_not_found"
    CONTAINER_EXIT = "container_exit"
    BACKEND_DECLARATION = "backend_declaration"
    # Negative signals: the domain is known not to be closed.
    STILL_RUNNING = "still_running"
    CLEANUP_FAILED = "cleanup_failed"


POSITIVE_SIGNALS: dict[ClosureDomain, frozenset[ClosureSignal]] = {
    ClosureDomain.LOCAL_PROCESS: frozenset({ClosureSignal.EMPTY_SCOPE_OBSERVED}),
    ClosureDomain.PROVIDER_SESSION: frozenset(
        {
            ClosureSignal.AUTHORITATIVE_PROVIDER_COMPLETION,
            ClosureSignal.AUTHORITATIVE_PROVIDER_CANCELLATION,
        }
    ),
}
NEGATIVE_SIGNALS = frozenset(
    {ClosureSignal.STILL_RUNNING, ClosureSignal.CLEANUP_FAILED}
)


class ClosureEvidenceDurability(str, enum.Enum):
    DURABLE_VERIFIED = "durable_verified"
    NOT_DURABLE = "not_durable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClosureObservation:
    attempt: A1AttemptIdentity
    domain: ClosureDomain
    signal: ClosureSignal
    evidence_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.domain, ClosureDomain):
            raise ValueError("domain must be a ClosureDomain")
        if not isinstance(self.signal, ClosureSignal):
            raise ValueError("signal must be a ClosureSignal")
        if self.evidence_ref is not None:
            validate_opaque_ref(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True)
class ClosureEvidence:
    attempt: A1AttemptIdentity
    durability: ClosureEvidenceDurability
    evidence_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.durability, ClosureEvidenceDurability):
            raise ValueError("durability must be a ClosureEvidenceDurability")
        if self.evidence_ref is not None:
            validate_opaque_ref(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True)
class AttemptClosureVerdict:
    attempt: A1AttemptIdentity
    domains: tuple[tuple[ClosureDomain, ClosureStatus], ...]
    evidence_durable: bool
    overall: ClosureStatus
    reasons: tuple[str, ...]

    def status_for(self, domain: ClosureDomain) -> ClosureStatus:
        return dict(self.domains)[domain]


def _domain_status(
    domain: ClosureDomain, observations: list[ClosureObservation]
) -> tuple[ClosureStatus, Optional[str]]:
    signals = [o for o in observations if o.domain is domain]
    if any(o.signal in NEGATIVE_SIGNALS for o in signals):
        return ClosureStatus.NOT_CLOSED, f"{domain.value}:negative_signal"
    positive = [
        o
        for o in signals
        if o.signal in POSITIVE_SIGNALS[domain] and o.evidence_ref is not None
    ]
    if positive:
        return ClosureStatus.POSITIVELY_VERIFIED, None
    if any(o.signal in POSITIVE_SIGNALS[domain] for o in signals):
        return ClosureStatus.UNKNOWN, f"{domain.value}:positive_signal_without_evidence"
    if signals:
        return ClosureStatus.UNKNOWN, f"{domain.value}:insufficient_signals_only"
    return ClosureStatus.UNKNOWN, f"{domain.value}:no_observation"


def evaluate_attempt_closure(
    attempt: A1AttemptIdentity,
    observations: Iterable[ClosureObservation],
    evidence: Optional[ClosureEvidence],
) -> AttemptClosureVerdict:
    """Return the closure verdict for ``attempt``.

    Raises ``AttemptIdentityMismatch`` if any observation or the evidence
    belongs to a different Attempt identity.
    """

    observed = list(observations)
    for observation in observed:
        require_same_attempt(attempt, observation.attempt)
    if evidence is not None:
        require_same_attempt(attempt, evidence.attempt)

    reasons: list[str] = []
    domains: list[tuple[ClosureDomain, ClosureStatus]] = []
    for domain in REQUIRED_CLOSURE_DOMAINS:
        status, reason = _domain_status(domain, observed)
        domains.append((domain, status))
        if reason:
            reasons.append(reason)

    evidence_durable = (
        evidence is not None
        and evidence.durability is ClosureEvidenceDurability.DURABLE_VERIFIED
        and evidence.evidence_ref is not None
    )
    if not evidence_durable:
        reasons.append("closure_evidence_not_durable")

    statuses = [status for _, status in domains]
    if ClosureStatus.NOT_CLOSED in statuses:
        overall = ClosureStatus.NOT_CLOSED
    elif (
        all(s is ClosureStatus.POSITIVELY_VERIFIED for s in statuses)
        and evidence_durable
    ):
        overall = ClosureStatus.POSITIVELY_VERIFIED
    else:
        overall = ClosureStatus.UNKNOWN

    return AttemptClosureVerdict(
        attempt=attempt,
        domains=tuple(domains),
        evidence_durable=evidence_durable,
        overall=overall,
        reasons=tuple(reasons),
    )
