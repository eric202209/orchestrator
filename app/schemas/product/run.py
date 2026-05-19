"""Product-facing Run schemas.

These schemas translate internal task/session/change-set fields into the
language used by the main product surface. They do not replace ORM models or
orchestration contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ProductRunState = Literal[
    "running",
    "failed",
    "needs_review",
    "accepted",
    "rejected",
    "rollback_available",
]

_RUNNING_STATUSES = {"pending", "running", "active", "awaiting_input"}
_FAILED_STATUSES = {"failed", "cancelled", "stopped"}
_COMPLETED_STATUSES = {"done", "completed", "complete", "success"}
_ACCEPTED_DISPOSITIONS = {"accepted", "promoted"}
_REJECTED_DISPOSITIONS = {"rejected", "changes_requested"}


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def derive_product_run_state(
    *,
    session_status: Optional[str] = None,
    task_status: Optional[str] = None,
    workspace_status: Optional[str] = None,
    review_decision: Optional[dict[str, Any]] = None,
    change_disposition: Optional[str] = None,
    changed_count: int = 0,
    rollback_available: bool = False,
) -> ProductRunState:
    """Collapse internal status fields into one user-visible Run state."""

    normalized_workspace = _normalize(workspace_status)
    normalized_disposition = _normalize(change_disposition)
    normalized_task = _normalize(task_status)
    normalized_session = _normalize(session_status)

    if (
        normalized_workspace == "promoted"
        or normalized_disposition in _ACCEPTED_DISPOSITIONS
    ):
        return "accepted"
    if (
        normalized_workspace == "changes_requested"
        or normalized_disposition in _REJECTED_DISPOSITIONS
    ):
        return "rejected"

    review = review_decision or {}
    if bool(review.get("held_for_review")) or (
        normalized_workspace == "ready" and changed_count > 0
    ):
        return "needs_review"

    if normalized_task in _RUNNING_STATUSES or normalized_session in _RUNNING_STATUSES:
        return "running"

    if rollback_available:
        return "rollback_available"
    if normalized_task in _FAILED_STATUSES or normalized_session in _FAILED_STATUSES:
        return "failed"
    if (
        normalized_task in _COMPLETED_STATUSES
        or normalized_session in _COMPLETED_STATUSES
    ):
        return "accepted"

    return "running"


class ProductChangeSummary(BaseModel):
    changed_count: int = 0
    added_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    warning_flags: list[str] = Field(default_factory=list)


class ProductReviewSummary(BaseModel):
    required: bool = False
    reason: Optional[str] = None
    warning_flags: list[str] = Field(default_factory=list)


class ProductRunView(BaseModel):
    """Main UI Run shape.

    Internal IDs are retained for navigation and actions, while labels and state
    names use product language.
    """

    run_id: Optional[int] = None
    project_id: int
    task_id: Optional[int] = None
    title: str
    state: ProductRunState
    changes: Optional[ProductChangeSummary] = None
    review: ProductReviewSummary = Field(default_factory=ProductReviewSummary)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    diagnostics_ref: Optional[str] = None

    @classmethod
    def from_internal(
        cls,
        *,
        project_id: int,
        title: str,
        run_id: Optional[int] = None,
        task_id: Optional[int] = None,
        session_status: Optional[str] = None,
        task_status: Optional[str] = None,
        workspace_status: Optional[str] = None,
        review_decision: Optional[dict[str, Any]] = None,
        change_set: Optional[dict[str, Any]] = None,
        change_disposition: Optional[str] = None,
        rollback_available: bool = False,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        diagnostics_ref: Optional[str] = None,
    ) -> "ProductRunView":
        change_payload = change_set or {}
        changed_count = int(change_payload.get("changed_count") or 0)
        review_payload = review_decision or {}
        warning_flags = [
            str(flag)
            for flag in (
                review_payload.get("warning_flags")
                or change_payload.get("warning_flags")
                or []
            )
        ]
        changes = None
        if change_set is not None:
            changes = ProductChangeSummary(
                changed_count=changed_count,
                added_count=int(change_payload.get("added_count") or 0),
                modified_count=int(change_payload.get("modified_count") or 0),
                deleted_count=int(change_payload.get("deleted_count") or 0),
                warning_flags=warning_flags,
            )

        return cls(
            run_id=run_id,
            project_id=project_id,
            task_id=task_id,
            title=title,
            state=derive_product_run_state(
                session_status=session_status,
                task_status=task_status,
                workspace_status=workspace_status,
                review_decision=review_decision,
                change_disposition=change_disposition,
                changed_count=changed_count,
                rollback_available=rollback_available,
            ),
            changes=changes,
            review=ProductReviewSummary(
                required=bool(review_payload.get("held_for_review")),
                reason=review_payload.get("reason"),
                warning_flags=warning_flags,
            ),
            started_at=started_at,
            completed_at=completed_at,
            diagnostics_ref=diagnostics_ref,
        )
