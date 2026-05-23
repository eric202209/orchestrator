from app.models import Project, Task, TaskStatus
from app.services.orchestration.execution.runtime import build_project_state_snapshot


def test_project_state_snapshot_scopes_inconsistent_completed_tasks_by_plan(
    db_session,
):
    project = Project(name="State Plan Scope", workspace_path="/tmp/state_plan_scope")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    original_failed = Task(
        project_id=project.id,
        plan_id=1,
        title="Original failed task",
        status=TaskStatus.FAILED,
        plan_position=1,
    )
    recovery_diagnose = Task(
        project_id=project.id,
        plan_id=2,
        title="Recovery diagnosis",
        status=TaskStatus.DONE,
        plan_position=1,
    )
    recovery_plan = Task(
        project_id=project.id,
        plan_id=2,
        title="Recovery plan",
        status=TaskStatus.DONE,
        plan_position=2,
    )
    recovery_validate = Task(
        project_id=project.id,
        plan_id=2,
        title="Recovery validation",
        status=TaskStatus.PENDING,
        plan_position=3,
    )
    db_session.add_all(
        [original_failed, recovery_diagnose, recovery_plan, recovery_validate]
    )
    db_session.commit()

    snapshot = build_project_state_snapshot(
        db_session, project, recovery_validate, session_id=1
    )

    assert snapshot["status"] == "unsynced"
    assert snapshot["failed_or_cancelled_task_ids"] == [original_failed.id]
    assert snapshot["inconsistent_completed_tasks"] == []
