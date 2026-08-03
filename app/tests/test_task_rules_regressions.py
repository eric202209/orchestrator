import json

from app.models import Plan, Project, Task, TaskStatus
from app.services.orchestration.task_rules import (
    classify_task_intent,
    get_task_report_path,
    get_workflow_profile,
    is_verification_style_task,
    run_virtual_merge_gate,
    should_force_review_execution_profile,
)


def test_force_review_profile_for_true_inspection_task():
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Inspect current project architecture and inventory extension points.",
            "Inspect current project architecture",
            "Review the real files before implementation.",
        )
        is True
    )


def test_do_not_force_review_profile_for_build_task_with_clean_architecture():
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Set up frontend (React or Vite) and backend (Node.js or FastAPI) with clean architecture.",
            "SkillSync AI Hiring Platform",
            "Set up frontend (React or Vite) and backend (Node.js or FastAPI) with clean architecture.",
        )
        is False
    )


# --- Recovery-requeue false-positive regression tests ---
# Before the fix, worker.py passed the full built prompt (including recovery
# boilerplate that says "inspect") as task_prompt. After the fix it passes
# task.description. These tests document both the pre-fix risk and the correct
# post-fix behaviour, exercising the function with the two different inputs.

_RECOVERY_PROMPT = (
    "Bootstrap parse_amount parser with EMPTY/FORMAT/OVERFLOW codes.\n\n"
    "Recovery instructions:\n"
    "- The previous execution did not complete successfully.\n"
    "- First inspect the real current workspace, tests, fixtures, and configs"
    " before proposing new structure.\n"
    "- Diagnose and fix the underlying mistake or bug instead of repeating the"
    " same plan.\n"
    "- Previous failure details: Plan validation failed after repair: Plan uses"
    " weak verification for implementation-heavy work (steps: [1])\n\n"
    "Automatic recovery requested: inspect the real workspace and repair the bug"
    " instead of repeating the previous assumptions."
)

_IMPL_TITLE = "Bootstrap parse_amount parser"
_IMPL_DESC = (
    "Create a parse_amount(text: str) -> dict function that returns "
    '{"ok": True, "value": int} on success and '
    '{"ok": False, "code": str} where code is one of EMPTY, FORMAT, OVERFLOW. '
    "Include pytest tests."
)


def test_recovery_boilerplate_triggers_false_positive_when_passed_as_full_prompt():
    # Documents the pre-fix risk: passing the full recovery prompt as task_prompt
    # returns True because "inspect" appears in the recovery instructions.
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            _RECOVERY_PROMPT,
            _IMPL_TITLE,
            _IMPL_DESC,
        )
        is True
    )


def test_recovery_boilerplate_does_not_force_review_when_description_used_as_prompt():
    # Post-fix calling convention: task_prompt = task.description (not full built prompt).
    # No review markers in the original description → implementation profile preserved.
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            _IMPL_DESC,
            _IMPL_TITLE,
            _IMPL_DESC,
        )
        is False
    )


def test_genuine_inspection_task_still_forces_review_with_description_as_prompt():
    # Genuine inspection tasks whose description/title contain "inspect" must
    # still be detected correctly under the new calling convention.
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Inspect current codebase and produce an inventory of extension points.",
            "Inspect and analyze current project",
            "Inspect current codebase and produce an inventory of extension points.",
        )
        is True
    )


def test_genuine_review_task_still_forces_review_with_description_as_prompt():
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Analyze the current project architecture and report findings.",
            "Current project architecture analysis",
            "Analyze the current project architecture and report findings.",
        )
        is True
    )


def test_recovery_prompt_inspect_does_not_fire_for_calclib_parser_task():
    # Exact scenario from the WM pilot T1 instability: task 925 (on-r4c).
    # title="Bootstrap parse_amount parser", description=implementation spec,
    # task_prompt=task.description (post-fix convention) — must not be review_only.
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            _IMPL_DESC,
            _IMPL_TITLE,
            _IMPL_DESC,
        )
        is False
    )


def test_fullstack_scaffold_task_resolves_workflow_profile():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "SkillSync AI Hiring Platform",
            "Set up frontend (React or Vite) and backend (FastAPI) with clean architecture.",
        )
        == "fullstack_scaffold"
    )


def test_backend_api_task_with_negated_frontend_resolves_backend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Tiny FastAPI notes API",
            "Build a FastAPI notes API. Do not create a frontend or package manager setup.",
        )
        == "backend_only"
    )


def test_static_frontend_task_with_negated_backend_resolves_frontend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Static productivity timer landing page",
            "Build a static frontend landing page. Do not create a backend.",
        )
        == "frontend_only"
    )


def test_plain_static_site_with_preview_server_exclusion_stays_frontend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Step 1: create base status site files",
            (
                "Create the base plain static site under public/status-site. "
                "Required files are public/status-site/index.html, "
                "public/status-site/css/style.css, and "
                "public/status-site/images/status-badge.svg. "
                "No React, Vite, npm, or preview server."
            ),
        )
        == "frontend_only"
    )


def test_plain_static_site_with_api_label_does_not_resolve_backend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Step 2: add incident summary section",
            (
                "Update public/status-site/index.html and "
                "public/status-site/css/style.css with three status cards: "
                "API, Queue, and Knowledge."
            ),
        )
        == "frontend_only"
    )


def test_virtual_merge_gate_ignores_stale_unsynced_state_for_current_task_retry(
    db_session, tmp_path
):
    project_root = tmp_path / "legacy-retry"
    state_dir = project_root / ".agent"
    state_dir.mkdir(parents=True)

    project = Project(name="Legacy Retry", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    current_task = Task(
        project_id=project.id,
        title="Task 5: Verify rollback safety",
        description="Verify the current project state.",
        status=TaskStatus.FAILED,
        plan_position=1,
        task_subfolder="task-verify",
    )
    db_session.add(current_task)
    db_session.commit()
    db_session.refresh(current_task)

    (state_dir / "state_manager.json").write_text(
        json.dumps(
            {
                "status": "unsynced",
                "failed_or_cancelled_task_ids": [current_task.id],
                "inconsistent_completed_tasks": [],
            }
        ),
        encoding="utf-8",
    )

    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            current_task,
            "full_lifecycle",
            lambda root: root / ".agent" / "state_manager.json",
        )
        is None
    )


def test_virtual_merge_gate_blocks_unsynced_prior_task(db_session, tmp_path):
    project_root = tmp_path / "prior-unsynced"
    state_dir = project_root / ".agent"
    state_dir.mkdir(parents=True)

    project = Project(name="Prior Unsynced", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        title="Task 1: Build page",
        description="Build the page.",
        status=TaskStatus.DONE,
        plan_position=1,
        task_subfolder="task-build",
    )
    current_task = Task(
        project_id=project.id,
        title="Task 2: Verify page",
        description="Verify the page.",
        status=TaskStatus.PENDING,
        plan_position=2,
        task_subfolder="task-verify",
    )
    db_session.add_all([prior_task, current_task])
    db_session.commit()
    db_session.refresh(prior_task)
    db_session.refresh(current_task)

    report_path = get_task_report_path(project_root, prior_task)
    report_path.parent.mkdir(parents=True)
    report_path.write_text("done\n", encoding="utf-8")
    baseline_dir = project_root / ".agent" / "project_baseline"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "index.html").write_text("<main></main>\n", encoding="utf-8")
    (state_dir / "state_manager.json").write_text(
        json.dumps(
            {
                "status": "unsynced",
                "failed_or_cancelled_task_ids": [prior_task.id],
                "inconsistent_completed_tasks": [],
            }
        ),
        encoding="utf-8",
    )

    reason = run_virtual_merge_gate(
        db_session,
        project,
        current_task,
        "full_lifecycle",
        lambda root: root / ".agent" / "state_manager.json",
    )

    assert reason is not None
    assert "prior failed/cancelled tasks" in reason


def test_virtual_merge_gate_scopes_prior_tasks_to_same_plan(db_session, tmp_path):
    project_root = tmp_path / "plan-scoped-gate"
    project_root.mkdir(parents=True)

    project = Project(name="Plan Scoped Gate", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    unrelated_failed_task = Task(
        project_id=project.id,
        plan_id=1,
        title="Original failed task",
        description="Original plan failed.",
        status=TaskStatus.FAILED,
        plan_position=1,
        task_subfolder="task-original",
    )
    recovery_validation = Task(
        project_id=project.id,
        plan_id=2,
        title="Validate recovery path",
        description="Run focused recovery validation.",
        status=TaskStatus.PENDING,
        execution_profile="test_only",
        workflow_stage="validate",
        plan_position=4,
        task_subfolder="task-recovery-validate",
    )
    db_session.add_all([unrelated_failed_task, recovery_validation])
    db_session.commit()
    db_session.refresh(recovery_validation)

    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            recovery_validation,
            "test_only",
            lambda root: root / ".agent" / "state_manager.json",
        )
        is None
    )


def test_virtual_merge_gate_accepts_legacy_root_task_report(db_session, tmp_path):
    project_root = tmp_path / "legacy-report"
    project_root.mkdir(parents=True)

    project = Project(name="Legacy Report", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        title="Task 1: Build page",
        description="Build the page.",
        status=TaskStatus.DONE,
        plan_position=1,
        task_subfolder="task-build",
    )
    current_task = Task(
        project_id=project.id,
        title="Task 2: Verify page",
        description="Verify the page.",
        status=TaskStatus.PENDING,
        plan_position=2,
        task_subfolder="task-verify",
    )
    db_session.add_all([prior_task, current_task])
    db_session.commit()
    db_session.refresh(prior_task)
    db_session.refresh(current_task)

    (project_root / f"task_report_{prior_task.id}.md").write_text(
        "done\n", encoding="utf-8"
    )
    baseline_dir = project_root / ".agent" / "project_baseline"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "index.html").write_text("<main></main>\n", encoding="utf-8")

    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            current_task,
            "full_lifecycle",
            lambda root: root / ".agent" / "state_manager.json",
        )
        is None
    )


_E2_IMPLEMENTATION_TITLE = (
    "Add utc_now() helper and migrate one naive datetime consumer"
)
_E2_IMPLEMENTATION_DESCRIPTION = (
    "Add app/time_utils.py with utc_now() returning an aware UTC datetime. "
    "Migrate app/services/workspace/context_service.py and add the regression "
    "coverage in app/tests/test_utc_now_helper.py. Run pytest for the acceptance "
    "tests and preserve the existing exported_at behavior."
)


def test_e2_implementation_task_with_test_contract_is_not_verification_style():
    assert (
        is_verification_style_task(
            "full_lifecycle",
            _E2_IMPLEMENTATION_TITLE,
            _E2_IMPLEMENTATION_DESCRIPTION,
        )
        is False
    )


def test_e2_implementation_task_is_not_blocked_by_incomplete_prior_tasks(
    db_session, tmp_path
):
    project = Project(name="E2 Admission", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        title="Historical incomplete task",
        description="The prior work is still pending.",
        status=TaskStatus.PENDING,
        plan_position=1,
        task_subfolder="task-prior",
    )
    current_task = Task(
        project_id=project.id,
        title=_E2_IMPLEMENTATION_TITLE,
        description=_E2_IMPLEMENTATION_DESCRIPTION,
        status=TaskStatus.PENDING,
        plan_position=2,
        task_subfolder="task-e2",
    )
    db_session.add_all([prior_task, current_task])
    db_session.commit()
    db_session.refresh(current_task)

    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            current_task,
            "full_lifecycle",
            lambda root: root / ".agent" / "state_manager.json",
        )
        is None
    )


def test_explicit_verification_intent_remains_verification_style():
    verification_titles = (
        "Verify the recovery lifecycle without modifying source",
        "Audit the deployment identity contract",
        "Review the generated migration and report inconsistencies",
        "Run integration validation and produce evidence only",
    )

    for title in verification_titles:
        assert is_verification_style_task("full_lifecycle", title, "") is True


def test_explicit_verification_profiles_remain_verification_style():
    assert is_verification_style_task("test_only", "Run the checks", "") is True
    assert is_verification_style_task("review_only", "Inspect the result", "") is True


def test_e2_classification_exposes_title_authority_and_reason():
    classification = classify_task_intent(
        "full_lifecycle",
        _E2_IMPLEMENTATION_TITLE,
        _E2_IMPLEMENTATION_DESCRIPTION,
    )

    assert classification.as_dict() == {
        "classification": "implementation_style",
        "classification_reason": "implementation_intent_in_title",
        "classification_authority": "task_title",
    }


def test_genuine_verification_hold_exposes_classification_and_blocking_scope(
    db_session, tmp_path
):
    project = Project(
        name="Verification Hold Diagnostics", workspace_path=str(tmp_path)
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        title="Implementation predecessor",
        description="The implementation is pending.",
        status=TaskStatus.PENDING,
        plan_position=1,
        task_subfolder="task-prior",
    )
    current_task = Task(
        project_id=project.id,
        title="Verify the recovery lifecycle without modifying source",
        description="Produce evidence only.",
        status=TaskStatus.PENDING,
        plan_position=2,
        task_subfolder="task-verification",
    )
    db_session.add_all([prior_task, current_task])
    db_session.commit()
    db_session.refresh(current_task)

    reason = run_virtual_merge_gate(
        db_session,
        project,
        current_task,
        "full_lifecycle",
        lambda root: root / ".agent" / "state_manager.json",
    )

    assert reason is not None
    assert reason.admission_metadata == {
        "classification": "verification_style",
        "classification_reason": "explicit_verification_intent_in_title",
        "classification_authority": "task_title",
        "blocking_scope": "ordered_project_history",
        "blocking_task_ids": [prior_task.id],
    }
    assert "classification_reason=explicit_verification_intent_in_title" in reason
    assert "classification_authority=task_title" in reason
    assert f"blocking_task_ids={prior_task.id}" in reason


def test_explicit_plan_predecessor_still_blocks_implementation_task(
    db_session, tmp_path
):
    project = Project(name="Explicit Plan Ordering", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Implementation plan",
        source_brain="local",
        requirement="Keep explicit predecessor ordering.",
        markdown="1. predecessor\n2. implementation",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must finish first.",
        status=TaskStatus.PENDING,
        plan_position=1,
        task_subfolder="task-plan-prior",
    )
    current_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Add the implementation",
        description="Add the feature and include regression coverage.",
        status=TaskStatus.PENDING,
        plan_position=2,
        task_subfolder="task-plan-current",
    )
    db_session.add_all([predecessor, current_task])
    db_session.commit()
    db_session.refresh(current_task)

    reason = run_virtual_merge_gate(
        db_session,
        project,
        current_task,
        "full_lifecycle",
        lambda root: root / ".agent" / "state_manager.json",
    )

    assert reason is not None
    assert "earlier ordered tasks are incomplete" in reason
    assert reason.admission_metadata["blocking_scope"] == "explicit_plan_predecessors"
    assert reason.admission_metadata["blocking_task_ids"] == [predecessor.id]


def test_implementation_intent_wins_over_test_contract_language():
    implementation_tasks = (
        ("Add utc_now() and add tests", "Use pytest for regression coverage."),
        ("Fix the endpoint and run pytest", "The acceptance tests must pass."),
        ("Update app/tests/test_example.py", "Keep the existing behavior."),
        (
            "Implement validation with regression coverage",
            "Add the required test cases and run them.",
        ),
        (
            "Update app/services/integration_test.py",
            "The path is part of the implementation contract.",
        ),
        (
            "Add quadratic interpolation support",
            "The unrelated word must not be treated as a QA task.",
        ),
    )

    for title, description in implementation_tasks:
        assert is_verification_style_task("full_lifecycle", title, description) is False
