"""P42-V17-I: "inventory" as a domain noun must not force review_only.

Task 304 persisted ``full_lifecycle`` but the standalone review marker
``inventory`` forced the Worker's effective profile to ``review_only`` before
Planning. These tests pin the corrected boundary without weakening genuine
review detection.
"""

import pytest

from app.models import Project, Task, TaskStatus
from app.services.orchestration.task_rules import (
    EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC,
    EXECUTION_PROFILE_SOURCE_TASK_ROW,
    classify_task_intent,
    run_virtual_merge_gate,
    should_force_review_execution_profile,
)
from app.services.orchestration.validation.validator import ValidatorService

# Read-only copy of Task 304's persisted title/description (P42-V17-H).
_TASK304_TITLE = "Add Inventory Reservation Support"
_TASK304_DESCRIPTION = (
    "Extend the existing inventory library to support reserving and releasing "
    "stock. Acceptance criteria: (1) A caller can reserve a positive quantity of "
    "available stock. (2) Reservations cannot exceed available stock. (3) A "
    "caller can release a previously reserved quantity. (4) Releasing more than "
    "the reserved quantity is rejected. (5) Available stock reflects outstanding "
    "reservations. (6) Total stock and reserved stock never become negative. "
    "(7) Existing add/remove/query behavior remains correct. (8) Add regression "
    "tests covering successful and rejected reservations. (9) All existing and "
    "new tests must pass. Make the implementation changes across the relevant "
    "inventory model/service and test files; do not alter unrelated files."
)


def _forces_review(title, description, profile="full_lifecycle"):
    # Worker calling convention: task_prompt = task.description.
    return should_force_review_execution_profile(
        profile, description, title, description
    )


def _effective_profile(persisted, title, description):
    """Mirror of the Worker's profile-selection step (app/tasks/worker.py)."""
    if _forces_review(title, description, persisted):
        return "review_only", EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC
    return persisted, EXECUTION_PROFILE_SOURCE_TASK_ROW


# A. Exact historical reproduction


def test_task304_full_lifecycle_is_not_forced_to_review_only():
    assert _forces_review(_TASK304_TITLE, _TASK304_DESCRIPTION) is False
    assert _effective_profile(
        "full_lifecycle", _TASK304_TITLE, _TASK304_DESCRIPTION
    ) == ("full_lifecycle", EXECUTION_PROFILE_SOURCE_TASK_ROW)


def test_task304_intent_classifier_recognizes_implementation():
    assert classify_task_intent(
        "full_lifecycle", _TASK304_TITLE, _TASK304_DESCRIPTION
    ).as_dict() == {
        "classification": "implementation_style",
        "classification_reason": "implementation_intent_in_title",
        "classification_authority": "task_title",
    }


# B. Domain-word controls


@pytest.mark.parametrize(
    "title, description",
    [
        ("Add Inventory Reservation Support", "Support reserving inventory."),
        ("Implement inventory reservation logic", "Reserve and release inventory."),
        ("Fix inventory stock calculation", "Available inventory is wrong."),
        ("Add tests for inventory reservation behavior", "Cover inventory reserve."),
        ("Add stock reservation support", "Reserve and release stock."),
        ("Implement reservation expiry", "Reservations expire after a timeout."),
    ],
)
def test_domain_terms_alone_do_not_force_review(title, description):
    assert _forces_review(title, description) is False


# C. Genuine review controls


@pytest.mark.parametrize(
    "title, description",
    [
        ("Review the inventory implementation", "Report findings."),
        (
            "Inventory service inspection",
            "Inspect the inventory service without modifying files.",
        ),
        ("Analyze the inventory module", "Summarize how reservations work."),
        (
            "Inventory handling walkthrough",
            "Perform a codebase walkthrough of inventory handling.",
        ),
        (
            "Inspect current project architecture",
            "Inventory extension points before implementation.",
        ),
        (
            "Inventory of reservation code",
            "Produce an inventory of the reservation service and its tests.",
        ),
        # Implementation nouns inside review requests stay review-only.
        (
            "Analyze the reservation implementation",
            "Analyze how stock fixes were added to the inventory service.",
        ),
    ],
)
def test_genuine_review_requests_remain_review_only(title, description):
    assert _forces_review(title, description) is True


# D. Ambiguous intent does not gain mutation authority


@pytest.mark.parametrize(
    "title, description",
    [
        ("Review the inventory module and fix any issues", "Check reservations."),
        ("Audit and update inventory reservations", "Check reservation rules."),
        ("Do not add inventory features", "Describe the inventory module."),
        ("Inventory reservations", "Add and fix whatever is needed."),
        ("Add inventory reservation support", "Inspect the inventory service."),
    ],
)
def test_ambiguous_requests_keep_conservative_review_only(title, description):
    assert _forces_review(title, description) is True


def test_explicit_non_mutating_profiles_are_never_overridden():
    for profile in ("review_only", "test_only", "debug_only"):
        assert _forces_review(_TASK304_TITLE, _TASK304_DESCRIPTION, profile) is False


# E. Downstream profile propagation (provider-free)


_MUTATING_PLAN = [
    {
        "step_number": 1,
        "description": "Add reservation support to the inventory service",
        "commands": ["touch inventory/reservations.py"],
        "verification": "test -f inventory/reservations.py",
        "rollback": "rm -f inventory/reservations.py",
        "expected_files": ["inventory/reservations.py"],
    }
]


def _validate(profile, title, description):
    # Validation only; the plan is never executed.
    return ValidatorService.validate_plan(
        _MUTATING_PLAN,
        output_text="[]",
        task_prompt=description,
        execution_profile=profile,
        title=title,
        description=description,
    )


def test_effective_profile_reaches_validator_read_only_boundary():
    corrected, _ = _effective_profile(
        "full_lifecycle", _TASK304_TITLE, _TASK304_DESCRIPTION
    )
    review, review_source = _effective_profile(
        "full_lifecycle", "Review the inventory implementation", "Report findings."
    )
    assert corrected == "full_lifecycle"
    assert (review, review_source) == (
        "review_only",
        EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC,
    )

    mutation_capable = _validate(corrected, _TASK304_TITLE, _TASK304_DESCRIPTION)
    assert "read_only_stage_mutation_steps" not in mutation_capable.details

    verification_only = _validate(
        review, "Review the inventory implementation", "Report findings."
    )
    assert verification_only.details["read_only_stage_mutation_steps"] == [1]


def _gate_project(db_session, tmp_path, current_title, current_description):
    project = Project(name="P42-V17-I Gate", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    prior = Task(
        project_id=project.id,
        title="Historical incomplete task",
        description="The prior work is still pending.",
        status=TaskStatus.PENDING,
        plan_position=1,
        task_subfolder="task-prior",
    )
    current = Task(
        project_id=project.id,
        title=current_title,
        description=current_description,
        status=TaskStatus.PENDING,
        execution_profile="full_lifecycle",
        plan_position=2,
        task_subfolder="task-current",
    )
    db_session.add_all([prior, current])
    db_session.commit()
    db_session.refresh(current)
    return project, current


def _state_path(root, **_kw):
    return root / ".agent" / "state_manager.json"


def test_task304_effective_profile_admitted_as_implementation_by_merge_gate(
    db_session, tmp_path
):
    project, current = _gate_project(
        db_session, tmp_path, _TASK304_TITLE, _TASK304_DESCRIPTION
    )
    profile, source = _effective_profile(
        current.execution_profile, current.title, current.description
    )
    assert (
        run_virtual_merge_gate(
            db_session, project, current, profile, _state_path, profile_source=source
        )
        is None
    )


def test_review_effective_profile_still_reaches_merge_gate_as_heuristic(
    db_session, tmp_path
):
    project, current = _gate_project(
        db_session,
        tmp_path,
        "Review the inventory implementation",
        "Report findings.",
    )
    profile, source = _effective_profile(
        current.execution_profile, current.title, current.description
    )
    reason = run_virtual_merge_gate(
        db_session, project, current, profile, _state_path, profile_source=source
    )
    assert reason is not None
    assert reason.admission_metadata["classification"] == "verification_style"
    assert reason.admission_metadata["classification_authority"] == (
        EXECUTION_PROFILE_SOURCE_REVIEW_HEURISTIC
    )
