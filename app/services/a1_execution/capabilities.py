"""Evidence-graded capability model for A1 execution backends.

A capability declaration is not proof.  Each required capability carries an
evidence grade, and only ``VERIFIED`` with an evidence reference satisfies a
requirement.  Even when every requirement is met, the decision never permits
runtime admission: admission is a separate operator gate (K11) and is
disabled while A1-EXECUTION-CONTRACT/2.2 is frozen and not accepted.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from app.services.a1_execution.identity import validate_opaque_ref


class A1Capability(str, enum.Enum):
    ATTEMPT_IDENTITY_BINDING = "attempt_identity_binding"
    PROCESS_ISOLATION = "process_isolation"
    DESCENDANT_CONTAINMENT = "descendant_containment"
    FILESYSTEM_ISOLATION = "filesystem_isolation"
    NETWORK_ISOLATION = "network_isolation"
    POSITIVE_LOCAL_PROCESS_CLOSURE = "positive_local_process_closure"
    PROVIDER_REQUEST_IDENTITY = "provider_request_identity"
    POSITIVE_PROVIDER_CLOSURE = "positive_provider_closure"
    DURABLE_CLOSURE_EVIDENCE = "durable_closure_evidence"
    RECOVERY_RECONCILIATION = "recovery_reconciliation"


class CapabilityEvidence(str, enum.Enum):
    SUPPORTED_BY_DESIGN = "supported_by_design"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


# Every capability is required for A1; none is optional in the initial scope.
REQUIRED_A1_CAPABILITIES: tuple[A1Capability, ...] = tuple(A1Capability)

RUNTIME_ADMISSION_DISABLED_REASON = "runtime_admission_disabled"


@dataclass(frozen=True)
class CapabilityDeclaration:
    capability: A1Capability
    evidence: CapabilityEvidence
    evidence_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, A1Capability):
            raise ValueError("capability must be an A1Capability")
        if not isinstance(self.evidence, CapabilityEvidence):
            raise ValueError("evidence must be a CapabilityEvidence")
        if self.evidence_ref is not None:
            validate_opaque_ref(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True)
class BackendCapabilityProfile:
    """A backend's declared capabilities, keyed by capability.

    Capabilities absent from the profile are treated as ``UNKNOWN``.
    """

    backend_id: str
    declarations: Mapping[A1Capability, CapabilityDeclaration]

    def __post_init__(self) -> None:
        for capability, declaration in self.declarations.items():
            if declaration.capability is not capability:
                raise ValueError("declaration key does not match its capability")
        # Freeze the mapping so the profile is immutable after construction.
        object.__setattr__(
            self, "declarations", MappingProxyType(dict(self.declarations))
        )

    @classmethod
    def from_declarations(
        cls, backend_id: str, *declarations: CapabilityDeclaration
    ) -> "BackendCapabilityProfile":
        mapping: dict[A1Capability, CapabilityDeclaration] = {}
        for declaration in declarations:
            if declaration.capability in mapping:
                raise ValueError("duplicate capability declaration")
            mapping[declaration.capability] = declaration
        return cls(backend_id=backend_id, declarations=mapping)

    def declaration_for(self, capability: A1Capability) -> CapabilityDeclaration:
        return self.declarations.get(
            capability,
            CapabilityDeclaration(capability, CapabilityEvidence.UNKNOWN),
        )


@dataclass(frozen=True)
class EligibilityDecision:
    """Deterministic result of evaluating a profile against A1 requirements.

    ``blocking`` lists ``(capability, reason)`` pairs in requirement order.
    ``runtime_admission_permitted`` is always ``False`` in this scope.
    """

    backend_id: str
    capability_requirements_met: bool
    blocking: tuple[tuple[A1Capability, str], ...]
    runtime_admission_permitted: bool
    admission_refusal_reason: str


def _blocking_reason(declaration: CapabilityDeclaration) -> Optional[str]:
    if declaration.evidence is CapabilityEvidence.VERIFIED:
        return None if declaration.evidence_ref else "verified_without_evidence_ref"
    return declaration.evidence.value


def evaluate_capability_eligibility(
    profile: BackendCapabilityProfile,
) -> EligibilityDecision:
    blocking = tuple(
        (capability, reason)
        for capability in REQUIRED_A1_CAPABILITIES
        if (reason := _blocking_reason(profile.declaration_for(capability))) is not None
    )
    return EligibilityDecision(
        backend_id=profile.backend_id,
        capability_requirements_met=not blocking,
        blocking=blocking,
        runtime_admission_permitted=False,
        admission_refusal_reason=RUNTIME_ADMISSION_DISABLED_REASON,
    )
