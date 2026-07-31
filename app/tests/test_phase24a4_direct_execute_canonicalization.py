"""Contract guards for the Phase 24A-4 Direct Execute adapter."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import TaskStatus


ROOT = Path(__file__).resolve().parents[2]
TASKS_SOURCE = ROOT / "app/api/v1/endpoints/tasks.py"


def test_direct_execute_route_is_queued_and_delegates_to_canonical(monkeypatch):
    from app.api.v1.endpoints import tasks as tasks_module

    task = SimpleNamespace(
        id=41,
        status=TaskStatus.PENDING,
        description="task description",
        title="task title",
    )
    captured = {}

    monkeypatch.setattr(
        tasks_module, "require_task_runtime_eligibility", lambda *args: task
    )

    def fake_queue(*args, **kwargs):
        captured.update(kwargs)
        return {
            "task_id": 41,
            "session_id": 51,
            "task_execution_id": 61,
            "celery_task_id": "celery-61",
        }

    monkeypatch.setattr(tasks_module, "_queue_task_retry", fake_queue)

    response = tasks_module.queue_task_with_canonical_execution(
        41,
        tasks_module.DirectExecuteRequest(prompt="compatibility prompt"),
        object(),
        object(),
    )

    assert response["status"] == "queued"
    assert response["task_id"] == 41
    assert response["session_id"] == 51
    assert response["task_execution_id"] == 61
    assert response["status_url"] == "/api/v1/tasks/41"
    assert captured["prompt_override"] == "compatibility prompt"
    assert captured["retry_request"].session_id is None


def test_direct_execute_rejects_active_task(monkeypatch):
    from app.api.v1.endpoints import tasks as tasks_module

    task = SimpleNamespace(id=41, status=TaskStatus.RUNNING)
    monkeypatch.setattr(
        tasks_module, "require_task_runtime_eligibility", lambda *args: task
    )

    with pytest.raises(HTTPException) as exc_info:
        tasks_module.queue_task_with_canonical_execution(
            41,
            tasks_module.DirectExecuteRequest(),
            object(),
            object(),
        )
    assert exc_info.value.status_code == 409


def test_direct_execute_rejects_missing_prompt_and_task_text(monkeypatch):
    from app.api.v1.endpoints import tasks as tasks_module

    task = SimpleNamespace(
        id=41, status=TaskStatus.PENDING, description=None, title=None
    )
    monkeypatch.setattr(
        tasks_module, "require_task_runtime_eligibility", lambda *args: task
    )

    with pytest.raises(HTTPException) as exc_info:
        tasks_module.queue_task_with_canonical_execution(
            41,
            tasks_module.DirectExecuteRequest(),
            object(),
            object(),
        )
    assert exc_info.value.status_code == 400


def test_direct_execute_adapter_has_no_request_owned_execution_calls():
    tree = ast.parse(TASKS_SOURCE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "queue_task_with_canonical_execution"
    )
    source = ast.get_source_segment(TASKS_SOURCE.read_text(encoding="utf-8"), function)
    assert source is not None
    for forbidden in (
        "runtime.execute_task",
        "execute_task_with_streaming",
        "maybe_allocate_runtime_workspace",
        "snapshot_workspace_before_run",
        "persist_task_execution_change_set",
        "promote",
    ):
        assert forbidden not in source


def test_direct_execute_request_rejects_legacy_timeout_option():
    from pydantic import ValidationError
    from app.api.v1.endpoints import tasks as tasks_module

    with pytest.raises(ValidationError):
        tasks_module.DirectExecuteRequest.model_validate(
            {"prompt": "x", "timeout_seconds": 600}
        )
