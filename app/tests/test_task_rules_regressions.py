import json

from app.models import Project, Task, TaskStatus
from app.services.orchestration.task_rules import (
    get_workflow_profile,
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


def test_virtual_merge_gate_ignores_stale_unsynced_state_for_current_task_retry(
    db_session, tmp_path
):
    project_root = tmp_path / "legacy-retry"
    state_dir = project_root / ".openclaw"
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
            lambda root: root / ".openclaw" / "state_manager.json",
        )
        is None
    )


def test_virtual_merge_gate_blocks_unsynced_prior_task(db_session, tmp_path):
    project_root = tmp_path / "prior-unsynced"
    state_dir = project_root / ".openclaw"
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

    (project_root / f"task_report_{prior_task.id}.md").write_text(
        "done\n", encoding="utf-8"
    )
    baseline_dir = project_root / ".openclaw" / "project_baseline"
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
        lambda root: root / ".openclaw" / "state_manager.json",
    )

    assert reason is not None
    assert "prior failed/cancelled tasks" in reason
