from __future__ import annotations

import logging

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.types import OrchestrationRunContext
from app.services.planning.planner_service import PlannerService
from app.services.session.session_runtime_service import build_task_execution_prompt


class _FakeSelfTask:
    max_retries = 3

    class request:
        retries = 3

    def retry(self, exc):
        raise AssertionError(
            "retry should not be called in terminal auto-recovery path"
        )


def test_planner_marks_architecture_inspection_as_review_only():
    parsed = PlannerService.parse_markdown(
        """
## Task List
- [ ] TASK_START: Inspect current project architecture | Review the real files, tests, fixtures, and extension points before implementation.
"""
    )

    assert parsed
    assert parsed[0].execution_profile == "review_only"


def test_build_task_execution_prompt_includes_failure_recovery_context():
    task = Task(
        title="Inspect current project architecture",
        description="Review the current codebase and identify extension points.",
        workspace_status="changes_requested",
        error_message="Previous run guessed the wrong file layout and missed the existing tests.",
    )

    prompt = build_task_execution_prompt(task)

    assert "Recovery instructions:" in prompt
    assert "inspect the real current workspace" in prompt
    assert "Previous failure details:" in prompt


def test_handle_task_failure_queues_one_automatic_recovery_for_failed_ordered_task(
    db_session,
):
    project = Project(name="Recovery Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Recovery Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Inspect current project architecture",
        description="Inspect the existing workspace and identify the real structure.",
        status=TaskStatus.RUNNING,
        execution_profile="review_only",
        plan_position=1,
        workspace_status="isolated",
        task_subfolder="task-inspect-current-project-architecture",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    queued: list[int] = []

    def fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        queued.append(task_id)
        return {"task_id": task_id}

    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="review_only",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=type(
            "StubErrorHandler",
            (),
            {"should_retry": staticmethod(lambda _exc, _context: False)},
        )(),
        restore_workspace_snapshot_if_needed=None,
    )

    handle_task_failure(
        self_task=_FakeSelfTask(),
        ctx=ctx,
        exc=RuntimeError("Inspection guessed the wrong workspace structure"),
        get_latest_session_task_link_fn=lambda *_args, **_kwargs: None,
        queue_task_for_session_fn=fake_queue_task_for_session,
        write_project_state_snapshot_fn=lambda *_args, **_kwargs: None,
        save_orchestration_checkpoint_fn=lambda *_args, **_kwargs: None,
        record_live_log_fn=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(task)
    db_session.refresh(session)

    assert queued == [task.id]
    assert task.status == TaskStatus.PENDING
    assert task.workspace_status == "changes_requested"
    assert "Automatic recovery requested" in (task.error_message or "")
    assert session.status == "running"
    assert session.is_active is True
