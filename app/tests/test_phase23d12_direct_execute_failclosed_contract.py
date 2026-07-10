"""Phase 23D-12 -- Direct-Execute Fail-Closed Contract Fix.

Phase 23D-11 live certification found an S2 controlled-apply contract
violation: unlike `app/tasks/worker.py`'s Celery dispatch `finally` block
(fixed in Phase 23D-8/23D-10), the direct-execution endpoint
(`POST /tasks/{id}/execute`, `execute_task_with_runtime` in
`app/api/v1/endpoints/tasks.py`) had no exception handling around
`maybe_allocate_runtime_workspace(...)`. A genuine `TaskSandboxError` on
this path propagated straight to an unhandled 500 with no
`TaskExecutionChangeSet` row ever recorded for that execution --
`GET /tasks/{id}/change-set` then fell back to "latest change set for this
task_id", which, compounded by a hard `DELETE /tasks/{id}` reusing a freed
task id, surfaced a stale, unrelated, already-"promoted" change-set left
over from a different, already-cleaned-up task/project.

Two independent fixes, mirroring the Phase 23D-8/23D-10 pattern:

1. `execute_task_with_runtime`'s outer exception handler now records the
   fail-closed `runtime_not_allocated`/`unavailable` change-set state (same
   call worker.py already makes) when the failure is a `TaskSandboxError`
   and no sandbox was allocated -- never falling through to a stale read.
2. `ChangesetService.get_latest_task_change_set_for_task` now requires the
   resolved change-set to have been created at or after its Task's own
   `created_at`, closing the read-side leak for any already-orphaned rows
   (existing or future) without touching delete/cascade behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
    User,
)
from app.services.workspace.changeset_service import ChangesetService
from app.services.workspace.task_sandbox_allocator import TaskSandboxError


def _seed_project_task(db_session, tmp_path, *, name="Phase 23D-12"):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("SECRET=leak-me-not\n", encoding="utf-8")
    (project_root / "orchestrator.db").write_text("db-stand-in", encoding="utf-8")
    (project_root / "dump.rdb").write_text("redis-dump-stand-in", encoding="utf-8")

    user = User(
        email=f"{name.lower().replace(' ', '-')}@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    project = Project(name=name, workspace_path=str(project_root), user_id=user.id)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(user)
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Direct-execute allocation failure task",
        description="neutral prompt",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return user, project, task, project_root


class _FakeRuntime:
    async def create_session(self, task_description: str, context=None) -> str:
        return "agent:main:main"

    async def execute_task(self, prompt: str, timeout_seconds: int = 300, **_):
        return {"status": "completed", "output": "done"}


class _FakeRequest:
    async def json(self):
        return {"prompt": "neutral prompt", "timeout_seconds": 42}


@pytest.mark.asyncio
async def test_direct_execute_tasksandboxerror_records_runtime_not_allocated(
    db_session, monkeypatch, tmp_path
):
    from app.api.v1.endpoints import tasks as tasks_module
    from fastapi import HTTPException

    user, project, task, _ = _seed_project_task(db_session, tmp_path)

    monkeypatch.setattr(tasks_module.settings, "RUNTIME_WORKSPACE_ENABLED", True)
    monkeypatch.setattr(
        tasks_module,
        "create_agent_runtime",
        lambda db, session_id, task_id=None: _FakeRuntime(),
    )

    def _raise_sandbox_error(**kwargs):
        raise TaskSandboxError(
            "git worktree add failed: branch 'orchestrator/task-99' already exists"
        )

    monkeypatch.setattr(
        tasks_module, "maybe_allocate_runtime_workspace", _raise_sandbox_error
    )

    with pytest.raises(HTTPException) as exc_info:
        await tasks_module.execute_task_with_runtime(
            task.id, _FakeRequest(), db_session, user
        )
    assert exc_info.value.status_code == 500

    task_execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )
    change_set = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == task_execution.id)
        .one()
    )
    assert change_set.disposition == "unavailable"
    assert change_set.disposition_reason == "runtime_not_allocated"
    assert change_set.status == "runtime_not_allocated"
    assert change_set.snapshot_path is None
    assert change_set.target_path is None
    assert change_set.added_files == []
    assert change_set.modified_files == []
    assert change_set.deleted_files == []


@pytest.mark.asyncio
async def test_direct_execute_tasksandboxerror_does_not_scan_project_workspace(
    db_session, monkeypatch, tmp_path
):
    from app.api.v1.endpoints import tasks as tasks_module

    user, project, task, project_root = _seed_project_task(db_session, tmp_path)

    monkeypatch.setattr(tasks_module.settings, "RUNTIME_WORKSPACE_ENABLED", True)
    monkeypatch.setattr(
        tasks_module,
        "create_agent_runtime",
        lambda db, session_id, task_id=None: _FakeRuntime(),
    )
    monkeypatch.setattr(
        tasks_module,
        "maybe_allocate_runtime_workspace",
        lambda **kwargs: (_ for _ in ()).throw(TaskSandboxError("allocation failed")),
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await tasks_module.execute_task_with_runtime(
            task.id, _FakeRequest(), db_session, user
        )

    # The fail-closed recorder never reads/copies the Project Workspace.
    assert (project_root / ".env").read_text(encoding="utf-8") == "SECRET=leak-me-not\n"
    assert (project_root / "orchestrator.db").read_text(
        encoding="utf-8"
    ) == "db-stand-in"
    assert not (project_root / ".agent" / "change-sets").exists()


def test_get_latest_change_set_excludes_orphaned_row_after_task_id_reuse(
    db_session, tmp_path
):
    """Directly reproduces the Phase 23D-11 read-side leak: a
    TaskExecutionChangeSet row whose `created_at` predates its Task's own
    `created_at` can only exist because the task id was reused after the
    row's true originating task was hard-deleted. `get_latest_task_change_
    set_for_task` must never resolve such a row as the current task's
    latest change-set."""

    user, project, task, _ = _seed_project_task(
        db_session, tmp_path, name="Phase 23D-12 Orphan"
    )
    session = SessionModel(
        project_id=project.id,
        name="orphan session",
        status="stopped",
        is_active=False,
    )
    db_session.add(session)
    db_session.flush()
    execution = TaskExecution(session_id=session.id, task_id=task.id, attempt_number=1)
    db_session.add(execution)
    db_session.flush()

    orphaned = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        session_id=session.id,
        task_execution_id=execution.id,
        base_snapshot_key="stale-key",
        disposition="promoted",
        disposition_reason="burnin unrelated accept test",
        status="done",
        snapshot_exists=False,
    )
    db_session.add(orphaned)
    db_session.commit()
    db_session.refresh(orphaned)
    db_session.refresh(task)

    # Force the orphan's created_at to predate the (reused) task's own
    # created_at -- the exact invariant violation a reused task id produces.
    orphaned.created_at = task.created_at - timedelta(hours=1)
    db_session.add(orphaned)
    db_session.commit()

    service = ChangesetService(db_session)
    result = service.get_latest_task_change_set_for_task(task.id)

    assert result is None, (
        "orphaned pre-task-creation change-set row must not be surfaced as "
        "the current task's latest change-set"
    )


def test_get_latest_change_set_still_resolves_genuine_row(db_session, tmp_path):
    """Control case: a change-set genuinely created after its task exists
    is still resolved normally -- the orphan guard must not be overbroad."""

    user, project, task, _ = _seed_project_task(
        db_session, tmp_path, name="Phase 23D-12 Genuine"
    )
    session = SessionModel(
        project_id=project.id,
        name="genuine session",
        status="stopped",
        is_active=False,
    )
    db_session.add(session)
    db_session.flush()
    execution = TaskExecution(session_id=session.id, task_id=task.id, attempt_number=1)
    db_session.add(execution)
    db_session.flush()

    genuine = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        session_id=session.id,
        task_execution_id=execution.id,
        base_snapshot_key="genuine-key",
        disposition="unavailable",
        disposition_reason="runtime_not_allocated",
        status="runtime_not_allocated",
        snapshot_exists=False,
    )
    db_session.add(genuine)
    db_session.commit()

    service = ChangesetService(db_session)
    result = service.get_latest_task_change_set_for_task(task.id)

    assert result is not None
    assert result["task_execution_id"] == execution.id
    assert result["change_set"]["disposition"] == "unavailable"
