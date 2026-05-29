from __future__ import annotations

import asyncio

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.config import settings
from app.services.agents.openclaw_service import (
    OpenClawSessionError,
    OpenClawSessionService,
)


def _seed_service_models(db_session):
    project = Project(name="Prompt Retry Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Prompt Retry Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Retry overflowed prompt",
        description="Regression coverage for compact-prompt retry logic",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    return session, task


def test_execute_task_retries_context_overflow_with_compact_prompt(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "AGENT_MODEL", "local")
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )

    seen_prompts: list[str] = []

    async def fake_execute_task_with_streaming(
        prompt, timeout_seconds, log_callback, *, reuse_task_session=True
    ):
        seen_prompts.append(prompt)
        if len(seen_prompts) == 1:
            return {
                "status": "failed",
                "mode": "real",
                "output": "",
                "error": "Context window exceeded",
                "logs": [],
            }
        return {
            "status": "completed",
            "mode": "real",
            "output": '{"ok":true}',
            "error": "",
            "logs": [],
        }

    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    prompt = "\n".join(
        [
            "**Step:** Create the final Vitest test suite",
            "**Context:** " + ("existing workspace details " * 600),
            "**Output:** status, output, verification_output, files_changed, error_message",
        ]
    )

    result = asyncio.run(service.execute_task(prompt, timeout_seconds=30))

    assert result["status"] == "completed"
    assert result["backend"] == "local_openclaw"
    assert result["model_family"] == "local"
    assert result["backend_capabilities"]["supports_streaming"] is True
    assert len(seen_prompts) == 2
    assert len(seen_prompts[1]) < len(seen_prompts[0])
    assert "[Content truncated for performance]" in seen_prompts[1]


def test_execute_task_preserves_timeout_runtime_diagnostics(db_session, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_BACKEND", "local_openclaw")
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )

    async def fake_execute_task_with_streaming(*args, **kwargs):
        exc = OpenClawSessionError("Task timed out after 90s")
        exc.runtime_diagnostics = {
            "timed_out": True,
            "stdout_chars": 0,
            "stderr_contains_model_content": False,
            "output_channel_used": "none",
        }
        raise exc

    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    try:
        asyncio.run(service.execute_task("Return a plan", timeout_seconds=90))
    except OpenClawSessionError as exc:
        assert exc.runtime_diagnostics == {
            "timed_out": True,
            "stdout_chars": 0,
            "stderr_contains_model_content": False,
            "output_channel_used": "none",
        }
        return

    raise AssertionError("Expected timeout error")


def test_phase7f_debug_repair_uses_direct_no_thinking_chat(db_session, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "AGENT_MODEL", "local")
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )

    seen: dict[str, object] = {}

    async def fake_direct_repair(prompt, *, timeout_seconds, diagnostic_metadata=None):
        seen["prompt"] = prompt
        seen["timeout_seconds"] = timeout_seconds
        seen["diagnostic_metadata"] = diagnostic_metadata
        return {
            "status": "completed",
            "output": '{"ops":[]}',
            "logs": [],
            "backend": "phase7f_direct_chat_completions",
            "model_family": "qwen-local",
        }

    async def fake_execute_task_with_streaming(*args, **kwargs):
        raise AssertionError("Phase 7F should not use OpenClaw CLI streaming")

    monkeypatch.setattr(service, "_execute_phase7f_direct_repair", fake_direct_repair)
    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    result = asyncio.run(
        service.execute_task(
            "Return bounded JSON repair",
            timeout_seconds=180,
            diagnostic_label="PHASE7F_DEBUG_REPAIR",
            diagnostic_metadata={"debug_failure_class": "source_step_validation"},
        )
    )

    assert result["status"] == "completed"
    assert result["backend"] == "phase7f_direct_chat_completions"
    assert seen["prompt"] == "Return bounded JSON repair"
    assert seen["timeout_seconds"] == 180
    assert seen["diagnostic_metadata"]["debug_failure_class"] == (
        "source_step_validation"
    )


def test_phase7f_direct_repair_payload_disables_thinking(monkeypatch):
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_DISABLE_THINKING", True)

    payload = OpenClawSessionService._phase7f_repair_direct_payload(
        "Return JSON", "qwen-local"
    )

    assert payload["model"] == "qwen-local"
    assert payload["messages"] == [{"role": "user", "content": "Return JSON"}]
    assert payload["think"] is False
    assert payload["enable_thinking"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_non_phase7f_debug_repair_keeps_openclaw_streaming_path(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "AGENT_MODEL", "local")
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )

    calls: list[str] = []

    async def fake_direct_repair(*args, **kwargs):
        raise AssertionError("Non-Phase 7F calls must not use direct repair")

    async def fake_execute_task_with_streaming(
        prompt, timeout_seconds, log_callback, *, reuse_task_session=True, **kwargs
    ):
        calls.append(prompt)
        return {
            "status": "completed",
            "mode": "real",
            "output": "ok",
            "error": "",
            "logs": [],
        }

    monkeypatch.setattr(service, "_execute_phase7f_direct_repair", fake_direct_repair)
    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    result = asyncio.run(
        service.execute_task(
            "Return a normal answer",
            timeout_seconds=30,
            diagnostic_label="PLANNING",
        )
    )

    assert result["status"] == "completed"
    assert calls == ["Return a normal answer"]
