"""Stabilization windows for RR1 logical and physical observation.

A single satisfying observation is never sufficient.  A window requires the
condition to hold continuously, and it resets whenever the condition breaks or
whenever the observed identity fingerprint changes (a continuation appears, the
generation changes, or a new attempt starts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Hashable

#: Prospectively configurable defaults.  Callers override per protocol; RR1
#: does not freeze the Stratum-2 values.
DEFAULT_LOGICAL_STABILIZATION_SECONDS = 30.0
DEFAULT_PHYSICAL_STABILIZATION_SECONDS = 30.0

RESET_NOT_SATISFIED = "condition_not_satisfied"
RESET_FINGERPRINT_CHANGED = "fingerprint_changed"


@dataclass(frozen=True)
class StabilizationReset:
    at: datetime
    reason: str
    previous_fingerprint: Hashable | None
    fingerprint: Hashable | None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "reason": self.reason,
            "previous_fingerprint": _plain(self.previous_fingerprint),
            "fingerprint": _plain(self.fingerprint),
        }


@dataclass
class StabilizationWindow:
    """Track continuous satisfaction of one condition over a window."""

    name: str
    window_seconds: float
    first_observed_at: datetime | None = None
    stable_confirmed_at: datetime | None = None
    _fingerprint: Hashable | None = field(default=None, repr=False)
    _satisfied: bool = field(default=False, repr=False)
    resets: list[StabilizationReset] = field(default_factory=list)

    def __post_init__(self) -> None:
        if float(self.window_seconds) < 0:
            raise ValueError("window_seconds must be non-negative")

    @property
    def stable(self) -> bool:
        return self.stable_confirmed_at is not None

    def observe(
        self,
        *,
        satisfied: bool,
        at: datetime,
        fingerprint: Hashable | None = None,
    ) -> bool:
        """Record one observation and return whether the window is stable.

        The window never "remembers" an earlier satisfying observation across a
        break: a reset discards ``first_observed_at`` so terminality can not be
        back-dated to before the disturbance.
        """

        if not satisfied:
            if self._satisfied or self.first_observed_at is not None:
                self._record_reset(at, RESET_NOT_SATISFIED, fingerprint)
            self._satisfied = False
            self.first_observed_at = None
            self.stable_confirmed_at = None
            self._fingerprint = fingerprint
            return False

        if self.first_observed_at is None:
            self.first_observed_at = at
            self.stable_confirmed_at = None
            self._fingerprint = fingerprint
            self._satisfied = True
        elif fingerprint != self._fingerprint:
            self._record_reset(at, RESET_FINGERPRINT_CHANGED, fingerprint)
            self.first_observed_at = at
            self.stable_confirmed_at = None
            self._fingerprint = fingerprint
            self._satisfied = True

        if self.stable_confirmed_at is None:
            elapsed = at - self.first_observed_at
            if elapsed >= timedelta(seconds=float(self.window_seconds)):
                self.stable_confirmed_at = at
        return self.stable

    def _record_reset(
        self, at: datetime, reason: str, fingerprint: Hashable | None
    ) -> None:
        self.resets.append(
            StabilizationReset(
                at=at,
                reason=reason,
                previous_fingerprint=self._fingerprint,
                fingerprint=fingerprint,
            )
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "window_seconds": float(self.window_seconds),
            "first_observed_at": (
                self.first_observed_at.isoformat() if self.first_observed_at else None
            ),
            "stable_confirmed_at": (
                self.stable_confirmed_at.isoformat()
                if self.stable_confirmed_at
                else None
            ),
            "stable": self.stable,
            "reset_count": len(self.resets),
            "resets": [reset.as_evidence() for reset in self.resets],
        }


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value
