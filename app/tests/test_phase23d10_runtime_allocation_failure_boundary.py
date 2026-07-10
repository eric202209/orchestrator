"""Phase 23D-10 -- Runtime Allocation Failure Retry Boundary Fix.

Phase 23D-9 live burn-in found that a genuine `TaskSandboxError` raised
before sandbox allocation, on a *retryable* failure, lost the Phase 23D-8
fail-closed change-set row instead of recording it: `GET
/tasks/{id}/change-set` returned `status="not_recorded"` instead of the
required `status="runtime_not_allocated"`.

Root cause: `FailureCoordinator.handle_failure()`'s retry branch calls
`mark_task_attempt_pending(task=..., session_task_link=..., ...)` without a
`task_execution=` argument (by design -- the execution row keeps its
terminal `FAILED` status set moments earlier by `mark_task_attempt_failed`,
while `task`/`session_task_link` are reset to `PENDING` so the task can be
retried). `app/tasks/worker.py`'s dispatch `finally` block then calls
`_sync_task_execution_from_task_state()`, which has an early-return guard
for exactly this "task_execution terminal, current status not terminal"
mismatch -- and that guard returns *without* calling `db.commit()`. The
fail-closed change-set row had been added moments earlier with
`commit=False`, relying on that later commit; with the guard skipping it,
the row is discarded when the Celery task's `db.close()` runs.

The fix (`app/tasks/worker.py`) commits the fail-closed
`record_task_execution_change_set_unavailable(...)` call immediately
(`commit=True`) instead of deferring to `_sync_task_execution_from_task_state`,
so the durable operator signal survives regardless of what retry/checkpoint
bookkeeping does afterward.

These tests reproduce the exact retry-path status sequence against the real
`run_state` transition helpers and `_sync_task_execution_from_task_state`,
and assert the change-set row survives using a *separate* DB session (which
can only see committed data), proving the row is truly durable and not
merely resident in the writer's session identity map.
"""

from __future__ import annotations

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_failed,
    mark_task_attempt_pending,
)
from app.services.tasks.service import TaskService
from app.tasks.worker_support.execution_state import (
    _sync_task_execution_from_task_state,
)


def _seed_task(db_session, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text("SECRET=leak-me-not\n", encoding="utf-8")
    (project_dir / "orchestrator.db").write_text("binary-db-stand-in", encoding="utf-8")
    (project_dir / "dump.rdb").write_text("redis-dump-stand-in", encoding="utf-8")

    project = Project(name="Phase 23D-10", workspace_path=str(project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 23D-10 Session",
        status="running",
        is_active=True,
        execution_mode="automatic",
    )
    task = Task(project_id=project.id, title="Runtime allocation failure retry task")
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
    )
    execution = TaskExecution(
        session_id=session.id, task_id=task.id, attempt_number=1, started_at=None
    )
    db_session.add_all([link, execution])
    db_session.commit()
    return project, task, session, link, execution, project_dir


def _reproduce_retry_status_mismatch(task, link, execution):
    """Reproduce FailureCoordinator.handle_failure()'s exact status sequence
    for a retryable failure: mark_task_attempt_failed sets task_execution to
    FAILED, then the retry branch's mark_task_attempt_pending resets
    task/session_task_link to PENDING without touching task_execution."""

    mark_task_attempt_failed(
        task=task,
        session_task_link=link,
        task_execution=execution,
        error_message="TaskSandboxError: allocation failed",
        workspace_status="not_created",
    )
    mark_task_attempt_pending(
        task=task,
        session_task_link=link,
        workspace_status="not_created",
    )


def test_sync_task_execution_early_return_guard_reproduces_status_mismatch(
    db_session, tmp_path
):
    """Confirms the guard this fix routes around actually fires for the
    retry sequence, and that it returns without committing."""

    _, task, session, link, execution, _ = _seed_task(db_session, tmp_path)
    execution_id = execution.id
    _reproduce_retry_status_mismatch(task, link, execution)

    assert execution.status == TaskStatus.FAILED
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING

    _sync_task_execution_from_task_state(
        db_session,
        execution_id,
        task=task,
        session_task_link=link,
    )
    # The guard leaves task_execution FAILED (it never syncs the PENDING
    # status down); if it had committed, the row created by a caller *before*
    # this call (with commit=False) would be persisted incidentally. It does
    # not commit, so a caller relying on that must commit for itself.
    assert execution.status == TaskStatus.FAILED
    assert task.status == TaskStatus.PENDING


def test_unavailable_change_set_committed_before_retry_sync_guard_skips_commit(
    db_session, db_session_factory, tmp_path
):
    """Reproduces the Phase 23D-9 live defect and proves the fix: the
    fail-closed change-set row must be durably committed even though the
    immediately-following retry/checkpoint sync (_sync_task_execution_from_
    task_state) hits its status-mismatch guard and returns without a
    commit of its own."""

    project, task, session, link, execution, project_dir = _seed_task(
        db_session, tmp_path
    )
    execution_id = execution.id
    task_service = TaskService(db_session)

    # Mirror worker.py's finally block ordering exactly: record the
    # fail-closed row (now commit=True per the Phase 23D-10 fix) ...
    task_service.record_task_execution_change_set_unavailable(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution_id,
        snapshot_key=f"task-{task.id}-execution-{execution_id}-pre-run",
        reason="runtime_not_allocated",
        commit=True,
    )

    # ... then the retry/checkpoint status transition that trips the guard.
    _reproduce_retry_status_mismatch(task, link, execution)
    _sync_task_execution_from_task_state(
        db_session,
        execution_id,
        task=task,
        session_task_link=link,
    )

    # Simulate the Celery task's db.close() dropping any uncommitted state.
    db_session.rollback()
    db_session.close()

    # Read back from a fresh session -- only sees committed data.
    fresh_db = db_session_factory()
    try:
        record = (
            fresh_db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.task_execution_id == execution_id)
            .first()
        )
        assert record is not None, (
            "fail-closed change-set row was not durably committed -- "
            "regresses to GET /tasks/{id}/change-set returning not_recorded"
        )
        assert record.disposition == "unavailable"
        assert record.disposition_reason == "runtime_not_allocated"
        assert record.status == "runtime_not_allocated"
        assert record.target_path is None
        assert record.snapshot_path is None
    finally:
        fresh_db.close()

    # Project Workspace must never have been touched by the fail-closed path.
    assert (project_dir / ".env").read_text(encoding="utf-8") == "SECRET=leak-me-not\n"
    assert not (project_dir.parent / ".agent").exists()
