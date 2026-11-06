"""Deterministic end-to-end task completion through the production Worker path."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
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
from app.services.session.session_runtime_service import queue_task_for_session
from app.tasks import worker


TASK_DESCRIPTION = (
    "Modify calculator.py so that value() returns 2. Preserve the existing "
    "function interface and ensure test_calculator.py passes."
)
VERIFICATION_COMMAND = "python3 -m pytest -q test_calculator.py"


def _calculator_plan() -> list[dict[str, Any]]:
    return [
        {
            "step_number": 1,
            "description": (
                "Replace calculator.value's return value with 2 and run its focused test."
            ),
            "commands": [],
            "verification": VERIFICATION_COMMAND,
            "rollback": None,
            "expected_files": ["calculator.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "calculator.py",
                    "old": "    return 1\n",
                    "new": "    return 2\n",
                }
            ],
        }
    ]


class _DeterministicProviderAdapter:
    """Provider-shaped test adapter; planning output is fixed and no network is used."""

    backend = "openai_chat_completions"
    model = "qwen-local"

    def __init__(self) -> None:
        self.plan = _calculator_plan()
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
            kind = "discovery"
            output = json.dumps({"action": "read_file", "path": "calculator.py"})
        elif "independent QA evaluator" in prompt:
            kind = "completion_evaluation"
            output = json.dumps(self.plan)
        else:
            kind = "planner_or_execution_reasoning"
            output = json.dumps(self.plan)
        self.calls.append({"kind": kind, "prompt": prompt, **kwargs})
        return {"status": "completed", "output": output}

    async def invoke_prompt(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "residual_reasoning", "prompt": prompt, **kwargs})
        return {
            "status": "completed",
            "output": json.dumps(
                {
                    "status": "completed",
                    "output": "deterministic residual reasoning",
                    "files_changed": [],
                }
            ),
        }

    async def create_session(self, *args: Any, **kwargs: Any) -> str:
        return "p41-product-01-provider-free"

    async def pause_session(self) -> None:
        return None

    async def resume_session(self, checkpoint_name: str | None = None) -> str:
        return "p41-product-01-provider-free-resume"

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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.integration_contract
@pytest.mark.parametrize(
    "test_expected_value",
    [2, 3],
    ids=["positive", "verification-failure"],
)
def test_task_creation_to_verified_completion_and_negative_control(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
    test_expected_value,
):
    """The same trusted Task completes on a passing test and refuses a failing one."""

    # Keep all runtime settings local to this pytest process. The product path
    # uses its structured project workspace; no live services are started.
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

    db = db_session_factory()
    workspace = isolated_workspace_root / "p41-product-01-calculator"
    workspace.mkdir(parents=True)
    (workspace / "calculator.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )
    (workspace / "test_calculator.py").write_text(
        "from calculator import value\n\n\n"
        "def test_value():\n"
        f"    assert value() == {test_expected_value}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=p41-product-01@example.invalid",
            "-c",
            "user.name=P41-PRODUCT-01",
            "commit",
            "-qm",
            "disposable baseline",
        ],
        cwd=workspace,
        check=True,
    )

    project = Project(
        name="P41 PRODUCT 01 disposable calculator",
        description="Temporary deterministic end-to-end integration project",
        workspace_path=str(workspace),
        user_id=1,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # The live evaluator account is not available to this isolated database.
    # This dependency override uses its designated email only in memory and
    # does not create a User row, token, or live application session.
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
    task = db.get(Task, task_id)
    assert task is not None
    assert task.description == TASK_DESCRIPTION
    assert task.status == TaskStatus.PENDING

    session = SessionModel(
        project_id=project.id,
        name="P41 PRODUCT 01 disposable execution",
        description=TASK_DESCRIPTION,
        status="pending",
        execution_mode="manual",
        is_active=False,
        instance_id=f"p41-product-01-{uuid.uuid4()}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    provider = _DeterministicProviderAdapter()
    monkeypatch.setattr(
        worker, "create_agent_runtime", lambda *args, **kwargs: provider
    )
    monkeypatch.setattr(worker, "get_db_session", db_session_factory)
    real_worker_task = worker.execute_orchestration_task
    dispatches: list[dict[str, Any]] = []

    def deliver(kwargs: dict[str, Any], task_id: str | None = None):
        celery_id = task_id or f"p41-product-01-{uuid.uuid4()}"
        dispatches.append({"kwargs": dict(kwargs), "celery_task_id": celery_id})
        applied = real_worker_task.apply(kwargs=kwargs, task_id=celery_id)
        with allow_join_result():
            outcome = applied.get(propagate=True, disable_sync_subtasks=False)
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

    queue_result = queue_task_for_session(
        db,
        session,
        task_id,
        timeout_seconds=120,
    )
    task_execution_id = queue_result["task_execution_id"]
    assert task_execution_id

    db.expire_all()
    task = db.get(Task, task_id)
    session = db.get(SessionModel, session.id)
    task_execution = db.get(TaskExecution, task_execution_id)
    session_task = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.task_id == task_id,
        )
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
    plan_verdicts = [
        json.loads(checkpoint.state_snapshot)
        for checkpoint in checkpoints
        if checkpoint.checkpoint_type == "validation_plan"
    ]
    assert plan_verdicts
    assert any(
        verdict["status"] in {"accepted", "warning"} for verdict in plan_verdicts
    )
    assert provider.calls
    assert len(dispatches) == 1
    assert all(call["kwargs"].get("task_id") == task_id for call in dispatches)
    assert db.query(Task).filter(Task.project_id == project.id).count() == 1

    verification_path = next(
        isolated_runtime_root.rglob(
            f"verification_session_{session.id}_task_{task_id}_"
            f"execution_{task_execution_id}.jsonl"
        )
    )
    verification_records = _read_jsonl(verification_path)
    event_path = next(
        isolated_runtime_root.rglob(f"session_{session.id}_task_{task_id}.jsonl")
    )
    events = _read_jsonl(event_path)
    success_events = [
        event for event in events if event["event_type"] == "task_completed"
    ]
    pytest_verification_event_indexes = [
        index
        for index, event in enumerate(events)
        if event["event_type"] == "step_finished"
        and any(
            "pytest" in record.get("declared_command", "")
            and "test_calculator.py" in record.get("declared_command", "")
            for record in event.get("details", {}).get(
                "verification_evidence_records", []
            )
        )
    ]

    if test_expected_value == 2:
        assert (workspace / "calculator.py").read_text(encoding="utf-8") == (
            "def value():\n    return 2\n"
        )
        assert task.status == TaskStatus.DONE
        assert session_task.status == TaskStatus.DONE
        assert task_execution.status == TaskStatus.DONE
        assert session.status == "completed"
        assert any(
            record["verification_status"] == "passed"
            and record["command_execution_status"] == "completed"
            and "pytest" in record["declared_command"]
            and "test_calculator.py" in record["declared_command"]
            and record["process_exit_code"] == 0
            for record in verification_records
        )
        assert success_events
        assert pytest_verification_event_indexes
        assert min(pytest_verification_event_indexes) < events.index(success_events[0])
    else:
        assert task.status == TaskStatus.FAILED
        assert session_task.status == TaskStatus.FAILED
        assert task_execution.status == TaskStatus.FAILED
        assert session.status != "completed"
        assert any(
            record["verification_status"] == "failed"
            and record["command_execution_status"] == "completed"
            and "pytest" in record["declared_command"]
            and "test_calculator.py" in record["declared_command"]
            and record["process_exit_code"] != 0
            for record in verification_records
        )
        assert pytest_verification_event_indexes
        assert success_events == []

    db.close()
