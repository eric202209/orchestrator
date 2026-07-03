"""Canonical Candidate Recovery outcome contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from app.services.planning.plan_candidate import PlanCandidate


_OUTCOMES = frozenset({"selected", "exhausted", "skipped", "failed"})


def _tuple_of_strings(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(str(value) for value in (values or ()))


@dataclass(frozen=True)
class CandidatePlanningOutcome:
    """Immutable result shared by all future candidate recovery operators."""

    selected_candidate: Optional[PlanCandidate] = None
    candidate_count: int = 0
    operator_sequence: tuple[str, ...] = field(default_factory=tuple)
    outcome: str = "skipped"
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        candidate_count = int(self.candidate_count)
        if candidate_count < 0:
            raise ValueError("candidate_count must be non-negative")

        outcome = str(self.outcome).strip()
        if outcome not in _OUTCOMES:
            raise ValueError(f"unsupported candidate planning outcome: {outcome}")
        if outcome == "selected" and self.selected_candidate is None:
            raise ValueError("selected outcome requires selected_candidate")

        object.__setattr__(self, "candidate_count", candidate_count)
        object.__setattr__(
            self,
            "operator_sequence",
            _tuple_of_strings(self.operator_sequence),
        )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "audit_event_ids",
            _tuple_of_strings(self.audit_event_ids),
        )

    @classmethod
    def skipped(cls, *, reason: str = "not_enabled") -> "CandidatePlanningOutcome":
        return cls(
            selected_candidate=None,
            candidate_count=0,
            operator_sequence=(f"skipped:{reason}",),
            outcome="skipped",
            audit_event_ids=(),
        )

    def to_dict(self) -> Mapping[str, object]:
        return {
            "selected_candidate": (
                self.selected_candidate.to_dict() if self.selected_candidate else None
            ),
            "candidate_count": self.candidate_count,
            "operator_sequence": list(self.operator_sequence),
            "outcome": self.outcome,
            "audit_event_ids": list(self.audit_event_ids),
        }
