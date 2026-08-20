from __future__ import annotations

import json

import pytest

from app.models import (
    LogEntry,
    Plan,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
    User,
)
from app.services.tasks.service import TASK_CHANGE_SET_LOG_MESSAGE
from app.services.orchestration.task_rules import run_virtual_merge_gate


class _FakeAsyncResult:
    id = "celery-123"


def _stub_retry_dispatch(
    monkeypatch,
    captured_kwargs: dict | None = None,
    *,
    bypass_binding_admission: bool = True,
):
    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/retry-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-task-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    def _fake_delay(**kwargs):
        if captured_kwargs is not None:
            captured_kwargs.clear()
            captured_kwargs.update(kwargs)
        return _FakeAsyncResult()

    monkeypatch.setattr(worker_module.execute_orchestration_task, "delay", _fake_delay)
    if bypass_binding_admission:
        monkeypatch.setattr(
            "app.api.v1.endpoints.tasks.admit_project_openclaw_binding_for_dispatch",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "app.services.session.session_runtime_service.admit_project_openclaw_binding_for_dispatch",
            lambda *args, **kwargs: None,
        )


def test_sync_task_execution_uses_terminal_task_state_over_stale_running_link(
    db_session,
):
    from app.tasks.worker_support.execution_state import (
        _sync_task_execution_from_task_state,
    )

    project = Project(name="Terminal Sync Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Terminal Sync Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="Terminal sync task",
        description="fail after debug parse",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.flush()
    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db_session.add(session_task)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()

    _sync_task_execution_from_task_state(
        db_session,
        execution.id,
        task=task,
        session_task_link=session_task,
    )

    db_session.refresh(execution)
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at is not None


def test_task_retry_marks_attempt_failed_when_post_commit_dispatch_fails(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dispatch Failure Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry me",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)
    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/rollback-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-me-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )
    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "delay",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    with pytest.raises(RuntimeError, match="broker down"):
        authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    session = db_session.query(SessionModel).one()
    assert session.status == "stopped"
    assert session.is_active is False
    assert session.stopped_at is not None
    assert db_session.query(SessionTask).count() == 1
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.status == TaskStatus.FAILED
    assert task_execution.completed_at is not None
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "Failed to dispatch task to worker"


def test_task_retry_commits_records_before_worker_dispatch(
    authenticated_client, db_session, db_session_factory, monkeypatch
):
    project = Project(name="Dispatch Visibility Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry after commit",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)
    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/dispatch-visibility-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-after-commit",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    seen: dict[str, bool] = {}

    def _fake_delay(**kwargs):
        with db_session_factory() as fresh_db:
            seen["session_visible"] = (
                fresh_db.query(SessionModel)
                .filter(SessionModel.id == kwargs["session_id"])
                .first()
                is not None
            )
            seen["task_visible"] = (
                fresh_db.query(Task).filter(Task.id == kwargs["task_id"]).first()
                is not None
            )
            seen["task_execution_visible"] = (
                fresh_db.query(TaskExecution)
                .filter(TaskExecution.id == kwargs["task_execution_id"])
                .first()
                is not None
            )
        return _FakeAsyncResult()

    from app.tasks import worker as worker_module

    monkeypatch.setattr(worker_module.execute_orchestration_task, "delay", _fake_delay)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert seen == {
        "session_visible": True,
        "task_visible": True,
        "task_execution_visible": True,
    }


def test_task_retry_dual_writes_pending_task_execution(
    authenticated_client, db_session, monkeypatch, isolated_workspace_root
):
    project = Project(name="Dual Write Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry with execution",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    task_execution = db_session.query(TaskExecution).one()
    assert payload["task_execution_id"] == task_execution.id
    assert captured_kwargs["task_execution_id"] == task_execution.id
    assert task_execution.session_id == payload["session_id"]
    assert task_execution.task_id == task.id
    assert task_execution.attempt_number == 1
    assert task_execution.status == TaskStatus.PENDING
    assert (
        isolated_workspace_root
        / "dual-write-project"
        / "retry-task-1"
        / ".agent"
        / "events"
        / f"session_{payload['session_id']}_task_{task.id}.jsonl"
    ).exists()


def test_task_retry_default_reuses_latest_project_session_without_duplicates(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Workflow Retry Project")
    db_session.add(project)
    db_session.commit()

    older_session = SessionModel(
        project_id=project.id,
        name="Older workflow",
        status="stopped",
        is_active=False,
    )
    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
        instance_id="workflow-instance",
    )
    old_isolated_session = SessionModel(
        project_id=project.id,
        name="Retry without duplicates session",
        status="stopped",
        is_active=False,
        instance_id="orchestrator-task-999-123",
    )
    task = Task(
        project_id=project.id,
        title="Retry without duplicates",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([older_session, workflow_session, old_isolated_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    first = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")
    second = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert first.status_code == 200
    assert second.status_code == 409
    assert "project_execution_serialization_conflict" in second.json()["detail"]
    assert first.json()["session_id"] == workflow_session.id
    assert db_session.query(SessionModel).count() == 3
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert captured_kwargs["session_id"] == workflow_session.id
    assert captured_kwargs["task_execution_id"] == first.json()["task_execution_id"]


def test_task_retry_uses_requested_session_when_valid(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Requested Session Project")
    db_session.add(project)
    db_session.commit()

    requested_session = SessionModel(
        project_id=project.id,
        name="Requested workflow",
        status="stopped",
        is_active=False,
    )
    other_session = SessionModel(
        project_id=project.id,
        name="Other workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry requested session",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([requested_session, other_session, task])
    db_session.commit()
    db_session.refresh(requested_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"session_id": requested_session.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == requested_session.id
    assert captured_kwargs["session_id"] == requested_session.id
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.session_id == requested_session.id


def test_task_retry_explicit_new_session_preserves_legacy_isolated_session_creation(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Explicit New Session Project")
    db_session.add(project)
    db_session.commit()

    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry isolated",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([workflow_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] != workflow_session.id
    assert db_session.query(SessionModel).count() == 2
    new_session = (
        db_session.query(SessionModel)
        .filter(SessionModel.id == payload["session_id"])
        .one()
    )
    assert new_session.name == "Retry isolated session"


def test_task_retry_rejects_unbound_openclaw_project_before_task_execution(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    project_workspace = tmp_path / "unbound-project"
    project_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps({"agents": {"list": []}}), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    project = Project(
        name="Unbound OpenClaw Project",
        workspace_path=str(project_workspace),
    )
    task = Task(
        project=project,
        title="Reject before runtime",
        description="must fail closed",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([project, task])
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch, bypass_binding_admission=False)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["category"] == "openclaw_workspace_binding_unavailable"
    assert detail["project_id"] == project.id
    assert detail["matching_agent_count"] == 0
    assert detail["admission_stage"] == "task_retry_dispatch"
    assert db_session.query(TaskExecution).count() == 0
    assert db_session.query(SessionModel).count() == 0


def test_task_retry_reaches_dispatch_with_valid_openclaw_binding(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    project_workspace = tmp_path / "bound-project"
    project_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [{"id": "bound-agent", "workspace": str(project_workspace)}]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    project = Project(
        name="Bound OpenClaw Project",
        workspace_path=str(project_workspace),
    )
    task = Task(
        project=project,
        title="Reach dispatch",
        description="provider-free dispatch reachability",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([project, task])
    db_session.commit()
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch, bypass_binding_admission=False)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert db_session.query(TaskExecution).count() == 1


def test_task_retry_new_session_isolates_historical_ordered_tasks(
    authenticated_client, db_session, monkeypatch
):
    """Explicit isolated retry must not inherit unrelated project history."""
    project = Project(name="Historical Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Belongs to an earlier workflow record.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        title="Selected isolated task",
        description="Run in an explicitly isolated execution session.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add_all([historical_pending, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    _stub_retry_dispatch(monkeypatch)

    workflow_response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/retry"
    )
    assert workflow_response.status_code == 409
    assert "Earlier ordered tasks must finish" in workflow_response.json()["detail"]

    isolated_response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert isolated_response.status_code == 200
    payload = isolated_response.json()
    assert payload["execution_scope"] == "isolated_session"
    assert payload["isolated_session"] is True
    assert payload["task_id"] == selected_task.id


def test_task_retry_new_session_keeps_plan_scoped_ordering(
    authenticated_client, db_session
):
    """Isolation must not bypass predecessors in the same explicit Plan."""
    project = Project(name="Plan Scoped Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Bounded plan",
        source_brain="local",
        requirement="Preserve ordered execution.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan selected task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add_all([predecessor, selected_task])
    db_session.commit()
    db_session.refresh(selected_task)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_dogfood_admission_persists_queue_isolation_authority(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dogfood Admission Persistence Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    monkeypatch.setattr(
        "app.api.v1.endpoints.sessions.admit_dogfood_workspace",
        lambda *args, **kwargs: None,
    )

    response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": project.id,
            "name": "admitted bounded session",
            "execution_mode": "manual",
            "dogfood_admission": True,
        },
    )

    assert response.status_code == 201
    created = db_session.query(SessionModel).filter_by(id=response.json()["id"]).one()
    assert created.dogfood_admitted is True


def test_admitted_dogfood_session_isolates_legacy_project_queue(
    authenticated_client, db_session, monkeypatch
):
    """Dogfood admission isolates only the historical unplanned queue."""
    project = Project(name="Admitted Historical Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_planning = PlanningSession(
        project_id=project.id,
        title="Historical planning session",
        prompt="Preserve the original planning history.",
        status="completed",
        source_brain="local",
    )
    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Belongs to an earlier workflow record.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Failed in an earlier workflow record.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    historical_cancelled = Task(
        project_id=project.id,
        title="Historical cancelled task",
        description="Was cancelled in an earlier workflow record.",
        status=TaskStatus.CANCELLED,
        plan_position=3,
    )
    selected_task = Task(
        project_id=project.id,
        title="Selected dogfood task",
        description="Run in the admitted bounded execution session.",
        status=TaskStatus.PENDING,
        plan_position=4,
    )
    standard_session = SessionModel(
        project_id=project.id,
        name="Standard session",
        status="pending",
        execution_mode="manual",
        instance_id="standard-session-instance",
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted dogfood session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-dogfood-session-instance",
    )
    # This attribute is the persisted admission marker added by the fix.
    admitted_session.dogfood_admitted = True
    db_session.add_all(
        [
            historical_planning,
            historical_pending,
            historical_failed,
            historical_cancelled,
            selected_task,
            standard_session,
            admitted_session,
        ]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(standard_session)
    db_session.refresh(admitted_session)
    db_session.refresh(historical_planning)

    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.services.session.session_runtime_service.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/admitted-dogfood-project",
            "task_subfolder": None,
            "stored_task_subfolder": "selected-dogfood-task",
            "workspace_scope": "isolated_task_workspace",
        },
    )
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.append_orchestration_event",
        lambda **kwargs: {"event_id": "queued-event-1"},
    )
    monkeypatch.setattr(
        "app.services.session.session_runtime_service._maybe_compact_checkpoint_before_dispatch",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.admit_project_openclaw_binding_for_dispatch",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "delay",
        lambda **kwargs: _FakeAsyncResult(),
    )

    standard_response = authenticated_client.post(
        f"/api/v1/sessions/{standard_session.id}/tasks/{selected_task.id}/run"
    )
    assert standard_response.status_code == 409

    admitted_response = authenticated_client.post(
        f"/api/v1/sessions/{admitted_session.id}/tasks/{selected_task.id}/run"
    )

    assert admitted_response.status_code == 200
    assert admitted_response.json()["queued_task"]["task_id"] == selected_task.id
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert db_session.get(Task, historical_pending.id).status == TaskStatus.PENDING
    assert db_session.get(Task, historical_failed.id).status == TaskStatus.FAILED
    assert db_session.get(Task, historical_cancelled.id).status == TaskStatus.CANCELLED
    preserved_planning = db_session.get(PlanningSession, historical_planning.id)
    assert preserved_planning is not None
    assert preserved_planning.status == "completed"
    assert preserved_planning.prompt == "Preserve the original planning history."


def test_admitted_dogfood_session_keeps_plan_scoped_ordering(
    authenticated_client, db_session
):
    """Admission cannot bypass a predecessor in the same explicit Plan."""
    project = Project(name="Admitted Plan Scoped Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Bounded plan",
        source_brain="local",
        requirement="Preserve ordered execution.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan selected task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted plan session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-plan-session-instance",
    )
    admitted_session.dogfood_admitted = True
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    response = authenticated_client.post(
        f"/api/v1/sessions/{admitted_session.id}/tasks/{selected_task.id}/run"
    )

    assert response.status_code == 409
    assert "earlier ordered work" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_uses_admitted_session_queue_isolation(
    authenticated_client, db_session, monkeypatch
):
    """The R6 compatibility launch must honor the persisted session authority."""
    project = Project(name="R6 Compatibility Queue Isolation Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="R6 compatibility task",
        description="Run through the compatibility endpoint.",
        status=TaskStatus.PENDING,
        plan_position=3,
        plan_id=None,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="R6 admitted session",
        status="pending",
        execution_mode="manual",
        instance_id="r6-admitted-session-instance",
        dogfood_admitted=True,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, admitted_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)
    db_session.expire_all()
    assert db_session.get(SessionModel, admitted_session.id).dogfood_admitted is True

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["session_id"] == admitted_session.id
    assert payload["task_id"] == selected_task.id
    assert db_session.get(Task, historical_pending.id).status == TaskStatus.PENDING
    assert db_session.get(Task, historical_failed.id).status == TaskStatus.FAILED
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1
    assert (
        db_session.query(LogEntry)
        .filter(LogEntry.task_execution_id == payload["task_execution_id"])
        .count()
        == 3
    )
    assert captured_kwargs["session_id"] == admitted_session.id
    assert captured_kwargs["task_id"] == selected_task.id
    assert captured_kwargs["task_execution_id"] == payload["task_execution_id"]


def test_compatibility_execute_e2_shape_reaches_planning_boundary_without_duplicates(
    authenticated_client, db_session, monkeypatch, tmp_path
):
    """The exact compatibility route admits E2-shaped implementation work once."""
    project = Project(
        name="E2 Exact Route Project",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="Add utc_now() helper and migrate one naive datetime consumer",
        description=(
            "Add app/time_utils.py with utc_now() returning an aware UTC datetime. "
            "Migrate app/services/workspace/context_service.py and add regression "
            "coverage in app/tests/test_utc_now_helper.py. Run pytest for the "
            "acceptance tests."
        ),
        status=TaskStatus.PENDING,
        plan_position=3,
        plan_id=None,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="E2 exact route admitted session",
        status="pending",
        execution_mode="manual",
        instance_id="e2-exact-route-session",
        dogfood_admitted=True,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, admitted_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 202
    payload = response.json()
    assert captured_kwargs["task_id"] == selected_task.id
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1

    # This is the worker's pre-planning boundary. The historical project
    # queue is not an explicit Plan predecessor for this implementation task.
    assert (
        run_virtual_merge_gate(
            db_session,
            project,
            selected_task,
            "full_lifecycle",
            lambda root, **_kw: root / ".agent" / "state_manager.json",
        )
        is None
    )
    assert payload["task_execution_id"] == db_session.query(TaskExecution).one().id


def test_compatibility_execute_keeps_ordinary_legacy_ordering(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Ordinary Compatibility Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    historical_pending = Task(
        project_id=project.id,
        title="Historical pending task",
        description="Preserve pending history.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    historical_failed = Task(
        project_id=project.id,
        title="Historical failed task",
        description="Preserve failed history.",
        status=TaskStatus.FAILED,
        plan_position=2,
    )
    selected_task = Task(
        project_id=project.id,
        title="Ordinary compatibility task",
        description="Remain project-ordered.",
        status=TaskStatus.PENDING,
        plan_position=3,
    )
    ordinary_session = SessionModel(
        project_id=project.id,
        name="Ordinary session",
        status="pending",
        execution_mode="manual",
        instance_id="ordinary-session-instance",
        dogfood_admitted=False,
    )
    db_session.add_all(
        [historical_pending, historical_failed, selected_task, ordinary_session]
    )
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(ordinary_session)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": ordinary_session.id},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_keeps_admitted_plan_predecessor_ordering(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Admitted Compatibility Plan Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    plan = Plan(
        project_id=project.id,
        title="Compatibility plan",
        source_brain="local",
        requirement="Preserve Plan ordering.",
        markdown="1. predecessor\n2. selected task",
        status="committed",
    )
    db_session.add(plan)
    db_session.flush()
    predecessor = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan predecessor",
        description="Must remain first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        plan_id=plan.id,
        title="Plan compatibility task",
        description="Must remain ordered.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted compatibility session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-compatibility-plan-session",
        dogfood_admitted=True,
    )
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_keeps_admitted_session_owned_ordering(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Admitted Session-Owned Queue Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    predecessor = Task(
        project_id=project.id,
        title="Session predecessor",
        description="Must finish in the current session first.",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    selected_task = Task(
        project_id=project.id,
        title="Session-owned compatibility task",
        description="Must remain ordered within the admitted session.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    admitted_session = SessionModel(
        project_id=project.id,
        name="Admitted session-owned session",
        status="pending",
        execution_mode="manual",
        instance_id="admitted-session-owned-instance",
        dogfood_admitted=True,
    )
    db_session.add_all([predecessor, selected_task, admitted_session])
    db_session.commit()
    db_session.refresh(selected_task)
    db_session.refresh(admitted_session)
    db_session.add(
        SessionTask(
            session_id=admitted_session.id,
            task_id=predecessor.id,
            status=TaskStatus.PENDING,
        )
    )
    db_session.commit()

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{selected_task.id}/execute",
        json={"session_id": admitted_session.id},
    )

    assert response.status_code == 409
    assert "Earlier ordered tasks must finish" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 0


def test_compatibility_execute_does_not_duplicate_active_task_execution(
    authenticated_client, db_session
):
    project = Project(name="Active Compatibility Task Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already running compatibility task",
        description="Must not be duplicated.",
        status=TaskStatus.RUNNING,
        plan_position=1,
    )
    session = SessionModel(
        project_id=project.id,
        name="Active compatibility session",
        status="running",
        is_active=True,
        instance_id="active-compatibility-session-instance",
        dogfood_admitted=True,
    )
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
        )
    )
    db_session.add(
        TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
    )
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/execute",
        json={"session_id": session.id},
    )

    assert response.status_code == 409
    assert "already has an active canonical execution" in response.json()["detail"]
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 1


def test_task_retry_with_requested_changes_injects_operator_note_and_change_set(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Requested Changes Repair Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Repair requested",
        description="original task prompt",
        status=TaskStatus.DONE,
        workspace_status="changes_requested",
        promotion_note="Tighten the README and remove the extra file.",
        task_subfolder="task-repair-requested",
    )
    session = SessionModel(
        project_id=project.id,
        name="Previous run",
        status="stopped",
        is_active=False,
    )
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    db_session.add(
        TaskExecutionChangeSet(
            project_id=project.id,
            task_id=task.id,
            session_id=session.id,
            task_execution_id=execution.id,
            base_snapshot_key="requested-changes-snapshot",
            added_files=["extra.md"],
            modified_files=["README.md"],
            deleted_files=["old.md"],
            warning_flags=["deleted_files"],
            disposition="captured",
        )
    )
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            level="INFO",
            message=TASK_CHANGE_SET_LOG_MESSAGE,
            log_metadata=json.dumps(
                {
                    "schema": "openclaw.task_execution_change_set.v1",
                    "task_id": task.id,
                    "task_execution_id": execution.id,
                    "added_count": 1,
                    "modified_count": 1,
                    "deleted_count": 1,
                    "changed_count": 3,
                    "added_files": ["extra.md"],
                    "modified_files": ["README.md"],
                    "deleted_files": ["old.md"],
                    "warning_flags": ["deleted_files"],
                }
            ),
        )
    )
    db_session.commit()

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)
    monkeypatch.setattr(
        "app.services.tasks.service.TaskService.archive_task_workspace_for_repair_rerun",
        lambda *a, **kw: {"archived": False, "reason": "test"},
    )

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session", "create_new_session": True},
    )

    assert response.status_code == 200
    prompt = captured_kwargs["prompt"]
    assert "Operator requested changes" in prompt
    assert "Tighten the README" in prompt
    assert "added=1, modified=1, deleted=1" in prompt
    assert "README.md" in prompt
    assert "extra.md" in prompt
    assert "old.md" in prompt


def test_legacy_worker_alias_still_exists():
    from app.tasks import worker as worker_module

    assert (
        worker_module.execute_openclaw_task is worker_module.execute_orchestration_task
    )
