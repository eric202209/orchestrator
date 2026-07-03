"""Immutable Candidate Recovery planning candidate contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


_VALIDATOR_STATUSES = frozenset(
    {
        "accepted",
        "warning",
        "repair_required",
        "rejected",
    }
)


def _tuple_of_strings(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(str(value) for value in (values or ()))


@dataclass(frozen=True)
class PlanCandidate:
    """Canonical immutable candidate record.

    Candidate Recovery infrastructure tracks normal planning artifacts through
    metadata only. The candidate artifact itself is referenced by hash/lineage;
    future candidate operators still return exactly one normal Plan artifact.
    """

    candidate_id: str
    parent_candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    operator: str = "original"
    source_lineage: str = "primary"
    artifact_hash: str = ""
    validator_status: str = "rejected"
    validator_reasons: tuple[str, ...] = field(default_factory=tuple)
    planning_failure_signature: str = ""
    runtime_profile: str = "unknown"

    def __post_init__(self) -> None:
        candidate_id = str(self.candidate_id).strip()
        if not candidate_id:
            raise ValueError("candidate_id is required")

        validator_status = str(self.validator_status).strip()
        if validator_status not in _VALIDATOR_STATUSES:
            raise ValueError(f"unsupported validator_status: {validator_status}")

        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(
            self,
            "parent_candidate_ids",
            _tuple_of_strings(self.parent_candidate_ids),
        )
        object.__setattr__(self, "operator", str(self.operator).strip() or "unknown")
        object.__setattr__(
            self,
            "source_lineage",
            str(self.source_lineage).strip() or "unknown",
        )
        object.__setattr__(self, "artifact_hash", str(self.artifact_hash).strip())
        object.__setattr__(self, "validator_status", validator_status)
        object.__setattr__(
            self,
            "validator_reasons",
            _tuple_of_strings(self.validator_reasons),
        )
        object.__setattr__(
            self,
            "planning_failure_signature",
            str(self.planning_failure_signature).strip(),
        )
        object.__setattr__(
            self,
            "runtime_profile",
            str(self.runtime_profile).strip() or "unknown",
        )

    @property
    def accepted(self) -> bool:
        return self.validator_status in {"accepted", "warning"}

    @property
    def repairable(self) -> bool:
        return self.validator_status == "repair_required"

    @property
    def rejected(self) -> bool:
        return self.validator_status == "rejected"

    @property
    def rejected_reason_count(self) -> int:
        return len(self.validator_reasons) if self.rejected else 0

    @property
    def repairable_reason_count(self) -> int:
        return len(self.validator_reasons) if self.repairable else 0

    def to_dict(self) -> Mapping[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "parent_candidate_ids": list(self.parent_candidate_ids),
            "operator": self.operator,
            "source_lineage": self.source_lineage,
            "artifact_hash": self.artifact_hash,
            "validator_status": self.validator_status,
            "validator_reasons": list(self.validator_reasons),
            "planning_failure_signature": self.planning_failure_signature,
            "runtime_profile": self.runtime_profile,
        }
