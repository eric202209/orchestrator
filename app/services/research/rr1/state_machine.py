"""RR1 measurement state machine (§21).

These are measurement states only.  Nothing here writes Product lifecycle
state, and no state implies a Product lifecycle phase: the harness observes
the lifecycle authority, it never mirrors or duplicates it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

WAITING_FOR_RUN = "WAITING_FOR_RUN"
OBSERVING_LOGICAL_EXECUTION = "OBSERVING_LOGICAL_EXECUTION"
LOGICAL_ENDPOINT_STABILIZING = "LOGICAL_ENDPOINT_STABILIZING"
WAITING_FOR_PHYSICAL_RELEASE = "WAITING_FOR_PHYSICAL_RELEASE"
PHYSICAL_RELEASE_STABILIZING = "PHYSICAL_RELEASE_STABILIZING"
EVIDENCE_FINALIZATION = "EVIDENCE_FINALIZATION"
READY_FOR_NEXT_RUN = "READY_FOR_NEXT_RUN"
CENSORED_OR_BLOCKED = "CENSORED_OR_BLOCKED"

MEASUREMENT_STATES = (
    WAITING_FOR_RUN,
    OBSERVING_LOGICAL_EXECUTION,
    LOGICAL_ENDPOINT_STABILIZING,
    WAITING_FOR_PHYSICAL_RELEASE,
    PHYSICAL_RELEASE_STABILIZING,
    EVIDENCE_FINALIZATION,
    READY_FOR_NEXT_RUN,
    CENSORED_OR_BLOCKED,
)

TERMINAL_MEASUREMENT_STATES = frozenset({READY_FOR_NEXT_RUN, CENSORED_OR_BLOCKED})

_ALLOWED: dict[str, frozenset[str]] = {
    WAITING_FOR_RUN: frozenset({OBSERVING_LOGICAL_EXECUTION, CENSORED_OR_BLOCKED}),
    OBSERVING_LOGICAL_EXECUTION: frozenset(
        {LOGICAL_ENDPOINT_STABILIZING, CENSORED_OR_BLOCKED}
    ),
    LOGICAL_ENDPOINT_STABILIZING: frozenset(
        {
            # Stability may break and return the harness to observation.
            OBSERVING_LOGICAL_EXECUTION,
            WAITING_FOR_PHYSICAL_RELEASE,
            CENSORED_OR_BLOCKED,
        }
    ),
    WAITING_FOR_PHYSICAL_RELEASE: frozenset(
        {PHYSICAL_RELEASE_STABILIZING, CENSORED_OR_BLOCKED}
    ),
    PHYSICAL_RELEASE_STABILIZING: frozenset(
        {WAITING_FOR_PHYSICAL_RELEASE, EVIDENCE_FINALIZATION, CENSORED_OR_BLOCKED}
    ),
    EVIDENCE_FINALIZATION: frozenset({READY_FOR_NEXT_RUN, CENSORED_OR_BLOCKED}),
    READY_FOR_NEXT_RUN: frozenset(),
    CENSORED_OR_BLOCKED: frozenset(),
}


class IllegalMeasurementTransition(RuntimeError):
    """Raised when a caller requests a transition the model does not allow."""


@dataclass(frozen=True)
class MeasurementTransition:
    at: datetime
    previous: str
    current: str
    reason: str

    def as_evidence(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "previous": self.previous,
            "current": self.current,
            "reason": self.reason,
        }


@dataclass
class MeasurementStateMachine:
    state: str = WAITING_FOR_RUN
    transitions: list[MeasurementTransition] = field(default_factory=list)

    def transition(self, target: str, *, at: datetime, reason: str) -> str:
        if target not in MEASUREMENT_STATES:
            raise IllegalMeasurementTransition(f"unknown measurement state: {target}")
        if target == self.state:
            return self.state
        if target not in _ALLOWED[self.state]:
            raise IllegalMeasurementTransition(
                f"{self.state} -> {target} is not a permitted measurement transition"
            )
        self.transitions.append(
            MeasurementTransition(
                at=at, previous=self.state, current=target, reason=reason
            )
        )
        self.state = target
        return self.state

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_MEASUREMENT_STATES

    def as_evidence(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "finished": self.finished,
            "transitions": [t.as_evidence() for t in self.transitions],
        }
