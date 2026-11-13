"""P41-PRODUCT-11A: offline provider-dependency audit of the PRODUCT-01 lifecycle.

Runs the production Task API -> Session queue -> Worker ``.apply`` -> Planning ->
Validator path with a fail-closed recording runtime. Exactly one fixed Planning
response is served; every other model-shaped call is recorded and refused.

The audit stops at the first structured file operation: file ops are refused
(never applied), and every non-``git`` subprocess and every network connection
is refused, so no generated operation or command runs in this container.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import traceback
import uuid
from types import SimpleNamespace
from typing import Any

from celery.result import allow_join_result
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.dependencies import get_current_active_user, get_current_user
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.agents import agent_runtime
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.phases import completion_summary
from app.services.orchestration.planning import read_only_discovery
from app.services.session import session_execution_service
from app.services.session.session_runtime_service import queue_task_for_session
from app.tasks import worker
from app.tests.test_p41_product_01_deterministic_e2e import (
    TASK_DESCRIPTION,
    _calculator_plan,
    _read_jsonl,
)

_PLANNER_FILE = "app/services/orchestration/planning/planner.py"
_AUTHORIZED_PLANNING_FUNCTION = "_execute_task_with_planning_lock"


class ProviderCallRefused(RuntimeError):
    """Distinguishable fail-closed diagnostic for an unauthorized model call."""


def _origin() -> list[str]:
    """Nearest production frames (outside the agents layer and this file)."""

    frames = []
    for frame in reversed(traceback.extract_stack()[:-2]):
        path = frame.filename.replace(os.sep, "/")
        if "/app/" not in path or "/app/tests/" in path:
            continue
        if "/app/services/agents/" in path:
            continue
        frames.append(f"{path.split('/app/', 1)[1]}:{frame.name}:{frame.lineno}")
        if len(frames) == 3:
            break
    return frames


class _FailClosedRecordingRuntime:
    """Serves one authorized Planning response; refuses everything else."""

    backend = "openai_chat_completions"
    model = "qwen-local"

    def __init__(
        self,
        ledger: list[dict[str, Any]],
        designated: tuple[str, ...] = ("planning",),
    ) -> None:
        self.ledger = ledger
        self.designated = designated
        self.plan = _calculator_plan()
        self.backend_descriptor = SimpleNamespace(name=self.backend)

    def _record(self, method: str, prompt: str, served: bool) -> dict[str, Any]:
        origin = _origin()
        if "READ-ONLY DISCOVERY ONLY" in prompt:
            kind = "read_only_discovery"
        elif "independent QA evaluator" in prompt:
            kind = "completion_evaluation"
        elif method == "invoke_prompt":
            kind = "invoke_prompt"
        else:
            kind = "execute_task"
        entry = {
            "method": method,
            "kind": kind,
            "origin": origin,
            "served": served,
            "prompt_chars": len(prompt),
            "prompt_head": " ".join(prompt.split())[:120],
        }
        self.ledger.append(entry)
        return entry

    def _is_authorized_planning(self, prompt: str) -> bool:
        origin = _origin()
        return (
            bool(origin)
            and origin[0].startswith(
                f"{_PLANNER_FILE.split('app/', 1)[1]}:"
                f"{_AUTHORIZED_PLANNING_FUNCTION}:"
            )
            and "READ-ONLY DISCOVERY ONLY" not in prompt
            and TASK_DESCRIPTION in prompt
            and not any(
                call["served"] and call["kind"] != "read_only_discovery"
                for call in self.ledger
            )
        )

    async def execute_task(self, prompt: str, *args: Any, **kwargs: Any):
        if (
            "read_only_discovery" in self.designated
            and "READ-ONLY DISCOVERY ONLY" in prompt
        ):
            # Variant B only: PRODUCT-01's existing read-only action request.
            # It asks to read a file; it is not a completion or success claim.
            self._record("execute_task", prompt, served=True)
            return {
                "status": "completed",
                "output": json.dumps({"action": "read_file", "path": "calculator.py"}),
            }
        if self._is_authorized_planning(prompt):
            self._record("execute_task", prompt, served=True)
            return {"status": "completed", "output": json.dumps(self.plan)}
        entry = self._record("execute_task", prompt, served=False)
        raise ProviderCallRefused(
            f"P41-PRODUCT-11A refused {entry['kind']} from {entry['origin'][:1]}"
        )

    async def invoke_prompt(self, prompt: str, *args: Any, **kwargs: Any):
        entry = self._record("invoke_prompt", prompt, served=False)
        raise ProviderCallRefused(
            f"P41-PRODUCT-11A refused {entry['kind']} from {entry['origin'][:1]}"
        )

    async def create_session(self, *args: Any, **kwargs: Any) -> str:
        return "p41-product-11a-provider-free"

    async def pause_session(self) -> None:
        return None

    async def resume_session(self, checkpoint_name: str | None = None) -> str:
        return "p41-product-11a-provider-free-resume"

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


# The fixed read-only inspection command that production Plan shaping inserts
# as step 1 (planning_plan_shape.py). It is an orchestrator constant, not
# Planning-response content, and is allowed only inside the disposable root.
_ORCHESTRATOR_INSPECTION_COMMAND = "rg --files . | sort"


def _install_execution_guards(
    monkeypatch, tally: dict[str, list], workspace_root: Any
) -> None:
    real_popen = subprocess.Popen
    allowed_root = os.path.realpath(str(workspace_root))

    def guarded_popen(args, *popen_args, **popen_kwargs):
        argv = args if isinstance(args, (list, tuple)) else [str(args)]
        executable = os.path.basename(str(argv[0])) if argv else ""
        cwd = os.path.realpath(str(popen_kwargs.get("cwd") or os.getcwd()))
        inside_root = cwd == allowed_root or cwd.startswith(allowed_root + os.sep)
        allowed = (
            isinstance(args, (list, tuple))
            and executable == "git"
            and not popen_kwargs.get("shell")
        ) or (
            isinstance(args, str)
            and args == _ORCHESTRATOR_INSPECTION_COMMAND
            and bool(popen_kwargs.get("shell"))
            and inside_root
        )
        tally["subprocess"].append(
            {
                "argv": [str(part) for part in argv][:6],
                "shell": bool(popen_kwargs.get("shell")),
                "allowed": allowed,
                "origin": _origin(),
            }
        )
        if not allowed:
            # The refused command never runs. A fixed, test-authored process
            # stands in so the caller sees an ordinary failed command (exit
            # 126) instead of an exception: fail closed, never a pass.
            popen_kwargs.pop("shell", None)
            popen_kwargs.pop("executable", None)
            return real_popen(
                [
                    "/bin/sh",
                    "-c",
                    "echo 'P41-PRODUCT-11A: command refused, not executed' >&2;"
                    " exit 126",
                ],
                *popen_args,
                **popen_kwargs,
            )
        return real_popen(args, *popen_args, **popen_kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)

    async def refused_async_subprocess(*args: Any, **kwargs: Any):
        tally["async_subprocess"].append({"argv": [str(a) for a in args][:6]})
        raise PermissionError("P41-PRODUCT-11A: async subprocess refused")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refused_async_subprocess)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refused_async_subprocess)

    def refused_system(command: str) -> int:
        tally["os_system"].append(command)
        raise PermissionError("P41-PRODUCT-11A: os.system refused")

    monkeypatch.setattr(os, "system", refused_system)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check_socket(sock: socket.socket, address: Any) -> None:
        if sock.family != socket.AF_UNIX:
            tally["network"].append({"address": repr(address), "origin": _origin()})
            raise PermissionError("P41-PRODUCT-11A: network connection refused")

    def guarded_connect(sock, address):
        _check_socket(sock, address)
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        _check_socket(sock, address)
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    real_execute_file_ops = ExecutorService.execute_file_ops

    def refused_file_ops(project_dir, ops, **kwargs):
        refused = bool(ops)
        tally["file_ops"].append(
            {
                "ops": [
                    {"op": op.get("op"), "path": op.get("path")}
                    for op in (ops or [])
                    if isinstance(op, dict)
                ],
                "refused": refused,
                "origin": _origin(),
            }
        )
        if not refused:
            # An empty op list is a production no-op (returns before touching
            # the workspace); refusing it would fabricate a failure.
            return real_execute_file_ops(project_dir, ops, **kwargs)
        return {
            "success": False,
            "files_changed": [],
            "output": (
                "P41-PRODUCT-11A: structured file operations refused at "
                "audit boundary (not applied)"
            ),
        }

    monkeypatch.setattr(
        ExecutorService, "execute_file_ops", staticmethod(refused_file_ops)
    )


@pytest.mark.integration_contract
@pytest.mark.parametrize(
    "designated",
    [("planning",), ("read_only_discovery", "planning")],
    ids=["A-planning-only", "B-discovery-and-planning"],
)
def test_calculator_lifecycle_provider_calls_up_to_file_op_boundary(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
    designated,
):
    for setting, value in (
        ("INLINE_PLANNING", True),
        ("PLANNING_BACKEND", "openai_chat_completions"),
        ("EXECUTION_BACKEND", "openai_chat_completions"),
        ("PLANNER_MODEL", "qwen-local"),
        ("EXECUTION_MODEL", "qwen-local"),
        ("AGENT_MODEL", "qwen-local"),
        ("PLANNING_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_CONTEXT_TOKENS", 64_000),
        ("DEBUG_REPAIR_CONTEXT_TOKENS", 64_000),
        ("PLANNING_REPAIR_ENABLED", False),
        ("RUNTIME_WORKSPACE_ENABLED", False),
        ("DEMO_MODE", False),
    ):
        monkeypatch.setattr(settings, setting, value)
    monkeypatch.delenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", raising=False)

    db = db_session_factory()
    workspace = isolated_workspace_root / "p41-product-11a-calculator"
    workspace.mkdir(parents=True)
    calculator_source = "def value():\n    return 1\n"
    (workspace / "calculator.py").write_text(calculator_source, encoding="utf-8")
    (workspace / "test_calculator.py").write_text(
        "from calculator import value\n\n\ndef test_value():\n"
        "    assert value() == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=p41-product-11a@example.invalid",
            "-c",
            "user.name=P41-PRODUCT-11A",
            "commit",
            "-qm",
            "disposable baseline",
        ],
        cwd=workspace,
        check=True,
    )

    project = Project(
        name="P41 PRODUCT 11A disposable calculator",
        description="Temporary provider-dependency audit project",
        workspace_path=str(workspace),
        user_id=1,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    evaluator = User(
        id=1,
        email="eval@local.dev",
        hashed_password="offline-test-only",
        is_active=True,
    )
    api_app.dependency_overrides[get_current_user] = lambda: evaluator
    api_app.dependency_overrides[get_current_active_user] = lambda: evaluator
    client = TestClient(api_app)
    try:
        response = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project.id,
                "title": "Modify calculator.py",
                "description": TASK_DESCRIPTION,
            },
        )
    finally:
        client.close()
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    session = SessionModel(
        project_id=project.id,
        name="P41 PRODUCT 11A disposable execution",
        description=TASK_DESCRIPTION,
        status="pending",
        execution_mode="manual",
        is_active=False,
        instance_id=f"p41-product-11a-{uuid.uuid4()}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    ledger: list[dict[str, Any]] = []
    factory_requests: list[dict[str, Any]] = []
    runtime = _FailClosedRecordingRuntime(ledger, designated)
    admissions: list[dict[str, Any]] = []
    real_assess = read_only_discovery.assess_discovery_admission

    def recording_assess(*args: Any, **kwargs: Any):
        admission = real_assess(*args, **kwargs)
        admissions.append(
            {
                "status": getattr(admission, "status", None),
                "reason": getattr(admission, "reason", None),
            }
        )
        return admission

    monkeypatch.setattr(
        read_only_discovery, "assess_discovery_admission", recording_assess
    )

    def recording_factory(*args: Any, **kwargs: Any):
        factory_requests.append(
            {"role": str(kwargs.get("role", "")), "origin": _origin()}
        )
        return runtime

    for module in (worker, agent_runtime, session_execution_service):
        monkeypatch.setattr(module, "create_agent_runtime", recording_factory)
    monkeypatch.setattr(worker, "get_db_session", db_session_factory)

    tally: dict[str, list] = {
        "subprocess": [],
        "async_subprocess": [],
        "os_system": [],
        "network": [],
        "file_ops": [],
    }
    real_worker_task = worker.execute_orchestration_task
    dispatches: list[str] = []

    def deliver(kwargs: dict[str, Any], task_id: str | None = None):
        celery_id = task_id or f"p41-product-11a-{uuid.uuid4()}"
        dispatches.append(celery_id)
        applied = real_worker_task.apply(kwargs=kwargs, task_id=celery_id)
        with allow_join_result():
            outcome = applied.get(propagate=False, disable_sync_subtasks=False)
        return SimpleNamespace(id=celery_id, result=outcome)

    class InlineWorkerTask:
        @staticmethod
        def delay(**kwargs: Any):
            return deliver(kwargs)

        @staticmethod
        def apply_async(
            *, kwargs: dict[str, Any], task_id: str | None = None, **_options: Any
        ):
            return deliver(kwargs, task_id=task_id)

    monkeypatch.setattr(worker, "execute_orchestration_task", InlineWorkerTask)

    _install_execution_guards(monkeypatch, tally, isolated_workspace_root)
    queue_result = queue_task_for_session(db, session, task_id, timeout_seconds=120)
    task_execution_id = queue_result["task_execution_id"]

    db.expire_all()
    task = db.get(Task, task_id)
    task_execution = db.get(TaskExecution, task_execution_id)
    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task_id)
        .one()
    )
    checkpoints = (
        db.query(TaskCheckpoint)
        .filter(
            TaskCheckpoint.session_id == session.id,
            TaskCheckpoint.task_id == task_id,
        )
        .order_by(TaskCheckpoint.id.asc())
        .all()
    )
    verdicts = [
        json.loads(cp.state_snapshot)
        for cp in checkpoints
        if cp.checkpoint_type == "validation_plan"
    ]
    event_paths = list(
        isolated_runtime_root.rglob(f"session_{session.id}_task_{task_id}.jsonl")
    )
    events = _read_jsonl(event_paths[0]) if event_paths else []

    report = {
        "task_status": str(task.status),
        "session_task_status": str(session_task.status),
        "task_execution_status": str(task_execution.status),
        "dispatches": len(dispatches),
        "task_executions": [
            (row.id, row.attempt_number, str(row.status))
            for row in db.query(TaskExecution)
            .filter(TaskExecution.task_id == task_id)
            .order_by(TaskExecution.id.asc())
            .all()
        ],
        "plan_verdicts": [v.get("status") for v in verdicts],
        "authority_present": any(
            v.get("accepted_path_authority") for v in verdicts if isinstance(v, dict)
        ),
        "designated": list(designated),
        "checkpoint_types": [cp.checkpoint_type for cp in checkpoints],
        "accepted_plan": next(
            (
                [
                    {
                        key: step.get(key)
                        for key in (
                            "step_number",
                            "description",
                            "commands",
                            "verification",
                            "ops",
                        )
                    }
                    for step in snapshot["plan"]
                    if isinstance(step, dict)
                ]
                for snapshot in (
                    json.loads(cp.state_snapshot)
                    for cp in checkpoints
                    if cp.state_snapshot
                )
                if isinstance(snapshot, dict) and isinstance(snapshot.get("plan"), list)
            ),
            None,
        ),
        "discovery_admissions": admissions,
        "runtime_calls": ledger,
        "factory_requests": factory_requests,
        "guards": tally,
        "event_types": [event["event_type"] for event in events],
        "calculator_unchanged": (workspace / "calculator.py").read_text(
            encoding="utf-8"
        )
        == calculator_source,
    }
    print("P41_PRODUCT_11A_TALLY " + json.dumps(report, default=str))

    # Boundary invariants: nothing generated ran, nothing left the process.
    # Every network attempt was refused; the only observed origin is the
    # knowledge store (Qdrant) on the failure path, never a provider adapter.
    assert all(
        attempt["origin"]
        and attempt["origin"][0].startswith("services/knowledge/knowledge_service.py")
        for attempt in tally["network"]
    )
    assert tally["async_subprocess"] == [] and tally["os_system"] == []
    assert all(
        (os.path.basename(entry["argv"][0]) == "git" and not entry["shell"])
        or entry["argv"] == [_ORCHESTRATOR_INSPECTION_COMMAND]
        for entry in tally["subprocess"]
        if entry["allowed"]
    )
    assert report["calculator_unchanged"]
    assert admissions
    assert admissions[0]["status"] == read_only_discovery.DISCOVERY_ADMISSION_REQUIRED
    # Refused calls and refused file ops must never surface as success.
    assert task.status == TaskStatus.FAILED
    assert task_execution.status == TaskStatus.FAILED
    assert "task_completed" not in report["event_types"]
    served = [call["kind"] for call in ledger if call["served"]]
    if designated == ("planning",):
        # Discovery is reached before Planning and, refused, fails the Task
        # closed before any Planning request is issued.
        assert ledger[0]["kind"] == "read_only_discovery"
        assert served == []
        assert report["plan_verdicts"] == []
        assert tally["file_ops"] == []
    else:
        assert served.count("execute_task") == 1
        assert any(
            status in {"accepted", "warning"} for status in report["plan_verdicts"]
        )
        assert any(
            entry["refused"] for entry in tally["file_ops"]
        ), "lifecycle did not reach a non-empty file-op boundary"


def test_completion_summary_default_path_makes_no_runtime_call(monkeypatch):
    """With the opt-in flag unset, Phase 5 summary is deterministic, no runtime."""

    calls: list[Any] = []
    monkeypatch.setattr(
        agent_runtime,
        "create_agent_runtime",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError),
    )
    state = SimpleNamespace(
        execution_results=[
            SimpleNamespace(status="completed", files_changed=["calculator.py"])
        ],
        plan=_calculator_plan(),
    )
    ctx = SimpleNamespace(orchestration_state=state, db=object(), emit_live=None)

    monkeypatch.delenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", raising=False)
    result = completion_summary._generate_task_summary_with_fallback(
        ctx=ctx, summary_prompt="unused"
    )
    assert calls == []
    assert result["source"] == "deterministic"
    assert result["status"] == "completed"

    # Opt-in path with a refusing runtime: the summary degrades to the same
    # deterministic text and never reports model content.
    emitted: list[Any] = []
    ctx.emit_live = lambda *a, **k: emitted.append(a)
    ledger: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent_runtime,
        "create_agent_runtime",
        lambda *a, **k: _FailClosedRecordingRuntime(ledger),
    )
    monkeypatch.setenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", "1")
    opt_in = completion_summary._generate_task_summary_with_fallback(
        ctx=ctx, summary_prompt="summarize"
    )
    assert [call["kind"] for call in ledger] == ["invoke_prompt"]
    assert opt_in["fallback"] is True
    assert opt_in["output"] == result["output"]
    assert emitted
