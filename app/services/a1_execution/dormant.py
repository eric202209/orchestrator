"""Non-executing A1 backend for deterministic tests and development.

It never spawns a process, opens a connection, invokes a container runtime,
an agent runtime, or a provider, or touches a repository.  Every capability is
declared ``UNSUPPORTED`` and every start is refused.  Closure is evaluated
only from synthetic observations supplied at construction, through the same
evaluator real backends use, so it can never report closure without
evidence.  It is not registered as a selectable backend anywhere.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.services.a1_execution.backend import (
    ExecutionStatus,
    ExecutionStatusReport,
    PreparationResult,
    PreparationStatus,
    StartAuthorization,
    StartRefused,
    TerminationRequestResult,
    TerminationRequestStatus,
)
from app.services.a1_execution.capabilities import (
    REQUIRED_A1_CAPABILITIES,
    BackendCapabilityProfile,
    CapabilityDeclaration,
    CapabilityEvidence,
)
from app.services.a1_execution.closure import (
    AttemptClosureVerdict,
    ClosureEvidence,
    ClosureObservation,
    evaluate_attempt_closure,
)
from app.services.a1_execution.identity import A1AttemptIdentity, require_same_attempt

DORMANT_BACKEND_ID = "dormant"
DORMANT_REASON = "dormant_backend_never_executes"


class DormantExecutionBackend:
    def __init__(
        self,
        attempt: A1AttemptIdentity,
        *,
        synthetic_observations: Iterable[ClosureObservation] = (),
        synthetic_evidence: Optional[ClosureEvidence] = None,
    ) -> None:
        self._attempt = attempt
        self._observations = tuple(synthetic_observations)
        self._evidence = synthetic_evidence

    @property
    def backend_id(self) -> str:
        return DORMANT_BACKEND_ID

    @property
    def attempt(self) -> A1AttemptIdentity:
        return self._attempt

    def capability_profile(self) -> BackendCapabilityProfile:
        return BackendCapabilityProfile.from_declarations(
            DORMANT_BACKEND_ID,
            *(
                CapabilityDeclaration(capability, CapabilityEvidence.UNSUPPORTED)
                for capability in REQUIRED_A1_CAPABILITIES
            ),
        )

    async def prepare(self, attempt: A1AttemptIdentity) -> PreparationResult:
        require_same_attempt(self._attempt, attempt)
        return PreparationResult(
            attempt, PreparationStatus.NOT_AUTHORIZED, (DORMANT_REASON,)
        )

    async def start(
        self, attempt: A1AttemptIdentity, authorization: StartAuthorization
    ) -> None:
        require_same_attempt(self._attempt, attempt)
        require_same_attempt(self._attempt, authorization.attempt)
        raise StartRefused(attempt, (DORMANT_REASON, *authorization.reasons))

    async def inspect(self, attempt: A1AttemptIdentity) -> ExecutionStatusReport:
        require_same_attempt(self._attempt, attempt)
        return ExecutionStatusReport(
            attempt, ExecutionStatus.NOT_STARTED, (DORMANT_REASON,)
        )

    async def request_termination(
        self, attempt: A1AttemptIdentity
    ) -> TerminationRequestResult:
        require_same_attempt(self._attempt, attempt)
        return TerminationRequestResult(
            attempt, TerminationRequestStatus.NOTHING_TO_TERMINATE, (DORMANT_REASON,)
        )

    async def verify_closure(self, attempt: A1AttemptIdentity) -> AttemptClosureVerdict:
        require_same_attempt(self._attempt, attempt)
        return evaluate_attempt_closure(attempt, self._observations, self._evidence)

    async def closure_evidence(
        self, attempt: A1AttemptIdentity
    ) -> Optional[ClosureEvidence]:
        require_same_attempt(self._attempt, attempt)
        return self._evidence
