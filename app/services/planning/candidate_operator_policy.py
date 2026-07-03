"""Candidate Recovery operator contracts and machine-profile policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


OPERATOR_SIBLING_GENERATION = "sibling_generation"
OPERATOR_SLOT_MERGE = "slot_merge"

PROFILE_STANDARD = "standard"
PROFILE_MEDIUM = "medium"
PROFILE_LOW_RESOURCE = "low_resource"
PROFILE_COMPACT_LOCAL = "compact_local"


class CandidateOperator(Protocol):
    """Common contract for deterministic Candidate Recovery operators."""

    operator_name: str

    def execute(self, request: Any) -> Any:
        """Return one CandidateRuntimeResult without adaptive search."""
        ...


@dataclass(frozen=True)
class CandidateOperatorPolicy:
    runtime_profile: str
    operator: str
    feature_flag: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class CandidateOperatorPolicyDecision:
    allowed: bool
    reason: str
    operator: str = ""


CANDIDATE_OPERATOR_POLICY: Mapping[str, CandidateOperatorPolicy] = {
    PROFILE_STANDARD: CandidateOperatorPolicy(
        runtime_profile=PROFILE_STANDARD,
        operator=OPERATOR_SIBLING_GENERATION,
    ),
    PROFILE_MEDIUM: CandidateOperatorPolicy(
        runtime_profile=PROFILE_MEDIUM,
        operator=OPERATOR_SLOT_MERGE,
        feature_flag="CANDIDATE_SLOT_MERGE_ENABLED",
    ),
    PROFILE_LOW_RESOURCE: CandidateOperatorPolicy(
        runtime_profile=PROFILE_LOW_RESOURCE,
        operator="",
        enabled=False,
    ),
    PROFILE_COMPACT_LOCAL: CandidateOperatorPolicy(
        runtime_profile=PROFILE_COMPACT_LOCAL,
        operator="",
        enabled=False,
    ),
}


def operator_for_runtime_profile(runtime_profile: str) -> str:
    policy = CANDIDATE_OPERATOR_POLICY.get(str(runtime_profile or ""))
    if not policy or not policy.enabled:
        return ""
    return policy.operator


def evaluate_candidate_operator_policy(
    *,
    runtime_profile: str,
    candidate_operator: str = "",
    candidate_recovery_enabled: bool,
    slot_merge_enabled: bool,
) -> CandidateOperatorPolicyDecision:
    if not candidate_recovery_enabled:
        return CandidateOperatorPolicyDecision(False, "not_enabled")

    policy = CANDIDATE_OPERATOR_POLICY.get(str(runtime_profile or ""))
    if not policy or not policy.enabled:
        return CandidateOperatorPolicyDecision(False, "unsupported_runtime_profile")

    if policy.feature_flag and not candidate_operator:
        return CandidateOperatorPolicyDecision(False, "unsupported_runtime_profile")

    requested_operator = str(candidate_operator or policy.operator)
    if requested_operator != policy.operator:
        return CandidateOperatorPolicyDecision(False, "unsupported_runtime_profile")

    if policy.feature_flag == "CANDIDATE_SLOT_MERGE_ENABLED" and not slot_merge_enabled:
        return CandidateOperatorPolicyDecision(False, "unsupported_runtime_profile")

    return CandidateOperatorPolicyDecision(True, "", policy.operator)
