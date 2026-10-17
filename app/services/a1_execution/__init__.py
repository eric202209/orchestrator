"""Dormant A1 execution foundation (PHASE38E1).

Provider-, platform- and architecture-neutral types for future A1 execution
backends: Attempt identity, an evidence-graded capability model, closure
semantics, and a non-executing backend.  Nothing in this package spawns a
process, opens a network connection, or touches a repository, and no API
route, Celery task, scheduler, or configuration flag imports it.  Runtime
admission stays disabled (A1-EXECUTION-CONTRACT/2.2 is frozen, not accepted).

This package is not Phase 29 execution: its identities are deliberately
separate from ``ExecutionTaskAttempt`` lineage (contract §14).
"""

from app.services.a1_execution.backend import (
    A1ExecutionBackend,
    ExecutionStatus,
    ExecutionStatusReport,
    PreparationResult,
    PreparationStatus,
    StartAuthorization,
    StartRefused,
    TerminationRequestResult,
    TerminationRequestStatus,
    decide_start_authorization,
)
from app.services.a1_execution.capabilities import (
    REQUIRED_A1_CAPABILITIES,
    A1Capability,
    BackendCapabilityProfile,
    CapabilityDeclaration,
    CapabilityEvidence,
    EligibilityDecision,
    evaluate_capability_eligibility,
)
from app.services.a1_execution.closure import (
    AttemptClosureVerdict,
    ClosureDomain,
    ClosureEvidence,
    ClosureEvidenceDurability,
    ClosureObservation,
    ClosureSignal,
    ClosureStatus,
    evaluate_attempt_closure,
)
from app.services.a1_execution.dormant import DormantExecutionBackend
from app.services.a1_execution.identity import (
    A1AttemptIdentity,
    AttemptIdentityMismatch,
    require_same_attempt,
)

__all__ = [
    "A1AttemptIdentity",
    "A1Capability",
    "A1ExecutionBackend",
    "AttemptClosureVerdict",
    "AttemptIdentityMismatch",
    "BackendCapabilityProfile",
    "CapabilityDeclaration",
    "CapabilityEvidence",
    "ClosureDomain",
    "ClosureEvidence",
    "ClosureEvidenceDurability",
    "ClosureObservation",
    "ClosureSignal",
    "ClosureStatus",
    "DormantExecutionBackend",
    "EligibilityDecision",
    "ExecutionStatus",
    "ExecutionStatusReport",
    "PreparationResult",
    "PreparationStatus",
    "REQUIRED_A1_CAPABILITIES",
    "StartAuthorization",
    "StartRefused",
    "TerminationRequestResult",
    "TerminationRequestStatus",
    "decide_start_authorization",
    "evaluate_attempt_closure",
    "evaluate_capability_eligibility",
    "require_same_attempt",
]
