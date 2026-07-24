"""Phase 29D-3B canonical apply-requirement authority.

Answers exactly one question, from immutable accepted candidate content
alone: does this accepted candidate outcome require Controlled Apply before
its owning ``ExecutionTask`` may reach ``succeeded``?  This is the single
place that decision is made; validation acceptance finalization (and any
future lifecycle service) must call it rather than re-deriving the rule from
``ExecutionTaskCandidateContent.media_type`` directly.

The decision depends only on the candidate outcome's own content row and its
independently verified integrity — never on whether a ChangeSet, Apply
Authorization, Apply Attempt, or Apply Result happens to exist.  A candidate
outcome with no content row at all cannot describe a mutation and does not
require apply.  A content row that fails integrity verification cannot be
safely classified and fails closed rather than defaulting to either outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import ExecutionTaskCandidateContent
from app.services.execution.candidate_content import (
    CHANGESET_MEDIA_TYPE,
    CandidateContentStore,
    verify_candidate_content_integrity,
)


APPLY_REQUIRED = "apply_required"
APPLY_NOT_REQUIRED = "apply_not_required"
APPLY_REQUIREMENT_BLOCKED = "apply_requirement_blocked"
APPLY_REQUIREMENT_OUTCOMES = frozenset(
    {APPLY_REQUIRED, APPLY_NOT_REQUIRED, APPLY_REQUIREMENT_BLOCKED}
)


@dataclass(frozen=True)
class ApplyRequirementDecision:
    outcome: str
    candidate_outcome_id: int
    candidate_content_id: int | None
    candidate_content_media_type: str | None
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)


def determine_apply_requirement(
    db: Session,
    *,
    candidate_outcome_id: int,
    store: CandidateContentStore | None = None,
) -> ApplyRequirementDecision:
    content = (
        db.query(ExecutionTaskCandidateContent)
        .filter(
            ExecutionTaskCandidateContent.candidate_outcome_id == candidate_outcome_id
        )
        .one_or_none()
    )
    if content is None:
        return ApplyRequirementDecision(
            outcome=APPLY_NOT_REQUIRED,
            candidate_outcome_id=candidate_outcome_id,
            candidate_content_id=None,
            candidate_content_media_type=None,
        )
    integrity = verify_candidate_content_integrity(db, content.id, store=store)
    if not integrity.verified:
        return ApplyRequirementDecision(
            outcome=APPLY_REQUIREMENT_BLOCKED,
            candidate_outcome_id=candidate_outcome_id,
            candidate_content_id=content.id,
            candidate_content_media_type=content.media_type,
            blocked_reasons=tuple(integrity.issues or ()),
        )
    outcome = (
        APPLY_REQUIRED
        if content.media_type == CHANGESET_MEDIA_TYPE
        else APPLY_NOT_REQUIRED
    )
    return ApplyRequirementDecision(
        outcome=outcome,
        candidate_outcome_id=candidate_outcome_id,
        candidate_content_id=content.id,
        candidate_content_media_type=content.media_type,
    )


__all__ = [
    "APPLY_REQUIRED",
    "APPLY_NOT_REQUIRED",
    "APPLY_REQUIREMENT_BLOCKED",
    "APPLY_REQUIREMENT_OUTCOMES",
    "ApplyRequirementDecision",
    "determine_apply_requirement",
]
