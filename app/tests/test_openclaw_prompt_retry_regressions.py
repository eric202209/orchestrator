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
            "backend": "debug_repair_direct_chat_completions",
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
    assert result["backend"] == "debug_repair_direct_chat_completions"
    assert seen["prompt"] == "Return bounded JSON repair"
    assert seen["timeout_seconds"] == 180
    assert seen["diagnostic_metadata"]["debug_failure_class"] == (
        "source_step_validation"
    )


def test_bounded_debug_repair_architecture_label_uses_direct_chat(
    db_session, monkeypatch
):
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
            "backend": "debug_repair_direct_chat_completions",
            "model_family": "qwen-local",
        }

    async def fake_execute_task_with_streaming(*args, **kwargs):
        raise AssertionError("bounded debug repair should not use CLI streaming")

    monkeypatch.setattr(service, "_execute_phase7f_direct_repair", fake_direct_repair)
    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    result = asyncio.run(
        service.execute_task(
            "Return bounded JSON repair",
            timeout_seconds=180,
            diagnostic_label="BOUNDED_EXECUTION_DEBUG_REPAIR",
            diagnostic_metadata={"debug_failure_class": "source_step_validation"},
        )
    )

    assert result["status"] == "completed"
    assert result["backend"] == "debug_repair_direct_chat_completions"
    assert seen["prompt"] == "Return bounded JSON repair"
    assert seen["timeout_seconds"] == 180
    assert seen["diagnostic_metadata"]["debug_failure_class"] == (
        "source_step_validation"
    )


def test_bounded_debug_repair_diagnostic_label_architecture_alias():
    assert (
        OpenClawSessionService._diagnostic_label_architecture("PHASE7F_DEBUG_REPAIR")
        == "BOUNDED_EXECUTION_DEBUG_REPAIR"
    )
    assert (
        OpenClawSessionService._diagnostic_label_architecture(
            "BOUNDED_EXECUTION_DEBUG_REPAIR"
        )
        == "BOUNDED_EXECUTION_DEBUG_REPAIR"
    )
    assert OpenClawSessionService._diagnostic_label_architecture("PLANNING") is None


def test_phase7f_direct_repair_payload_disables_thinking(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_DISABLE_THINKING", True)

    payload = OpenClawSessionService._debug_repair_direct_payload(
        "Return JSON", "qwen-local"
    )

    assert payload["model"] == "qwen-local"
    assert payload["messages"] == [{"role": "user", "content": "Return JSON"}]
    assert payload["think"] is False
    assert payload["enable_thinking"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_debug_repair_responses_payload_uses_openai_responses_shape():
    payload = OpenClawSessionService._debug_repair_responses_payload(
        "Return bounded JSON", "gpt-5.5"
    )

    assert payload == {
        "model": "gpt-5.5",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Return bounded JSON"}],
            }
        ],
    }


def test_debug_repair_extracts_responses_output_text():
    body = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": '{"ops":['},
                    {"type": "output_text", "text": "]}"},
                ]
            }
        ]
    }

    assert OpenClawSessionService._extract_responses_output_text(body) == '{"ops":[]}'


def test_debug_repair_direct_config_keeps_phase7f_env_fallback(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "")
    monkeypatch.setattr(
        settings, "PHASE7F_REPAIR_BASE_URL", "https://legacy.example/v1"
    )
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_API_KEY", "legacy-key")

    config = OpenClawSessionService._debug_repair_direct_config()

    assert config["base_url"] == "https://legacy.example/v1"
    assert config["model"] == "legacy-model"
    assert config["api_key"] == "legacy-key"


def test_debug_repair_direct_config_prefers_architecture_names(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "https://debug.example/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "debug-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "debug-key")
    monkeypatch.setattr(
        settings, "PHASE7F_REPAIR_BASE_URL", "https://legacy.example/v1"
    )
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_API_KEY", "legacy-key")

    config = OpenClawSessionService._debug_repair_direct_config()

    assert config["base_url"] == "https://debug.example/v1"
    assert config["model"] == "debug-model"
    assert config["api_key"] == "debug-key"


def test_debug_repair_disable_thinking_prefers_architecture_setting(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_DISABLE_THINKING", False)
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_DISABLE_THINKING", True)

    assert OpenClawSessionService._debug_repair_disable_thinking() is False


def test_debug_repair_disable_thinking_falls_back_to_phase7f_setting(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_DISABLE_THINKING", None)
    monkeypatch.setattr(settings, "PHASE7F_REPAIR_DISABLE_THINKING", True)

    assert OpenClawSessionService._debug_repair_disable_thinking() is True


def test_debug_repair_openai_responses_path_posts_to_responses(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "openai_responses_api")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "https://api.example/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "gpt-5.5")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "secret")
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    seen: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": '{"ops":[]}', "usage": {"input_tokens": 1}}

    class FakeAsyncClient:
        def __init__(self, timeout):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "app.services.agents.openclaw_service.httpx.AsyncClient", FakeAsyncClient
    )

    result = asyncio.run(
        service._execute_phase7f_direct_repair("Return JSON", timeout_seconds=42)
    )

    assert seen["url"] == "https://api.example/v1/responses"
    assert seen["json"] == {
        "model": "gpt-5.5",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Return JSON"}],
            }
        ],
    }
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert result["backend"] == "debug_repair_openai_responses_api"
    assert result["output"] == '{"ops":[]}'


def test_debug_repair_local_chat_path_remains_chat_completions(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "https://local.example/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_DISABLE_THINKING", True)
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    seen: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ops":[]}'}}]}

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            seen["url"] = url
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        "app.services.agents.openclaw_service.httpx.AsyncClient", FakeAsyncClient
    )

    result = asyncio.run(
        service._execute_phase7f_direct_repair("Return JSON", timeout_seconds=42)
    )

    assert seen["url"] == "https://local.example/v1/chat/completions"
    assert seen["json"]["think"] is False
    assert result["backend"] == "debug_repair_direct_chat_completions"


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
