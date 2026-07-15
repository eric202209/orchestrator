"""Phase 26D-5 worker Guidance-runtime caller contract regressions."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.models import Session as SessionModel
from app.models import Task
from app.services.agents.agent_runtime import BackendRole
from app.services.agents.runtime_configuration import RoleRuntimeConfiguration
from app.services.human_guidance.service import resolve_guidance_runtime_target


def _configuration(
    *,
    role: BackendRole,
    backend: str,
    model: str,
    profile: str,
) -> RoleRuntimeConfiguration:
    return RoleRuntimeConfiguration(
        role=role,
        backend_name=backend,
        model_family=model,
        adaptation_profile=profile,
    )


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _DB:
    def __init__(self, session, task):
        self._session = session
        self._task = task
        self.closed = False

    def query(self, model):
        if model is SessionModel:
            return _Query(self._session)
        if model is Task:
            return _Query(self._task)
        return _Query(None)

    def add(self, *args, **kwargs):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class _TaskService:
    def __init__(self, db):
        self.db = db

    def build_project_execution_context(self, **kwargs):
        return "bounded worker seam"

    def review_existing_workspace(self, **kwargs):
        return {"has_existing_files": False}


class _Runtime:
    def __init__(self, metadata, provider_calls):
        self._metadata = metadata
        self._provider_calls = provider_calls

    def get_backend_metadata(self):
        return dict(self._metadata)

    async def get_session_context(self):
        return {}

    async def invoke_prompt(self, *args, **kwargs):
        self._provider_calls.append((args, kwargs))
        raise AssertionError("provider/model invocation is forbidden in this seam test")


def _run_worker_handoff(monkeypatch, tmp_path, *, mixed_lane, legacy_nulls=False):
    import app.tasks.worker as worker

    planning = _configuration(
        role=BackendRole.PLANNING,
        backend="direct_ollama" if mixed_lane else "local_openclaw",
        model="qwen3-coder:30b" if mixed_lane else "qwen3.6:27B",
        profile="planning_default" if mixed_lane else "openclaw_default",
    )
    execution = _configuration(
        role=BackendRole.EXECUTION,
        backend="local_openclaw",
        model="qwen3.6:27B",
        profile="openclaw_default",
    )
    session = SimpleNamespace(
        id=41,
        project_id=None,
        status="pending",
        instance_id="phase26d5-instance",
        default_execution_profile=None,
        escalation_backend_id=None,
    )
    task = SimpleNamespace(
        id=79,
        title="bounded seam task",
        description="bounded seam task",
        execution_profile=None,
        task_subfolder=None,
        plan_position=1,
        steps=None,
        started_at=None,
        current_step=0,
        workflow_stage=None,
    )
    task_execution = SimpleNamespace(
        id=126,
        backend_id=None,
        planner_backend=None if legacy_nulls else planning.backend_name,
        planner_model=None if legacy_nulls else planning.model_family,
        planning_adaptation_profile=(
            None if legacy_nulls else planning.adaptation_profile
        ),
        execution_backend=execution.backend_name,
        execution_model=execution.model_family,
    )
    db = _DB(session, task)
    provider_calls = []
    runtime_calls = []
    resolver_calls = []
    coordinator_calls = []
    failure_calls = []
    state = SimpleNamespace(
        project_dir=tmp_path,
        plan=[{"step": "intentional no-model stop"}],
        current_step_index=0,
        execution_results=[],
        completed_steps=[],
        project_context="",
        status="planning",
        abort_reason="",
    )

    def resolve_guidance_runtime_target(
        *, backend, runtime_metadata, planning_backend, execution_backend
    ):
        resolver_calls.append(
            {
                "backend": backend,
                "runtime_metadata": dict(runtime_metadata),
                "planning_backend": planning_backend,
                "execution_backend": execution_backend,
            }
        )
        return {
            "backend": backend,
            "model_name": runtime_metadata["model"],
            "model_family": runtime_metadata["model_family"],
        }

    class _ExecutionCoordinator:
        def run_execution(self, **kwargs):
            coordinator_calls.append(kwargs)
            return {"status": "stopped", "reason": "phase26d5_intentional_stop"}

    class _FailureCoordinator:
        def handle_failure(self, **kwargs):
            failure_calls.append(kwargs)

    def _create_runtime(db_arg, session_id, task_id, *, role, backend_override=None):
        runtime_calls.append(role)
        config = planning if role is BackendRole.PLANNING else execution
        return _Runtime(
            {
                "backend": config.backend_name,
                "model": config.model_family,
                "model_family": config.model_family,
                "adaptation_profile": config.adaptation_profile,
            },
            provider_calls,
        )

    monkeypatch.setattr(worker, "get_db_session", lambda: db)
    monkeypatch.setattr(
        worker,
        "resolve_runtime_configuration",
        lambda db, role: {
            BackendRole.PLANNING: planning,
            BackendRole.EXECUTION: execution,
        }[role],
    )
    monkeypatch.setattr(worker, "create_agent_runtime", _create_runtime)
    monkeypatch.setattr(worker, "TaskService", _TaskService)
    monkeypatch.setattr(worker, "OrchestrationState", lambda **kwargs: state)
    monkeypatch.setattr(
        worker, "get_task_execution", lambda db, execution_id: task_execution
    )
    monkeypatch.setattr(
        worker, "_get_latest_session_task_link", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker,
        "_claim_queued_task_for_worker",
        lambda **kwargs: (True, "claimed", None, None),
    )
    monkeypatch.setattr(worker, "_runtime_selection_details", lambda db, **kwargs: {})
    monkeypatch.setattr(worker, "_build_claimed_details", lambda **kwargs: {})
    monkeypatch.setattr(
        worker, "_run_start_runtime_identity", lambda *args: {"config": {}}
    )
    monkeypatch.setattr(worker, "_record_live_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "_append_orchestration_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker, "_write_project_state_snapshot", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker, "_snapshot_workspace_before_run", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(worker, "_save_orchestration_checkpoint", lambda *args: None)
    monkeypatch.setattr(worker, "mark_execution_running", lambda **kwargs: None)
    monkeypatch.setattr(worker, "mark_session_running", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker, "_should_force_review_execution_profile", lambda *args: False
    )
    monkeypatch.setattr(
        worker, "get_effective_policy_profile", lambda db=None: "balanced"
    )
    monkeypatch.setattr(
        worker,
        "get_policy_profile",
        lambda name: SimpleNamespace(
            name=name,
            validation_severity="standard",
            completion_repair_budget=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "get_backend_descriptor",
        lambda backend: SimpleNamespace(
            name=backend,
            capabilities=SimpleNamespace(max_parallel_sessions=None),
        ),
    )
    monkeypatch.setattr(
        worker, "register_forced_termination_cleanup", lambda callback: lambda: None
    )
    monkeypatch.setattr(
        worker, "start_langfuse_observation", lambda **kwargs: nullcontext(None)
    )
    monkeypatch.setattr(
        worker, "update_langfuse_observation", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(worker, "flush_langfuse", lambda: None)
    monkeypatch.setattr(worker, "langfuse_tracing_enabled", lambda: False)
    monkeypatch.setattr(worker, "CheckpointService", lambda db: SimpleNamespace())
    monkeypatch.setattr(
        worker,
        "ValidatorService",
        SimpleNamespace(infer_validation_profile=lambda *args, **kwargs: "standard"),
    )
    monkeypatch.setattr(worker, "_run_virtual_merge_gate", lambda **kwargs: None)
    monkeypatch.setattr(worker, "_ExecutionCoordinator", _ExecutionCoordinator)
    monkeypatch.setattr(worker, "_FailureCoordinator", _FailureCoordinator)
    monkeypatch.setattr(
        worker, "resolve_guidance_runtime_target", resolve_guidance_runtime_target
    )

    result = worker.execute_orchestration_task.run(
        session_id=session.id,
        task_id=task.id,
        prompt="bounded seam prompt",
        timeout_seconds=1,
        task_execution_id=task_execution.id,
    )
    return SimpleNamespace(
        result=result,
        planning=planning,
        execution=execution,
        task_execution=task_execution,
        resolver_calls=resolver_calls,
        runtime_calls=runtime_calls,
        coordinator_calls=coordinator_calls,
        failure_calls=failure_calls,
        provider_calls=provider_calls,
        db=db,
    )


def test_worker_guidance_handoff_uses_explicit_resolver_contract(tmp_path, monkeypatch):
    outcome = _run_worker_handoff(monkeypatch, tmp_path, mixed_lane=True)

    assert outcome.failure_calls == []
    assert outcome.result == {
        "status": "stopped",
        "reason": "phase26d5_intentional_stop",
    }
    assert outcome.resolver_calls == [
        {
            "backend": "direct_ollama",
            "runtime_metadata": {
                "backend": "direct_ollama",
                "model": "qwen3-coder:30b",
                "model_family": "qwen3-coder:30b",
                "adaptation_profile": "planning_default",
            },
            "planning_backend": "direct_ollama",
            "execution_backend": "local_openclaw",
        }
    ]
    assert outcome.runtime_calls == [BackendRole.EXECUTION, BackendRole.PLANNING]
    assert len(outcome.coordinator_calls) == 1
    assert outcome.coordinator_calls[0]["ctx"].planning_backend == "direct_ollama"
    assert outcome.coordinator_calls[0]["ctx"].execution_backend == "local_openclaw"
    assert outcome.coordinator_calls[0]["ctx"].guidance_backend == "direct_ollama"
    assert outcome.coordinator_calls[0]["ctx"].guidance_model_name == "qwen3-coder:30b"
    assert outcome.provider_calls == []
    assert outcome.db.closed
    assert outcome.planning.adaptation_profile == "planning_default"
    assert outcome.task_execution.planner_model == "qwen3-coder:30b"
    assert outcome.task_execution.execution_backend == "local_openclaw"


def test_worker_guidance_handoff_keeps_a0_same_runtime_compatibility(
    tmp_path, monkeypatch
):
    outcome = _run_worker_handoff(monkeypatch, tmp_path, mixed_lane=False)

    assert outcome.failure_calls == []
    assert outcome.result["reason"] == "phase26d5_intentional_stop"
    assert outcome.resolver_calls[0]["backend"] == "local_openclaw"
    assert outcome.resolver_calls[0]["planning_backend"] == "local_openclaw"
    assert outcome.resolver_calls[0]["execution_backend"] == "local_openclaw"
    assert outcome.runtime_calls == [BackendRole.EXECUTION]
    assert outcome.planning.adaptation_profile == "openclaw_default"
    assert outcome.provider_calls == []
    assert outcome.db.closed


def test_worker_guidance_handoff_accepts_nullable_legacy_identity(
    tmp_path, monkeypatch
):
    outcome = _run_worker_handoff(
        monkeypatch,
        tmp_path,
        mixed_lane=False,
        legacy_nulls=True,
    )

    assert outcome.failure_calls == []
    assert outcome.result["status"] == "stopped"
    assert outcome.task_execution.planner_model is None
    assert outcome.task_execution.planning_adaptation_profile is None
    assert len(outcome.resolver_calls) == 1
    assert outcome.provider_calls == []
    assert outcome.db.closed


def test_guidance_resolver_rejects_unknown_critical_keyword():
    with pytest.raises(TypeError, match="planning_adaptation_profile"):
        resolve_guidance_runtime_target(
            backend="direct_ollama",
            planning_adaptation_profile="planning_default",
        )
