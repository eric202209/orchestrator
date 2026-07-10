"""Phase 23D-14 -- Direct-Execute Success Change-Set Capture.

Phase 23D-13 certification found an S2 controlled-apply contract violation:
`POST /tasks/{id}/execute` (`execute_task_with_runtime` in
`app/api/v1/endpoints/tasks.py`) allocated and used a Runtime Workspace
sandbox correctly, but on a *successful* dispatch never persisted a
`TaskExecutionChangeSet` -- the `finally` block disposed the sandbox
unconditionally with no capture step, silently and irrecoverably discarding
the work product. `task.workspace_status` stayed `"not_created"`, so the
task never appeared under `GET /tasks?needs_review=true` and accept/reject
had nothing to operate on.

The fix mirrors `app/tasks/worker.py`'s success-path behavior:

1. A pre-run snapshot is captured inside the sandbox after allocation
   (`snapshot_workspace_before_run`, same call worker.py makes), so the
   post-run diff covers only what the execution actually changed.
2. The endpoint's inner `finally` persists the change-set
   (`persist_task_execution_change_set`, targeting the sandbox) *before*
   releasing the binding and disposing the sandbox.
3. The success branch sets `workspace_status="ready"` with the same
   awaiting-review promotion note `finalize_success()` writes (Phase
   23D-8), leaving promotion exclusively to `POST /tasks/{id}/accept`.
"""

from __future__ import annotations

import json

import pytest

from app.models import (
    Project,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
    User,
)
from app.schemas import TaskPromotionRequest
from app.services.workspace.task_sandbox_allocator import TaskSandbox


def _seed_project_task(db_session, tmp_path, *, name="Phase 23D-14"):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "existing.txt").write_text("baseline content\n", encoding="utf-8")
    (project_root / ".env").write_text("SECRET=leak-me-not\n", encoding="utf-8")

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
        title="Direct-execute success capture task",
        description="neutral prompt",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return user, project, task, project_root


class _SandboxWritingRuntime:
    """Fake runtime that writes a marker file into the bound sandbox cwd,
    simulating real agent output inside the Runtime Workspace."""

    def __init__(self):
        self.execution_cwd_override = None

    async def create_session(self, task_description: str, context=None) -> str:
        return "agent:main:main"

    async def execute_task(self, prompt: str, timeout_seconds: int = 300, **_):
        assert self.execution_cwd_override, "sandbox cwd must be bound before execution"
        from pathlib import Path

        (Path(self.execution_cwd_override) / "marker.txt").write_text(
            "hello-from-runtime-workspace\n", encoding="utf-8"
        )
        return {"status": "completed", "output": "done"}


class _FakeRequest:
    async def json(self):
        return {"prompt": "neutral prompt", "timeout_seconds": 42}


@pytest.fixture
def direct_execute_env(db_session, monkeypatch, tmp_path):
    """Wire execute_task_with_runtime to a real on-disk fake sandbox and
    record disposal ordering."""
    from app.api.v1.endpoints import tasks as tasks_module

    user, project, task, project_root = _seed_project_task(db_session, tmp_path)

    monkeypatch.setattr(tasks_module.settings, "RUNTIME_WORKSPACE_ENABLED", True)
    runtime = _SandboxWritingRuntime()
    monkeypatch.setattr(
        tasks_module,
        "create_agent_runtime",
        lambda db, session_id, task_id=None: runtime,
    )

    sandbox_root = tmp_path / "runtime" / "sandbox"
    state = {"sandbox": None, "dispose_calls": []}

    def _fake_allocate(**kwargs):
        # Hydrate the sandbox from the project baseline the way a git
        # worktree allocation would, minus excluded names.
        sandbox_root.mkdir(parents=True)
        (sandbox_root / "existing.txt").write_text(
            (project_root / "existing.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        sandbox = TaskSandbox(
            path=sandbox_root,
            project_id=kwargs["project_id"],
            task_execution_id=kwargs["task_execution_id"],
            executor=kwargs["executor"],
            is_git=False,
        )
        sandbox.metadata_path.write_text(
            json.dumps({"base_commit": None}), encoding="utf-8"
        )
        state["sandbox"] = sandbox
        return sandbox

    def _fake_dispose(sandbox, *, project_root=None, logger_obj=None):
        # Record whether the change-set row was already durable at the
        # moment of disposal (capture-before-dispose contract).
        row_count = (
            db_session.query(TaskExecutionChangeSet)
            .filter(
                TaskExecutionChangeSet.task_execution_id
                == (sandbox.task_execution_id if sandbox else None)
            )
            .count()
        )
        state["dispose_calls"].append(
            {"sandbox": sandbox, "change_set_rows_at_dispose": row_count}
        )
        if sandbox is not None and sandbox.path.exists():
            import shutil

            shutil.rmtree(sandbox.path)
        return sandbox is not None

    monkeypatch.setattr(
        tasks_module, "maybe_allocate_runtime_workspace", _fake_allocate
    )
    monkeypatch.setattr(tasks_module, "dispose_runtime_workspace_safely", _fake_dispose)

    return {
        "tasks_module": tasks_module,
        "user": user,
        "project": project,
        "task": task,
        "project_root": project_root,
        "state": state,
        "db": db_session,
    }


async def _run_direct_execute(env):
    result = await env["tasks_module"].execute_task_with_runtime(
        env["task"].id, _FakeRequest(), env["db"], env["user"]
    )
    env["db"].refresh(env["task"])
    task_execution = (
        env["db"]
        .query(TaskExecution)
        .filter(TaskExecution.task_id == env["task"].id)
        .one()
    )
    return result, task_execution


@pytest.mark.asyncio
async def test_direct_execute_success_persists_change_set(direct_execute_env):
    result, task_execution = await _run_direct_execute(direct_execute_env)
    assert result["status"] == "completed"

    change_set = (
        direct_execute_env["db"]
        .query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == task_execution.id)
        .one()
    )
    assert change_set.disposition == "captured"
    assert change_set.added_files == ["marker.txt"]
    assert change_set.modified_files == []
    assert change_set.deleted_files == []

    # The captured artifact must survive sandbox disposal, durably, under
    # the project's .agent/change-sets tree.
    artifact = (
        direct_execute_env["project_root"]
        / ".agent"
        / "change-sets"
        / str(task_execution.id)
        / "files"
        / "marker.txt"
    )
    assert artifact.read_text(encoding="utf-8") == "hello-from-runtime-workspace\n"
    assert not direct_execute_env["state"]["sandbox"].path.exists()


@pytest.mark.asyncio
async def test_direct_execute_success_sets_workspace_ready(direct_execute_env):
    await _run_direct_execute(direct_execute_env)
    task = direct_execute_env["task"]
    assert task.status == TaskStatus.DONE
    assert task.workspace_status == "ready"
    assert task.promoted_at is None
    assert "awaiting operator review" in (task.promotion_note or "")


@pytest.mark.asyncio
async def test_direct_execute_success_visible_under_needs_review(direct_execute_env):
    await _run_direct_execute(direct_execute_env)
    from app.api.v1.endpoints.tasks import _apply_task_filters

    db = direct_execute_env["db"]
    tasks = _apply_task_filters(
        db.query(Task),
        status=None,
        workspace_status=None,
        needs_review=True,
        project_id=direct_execute_env["project"].id,
        search=None,
        db=db,
        current_user=direct_execute_env["user"],
    ).all()
    assert [t.id for t in tasks] == [direct_execute_env["task"].id]


@pytest.mark.asyncio
async def test_direct_execute_change_set_endpoint_returns_capture(direct_execute_env):
    _, task_execution = await _run_direct_execute(direct_execute_env)
    from app.services.tasks.service import TaskService

    latest = TaskService(direct_execute_env["db"]).get_latest_task_change_set_for_task(
        direct_execute_env["task"].id
    )
    assert latest is not None
    assert latest["task_execution_id"] == task_execution.id
    assert latest["change_set"]["disposition"] == "captured"
    assert latest["change_set"]["added_files"] == ["marker.txt"]


@pytest.mark.asyncio
async def test_sandbox_disposed_only_after_capture(direct_execute_env):
    await _run_direct_execute(direct_execute_env)
    dispose_calls = direct_execute_env["state"]["dispose_calls"]
    assert len(dispose_calls) == 1
    assert dispose_calls[0]["sandbox"] is direct_execute_env["state"]["sandbox"]
    assert dispose_calls[0]["change_set_rows_at_dispose"] == 1


@pytest.mark.asyncio
async def test_project_workspace_untouched_before_accept(direct_execute_env):
    await _run_direct_execute(direct_execute_env)
    project_root = direct_execute_env["project_root"]
    assert not (project_root / "marker.txt").exists()
    assert (project_root / "existing.txt").read_text(
        encoding="utf-8"
    ) == "baseline content\n"
    assert (project_root / ".env").read_text(encoding="utf-8") == "SECRET=leak-me-not\n"


@pytest.mark.asyncio
async def test_accept_promotes_captured_artifact(direct_execute_env):
    _, task_execution = await _run_direct_execute(direct_execute_env)
    from app.api.v1.endpoints.tasks import accept_task_workspace

    accept_task_workspace(
        direct_execute_env["task"].id,
        TaskPromotionRequest(task_execution_id=task_execution.id),
        direct_execute_env["db"],
        direct_execute_env["user"],
    )
    direct_execute_env["db"].refresh(direct_execute_env["task"])
    task = direct_execute_env["task"]
    assert task.workspace_status == "promoted"
    assert task.promoted_at is not None
    promoted = direct_execute_env["project_root"] / "marker.txt"
    assert promoted.read_text(encoding="utf-8") == "hello-from-runtime-workspace\n"

    change_set = (
        direct_execute_env["db"]
        .query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == task_execution.id)
        .one()
    )
    assert change_set.disposition == "promoted"


@pytest.mark.asyncio
async def test_reject_does_not_promote(direct_execute_env):
    _, task_execution = await _run_direct_execute(direct_execute_env)
    from app.api.v1.endpoints.tasks import (
        TaskChangeSetRejectRequest,
        reject_latest_task_change_set,
    )

    reject_latest_task_change_set(
        direct_execute_env["task"].id,
        TaskChangeSetRejectRequest(task_execution_id=task_execution.id),
        direct_execute_env["db"],
        direct_execute_env["user"],
    )
    direct_execute_env["db"].refresh(direct_execute_env["task"])
    task = direct_execute_env["task"]
    assert task.workspace_status != "promoted"
    assert task.promoted_at is None
    assert not (direct_execute_env["project_root"] / "marker.txt").exists()

    change_set = (
        direct_execute_env["db"]
        .query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == task_execution.id)
        .one()
    )
    assert change_set.disposition != "promoted"


@pytest.mark.asyncio
async def test_flag_off_path_unchanged(db_session, monkeypatch, tmp_path):
    """With RUNTIME_WORKSPACE_ENABLED off, no sandbox is allocated and the
    endpoint keeps its pre-23D-14 behavior: no capture, no ready state."""
    from app.api.v1.endpoints import tasks as tasks_module

    user, project, task, project_root = _seed_project_task(
        db_session, tmp_path, name="Phase 23D-14 Flag Off"
    )

    monkeypatch.setattr(tasks_module.settings, "RUNTIME_WORKSPACE_ENABLED", False)

    class _PlainRuntime:
        async def create_session(self, task_description: str, context=None) -> str:
            return "agent:main:main"

        async def execute_task(self, prompt: str, timeout_seconds: int = 300, **_):
            return {"status": "completed", "output": "done"}

    monkeypatch.setattr(
        tasks_module,
        "create_agent_runtime",
        lambda db, session_id, task_id=None: _PlainRuntime(),
    )

    result = await tasks_module.execute_task_with_runtime(
        task.id, _FakeRequest(), db_session, user
    )
    assert result["status"] == "completed"
    db_session.refresh(task)
    assert task.workspace_status != "ready"

    task_execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )
    rows = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == task_execution.id)
        .count()
    )
    assert rows == 0
    # No snapshot scaffolding was written into the live Project Workspace.
    assert not (project_root / ".agent" / "auto-snapshots").exists()
