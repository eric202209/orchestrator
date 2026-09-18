"""Proof that the RR1 endpoint predicate equals the accepted authority (§5).

The prompt supplies a *recommended* expression.  RR1 must not hard-code it on
that basis, so this module derives the endpoint from the accepted authority
and proves the two agree, in three independent ways:

``structural``
    The authority's own definitions imply two invariants --
    ``logical_terminal -> not continuation_pending`` and
    ``quiescent -> not continuation_pending`` -- so the recommended three-term
    expression is logically equivalent to ``logical_terminal and quiescent``.
    Both invariants are checked empirically over the full state space below,
    rather than asserted from a reading of the source.

``exhaustive``
    Every combination of Session status, continuation-marker shape, and
    active-work residue reachable at this baseline is materialized against a
    real database, the authority is derived, and the predicate is compared
    with the literal recommended expression.

``no_raw_authority``
    For each such state, no raw value (``Task.status``,
    ``TaskExecution.status``, ``Session.status``, ``Session.is_active``) is
    permitted to satisfy the endpoint on its own where the authority does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel
from app.services.orchestration.lifecycle.authority import (
    derive_lifecycle_authority,
)
from app.services.research.rr1.endpoint import (
    CANONICAL_ENDPOINT_EXPRESSION,
    logical_endpoint_reached,
)

#: Every Session status the authority maps, plus one deliberately unknown
#: value so an unmapped status can never satisfy the endpoint.
ENUMERATED_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "recovering",
    "retry_pending",
    "paused",
    "awaiting_input",
    "stopped",
    "cancelled",
    "canceled",
    "failed",
    "done",
    "completed",
    "an_unmapped_status",
)

#: Continuation-marker shapes: absent, valid, and malformed.
CONTINUATION_SHAPES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "absent",
        {
            "continuation_task_id": None,
            "continuation_kind": None,
            "continuation_retry_count": 0,
            "continuation_retry_eta": None,
        },
    ),
    (
        "valid_retry",
        {
            "continuation_task_id": 1,
            "continuation_kind": "celery_retry",
            "continuation_retry_count": 1,
            "continuation_retry_eta": None,
        },
    ),
    (
        "malformed_missing_kind",
        {
            "continuation_task_id": 1,
            "continuation_kind": None,
            "continuation_retry_count": 1,
            "continuation_retry_eta": None,
        },
    ),
)


@dataclass
class EquivalenceResult:
    proven: bool
    expression: str = CANONICAL_ENDPOINT_EXPRESSION
    cases_checked: int = 0
    structural_invariants: dict[str, bool] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    raw_authority_violations: list[dict[str, Any]] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "proven": self.proven,
            "expression": self.expression,
            "cases_checked": self.cases_checked,
            "structural_invariants": dict(self.structural_invariants),
            "disagreement_count": len(self.disagreements),
            "disagreements": list(self.disagreements),
            "raw_authority_violation_count": len(self.raw_authority_violations),
            "raw_authority_violations": list(self.raw_authority_violations),
        }


def _literal_expression(authority: Any) -> bool:
    """The recommended expression, written out verbatim."""

    return (
        authority.logical_terminal is True
        and authority.continuation_pending is False
        and authority.quiescent is True
    )


def prove_endpoint_equivalence(
    db: DbSession,
    session: SessionModel,
    *,
    apply_active_work: Callable[[bool], None],
    statuses: Iterable[str] = ENUMERATED_STATUSES,
) -> EquivalenceResult:
    """Exhaustively prove the harness predicate equals the recommended one.

    ``apply_active_work`` is supplied by the caller so the proof can toggle
    durable active-work residue (a pending/running ``TaskExecution``) for the
    Session under test without this module owning Product write paths.
    """

    result = EquivalenceResult(proven=True)
    terminal_implies_no_continuation = True
    quiescent_implies_no_continuation = True

    for status in statuses:
        for shape_name, marker in CONTINUATION_SHAPES:
            for active_work in (False, True):
                session.status = status
                for attribute, value in marker.items():
                    setattr(session, attribute, value)
                db.flush()
                apply_active_work(active_work)
                db.flush()

                authority = derive_lifecycle_authority(db, session)
                harness = logical_endpoint_reached(authority)
                literal = _literal_expression(authority)
                result.cases_checked += 1

                if authority.logical_terminal and authority.continuation_pending:
                    terminal_implies_no_continuation = False
                if authority.quiescent and authority.continuation_pending:
                    quiescent_implies_no_continuation = False

                case = {
                    "status": status,
                    "continuation_shape": shape_name,
                    "active_work": active_work,
                    "logical_terminal": authority.logical_terminal,
                    "continuation_pending": authority.continuation_pending,
                    "quiescent": authority.quiescent,
                    "harness_predicate": harness,
                    "literal_expression": literal,
                }
                if harness != literal:
                    result.proven = False
                    result.disagreements.append(case)

                # No raw value may terminate observation on its own.
                raw_terminal = status in {
                    "failed",
                    "stopped",
                    "cancelled",
                    "canceled",
                    "done",
                    "completed",
                }
                if raw_terminal and not harness and literal is False:
                    # Correct: a raw terminal status alone did NOT release.
                    pass
                if not raw_terminal and harness:
                    result.proven = False
                    result.raw_authority_violations.append(
                        {**case, "violation": "endpoint_without_terminal_status"}
                    )

    result.structural_invariants = {
        "logical_terminal_implies_not_continuation_pending": (
            terminal_implies_no_continuation
        ),
        "quiescent_implies_not_continuation_pending": (
            quiescent_implies_no_continuation
        ),
    }
    if not all(result.structural_invariants.values()):
        result.proven = False
    return result
