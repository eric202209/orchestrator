"""POST33-EXEC3R1 evaluation harness.

The harness follows the production seam:

    PlanningSessionService -> PlanningSessionService.commit
        -> PlanCommitService -> queue_task_for_session
        -> execute_orchestration_task -> execute_step_loop

Stage A replaces only provider boundaries. It does not add parser behavior,
construct an APA, or substitute a worker/execution lifecycle.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery.result import allow_join_result, denied_join_result

from app.config import settings
from app.models import (
    ExecutionTaskRuntimeLease,
    Plan,
    Project,
    PlanningSession,
    Session as SessionModel,
    SessionTask,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.session.session_runtime_service import queue_task_for_session


def _planning_artifacts(project_name: str) -> dict[str, str]:
    markdown = "\n".join(
        [
            f"# Project: {project_name}",
            "",
            "## Task List",
            "- [ ] TASK_START: Create tiny calculation source | Create tiny_calc.py so answer() returns 42 and run its focused test | order=1 | P1 | effort=small | stage=execute | profile=full_lifecycle",
        ]
    )
    return {
        "requirements": "# Requirements\n\n- Change answer() from 41 to 42.",
        "design": "# Design\n\n- Keep the change limited to tiny_calc.py.",
        "implementation_plan": "# Implementation Plan\n\n1. Update tiny_calc.py.\n2. Run the focused test.",
        "planner_markdown": markdown,
    }


def _execution_plan() -> list[dict[str, Any]]:
    return [
        {
            "step_number": 1,
            "description": "Create tiny_calc.py with answer() returning 42.",
            "commands": [],
            "verification": "python3 -m pytest -q test_tiny_calc.py",
            "rollback": None,
            "expected_files": ["tiny_calc.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tiny_calc.py",
                    "content": "def answer():\n    return 42\n",
                }
            ],
        }
    ]


@dataclass
class ProviderFreeExecutionRuntime:
    """Direct-runtime-shaped boundary stub with no OpenClaw methods."""

    plan: list[dict[str, Any]]
    backend: str = "openai_chat_completions"
    model: str = "qwen-local"

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.backend_descriptor = SimpleNamespace(name=self.backend)

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if "READ-ONLY DISCOVERY ONLY" in prompt:
            self.calls.append({"kind": "discovery", "prompt": prompt, **kwargs})
            return {
                "status": "completed",
                "output": {"action": "read_file", "path": "test_tiny_calc.py"},
            }
        if "independent QA evaluator" in prompt:
            self.calls.append(
                {"kind": "completion_evaluation", "prompt": prompt, **kwargs}
            )
            return {"status": "completed", "output": json.dumps(self.plan)}
        self.calls.append({"kind": "execution_reasoning", "prompt": prompt, **kwargs})
        return {"status": "completed", "output": json.dumps(self.plan)}

    async def invoke_prompt(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "residual_reasoning", "prompt": prompt, **kwargs})
        return {
            "status": "completed",
            "output": json.dumps(
                {
                    "status": "completed",
                    "output": "provider-free residual reasoning",
                    "files_changed": [],
                }
            ),
        }

    async def create_session(self, *args: Any, **kwargs: Any) -> str:
        return "provider-free-direct-session"

    async def pause_session(self) -> None:
        return None

    async def resume_session(self, checkpoint_name: str | None = None) -> str:
        return "provider-free-direct-resume"

    async def stop_session(self) -> None:
        return None

    async def get_session_context(self) -> dict[str, Any]:
        return {"backend": self.backend, "model": self.model}

    def get_backend_metadata(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model_family": self.model,
            "role": "execution",
            "capabilities": {"supports_step_reasoning": True},
        }

    def reports_context_overflow(self, result: dict[str, Any] | None) -> bool:
        return False


def _make_worker_sync(monkeypatch, db_session_factory):
    import app.tasks.worker as worker_module

    real_task = worker_module.execute_orchestration_task

    class SyncResult:
        def __init__(self, result: Any):
            self.id = "post33-exec3r1-provider-free-worker"
            self.result = result

    class SyncTask:
        @staticmethod
        def delay(**kwargs: Any) -> SyncResult:
            applied = real_task.apply(kwargs=kwargs)
            # Eager task execution runs the real Celery task body in-process.
            # Celery's task trace may leave synchronous-result joins denied,
            # but this adapter is collecting an already-completed eager result,
            # not launching a child task from production code.
            with allow_join_result():
                outcome = applied.get(propagate=True)
            return SyncResult(outcome)

    monkeypatch.setattr(worker_module, "get_db_session", db_session_factory)
    monkeypatch.setattr(worker_module, "execute_orchestration_task", SyncTask)
    return worker_module


def _openclaw_process_snapshot() -> dict[int, str]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    )
    snapshot: dict[int, str] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        command_line = fields[1]
        if "openclaw-gateway" not in command_line and not any(
            token in {"openclaw", "openclaw-gateway"} for token in command_line.split()
        ):
            continue
        try:
            snapshot[int(fields[0])] = command_line
        except ValueError:
            continue
    return snapshot


def _openclaw_config_fingerprint() -> str:
    candidates = (
        Path("/root/.openclaw/openclaw.json"),
        Path("/root/.openclaw/config.json"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    return "<missing>"


def _git_workspace_snapshot(path: Path) -> dict[str, Any]:
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    files: dict[str, str] = {}
    for relative in ("tiny_calc.py", "test_tiny_calc.py"):
        target = path / relative
        if target.is_file():
            files[relative] = target.read_text(encoding="utf-8")
    return {
        "status": status.stdout,
        "diff": diff.stdout,
        "files": files,
        "exists": path.exists(),
    }


def _active_runtime_counts(db) -> dict[str, int]:
    return {
        "active_task_executions": db.query(TaskExecution)
        .filter(TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
        .count(),
        "active_sessions": db.query(SessionModel)
        .filter(SessionModel.status.in_(["pending", "running", "active"]))
        .count(),
        "active_planning_sessions": db.query(PlanningSession)
        .filter(PlanningSession.status.in_(["active", "waiting_for_input"]))
        .count(),
        "active_runtime_leases": db.query(ExecutionTaskRuntimeLease)
        .filter(ExecutionTaskRuntimeLease.lease_status == "active")
        .count(),
    }


def _lock_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and ("lock" in path.name.lower() or "locks" in path.parts)
    )


def _authority_paths(value: Any) -> list[str]:
    paths: list[str] = []

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                if "path" in str(key).lower() and isinstance(child, str):
                    paths.append(child)
                else:
                    visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return sorted(set(paths))


@pytest.fixture
def provider_free_harness(db_session_factory, isolated_workspace_root, monkeypatch):
    """Build the isolated Stage A lane using deterministic provider stubs."""

    monkeypatch.setattr(settings, "INLINE_PLANNING", True)
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "AGENT_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "PLANNING_ADAPTATION_PROFILE", "ollama_default")
    monkeypatch.setattr(settings, "EXECUTION_ADAPTATION_PROFILE", "ollama_default")
    # CI intentionally runs without the developer .env. The provider-free
    # execution stub has no provider catalog from which to derive capacity, so
    # the harness must declare the same verified execution-context contract
    # that production dispatch requires.
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 64_000)
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 64_000)
    monkeypatch.setattr(settings, "PLANNING_REPAIR_ENABLED", False)
    monkeypatch.setattr(settings, "RUNTIME_WORKSPACE_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)

    db = db_session_factory()
    workspace = isolated_workspace_root / "post33-exec3r1-project"
    workspace.mkdir(parents=True)
    (workspace / "test_tiny_calc.py").write_text(
        "from tiny_calc import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=post33-exec3r1@example.invalid",
            "-c",
            "user.name=POST33-EXEC3R1",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=workspace,
        check=True,
    )

    project = Project(
        name="POST33-EXEC3R1 tiny_calc",
        description="Provider-free structured execution certification fixture",
        workspace_path=str(workspace),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    execution_runtime = ProviderFreeExecutionRuntime(_execution_plan())
    import app.tasks.worker as worker_module

    monkeypatch.setattr(
        worker_module,
        "create_agent_runtime",
        lambda *args, **kwargs: execution_runtime,
    )
    _make_worker_sync(monkeypatch, db_session_factory)

    def fake_planning(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "output": json.dumps(_planning_artifacts(project.name)),
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_planning)

    yield {
        "db": db,
        "project": project,
        "workspace": workspace,
        "execution_runtime": execution_runtime,
    }
    db.close()


def test_exec3_failure_reproduced_provider_free():
    from app.services.orchestration.planning.planner import (
        PlannerService as FailedClass,
    )

    with pytest.raises(AttributeError, match="parse_markdown"):
        FailedClass.parse_markdown("## Task List\n- change tiny_calc.py")


def test_exec3r1_provider_free_full_lifecycle_uses_production_seam(
    provider_free_harness,
):
    db = provider_free_harness["db"]
    project = provider_free_harness["project"]
    workspace = provider_free_harness["workspace"]
    runtime = provider_free_harness["execution_runtime"]

    planning_service = PlanningSessionService(db)
    planning_session = planning_service.start_session(
        project,
        "Create tiny_calc.py so answer() returns 42 and run python3 -m pytest -q test_tiny_calc.py.",
        skip_clarification=True,
    )
    assert planning_session.status == "completed"
    assert planning_session.finalized_plan_id is None

    # Production conversion/commit seam. The harness does not call
    # parse_markdown and does not construct authority itself.
    committed_session, plan, committed_tasks = planning_service.commit(
        planning_session.id
    )
    assert committed_session.id == planning_session.id
    assert isinstance(plan, Plan)
    assert committed_tasks and committed_tasks[0].plan_id == plan.id

    execution_session = SessionModel(
        project_id=project.id,
        name="POST33-EXEC3R1 execution",
        description="Provider-free full lifecycle",
        status="pending",
        execution_mode="manual",
    )
    db.add(execution_session)
    db.commit()
    db.refresh(execution_session)

    # Celery's worker trace normally enables this guard.  The provider-free
    # seam must remain valid when another test leaves that task-context guard
    # active, because it executes the production task eagerly in-process.
    with denied_join_result():
        queue_result = queue_task_for_session(
            db,
            execution_session,
            committed_tasks[0].id,
            timeout_seconds=120,
        )
    assert queue_result["task_execution_id"]

    db.refresh(committed_tasks[0])
    db.refresh(execution_session)
    task_execution = (
        db.query(TaskExecution)
        .filter(TaskExecution.id == queue_result["task_execution_id"])
        .one()
    )
    checkpoints = (
        db.query(TaskCheckpoint)
        .filter(
            TaskCheckpoint.session_id == execution_session.id,
            TaskCheckpoint.task_id == committed_tasks[0].id,
            TaskCheckpoint.checkpoint_type == "validation_plan",
        )
        .all()
    )
    assert checkpoints
    checkpoint_payload = json.loads(checkpoints[-1].state_snapshot)
    assert checkpoint_payload["details"]["accepted_path_authority"]
    assert task_execution.status == TaskStatus.DONE
    assert committed_tasks[0].status == TaskStatus.DONE
    assert execution_session.status == "completed"
    assert (
        (workspace / "tiny_calc.py").read_text(encoding="utf-8").endswith("return 42\n")
    )
    assert (workspace / "test_tiny_calc.py").exists()
    assert runtime.calls
    assert [call["kind"] for call in runtime.calls] == [
        "discovery",
        "execution_reasoning",
        "completion_evaluation",
    ]
    assert (
        db.query(SessionTask)
        .filter(SessionTask.session_id == execution_session.id)
        .count()
        == 1
    )


@pytest.fixture
def live_harness(db_session_factory, isolated_workspace_root, monkeypatch):
    """Build one private live lane; no application or persistent config changes."""

    monkeypatch.setattr(settings, "INLINE_PLANNING", True)
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "AGENT_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "PLANNING_ADAPTATION_PROFILE", "ollama_default")
    monkeypatch.setattr(settings, "EXECUTION_ADAPTATION_PROFILE", "ollama_default")
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 64_000)
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 64_000)
    monkeypatch.setattr(settings, "RUNTIME_WORKSPACE_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "OPENAI_CHAT_COMPLETIONS_BASE_URL",
        settings.PLANNING_DIRECT_BASE_URL,
    )
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "DEMO_MODE", False)

    db = db_session_factory()
    workspace = isolated_workspace_root / "post33-exec3r1-live-project"
    workspace.mkdir(parents=True)
    (workspace / "test_tiny_calc.py").write_text(
        "from tiny_calc import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=post33-exec3r1@example.invalid",
            "-c",
            "user.name=POST33-EXEC3R1",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=workspace,
        check=True,
    )

    project = Project(
        name="POST33-EXEC3R1 LIVE tiny_calc",
        description="Fresh isolated single-model structured execution certification",
        workspace_path=str(workspace),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    worker_module = _make_worker_sync(monkeypatch, db_session_factory)
    yield {
        "db": db,
        "project": project,
        "workspace": workspace,
        "worker_module": worker_module,
    }
    db.close()


@pytest.mark.live
def test_exec3r1_fresh_live_single_model_full_lifecycle(live_harness, monkeypatch):
    if os.environ.get("POST33_EXEC3R1_LIVE") != "1":
        pytest.skip("POST33_EXEC3R1_LIVE=1 is required for the fresh live gate")

    db = live_harness["db"]
    project = live_harness["project"]
    workspace = live_harness["workspace"]
    worker_module = live_harness["worker_module"]
    evidence: dict[str, Any] = {
        "gate": "POST33-EXEC3R1",
        "fixture": "tiny_calc",
        "expected_behavior_before": (
            "tiny_calc.py is absent; test_tiny_calc.py must fail with import error"
        ),
        "expected_behavior_fixed": (
            "tiny_calc.py answer() returns 42 and the focused pytest passes"
        ),
        "workspace_before": _git_workspace_snapshot(workspace),
        "openclaw_processes_before": _openclaw_process_snapshot(),
        "openclaw_config_fingerprint_before": _openclaw_config_fingerprint(),
        "counts_before": _active_runtime_counts(db),
        "provider_calls": [],
        "runtime_creations": [],
        "runtime_workspace_capture": {},
        "structured_file_op_count": 0,
        "local_command_count": 0,
        "verification_command_count": 0,
        "residual_reasoning_call_count": 0,
        "phase": "planning",
        "errors": [],
    }
    execution_session = None
    task_execution = None
    committed_task = None

    from app.services.agents import agent_runtime as agent_runtime_module
    from app.services.agents.agent_runtime import (
        BackendRole,
        resolve_runtime_configuration,
    )
    from app.services.orchestration.execution.executor import ExecutorService
    from app.services.orchestration.phases import execution_local_steps
    from app.services.orchestration.phases import execution_loop

    planning_configuration = resolve_runtime_configuration(db, BackendRole.PLANNING)
    execution_configuration = resolve_runtime_configuration(db, BackendRole.EXECUTION)
    evidence["planning_configuration_before"] = planning_configuration.to_dict()
    evidence["execution_configuration_before"] = execution_configuration.to_dict()
    evidence["persistent_role_graph_before"] = {
        "planning": planning_configuration.to_dict(),
        "execution": execution_configuration.to_dict(),
    }

    real_create_runtime = agent_runtime_module.create_agent_runtime

    def recording_create_runtime(*args: Any, **kwargs: Any):
        runtime = real_create_runtime(*args, **kwargs)
        configuration = getattr(runtime, "runtime_configuration", None)
        evidence["runtime_creations"].append(
            {
                "role": getattr(configuration, "role", kwargs.get("role")),
                "backend": getattr(configuration, "backend_name", None),
                "model": getattr(configuration, "model_family", None),
                "profile": getattr(configuration, "adaptation_profile", None),
                "execution_topology": str(
                    kwargs.get(
                        "execution_topology", "structured_orchestrator_execution"
                    )
                ),
            }
        )
        return runtime

    monkeypatch.setattr(
        agent_runtime_module, "create_agent_runtime", recording_create_runtime
    )
    monkeypatch.setattr(worker_module, "create_agent_runtime", recording_create_runtime)

    import httpx

    real_post = httpx.AsyncClient.post

    async def recording_post(client: Any, url: Any, *args: Any, **kwargs: Any):
        if len(evidence["provider_calls"]) >= 4:
            raise AssertionError("POST33-EXEC3R1 provider call budget exceeded")
        payload = kwargs.get("json") or {}
        messages = payload.get("messages") or []
        system = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        if "precise software development assistant" in system:
            call_kind = "execution_step_reasoning"
        elif evidence["phase"] == "execution":
            call_kind = "residual_execution_reasoning"
        else:
            call_kind = "planning"
        evidence["provider_calls"].append(
            {
                "url": str(url),
                "model": payload.get("model"),
                "has_native_tools": bool(payload.get("tools")),
                "phase": evidence["phase"],
                "kind": call_kind,
            }
        )
        return await real_post(client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", recording_post)

    real_execute_file_ops = ExecutorService.execute_file_ops

    def recording_execute_file_ops(project_dir: Path, ops: Any, **kwargs: Any):
        if isinstance(ops, list):
            evidence["structured_file_op_count"] += len(ops)
        return real_execute_file_ops(project_dir, ops, **kwargs)

    monkeypatch.setattr(ExecutorService, "execute_file_ops", recording_execute_file_ops)

    real_local_shell = execution_loop._execute_local_shell_commands_step

    def recording_local_shell(*, commands: list[Any], **kwargs: Any):
        result = real_local_shell(commands=commands, **kwargs)
        if result is not None:
            evidence["local_command_count"] += len(
                [str(command).strip() for command in commands if str(command).strip()]
            )
        return result

    monkeypatch.setattr(
        execution_loop, "_execute_local_shell_commands_step", recording_local_shell
    )

    real_verification = execution_local_steps.execute_verification_command

    def recording_verification(*args: Any, **kwargs: Any):
        evidence["verification_command_count"] += 1
        return real_verification(*args, **kwargs)

    monkeypatch.setattr(
        execution_local_steps,
        "execute_verification_command",
        recording_verification,
    )
    monkeypatch.setitem(
        execution_loop.assess_step_execution.__globals__,
        "execute_verification_command",
        recording_verification,
    )

    real_dispose = worker_module._dispose_runtime_workspace_safely

    def recording_dispose(sandbox: Any, **kwargs: Any):
        capture = evidence["runtime_workspace_capture"]
        if not capture:
            runtime_path = Path(sandbox.path)
            capture.update(
                {
                    "path": str(runtime_path),
                    "canonical_workspace": str(workspace),
                    "diff_before_dispose": _git_workspace_snapshot(runtime_path),
                }
            )
            if runtime_path.exists():
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "test_tiny_calc.py"],
                    cwd=runtime_path,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                capture["independent_ground_truth_after"] = {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
        return real_dispose(sandbox, **kwargs)

    monkeypatch.setattr(
        worker_module, "_dispose_runtime_workspace_safely", recording_dispose
    )

    try:
        planning_service = PlanningSessionService(db)
        planning_session = planning_service.start_session(
            project,
            (
                "Create tiny_calc.py in the project workspace so answer() returns 42. "
                "The existing test_tiny_calc.py is the focused verification and must pass. "
                "Use one narrowly scoped task and preserve the existing test."
            ),
            skip_clarification=True,
        )
        db.refresh(planning_session)
        evidence["planning_session_id"] = planning_session.id
        evidence["planning_session_status"] = planning_session.status
        evidence["planning_first_plan_valid"] = planning_session.status == "completed"
        assert planning_session.status == "completed"

        committed_session, plan, committed_tasks = planning_service.commit(
            planning_session.id
        )
        del committed_session
        assert isinstance(plan, Plan)
        assert committed_tasks
        committed_task = committed_tasks[0]
        evidence["planning_artifact_parsed"] = True
        evidence["plan_id"] = plan.id
        evidence["plan_commit_reached"] = True
        evidence["plan_commit_succeeded"] = True
        evidence["accepted_plan_paths"] = [
            str(getattr(task, "title", "")) for task in committed_tasks
        ]

        execution_session = SessionModel(
            project_id=project.id,
            name="POST33-EXEC3R1 LIVE synthetic execution",
            description="Fresh isolated qwen-local structured orchestrator lane",
            status="pending",
            execution_mode="manual",
        )
        db.add(execution_session)
        db.commit()
        db.refresh(execution_session)
        evidence["execution_session_id"] = execution_session.id

        evidence["phase"] = "execution"
        queue_result = queue_task_for_session(
            db,
            execution_session,
            committed_task.id,
            timeout_seconds=300,
        )
        evidence["queue_result"] = {
            key: str(value) for key, value in queue_result.items()
        }
        task_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == queue_result["task_execution_id"])
            .one()
        )
        db.refresh(execution_session)
        db.refresh(committed_task)
        evidence["task_id"] = committed_task.id
        evidence["task_execution_id"] = task_execution.id
        evidence["task_execution_status"] = str(task_execution.status)
        evidence["task_status"] = str(committed_task.status)
        evidence["session_status"] = execution_session.status
        checkpoints = (
            db.query(TaskCheckpoint)
            .filter(
                TaskCheckpoint.session_id == execution_session.id,
                TaskCheckpoint.task_id == committed_task.id,
                TaskCheckpoint.checkpoint_type == "validation_plan",
            )
            .all()
        )
        checkpoint_payload = (
            json.loads(checkpoints[-1].state_snapshot) if checkpoints else {}
        )
        checkpoint_details = checkpoint_payload.get("details") or {}
        authority = checkpoint_details.get("accepted_path_authority")
        evidence["apa_created"] = bool(authority)
        evidence["apa_paths"] = _authority_paths(authority)
        evidence["plan_verification_reached"] = bool(checkpoints)
        evidence["plan_verification_succeeded"] = bool(authority)
        evidence["source_version_fencing"] = any(
            key in checkpoint_details
            for key in (
                "source_plan_hash",
                "source_commit_identity",
                "source_version",
                "plan_source_hash",
            )
        )
        evidence["accepted_plan_paths"] = evidence["apa_paths"]
    except Exception as exc:
        db.rollback()
        evidence["errors"].append(repr(exc))
    finally:
        evidence["workspace_after"] = _git_workspace_snapshot(workspace)
        canonical_status_lines = [
            line
            for line in evidence["workspace_after"]["status"].splitlines()
            if line and ".agent" not in line
        ]
        evidence["canonical_repo_mutated_directly"] = bool(
            canonical_status_lines or evidence["workspace_after"]["diff"]
        )
        evidence["openclaw_processes_after"] = _openclaw_process_snapshot()
        evidence["openclaw_config_fingerprint_after"] = _openclaw_config_fingerprint()
        evidence["counts_after"] = _active_runtime_counts(db)
        evidence["mutation_lock_files_after"] = _lock_files(workspace / ".agent")
        evidence["provider_call_count"] = len(evidence["provider_calls"])
        evidence["planning_provider_calls"] = sum(
            call["phase"] == "planning" for call in evidence["provider_calls"]
        )
        evidence["execution_provider_calls"] = sum(
            call["phase"] == "execution" for call in evidence["provider_calls"]
        )
        evidence["residual_reasoning_call_count"] = sum(
            call["kind"] == "residual_execution_reasoning"
            for call in evidence["provider_calls"]
        )
        evidence["openclaw_new_pids"] = sorted(
            set(evidence["openclaw_processes_after"])
            - set(evidence["openclaw_processes_before"])
        )
        evidence["runtime_creations"] = [
            {
                **creation,
                "role": getattr(creation["role"], "value", creation["role"]),
            }
            for creation in evidence["runtime_creations"]
        ]
        evidence["execution_selected_topology"] = (
            "STRUCTURED_ORCHESTRATOR"
            if any(
                creation.get("role") == "execution"
                and "structured_orchestrator" in creation.get("execution_topology", "")
                for creation in evidence["runtime_creations"]
            )
            else None
        )
        evidence_dir = Path("docs/roadmap/reports/evidence/post33-exec3r1")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / "fresh-live-single-model.json"
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )

    assert not evidence["errors"], evidence["errors"]
    assert evidence["planning_provider_calls"] >= 1
    assert evidence["provider_call_count"] <= 4
    assert all(call["model"] == "qwen-local" for call in evidence["provider_calls"])
    assert all(not call["has_native_tools"] for call in evidence["provider_calls"])
    assert evidence["planning_artifact_parsed"]
    assert evidence["plan_commit_succeeded"]
    assert evidence["runtime_workspace_capture"]
    assert not evidence["canonical_repo_mutated_directly"]
    assert not evidence["openclaw_new_pids"]
    assert evidence["counts_after"]["active_task_executions"] == 0
    assert evidence["counts_after"]["active_sessions"] == 0
    assert evidence["counts_after"]["active_runtime_leases"] == 0
    assert evidence["structured_file_op_count"] >= 1
    assert evidence["verification_command_count"] >= 1
    assert evidence["apa_created"]
    assert evidence["plan_verification_succeeded"]
    assert evidence["source_version_fencing"]
    assert evidence["execution_selected_topology"] == "STRUCTURED_ORCHESTRATOR"
    assert (
        evidence["runtime_workspace_capture"]["independent_ground_truth_after"][
            "returncode"
        ]
        == 0
    )
