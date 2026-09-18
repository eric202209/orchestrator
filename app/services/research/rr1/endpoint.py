"""Canonical logical endpoint for RR1 research observation.

The Research harness has no independent terminality opinion.  It asks the
accepted lifecycle authority (``app.services.orchestration.lifecycle.authority``)
and applies exactly one predicate to the answer.

Raw values such as ``Task.status``, ``TaskExecution.status``,
``Session.status == failed|paused``, ``Session.is_active`` and the historical
``TASK_FAILED``/``ABORTED`` markers never independently terminate observation:
they reach this module only through the authority projection, and only as
inputs the authority itself has already reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel
from app.services.orchestration.lifecycle.authority import (
    LifecycleAuthority,
    derive_lifecycle_authority,
)

#: The prospective logical endpoint, stated exactly as Reconciliation A
#: recommended.  It is reproduced here as a *claim to be proven* against the
#: current authority, never as an independent implementation of terminality.
CANONICAL_ENDPOINT_EXPRESSION = (
    "logical_terminal == true "
    "AND continuation_pending == false "
    "AND quiescent == true"
)

#: Authority fields the predicate is allowed to read.  Anything outside this
#: set would be the harness re-deriving Product lifecycle authority.
CANONICAL_ENDPOINT_INPUTS = ("logical_terminal", "continuation_pending", "quiescent")

#: The obsolete RER-02 Cohort-1 release logic, retained only so the RER-02A
#: reproduction (§22) can demonstrate the defect it caused.  It is never used
#: to classify a prospective run.
LEGACY_COHORT1_TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})
LEGACY_COHORT1_TERMINAL_SESSION_STATUSES = frozenset(
    {"stopped", "completed", "failed", "cancelled", "done"}
)


@dataclass(frozen=True)
class LogicalEndpointObservation:
    """One read of the canonical logical endpoint for one Session."""

    session_id: int | None
    reached: bool
    logical_terminal: bool
    continuation_pending: bool
    quiescent: bool
    current_phase: str | None
    continuation_kind: str | None
    attempt_status: str | None
    terminal_reason: str | None
    observed_at: datetime
    generation: str | None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "logical_endpoint_reached": self.reached,
            "logical_terminal": self.logical_terminal,
            "continuation_pending": self.continuation_pending,
            "quiescent": self.quiescent,
            "current_phase": self.current_phase,
            "continuation_kind": self.continuation_kind,
            "attempt_status": self.attempt_status,
            "terminal_reason": self.terminal_reason,
            "observed_at": self.observed_at.isoformat(),
            "generation": self.generation,
            "predicate": CANONICAL_ENDPOINT_EXPRESSION,
        }


def logical_endpoint_reached(authority: LifecycleAuthority | Mapping[str, Any]) -> bool:
    """Apply the canonical endpoint predicate to an authority answer.

    Accepts either a :class:`LifecycleAuthority` or its projection mapping so
    the same predicate can be applied to a live derivation and to recorded
    evidence without a second implementation.
    """

    if isinstance(authority, Mapping):
        values = {name: authority.get(name) for name in CANONICAL_ENDPOINT_INPUTS}
    else:
        values = {
            name: getattr(authority, name, None) for name in CANONICAL_ENDPOINT_INPUTS
        }
    for name in CANONICAL_ENDPOINT_INPUTS:
        if not isinstance(values[name], bool):
            # A missing or non-boolean authority answer is not terminality.
            return False
    return (
        values["logical_terminal"] is True
        and values["continuation_pending"] is False
        and values["quiescent"] is True
    )


def legacy_cohort1_released(
    *, task_status: str | None, session_status: str | None
) -> bool:
    """Reproduce the obsolete Cohort-1 release logic verbatim.

    Historical shape::

        task.status in TERMINAL_TASK_STATUSES
        or session.status in TERMINAL_SESSION_STATUSES
    """

    task = str(task_status or "").strip().lower()
    session = str(session_status or "").strip().lower()
    return (
        task in LEGACY_COHORT1_TERMINAL_TASK_STATUSES
        or session in LEGACY_COHORT1_TERMINAL_SESSION_STATUSES
    )


def observe_logical_endpoint(
    db: DbSession,
    session: SessionModel,
    *,
    task_id: int | None = None,
    observed_at: datetime,
) -> LogicalEndpointObservation:
    """Derive the canonical authority once and apply the endpoint predicate."""

    authority = derive_lifecycle_authority(db, session, task_id=task_id)
    return LogicalEndpointObservation(
        session_id=getattr(session, "id", None),
        reached=logical_endpoint_reached(authority),
        logical_terminal=authority.logical_terminal,
        continuation_pending=authority.continuation_pending,
        quiescent=authority.quiescent,
        current_phase=authority.current_phase,
        continuation_kind=authority.continuation_kind,
        attempt_status=authority.attempt_status,
        terminal_reason=authority.terminal_reason,
        observed_at=observed_at,
        generation=_generation(session),
    )


def _generation(session: SessionModel) -> str | None:
    value = getattr(session, "instance_id", None)
    return str(value) if value else None
