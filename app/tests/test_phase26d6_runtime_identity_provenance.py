"""Phase 26D-6 queued/claimed/run-start identity provenance regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from app.config import settings
from app.models import Plan, PlanningSession, Project, Session as SessionModel, Task
from app.models import TaskStatus
from app.services.agents.agent_runtime import BackendRole
from app.services.agents.runtime_configuration import RoleRuntimeConfiguration
from app.services.orchestration.lifecycle.worker_bootstrap import (
    build_claimed_details,
    run_start_runtime_identity,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.session.session_runtime_service import queue_task_for_session


def _configuration(
    *, role: BackendRole, backend: str, model: str, profile: str
) -> RoleRuntimeConfiguration:
    return RoleRuntimeConfiguration(
        role=role,
        backend_name=backend,
        model_family=model,
        adaptation_profile=profile,
    )


def _execution(**overrides):
    values = {
        "id": 126,
        "planning_session_id": 44,
        "planning_backend": "direct_ollama",
        "planner_model": "qwen3-coder:30b",
        "reasoning_profile": "planning_default",
        "execution_backend": "local_openclaw",
        "executor_model": "qwen3.6:27B",
        "configuration_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_dispatch_identity_projection_uses_stored_task_execution_fields(monkeypatch):
    from app.tasks.worker_support import dispatch

    monkeypatch.setattr(settings, "PLANNER_MODEL", "local")
    monkeypatch.setattr(settings, "AGENT_MODEL", "local")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "direct_ollama")

    details = dispatch._runtime_selection_details(object(), task_execution=_execution())

    assert details["planning_backend"] == "direct_ollama"
    assert details["planner_model"] == "qwen3-coder:30b"
    assert details["execution_backend"] == "local_openclaw"
    assert details["executor_model"] == "qwen3.6:27B"
    assert details["execution_model"] == "qwen3.6:27B"
    assert details["task_execution_id"] == 126
    assert details["planning_session_id"] == 44
    assert details["planner_model"] != "local"
    assert details["execution_model"] != "qwen3-coder:30b"


def test_retry_projection_remains_stable_after_current_settings_change(monkeypatch):
    from app.tasks.worker_support import dispatch

    execution = _execution()
    before = dispatch._runtime_selection_details(object(), task_execution=execution)

    monkeypatch.setattr(settings, "PLANNING_BACKEND", "new-planning-backend")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "new-planning-model")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "new-execution-backend")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "new-execution-model")

    after = dispatch._runtime_selection_details(object(), task_execution=execution)

    for field_name in (
        "planning_backend",
        "planner_model",
        "reasoning_profile",
        "execution_backend",
        "executor_model",
        "execution_model",
        "configuration_fingerprint",
        "planning_session_id",
        "task_execution_id",
    ):
        assert after[field_name] == before[field_name]


def test_claimed_and_run_start_projection_keep_exact_field_direction():
    from app.services.observability.runtime_identity import (
        RuntimeIdentityProjection,
    )

    projection = RuntimeIdentityProjection(
        planning_backend="direct_ollama",
        planner_model="qwen3-coder:30b",
        reasoning_profile="planning_default",
        execution_backend="local_openclaw",
        executor_model="qwen3.6:27B",
        configuration_fingerprint="b" * 64,
        planning_session_id=44,
        task_execution_id=126,
        identity_source="stored_task_execution",
    )
    metadata = projection.to_metadata()
    claimed = build_claimed_details(
        session_instance_id="instance",
        expected_session_instance_id="instance",
        celery_task_id="celery-1",
        task_execution_id=126,
        dispatch_project_dir=None,
        queue_latency_seconds=0.1,
        queued_event=None,
        runtime_selection=metadata,
    )
    run_start = run_start_runtime_identity(None, metadata, projection)

    assert claimed["planner_model"] == run_start["models"]["planner"]
    assert claimed["execution_model"] == run_start["models"]["execution"]
    assert claimed["planning_backend"] == run_start["lanes"]["planning"]
    assert claimed["execution_backend"] == run_start["lanes"]["execution"]
    assert claimed["configuration_fingerprint"] == projection.configuration_fingerprint
    assert claimed["planning_session_id"] == projection.planning_session_id
    assert run_start["identity_source"] == "stored_task_execution"


def test_legacy_null_projection_uses_role_configuration_without_backfill():
    from app.services.observability.runtime_identity import (
        build_runtime_identity_projection,
    )

    planning = _configuration(
        role=BackendRole.PLANNING,
        backend="direct_ollama",
        model="qwen3-coder:30b",
        profile="planning_default",
    )
    execution = _configuration(
        role=BackendRole.EXECUTION,
        backend="local_openclaw",
        model="qwen3.6:27B",
        profile="openclaw_default",
    )

    projection = build_runtime_identity_projection(
        object(),
        task_execution=_execution(
            planning_session_id=None,
            planning_backend=None,
            planner_model=None,
            reasoning_profile=None,
            configuration_fingerprint=None,
            execution_backend=None,
            executor_model=None,
        ),
        planning_configuration=planning,
        execution_configuration=execution,
    )

    assert projection.planning_backend == "direct_ollama"
    assert projection.planner_model == "qwen3-coder:30b"
    assert projection.execution_backend == "local_openclaw"
    assert projection.executor_model == "qwen3.6:27B"
    assert projection.configuration_fingerprint is None
    assert projection.identity_source == "current_role_fallback"


def test_a0_projection_preserves_existing_metadata_shape():
    from app.services.observability.runtime_identity import (
        RuntimeIdentityProjection,
    )

    projection = RuntimeIdentityProjection(
        planning_backend="local_openclaw",
        planner_model="qwen3.6:27B",
        reasoning_profile="openclaw_default",
        execution_backend="local_openclaw",
        executor_model="qwen3.6:27B",
        task_execution_id=7,
    )
    run_start = run_start_runtime_identity(None, projection.to_metadata(), projection)

    assert run_start["lanes"]["planning"] == "local_openclaw"
    assert run_start["lanes"]["execution"] == "local_openclaw"
    assert run_start["models"]["planner"] == "qwen3.6:27B"
    assert run_start["models"]["execution"] == "qwen3.6:27B"


def test_before_task_execution_projection_uses_resolved_role_configurations():
    from app.services.observability.runtime_identity import (
        build_runtime_identity_projection,
    )

    planning = _configuration(
        role=BackendRole.PLANNING,
        backend="direct_ollama",
        model="planning-model",
        profile="planning_default",
    )
    execution = _configuration(
        role=BackendRole.EXECUTION,
        backend="local_openclaw",
        model="execution-model",
        profile="openclaw_default",
    )

    projection = build_runtime_identity_projection(
        object(),
        planning_configuration=planning,
        execution_configuration=execution,
        task_execution_id=None,
    )

    assert projection.task_execution_id is None
    assert projection.planning_backend == "direct_ollama"
    assert projection.planner_model == "planning-model"
    assert projection.execution_backend == "local_openclaw"
    assert projection.executor_model == "execution-model"


def test_task_queued_path_projects_the_new_task_execution_identity(
    db_session, monkeypatch, tmp_path
):
    project = Project(
        name="Queued Provenance Project",
        workspace_path=str(tmp_path / "workspace-root"),
    )
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Queued Provenance Session",
        status="pending",
        instance_id="queued-provenance-instance",
    )
    task = Task(
        project_id=project.id,
        title="Queued Provenance Task",
        description="bounded queue seam",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([session, task])
    db_session.flush()
    plan = Plan(
        project_id=project.id,
        title="Queued Provenance Plan",
        requirement="bounded queue seam",
        markdown="- bounded queue seam",
    )
    db_session.add(plan)
    db_session.flush()
    task.plan_id = plan.id
    planning_session = PlanningSession(
        project_id=project.id,
        title="Queued Planning Session",
        prompt="bounded queue seam",
        status="completed",
        planning_backend="direct_ollama",
        planner_model="qwen3-coder:30b",
        reasoning_profile="planning_default",
        configuration_fingerprint="c" * 64,
        finalized_plan_id=plan.id,
        committed_task_ids=json.dumps([task.id]),
    )
    db_session.add(planning_session)
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    monkeypatch.setattr(
        "app.services.tasks.execution.active_execution_identity",
        lambda db: {
            "planning_backend": "local_openclaw",
            "execution_backend": "local_openclaw",
            "planner_model": "local",
            "executor_model": "qwen3.6:27B",
            "reasoning_profile": "openclaw_default",
            "configuration_fingerprint": "d" * 64,
        },
    )
    event_workspace = Path(tmp_path) / "event-workspace"
    event_workspace.mkdir()
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.ensure_task_workspace",
        lambda *args, **kwargs: {
            "workspace_path": str(event_workspace),
            "task_subfolder": None,
            "stored_task_subfolder": None,
        },
    )

    class _FakeResult:
        id = "queued-provenance-celery"

    class _FakeWorkerTask:
        @staticmethod
        def delay(**kwargs):
            return _FakeResult()

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _FakeWorkerTask)

    queue_task_for_session(db_session, session, task.id)

    events = read_orchestration_events(event_workspace, session.id, task.id)
    queued = next(
        event for event in events if event["event_type"] == EventType.TASK_QUEUED
    )
    details = queued["details"]
    assert details["planning_backend"] == "direct_ollama"
    assert details["planner_model"] == "qwen3-coder:30b"
    assert details["execution_backend"] == "local_openclaw"
    assert details["executor_model"] == "qwen3.6:27B"
    assert details["execution_model"] == "qwen3.6:27B"
    assert details["planner_model"] != "local"
    assert details["task_execution_id"] is not None
