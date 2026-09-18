"""RR1 run observer and cross-run isolation gate.

The observer drives the measurement state machine from two independent
evidence sources -- the lifecycle authority (logical) and runtime inspection
(physical) -- and reconciles them before declaring the next run eligible.

It never writes Product lifecycle state.  In particular, when the research
observation timeout expires it does **not** manufacture terminality, clear a
continuation, or mutate anything: it classifies the observation as censored at
an operational boundary and leaves the Product alone (§7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel
from app.services.research.rr1 import RR1_HARNESS_VERSION
from app.services.research.rr1.endpoint import (
    CANONICAL_ENDPOINT_EXPRESSION,
    LogicalEndpointObservation,
    observe_logical_endpoint,
)
from app.services.research.rr1.physical import (
    PHYSICAL_STATE_UNCERTAIN,
    PHYSICALLY_RELEASED,
    PhysicalReleaseObservation,
    RunPhysicalIdentity,
    observe_physical_release,
)
from app.services.research.rr1.stabilization import (
    DEFAULT_LOGICAL_STABILIZATION_SECONDS,
    DEFAULT_PHYSICAL_STABILIZATION_SECONDS,
    StabilizationWindow,
)
from app.services.research.rr1.state_machine import (
    CENSORED_OR_BLOCKED,
    EVIDENCE_FINALIZATION,
    LOGICAL_ENDPOINT_STABILIZING,
    MeasurementStateMachine,
    OBSERVING_LOGICAL_EXECUTION,
    PHYSICAL_RELEASE_STABILIZING,
    READY_FOR_NEXT_RUN,
    WAITING_FOR_PHYSICAL_RELEASE,
)

#: Research measurement timeout, distinct from every Product lifecycle
#: timeout.  Its expiry classifies an observation; it never ends a run.
DEFAULT_RUN_OBSERVATION_TIMEOUT_SECONDS = 3600.0

CENSOR_OBSERVATION_TIMEOUT = "RUN_OBSERVATION_TIMEOUT_EXPIRED"
CENSOR_PHYSICAL_UNCERTAIN = "PHYSICAL_STATE_UNCERTAIN"
CENSOR_EVIDENCE_INTEGRITY = "EVIDENCE_INTEGRITY_PROBLEM"


@dataclass(frozen=True)
class ObservationConfig:
    """Prospectively configurable measurement parameters."""

    logical_stabilization_seconds: float = DEFAULT_LOGICAL_STABILIZATION_SECONDS
    physical_stabilization_seconds: float = DEFAULT_PHYSICAL_STABILIZATION_SECONDS
    run_observation_timeout_seconds: float = DEFAULT_RUN_OBSERVATION_TIMEOUT_SECONDS

    def as_evidence(self) -> dict[str, Any]:
        return {
            "logical_stabilization_seconds": self.logical_stabilization_seconds,
            "physical_stabilization_seconds": self.physical_stabilization_seconds,
            "run_observation_timeout_seconds": self.run_observation_timeout_seconds,
            "logical_endpoint_predicate": CANONICAL_ENDPOINT_EXPRESSION,
            "harness_version": RR1_HARNESS_VERSION,
        }


def _logical_fingerprint(observation: LogicalEndpointObservation) -> tuple:
    """Identity whose change resets logical stabilization (§6).

    Covers continuation appearance, generation change, and new attempt start.
    """

    return (
        observation.generation,
        observation.continuation_pending,
        observation.continuation_kind,
        observation.attempt_status,
        observation.current_phase,
    )


def _physical_fingerprint(observation: PhysicalReleaseObservation) -> tuple:
    return tuple(sorted(observation.busy_signals)), tuple(
        sorted(observation.uncertain_signals)
    )


@dataclass
class RunObserver:
    """Observe one research run to stable logical endpoint and physical drain."""

    research_run_id: str
    session_id: int
    identity: RunPhysicalIdentity
    started_at: datetime
    config: ObservationConfig = field(default_factory=ObservationConfig)
    task_id: int | None = None
    machine: MeasurementStateMachine = field(default_factory=MeasurementStateMachine)
    logical_window: StabilizationWindow = field(init=False)
    physical_window: StabilizationWindow = field(init=False)
    logical_observations: list[LogicalEndpointObservation] = field(default_factory=list)
    physical_observations: list[PhysicalReleaseObservation] = field(
        default_factory=list
    )
    censored_reason: str | None = None
    logical_first_observed_at: datetime | None = None
    logical_stable_confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.logical_window = StabilizationWindow(
            name="logical_endpoint",
            window_seconds=self.config.logical_stabilization_seconds,
        )
        self.physical_window = StabilizationWindow(
            name="physical_release",
            window_seconds=self.config.physical_stabilization_seconds,
        )
        self.machine.transition(
            OBSERVING_LOGICAL_EXECUTION,
            at=self.started_at,
            reason="run_observation_started",
        )

    # -- logical -----------------------------------------------------------

    def observe(
        self,
        db: DbSession,
        session: SessionModel,
        *,
        at: datetime,
        inspector: Any | None = None,
        redis_client: Any | None = None,
    ) -> str:
        """Advance the measurement state machine by one observation tick."""

        if self.machine.finished:
            return self.machine.state

        if self._observation_timed_out(at):
            return self._censor(CENSOR_OBSERVATION_TIMEOUT, at)

        if self.machine.state in {
            OBSERVING_LOGICAL_EXECUTION,
            LOGICAL_ENDPOINT_STABILIZING,
        }:
            return self._observe_logical(db, session, at=at)

        return self._observe_physical(
            db, at=at, inspector=inspector, redis_client=redis_client
        )

    def _observe_logical(
        self, db: DbSession, session: SessionModel, *, at: datetime
    ) -> str:
        observation = observe_logical_endpoint(
            db, session, task_id=self.task_id, observed_at=at
        )
        self.logical_observations.append(observation)

        was_stable = self.logical_window.stable
        stable = self.logical_window.observe(
            satisfied=observation.reached,
            at=at,
            fingerprint=_logical_fingerprint(observation),
        )
        self.logical_first_observed_at = self.logical_window.first_observed_at
        self.logical_stable_confirmed_at = self.logical_window.stable_confirmed_at

        if not observation.reached:
            # Includes retry_pending and recovering: a live continuation keeps
            # the harness observing, whether or not transport is visible.
            if self.machine.state == LOGICAL_ENDPOINT_STABILIZING:
                self.machine.transition(
                    OBSERVING_LOGICAL_EXECUTION,
                    at=at,
                    reason="logical_endpoint_no_longer_satisfied",
                )
            return self.machine.state

        if self.machine.state == OBSERVING_LOGICAL_EXECUTION:
            self.machine.transition(
                LOGICAL_ENDPOINT_STABILIZING,
                at=at,
                reason="logical_endpoint_first_observed",
            )
        if stable and not was_stable:
            self.machine.transition(
                WAITING_FOR_PHYSICAL_RELEASE,
                at=at,
                reason="logical_endpoint_stable",
            )
        return self.machine.state

    # -- physical ----------------------------------------------------------

    def _observe_physical(
        self,
        db: DbSession,
        *,
        at: datetime,
        inspector: Any | None,
        redis_client: Any | None,
    ) -> str:
        observation = observe_physical_release(
            db,
            self.identity,
            observed_at=at,
            inspector=inspector,
            redis_client=redis_client,
        )
        self.physical_observations.append(observation)

        released = observation.state == PHYSICALLY_RELEASED
        was_stable = self.physical_window.stable
        stable = self.physical_window.observe(
            satisfied=released,
            at=at,
            fingerprint=_physical_fingerprint(observation),
        )

        if not released:
            if self.machine.state == PHYSICAL_RELEASE_STABILIZING:
                self.machine.transition(
                    WAITING_FOR_PHYSICAL_RELEASE,
                    at=at,
                    reason=(
                        "physical_ownership_reappeared"
                        if observation.state != PHYSICAL_STATE_UNCERTAIN
                        else "physical_state_uncertain"
                    ),
                )
            return self.machine.state

        if self.machine.state == WAITING_FOR_PHYSICAL_RELEASE:
            self.machine.transition(
                PHYSICAL_RELEASE_STABILIZING,
                at=at,
                reason="physical_release_first_observed",
            )
        if stable and not was_stable:
            self.machine.transition(
                EVIDENCE_FINALIZATION, at=at, reason="physical_drain_stable"
            )
        return self.machine.state

    # -- closure -----------------------------------------------------------

    def finalize(self, *, at: datetime, evidence_intact: bool = True) -> str:
        """Close evidence and mark the next run eligible, or block it."""

        if self.machine.state != EVIDENCE_FINALIZATION:
            return self._censor(
                (
                    CENSOR_EVIDENCE_INTEGRITY
                    if not evidence_intact
                    else "FINALIZE_BEFORE_DRAIN"
                ),
                at,
            )
        if not evidence_intact:
            return self._censor(CENSOR_EVIDENCE_INTEGRITY, at)
        self.machine.transition(READY_FOR_NEXT_RUN, at=at, reason="evidence_finalized")
        return self.machine.state

    def _censor(self, reason: str, at: datetime) -> str:
        self.censored_reason = reason
        self.machine.transition(CENSORED_OR_BLOCKED, at=at, reason=reason)
        return self.machine.state

    def _observation_timed_out(self, at: datetime) -> bool:
        limit = timedelta(seconds=float(self.config.run_observation_timeout_seconds))
        return (at - self.started_at) >= limit

    # -- reporting ---------------------------------------------------------

    @property
    def next_run_eligible(self) -> bool:
        return self.machine.state == READY_FOR_NEXT_RUN

    def as_evidence(self) -> dict[str, Any]:
        return {
            "research_run_id": self.research_run_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "config": self.config.as_evidence(),
            "measurement_state": self.machine.as_evidence(),
            "censored_reason": self.censored_reason,
            "next_run_eligible": self.next_run_eligible,
            "logical_window": self.logical_window.as_evidence(),
            "physical_window": self.physical_window.as_evidence(),
            "logical_first_observed_at": (
                self.logical_first_observed_at.isoformat()
                if self.logical_first_observed_at
                else None
            ),
            "logical_stable_confirmed_at": (
                self.logical_stable_confirmed_at.isoformat()
                if self.logical_stable_confirmed_at
                else None
            ),
            "logical_observations": [
                observation.as_evidence() for observation in self.logical_observations
            ],
            "physical_observations": [
                observation.as_evidence() for observation in self.physical_observations
            ],
        }


# ---------------------------------------------------------------------------
# Cross-run isolation (§18)
# ---------------------------------------------------------------------------

ISOLATION_ALLOWED = "ALLOWED"
ISOLATION_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class IsolationDecision:
    decision: str
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.decision == ISOLATION_ALLOWED

    def as_evidence(self) -> dict[str, Any]:
        return {"decision": self.decision, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class RunClaim:
    """The resources one research run claims exclusively."""

    research_run_id: str
    correlation_id: str
    productroot_path: str | None
    workspace_path: str | None
    session_id: int | None
    generation: str | None
    backend_ids: tuple[str, ...] = ()


class CrossRunIsolationGate:
    """Refuse run N+1 until run N is logically and physically closed.

    Timing is never sufficient on its own: a launch requires the previous
    observer to have reached ``READY_FOR_NEXT_RUN``, the drift verdict to be
    ``MATCH``, and no resource overlap with any prior claim.
    """

    def __init__(self) -> None:
        self._claims: list[RunClaim] = []

    def register(self, claim: RunClaim) -> None:
        self._claims.append(claim)

    def evaluate(
        self,
        claim: RunClaim,
        *,
        previous: RunObserver | None,
        drift_verdict: str | None,
    ) -> IsolationDecision:
        reasons: list[str] = []

        if previous is not None and not previous.next_run_eligible:
            reasons.append(
                "previous_run_not_ready:"
                f"{previous.machine.state}"
                + (f":{previous.censored_reason}" if previous.censored_reason else "")
            )

        from app.services.research.rr1.manifest import MATCH

        if drift_verdict is None:
            reasons.append("treatment_drift_unverified")
        elif drift_verdict != MATCH:
            reasons.append(f"treatment_{drift_verdict.lower()}")

        for existing in self._claims:
            if existing.research_run_id == claim.research_run_id:
                reasons.append("duplicate_research_run_id")
            for attribute, label in (
                ("productroot_path", "productroot"),
                ("workspace_path", "workspace"),
                ("session_id", "session"),
                ("generation", "session_generation"),
                ("correlation_id", "research_correlation_identity"),
            ):
                value = getattr(claim, attribute)
                if value is not None and value == getattr(existing, attribute):
                    reasons.append(f"{label}_overlap_with:{existing.research_run_id}")
            overlap = set(claim.backend_ids) & set(existing.backend_ids)
            if overlap:
                reasons.append(
                    "backend_runtime_ownership_overlap:" + ",".join(sorted(overlap))
                )

        if reasons:
            return IsolationDecision(ISOLATION_BLOCKED, tuple(dict.fromkeys(reasons)))
        return IsolationDecision(ISOLATION_ALLOWED, ())

    def launch(
        self,
        claim: RunClaim,
        *,
        previous: RunObserver | None,
        drift_verdict: str | None,
    ) -> IsolationDecision:
        decision = self.evaluate(claim, previous=previous, drift_verdict=drift_verdict)
        if decision.allowed:
            self.register(claim)
        return decision


def observe_until(
    observer: RunObserver,
    *,
    db: DbSession,
    session_loader: Callable[[], SessionModel],
    clock: Callable[[], datetime],
    inspector: Any | None = None,
    redis_client: Any | None = None,
    max_ticks: int = 10_000,
) -> str:
    """Drive an observer to a terminal measurement state.

    ``clock`` and ``session_loader`` are injected so deterministic regressions
    can advance time and state without sleeping or touching a real runtime.
    """

    for _ in range(max_ticks):
        if observer.machine.finished:
            break
        at = clock()
        state = observer.observe(
            db,
            session_loader(),
            at=at,
            inspector=inspector,
            redis_client=redis_client,
        )
        if state == EVIDENCE_FINALIZATION:
            observer.finalize(at=at)
            break
    return observer.machine.state
