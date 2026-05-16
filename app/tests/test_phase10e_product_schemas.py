"""Phase 10E product presentation schema tests."""

from __future__ import annotations

from app.schemas.product import ProductRunView, derive_product_run_state


def test_product_run_state_prefers_acceptance_over_raw_done_status():
    state = derive_product_run_state(
        task_status="done",
        workspace_status="promoted",
        changed_count=3,
    )

    assert state == "accepted"


def test_product_run_state_marks_review_required_change_set():
    state = derive_product_run_state(
        task_status="done",
        workspace_status="ready",
        review_decision={"held_for_review": True},
        changed_count=2,
    )

    assert state == "needs_review"


def test_product_run_state_marks_request_changes_as_rejected():
    state = derive_product_run_state(
        task_status="done",
        workspace_status="changes_requested",
    )

    assert state == "rejected"


def test_product_run_state_surfaces_rollback_availability_before_failure():
    state = derive_product_run_state(
        task_status="failed",
        rollback_available=True,
    )

    assert state == "rollback_available"


def test_product_run_view_flattens_internal_change_set_language():
    view = ProductRunView.from_internal(
        project_id=7,
        task_id=11,
        title="Update docs",
        task_status="done",
        workspace_status="ready",
        review_decision={
            "held_for_review": True,
            "reason": "nontrivial_change_set_review_required",
            "warning_flags": ["deleted_files"],
        },
        change_set={
            "changed_count": 4,
            "added_count": 2,
            "modified_count": 1,
            "deleted_count": 1,
            "warning_flags": ["deleted_files"],
        },
        diagnostics_ref="/projects/7/tasks/11",
    )

    assert view.state == "needs_review"
    assert view.changes is not None
    assert view.changes.changed_count == 4
    assert view.review.required is True
    assert view.review.warning_flags == ["deleted_files"]
    assert view.diagnostics_ref == "/projects/7/tasks/11"
