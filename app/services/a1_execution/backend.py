"""A1 execution backend protocol.

The protocol separates preparation, start authorization, status inspection,
termination *requests*, positive closure verification, and durable closure
evidence.  It deliberately has no command, shell, path, or provider-request
parameter: a backend runs only the A1 task kind through the future
four-operation broker (list_entries, read_file, search_text,
read_git_identity), which is not implemented here.

Platform-specific isolation and closure mechanisms, and provider lifecycle
adapters, belong inside concrete backends, never in this core contract.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Protocol

from app.services.a1_execution.capabilities import (
    BackendCapabilityProfile,
    evaluate_capability_eligibility,
)
from app.services.a1_execution.closure import AttemptClosureVerdict, ClosureEvidence
from app.services.a1_execution.identity import A1AttemptIdentity


class PreparationStatus(str, enum.Enum):
    PREPARED = "prepared"
    NOT_AUTHORIZED = "not_authorized"
    UNSUPPORTED = "unsupported"


class ExecutionStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    # A process or container exit was observed; closure is not implied.
    EXITED_UNVERIFIED = "exited_unverified"
    UNKNOWN = "unknown"


class TerminationRequestStatus(str, enum.Enum):
    """Outcome of a termination *request*. No value implies closure."""

    REQUESTED = "requested"
    NOTHING_TO_TERMINATE = "nothing_to_terminate"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparationResult:
    attempt: A1AttemptIdentity
    status: PreparationStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionStatusReport:
    attempt: A1AttemptIdentity
    status: ExecutionStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminationRequestResult:
    attempt: A1AttemptIdentity
    status: TerminationRequestStatus
    reasons: tuple[str, ...] = ()


class StartRefused(RuntimeError):
    """A backend refused to start an Attempt."""

    def __init__(self, attempt: A1AttemptIdentity, reasons: tuple[str, ...]):
        super().__init__("A1 execution start refused: " + ", ".join(reasons))
        self.attempt = attempt
        self.reasons = reasons


@dataclass(frozen=True)
class StartAuthorization:
    """Decision on whether an Attempt may start on a backend.

    No runtime admission path exists in this scope, so a permitting
    authorization cannot be constructed.
    """

    attempt: A1AttemptIdentity
    backend_id: str
    permitted: bool
    reasons: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.permitted:
            raise ValueError(
                "A1 runtime admission is disabled; a permitting start "
                "authorization cannot be created"
            )


def decide_start_authorization(
    attempt: A1AttemptIdentity, profile: BackendCapabilityProfile
) -> StartAuthorization:
    decision = evaluate_capability_eligibility(profile)
    reasons = tuple(
        f"{capability.value}:{reason}" for capability, reason in decision.blocking
    ) + (decision.admission_refusal_reason,)
    return StartAuthorization(
        attempt=attempt,
        backend_id=profile.backend_id,
        permitted=False,
        reasons=reasons,
    )


class A1ExecutionBackend(Protocol):
    """One backend instance is bound to exactly one Attempt identity."""

    @property
    def backend_id(self) -> str: ...

    @property
    def attempt(self) -> A1AttemptIdentity: ...

    def capability_profile(self) -> BackendCapabilityProfile: ...

    async def prepare(self, attempt: A1AttemptIdentity) -> PreparationResult: ...

    async def start(
        self, attempt: A1AttemptIdentity, authorization: StartAuthorization
    ) -> None: ...

    async def inspect(self, attempt: A1AttemptIdentity) -> ExecutionStatusReport: ...

    async def request_termination(
        self, attempt: A1AttemptIdentity
    ) -> TerminationRequestResult: ...

    async def verify_closure(
        self, attempt: A1AttemptIdentity
    ) -> AttemptClosureVerdict: ...

    async def closure_evidence(
        self, attempt: A1AttemptIdentity
    ) -> Optional[ClosureEvidence]: ...
