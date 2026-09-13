"""Small provider-neutral logical deadline primitive."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Awaitable, Callable, TypeVar

from app.services.agents.interfaces import AgentRuntimeError


TRANSPORT_TIMEOUT_MARGIN_SECONDS = 30
CLEANUP_GRACE_SECONDS = 5
_T = TypeVar("_T")


def _diagnostic_number(value: float) -> int | float:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


@dataclass(frozen=True)
class ProviderDeadline:
    """A monotonic total deadline for one provider invocation."""

    started_at: float
    logical_timeout_seconds: float
    transport_timeout_seconds: float
    cleanup_grace_seconds: float = CLEANUP_GRACE_SECONDS

    @classmethod
    def start(
        cls,
        logical_timeout_seconds: float,
        *,
        transport_margin_seconds: float = TRANSPORT_TIMEOUT_MARGIN_SECONDS,
        cleanup_grace_seconds: float = CLEANUP_GRACE_SECONDS,
    ) -> "ProviderDeadline":
        logical_timeout = float(logical_timeout_seconds)
        if logical_timeout <= 0:
            raise ValueError("provider logical timeout must be positive")
        transport_timeout = logical_timeout + float(transport_margin_seconds)
        return cls(
            started_at=time.monotonic(),
            logical_timeout_seconds=logical_timeout,
            transport_timeout_seconds=transport_timeout,
            cleanup_grace_seconds=float(cleanup_grace_seconds),
        )

    @property
    def deadline_at(self) -> float:
        return self.started_at + self.logical_timeout_seconds

    def remaining(self, *, now: float | None = None) -> float:
        observed_at = time.monotonic() if now is None else now
        return max(0.0, self.deadline_at - observed_at)

    def diagnostics(
        self, *, timed_out: bool = False
    ) -> dict[str, int | float | bool | str]:
        return {
            "timed_out": timed_out,
            "timeout_boundary": "provider_deadline" if timed_out else None,
            "timeout_seconds": _diagnostic_number(self.logical_timeout_seconds),
            "configured_logical_timeout_seconds": _diagnostic_number(
                self.logical_timeout_seconds
            ),
            "effective_logical_deadline_seconds": _diagnostic_number(
                self.logical_timeout_seconds
            ),
            "effective_transport_timeout_seconds": _diagnostic_number(
                self.transport_timeout_seconds
            ),
            "cleanup_grace_seconds": _diagnostic_number(self.cleanup_grace_seconds),
            "timeout_classification": "provider_timeout" if timed_out else None,
        }


class ProviderDeadlineExceeded(AgentRuntimeError):
    """The Orchestrator logical deadline won over the transport margin."""

    def __init__(self, deadline: ProviderDeadline):
        self.provider_failure_classification = "provider_timeout"
        self.runtime_diagnostics = deadline.diagnostics(timed_out=True)
        logical = _diagnostic_number(deadline.logical_timeout_seconds)
        transport = _diagnostic_number(deadline.transport_timeout_seconds)
        super().__init__(
            f"Provider logical deadline exceeded after {logical}s "
            f"(transport timeout {transport}s)."
        )


async def invoke_with_provider_deadline(
    invocation: Callable[[], Awaitable[_T]], *, deadline: ProviderDeadline
) -> _T:
    """Run one invocation against an absolute monotonic logical deadline."""

    try:
        return await asyncio.wait_for(invocation(), timeout=deadline.remaining())
    except asyncio.TimeoutError as exc:
        # Preserve an inner provider timeout if it fired before the logical
        # deadline.  A padded transport timeout normally reaches this branch
        # only after the absolute logical deadline has expired.
        if deadline.remaining() > 0:
            raise
        raise ProviderDeadlineExceeded(deadline) from exc


__all__ = [
    "CLEANUP_GRACE_SECONDS",
    "ProviderDeadline",
    "ProviderDeadlineExceeded",
    "TRANSPORT_TIMEOUT_MARGIN_SECONDS",
    "invoke_with_provider_deadline",
]
