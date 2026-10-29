"""POST33-EXEC2 provider-free consumption proof for the real execution loop.

These tests construct only the minimum database/session/task/authority state
needed by ``execute_step_loop``.  Runtime calls stop at a direct-runtime
protocol stub; no provider, OpenClaw process, Celery task, or Product Attempt
is created.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.agent_backends import (
    ExecutionTopology,
    get_backend_descriptor,
)
from app.services.agents.agent_runtime import (
    RuntimeCapabilityError,
    resolve_runtime_configuration,
    validate_runtime_capabilities,
)
from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    ContextWindowPolicy,
    RetryStrategy,
    RuntimeBackendResult,
)
from app.services.agents.runtime_configuration import BackendRole
from app.services.orchestration.error_handler import error_handler
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.accepted_path_authority import (
    AcceptedPathAuthority,
    accepted_plan_identity,
)
from app.services.orchestration.validation.path_authority import (
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)


def _step(
    *,
    description: str,
    ops: list[dict[str, Any]] | None = None,
    commands: list[str] | None = None,
    verification: str = "",
    expected_files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "step_number": 1,
        "description": description,
        "commands": list(commands or []),
        "verification": verification,
        "rollback": None,
        "expected_files": list(expected_files or []),
        "ops": list(ops or []),
    }


def _authority(
    workspace: Path,
    plan: list[dict[str, Any]],
    grants: list[tuple[str, GrantClass]],
) -> AcceptedPathAuthority:
    path_grants = []
    for relative_path, grant_class in grants:
        path_grants.append(
            PathGrant(
                path=declare(relative_path),
                grant_class=grant_class,
                provenance=GrantProvenance.ACCEPTED_PLAN,
                baseline_content_hash=(
                    None if grant_class is GrantClass.CREATION_AUTHORIZED else "1" * 64
                ),
            )
        )
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(workspace.resolve()),
        maximum_scope_digest="2" * 64,
        grants=path_grants,
    )


def _persist_authority(
    db: Any,
    *,
    session_id: int,
    task_id: int,
    authority: AcceptedPathAuthority,
) -> None:
    db.add(
        TaskCheckpoint(
            session_id=session_id,
            task_id=task_id,
            checkpoint_type="validation_plan",
            description="POST33-EXEC2 accepted plan authority",
            state_snapshot=json.dumps(
                {
                    "stage": "plan",
                    "status": "accepted",
                    "details": {"accepted_path_authority": authority.to_dict()},
                }
            ),
        )
    )
    db.commit()


@dataclass
class DirectRuntimeStub:
    """Provider-boundary direct runtime with no OpenClaw-only surface."""

    result: dict[str, Any]
    backend: str = "direct_ollama"
    model: str = "qwen2.5-coder:7b"

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.backend_descriptor = SimpleNamespace(name=self.backend)

    async def create_session(
        self, task_description: str, context: dict[str, Any] | None = None
    ) -> str:
        return "direct-test-session"

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"prompt": prompt, "timeout_seconds": timeout_seconds, **kwargs}
        )
        return dict(self.result)

    async def pause_session(self) -> None:
        self.pause_calls += 1

    async def resume_session(self, checkpoint_name: str | None = None) -> str:
        self.resume_calls += 1
        return "direct-test-resume"

    async def stop_session(self) -> None:
        self.stop_calls += 1

    async def get_session_context(self) -> dict[str, Any]:
        return {"backend": self.backend, "model": self.model}

    async def invoke_prompt(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return dict(self.result)

    def get_backend_metadata(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model_family": self.model,
            "role": "execution",
            "capabilities": {
                "supports_step_reasoning": True,
                "supports_tool_execution": False,
                "supports_agent_workspace_binding": False,
                "supports_checkpoint_resume": False,
            },
        }

    def describe_interface(self) -> AgentInterfaceDescriptor:
        return AgentInterfaceDescriptor(
            backend=self.backend,
            model_family=self.model,
            planning_prompt_template="provider-neutral",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect="direct-test",
            tool_capability_map={
                "shell": False,
                "filesystem": False,
                "checkpoint_resume": False,
                "streaming": False,
            },
            tool_shape="none",
            preferred_retry_strategy=RetryStrategy(
                planning="none", execution="none", completion="none"
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=200_000,
                overflow_strategy="none",
                compaction_strategy="none",
            ),
        )

    def reports_context_overflow(self, result: dict[str, Any] | None) -> bool:
        return False


def _with_agent_runtime_topology(runtime: Any) -> Any:
    """Give a stub the declared capabilities of a tool-capable agent runtime."""

    runtime.backend_descriptor = get_backend_descriptor("local_openclaw")
    return runtime


def _make_loop_context(
    db: Any,
    tmp_path: Path,
    *,
    step: dict[str, Any],
    runtime: DirectRuntimeStub,
    session_status: str = "running",
    timeout_seconds: int = 30,
) -> tuple[OrchestrationRunContext, TaskExecution, Path]:
    workspace = tmp_path / "runtime-workspace"
    workspace.mkdir(parents=True)
    project = Project(name="POST33-EXEC2", workspace_path=str(workspace))
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="POST33-EXEC2 session",
        status=session_status,
        is_active=session_status not in {"stopped", "paused"},
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="POST33-EXEC2 task",
        description="Provider-free structured-orchestrator consumption proof",
        status=TaskStatus.RUNNING.value,
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        backend_id=runtime.backend,
    )
    db.add_all([link, execution])
    db.flush()

    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Provider-free structured-orchestrator consumption proof",
        project_name=project.name,
        project_context="",
        task_id=task.id,
        plan=[step],
        reasoning_artifact={
            "intent": "prove execution consumption",
            "workspace_facts": [f"project_dir={workspace}"],
            "planned_actions": [step["description"]],
            "verification_plan": ["verify the step result"],
        },
    )
    state._project_dir_override = str(workspace)
    grants = [
        (str(operation.get("path")), GrantClass.CREATION_AUTHORIZED)
        for operation in step.get("ops", [])
        if operation.get("op") in {"write_file", "mkdir"}
    ]
    granted_paths = {path for path, _ in grants}
    grants.extend(
        (path, GrantClass.CREATION_AUTHORIZED)
        for path in step.get("expected_files", [])
        if path not in granted_paths
    )
    _persist_authority(
        db,
        session_id=session.id,
        task_id=task.id,
        authority=_authority(workspace, [step], grants),
    )
    ctx = OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Prove structured-orchestrator execution consumption",
        timeout_seconds=timeout_seconds,
        execution_profile="test_only",
        validation_profile="verification",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("post33-exec2-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=error_handler,
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, execution, workspace


def _run_loop(ctx: OrchestrationRunContext) -> dict[str, Any]:
    return execute_step_loop(
        ctx=ctx,
        extract_structured_text=lambda value: (
            str(value.get("output", value)) if isinstance(value, dict) else str(value)
        ),
        normalize_step=lambda raw_step, project_dir, logger_obj, step_number: dict(
            raw_step
        ),
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )


def test_case_a_structured_file_op_consumes_real_loop_without_provider(
    db_session, tmp_path, monkeypatch
):
    step = _step(
        description="Create the accepted structured artifact",
        ops=[{"op": "write_file", "path": "accepted.txt", "content": "safe\n"}],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub(
        {"status": "completed", "output": "provider must not be called"}
    )
    ctx, execution, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    execution_readiness = validate_runtime_capabilities(
        get_backend_descriptor("direct_ollama"),
        BackendRole.EXECUTION,
        effective_context_tokens=200_000,
        dispatch=False,
    )
    assert execution_readiness["execution_topology"] == (
        ExecutionTopology.STRUCTURED_ORCHESTRATOR.value
    )
    file_op_calls = []
    from app.services.orchestration.execution.executor import ExecutorService

    original_execute_file_ops = ExecutorService.execute_file_ops

    def record_file_ops(project_dir, ops, **kwargs):
        file_op_calls.append((Path(project_dir), list(ops)))
        return original_execute_file_ops(project_dir, ops, **kwargs)

    monkeypatch.setattr(ExecutorService, "execute_file_ops", record_file_ops)

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert (workspace / "accepted.txt").read_text(encoding="utf-8") == "safe\n"
    assert runtime.calls == []
    assert len(file_op_calls) == 1
    assert file_op_calls[0][0] == workspace
    assert ctx.orchestration_state.current_step_index == 1
    assert ctx.orchestration_state.execution_results[-1].status == "success"
    assert execution.backend_id == "direct_ollama"


def test_case_a_unauthorized_structured_file_op_fails_closed_without_provider(
    db_session, tmp_path
):
    step = _step(
        description="Attempt a path absent from the accepted authority",
        ops=[
            {
                "op": "write_file",
                "path": "unauthorized.txt",
                "content": "must not exist\n",
            }
        ],
        expected_files=["unauthorized.txt"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, _, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )

    # The helper grants every structured write by default; remove the grant to
    # model an accepted Plan whose APA omitted this path.
    db_session.query(TaskCheckpoint).delete()
    _persist_authority(
        db_session,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        authority=_authority(workspace, [step], []),
    )

    result = _run_loop(ctx)

    assert result["status"] == "failed"
    assert result["reason"] == "execution_mutation_authority_denied"
    assert runtime.calls == []
    assert not (workspace / "unauthorized.txt").exists()
    assert ctx.orchestration_state.status.value == "aborted"


def test_case_b_local_command_runs_in_runtime_workspace_without_provider(
    db_session, tmp_path, monkeypatch
):
    step = _step(
        description="Run the Orchestrator-owned local command",
        commands=["printf 'local-command-ok\\n' > command-output.txt"],
        verification=(
            'python -c "import pathlib,sys; '
            "sys.exit(0 if pathlib.Path('command-output.txt').exists() else 1)\""
        ),
        expected_files=["command-output.txt"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, _, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    import app.services.orchestration.phases.execution_local_steps as local_steps

    subprocess_timeouts = []
    original_run = local_steps.subprocess.run

    def record_subprocess_run(*args, **kwargs):
        subprocess_timeouts.append(kwargs.get("timeout"))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(local_steps.subprocess, "run", record_subprocess_run)

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert (workspace / "command-output.txt").read_text(
        encoding="utf-8"
    ) == "local-command-ok\n"
    assert runtime.calls == []
    assert 30 in subprocess_timeouts
    assert ctx.orchestration_state.execution_results[-1].files_changed == [
        "command-output.txt"
    ]


def test_case_c_unsupported_command_is_not_dispatched_to_a_no_tools_runtime(
    db_session, tmp_path
):
    """Phase 40C-2: a no-tools runtime's reply is never command evidence."""

    step = _step(
        description="Inspect checkout totals",
        commands=['python -c "from pkg.mod import f; print(f())"'],
        verification='python -c "from pkg.mod import f; print(f())"',
    )
    runtime = DirectRuntimeStub(
        {
            "status": "completed",
            "output": json.dumps(
                {"status": "completed", "output": "Actual total: 2430"}
            ),
        }
    )
    ctx, execution, _ = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )

    result = _run_loop(ctx)

    assert result["status"] == "failed"
    assert result["reason"] == "execution_capability_unsupported"
    assert result["failure_category"] == "validation_failure"
    assert runtime.calls == []
    assert ctx.orchestration_state.status.value == "aborted"
    assert "nothing was executed" in ctx.orchestration_state.abort_reason
    assert "2430" not in ctx.orchestration_state.abort_reason
    db_session.refresh(execution)
    assert execution.status == TaskStatus.FAILED
    assert execution.failure_category == "validation_failure"


def test_case_c_no_tools_runtime_failure_self_report_is_not_a_command_result(
    db_session, tmp_path
):
    step = _step(
        description="Inspect checkout totals",
        commands=["custom-direct-runtime-step"],
    )
    runtime = DirectRuntimeStub(
        {
            "status": "failed",
            "output": json.dumps({"status": "failed", "error": "NameError"}),
            "error": "NameError",
        }
    )
    ctx, _, _ = _make_loop_context(db_session, tmp_path, step=step, runtime=runtime)

    result = _run_loop(ctx)

    assert result["reason"] == "execution_capability_unsupported"
    assert runtime.calls == []
    assert "NameError" not in ctx.orchestration_state.abort_reason


def test_case_c_safe_local_command_still_executes_under_no_tools_runtime(
    db_session, tmp_path
):
    step = _step(
        description="Inspect the workspace",
        commands=["ls"],
    )
    runtime = DirectRuntimeStub({"status": "failed", "output": "unused"})
    ctx, _, _ = _make_loop_context(db_session, tmp_path, step=step, runtime=runtime)

    assert _run_loop(ctx) == {"status": "completed"}
    assert runtime.calls == []


def test_case_c_agent_runtime_result_is_coerced_and_step_completes(
    db_session, tmp_path
):
    step = _step(
        description="Residual reasoning must reach the direct runtime",
        commands=["custom-direct-runtime-step"],
    )
    runtime = _with_agent_runtime_topology(
        DirectRuntimeStub(
            {
                "status": "completed",
                "output": json.dumps(
                    {
                        "status": "completed",
                        "output": "direct textual result",
                        "verification_output": "direct result accepted",
                        "files_changed": [],
                    }
                ),
            }
        )
    )
    ctx, execution, _ = _make_loop_context(
        db_session,
        tmp_path,
        step=step,
        runtime=runtime,
        timeout_seconds=7,
    )

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert (
        "Residual reasoning must reach the direct runtime" in runtime.calls[0]["prompt"]
    )
    # determine_step_timeout preserves a bounded provider timeout; the direct
    # runtime receives it without any OpenClaw subprocess/session machinery.
    assert runtime.calls[0]["timeout_seconds"] >= 300
    assert ctx.orchestration_state.execution_results[-1].status == "success"
    assert (
        "direct textual result" in ctx.orchestration_state.execution_results[-1].output
    )
    assert execution.tokens_in is None
    assert execution.tokens_out is None
    assert not hasattr(runtime, "bind_runtime_workspace")
    assert not hasattr(runtime, "release_runtime_workspace_binding")
    assert not hasattr(runtime, "normalize_execution_result")


def test_case_c_openclaw_shaped_metadata_is_optional_for_control_flow(
    db_session, tmp_path
):
    step = _step(
        description="Compare normalized backend metadata with direct text",
        commands=["custom-metadata-control"],
    )

    class NormalizedRuntime(DirectRuntimeStub):
        def normalize_execution_result(
            self, result: dict[str, Any], *, role: str, duration_seconds: float
        ) -> RuntimeBackendResult:
            return RuntimeBackendResult(
                backend_id=self.backend,
                role=role,
                success=result.get("status") == "completed",
                exit_reason="completed",
                output=str(result.get("output") or ""),
                duration_seconds=duration_seconds,
                tokens_in=11,
                tokens_out=7,
                token_source="provider-test",
            )

    raw = {
        "status": "completed",
        "output": json.dumps(
            {
                "status": "completed",
                "output": "same control-flow result",
                "files_changed": [],
            }
        ),
    }
    normalized_runtime = _with_agent_runtime_topology(
        NormalizedRuntime(raw, backend="local_openclaw")
    )
    direct_runtime = _with_agent_runtime_topology(DirectRuntimeStub(raw))
    normalized_ctx, normalized_execution, _ = _make_loop_context(
        db_session, tmp_path / "normalized", step=step, runtime=normalized_runtime
    )
    direct_ctx, direct_execution, _ = _make_loop_context(
        db_session, tmp_path / "direct", step=step, runtime=direct_runtime
    )

    assert _run_loop(normalized_ctx) == {"status": "completed"}
    assert _run_loop(direct_ctx) == {"status": "completed"}
    assert normalized_ctx.orchestration_state.current_step_index == 1
    assert direct_ctx.orchestration_state.current_step_index == 1
    assert normalized_execution.tokens_in == 11
    assert normalized_execution.tokens_out == 7
    assert direct_execution.tokens_in is None
    assert direct_execution.tokens_out is None


def test_case_d_direct_runtime_failure_uses_existing_terminal_failure_semantics(
    db_session, tmp_path, monkeypatch
):
    step = _step(
        description="Residual direct reasoning fails at the provider boundary",
        commands=["custom-direct-failure"],
    )
    runtime = _with_agent_runtime_topology(
        DirectRuntimeStub(
            {
                "status": "failed",
                "output": "provider rejected the residual step",
                "error": "provider rejected the residual step",
            }
        )
    )
    ctx, execution, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    # Force the real loop through its bounded terminal-failure branch without
    # invoking a recovery provider or waiting for retries.
    ctx.orchestration_state.execution_recovery_attempts = 99
    import app.services.orchestration.phases.execution_loop as execution_loop

    monkeypatch.setattr(execution_loop, "MAX_STEP_ATTEMPTS", 0)

    result = _run_loop(ctx)

    assert result["status"] == "failed"
    assert result["reason"] == "verification_failed"
    assert len(runtime.calls) == 1
    assert ctx.orchestration_state.status.value == "aborted"
    assert "provider rejected" in ctx.orchestration_state.abort_reason
    assert not (workspace / "provider-created.txt").exists()
    db_session.refresh(execution)
    assert execution.status == TaskStatus.FAILED
    assert runtime.pause_calls == 0
    assert runtime.resume_calls == 0
    assert runtime.stop_calls == 0


def test_case_e_cancellation_is_orchestrator_owned_and_skips_provider(
    db_session, tmp_path
):
    step = _step(
        description="A step after an operator pause",
        commands=["custom-cancelled-step"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, execution, _ = _make_loop_context(
        db_session,
        tmp_path,
        step=step,
        runtime=runtime,
        session_status="paused",
    )

    result = _run_loop(ctx)

    assert result["status"] == "cancelled"
    assert result["reason"] == "session_paused"
    assert runtime.calls == []
    db_session.refresh(execution)
    assert execution.status == TaskStatus.CANCELLED
    assert ctx.orchestration_state.current_step_index == 0
    assert runtime.pause_calls == 0
    assert runtime.resume_calls == 0
    assert runtime.stop_calls == 0


@pytest.mark.parametrize(
    ("backend", "model"),
    [
        ("direct_ollama", "qwen2.5-coder:7b"),
        ("openai_chat_completions", "qwen-local"),
    ],
)
def test_lowram_single_model_configuration_uses_structured_topology_only(
    db_session, monkeypatch, backend, model
):
    monkeypatch.setattr(settings, "AGENT_BACKEND", backend)
    monkeypatch.setattr(settings, "PLANNING_BACKEND", backend)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", backend)
    monkeypatch.setattr(settings, "PLANNER_MODEL", model)
    monkeypatch.setattr(settings, "EXECUTION_MODEL", model)
    monkeypatch.setattr(settings, "AGENT_MODEL", model)
    monkeypatch.setattr(settings, "PLANNING_ADAPTATION_PROFILE", "ollama_default")
    monkeypatch.setattr(settings, "EXECUTION_ADAPTATION_PROFILE", "ollama_default")
    if backend == "direct_ollama":
        monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", model)
    else:
        monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_MODEL", model)
        monkeypatch.setattr(
            settings, "OPENAI_CHAT_COMPLETIONS_BASE_URL", "http://direct.test/v1"
        )

    import app.services.agents.agent_runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "_read_openclaw_model_catalog",
        lambda: pytest.fail("direct low-resource validation consulted OpenClaw"),
    )
    planning = resolve_runtime_configuration(db_session, BackendRole.PLANNING)
    execution = resolve_runtime_configuration(db_session, BackendRole.EXECUTION)
    assert planning.backend_name == execution.backend_name == backend
    assert planning.model_family == execution.model_family == model
    assert planning.adaptation_profile == execution.adaptation_profile

    planning_readiness = validate_runtime_capabilities(
        get_backend_descriptor(backend),
        BackendRole.PLANNING,
        effective_context_tokens=200_000,
        dispatch=False,
    )
    execution_readiness = validate_runtime_capabilities(
        get_backend_descriptor(backend),
        BackendRole.EXECUTION,
        effective_context_tokens=200_000,
        dispatch=False,
    )
    assert planning_readiness["role"] == "planning"
    assert execution_readiness["execution_topology"] == (
        ExecutionTopology.STRUCTURED_ORCHESTRATOR.value
    )
    with pytest.raises(RuntimeCapabilityError) as exc_info:
        validate_runtime_capabilities(
            get_backend_descriptor(backend),
            BackendRole.EXECUTION,
            effective_context_tokens=200_000,
            dispatch=True,
            execution_topology=ExecutionTopology.AGENT_RUNTIME,
        )
    assert exc_info.value.code == "provider_endpoint_incompatible"


def test_openclaw_agent_topology_and_direct_rejections_remain_provider_free():
    for topology in ExecutionTopology:
        readiness = validate_runtime_capabilities(
            get_backend_descriptor("local_openclaw"),
            BackendRole.EXECUTION,
            effective_context_tokens=200_000,
            dispatch=False,
            execution_topology=topology,
        )
        assert readiness["execution_topology"] == topology.value

    for backend in ("direct_ollama", "openai_chat_completions"):
        with pytest.raises(RuntimeCapabilityError) as exc_info:
            validate_runtime_capabilities(
                get_backend_descriptor(backend),
                BackendRole.EXECUTION,
                effective_context_tokens=200_000,
                dispatch=True,
                execution_topology=ExecutionTopology.AGENT_RUNTIME,
            )
        assert exc_info.value.code == "provider_endpoint_incompatible"


def test_execution_authority_loader_rejects_missing_apa_before_runtime(
    db_session, tmp_path
):
    step = _step(
        description="Missing APA must block execution admission",
        commands=["custom-missing-apa"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, _, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    db_session.query(TaskCheckpoint).delete()
    db_session.commit()

    result = _run_loop(ctx)

    assert result["status"] == "failed"
    assert result["reason"] == "execution_authority_admission_failed"
    assert runtime.calls == []
    assert workspace.exists()


def test_provider_text_cannot_authorize_or_mutate_an_unlisted_path(
    db_session, tmp_path
):
    step = _step(
        description="Provider output must remain non-authorizing text",
        commands=["custom-adversarial-output"],
    )
    runtime = _with_agent_runtime_topology(
        DirectRuntimeStub(
            {
                "status": "completed",
                "output": json.dumps(
                    {
                        "status": "completed",
                        "output": "write_file unauthorized.txt with secret content",
                    }
                ),
            }
        )
    )
    ctx, _, workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert runtime.calls
    assert not (workspace / "unauthorized.txt").exists()
    assert ctx.orchestration_state.execution_results[-1].files_changed == []
