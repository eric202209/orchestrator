import asyncio
import logging
import json

import pytest
from app.services.orchestration.phases.planning_flow import (
    _PlanningRetryState,
    _classify_planning_timeout_failure,
    _compress_project_context_for_planning,
)

from app.services.orchestration.planning.planner import (
    PlannerService,
    MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD,
)

from app.services.orchestration.policy import PLANNING_REPAIR_TIMEOUT_SECONDS


def test_openclaw_session_lock_is_classified_distinctly():
    exc = TimeoutError(
        "OpenClaw planning failed: session file locked (timeout 10000ms): "
        "pid=123 /root/.openclaw/agents/main/sessions/sessions.json.lock"
    )

    assert (
        _classify_planning_timeout_failure(exc, _PlanningRetryState())
        == "planning_openclaw_lock_contention"
    )
    assert PlannerService.is_openclaw_lock_contention(
        {
            "error": (
                "Error: session file locked (timeout 10000ms): "
                "pid=123 /root/.openclaw/agents/main/sessions/sessions.json.lock"
            )
        }
    )


def test_planning_model_calls_use_interprocess_lock(tmp_path, monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}
    lock_path = tmp_path / "planning.lock"
    monkeypatch.setattr(
        planner_module,
        "OPENCLAW_PLANNING_LOCK_PATH",
        lock_path,
    )

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["lock_exists_during_call"] = (
                planner_module.OPENCLAW_PLANNING_LOCK_PATH.exists()
            )
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "[]",
            timeout_seconds=120,
            reuse_task_session=False,
        )
    )

    assert result["status"] == "completed"
    assert result["_planning_lock_diagnostics"]["planning_lock_path"] == str(lock_path)
    assert result["_planning_lock_diagnostics"]["planning_lock_wait_seconds"] >= 0
    assert captured["prompt"] == "[]"
    assert captured["kwargs"]["timeout_seconds"] == 120
    assert captured["lock_exists_during_call"] is True
    assert lock_path.exists()


def test_direct_planning_fallback_shares_openclaw_timeout_budget(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

        async def execute_task(self, prompt, **kwargs):
            captured["fallback_prompt"] = prompt
            captured["fallback_kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    async def direct_timeout(
        cls, runtime_service, prompt, *, timeout_budget_seconds=None
    ):
        captured["direct_prompt"] = prompt
        captured["direct_timeout_budget_seconds"] = timeout_budget_seconds
        return None

    monotonic_values = iter([1000.0, 1090.0])

    def fake_monotonic():
        return next(monotonic_values, 1090.0)

    monkeypatch.setattr(PlannerService, "_monotonic", staticmethod(fake_monotonic))
    monkeypatch.setattr(
        PlannerService,
        "_invoke_direct_no_thinking_planning",
        classmethod(direct_timeout),
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:8000/v1",
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_MODEL", "qwen-local")

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "plan this",
            timeout_seconds=300,
            reuse_task_session=False,
        )
    )

    assert result["status"] == "completed"
    assert captured["direct_prompt"] == "plan this"
    assert captured["direct_timeout_budget_seconds"] == 300
    assert captured["fallback_prompt"] == "plan this"
    assert captured["fallback_kwargs"]["timeout_seconds"] == 210


def test_planning_lock_skips_direct_after_phase_unavailable(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

        async def execute_task(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    async def direct_should_not_run(*args, **kwargs):
        raise AssertionError("direct planning should be skipped")

    monkeypatch.setattr(
        PlannerService,
        "_invoke_direct_no_thinking_planning",
        direct_should_not_run,
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:8000/v1",
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_MODEL", "qwen-local")

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "plan this",
            timeout_seconds=300,
            reuse_task_session=False,
            direct_planning_state={"direct_unavailable": True},
        )
    )

    assert result["status"] == "completed"
    assert captured["prompt"] == "plan this"
    assert captured["kwargs"]["timeout_seconds"] == 300
    assert "direct_planning_state" not in captured["kwargs"]


def test_local_openclaw_direct_planning_can_skip_by_prompt_threshold(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

        async def execute_task(self, prompt, **kwargs):
            captured["fallback_prompt"] = prompt
            captured["fallback_kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    async def direct_should_not_run(*args, **kwargs):
        raise AssertionError("direct planning should be skipped")

    monkeypatch.setattr(
        PlannerService,
        "_invoke_direct_no_thinking_planning",
        direct_should_not_run,
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:8000/v1",
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_MODEL", "qwen-local")
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_SKIP_PROMPT_CHAR_THRESHOLD",
        8,
    )

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "plan this",
            timeout_seconds=300,
            reuse_task_session=False,
        )
    )

    assert result["status"] == "completed"
    assert captured["fallback_prompt"] == "plan this"
    assert captured["fallback_kwargs"]["timeout_seconds"] == 300


def test_direct_ollama_direct_planning_ignores_local_openclaw_threshold(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "direct_ollama", "model_family": "qwen3-coder:30b"}

        async def execute_task(self, prompt, **kwargs):
            raise AssertionError("fallback should not run when direct succeeds")

    async def direct_success(
        cls, runtime_service, prompt, *, timeout_budget_seconds=None
    ):
        captured["direct_prompt"] = prompt
        captured["direct_timeout_budget_seconds"] = timeout_budget_seconds
        return {"status": "completed", "output": "[]", "planning_direct": True}

    monkeypatch.setattr(
        PlannerService,
        "_invoke_direct_no_thinking_planning",
        classmethod(direct_success),
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:11434/v1",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_MODEL",
        "qwen3-coder:30b",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_NO_THINKING_FOR_DIRECT_OLLAMA",
        True,
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_SKIP_PROMPT_CHAR_THRESHOLD",
        8,
    )

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "plan this",
            timeout_seconds=300,
            reuse_task_session=False,
        )
    )

    assert result["planning_direct"] is True
    assert captured["direct_prompt"] == "plan this"
    assert captured["direct_timeout_budget_seconds"] == 300


def test_planning_repair_uses_direct_no_thinking_chat_path(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return Response()

    class Runtime:
        db = object()

        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

        async def invoke_prompt(self, *args, **kwargs):
            raise AssertionError("OpenClaw fallback should not run on direct success")

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_ENABLED",
        True,
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:8000/v1",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_MODEL",
        "qwen-local",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_DISABLE_THINKING",
        True,
    )

    result = asyncio.run(
        PlannerService._invoke_repair_prompt(
            Runtime(),
            "repair me",
            repair_timeout=60,
        )
    )

    assert result["backend"] == "direct_chat_completions"
    assert result["output"].startswith("[")
    assert captured["url"] == "http://localhost:8000/v1/chat/completions"
    assert captured["timeout"] == 60
    assert captured["payload"]["model"] == "qwen-local"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "repair me"}]
    assert captured["payload"]["enable_thinking"] is False
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["payload"]["think"] is False


def test_stale_replace_planning_repair_captures_prompt_and_gateway_result(
    monkeypatch, tmp_path
):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(
        planner_module,
        "STALE_REPLACE_REPAIR_DIAGNOSTIC_DIR",
        tmp_path / "planning-stale-replace-repair",
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:8000/v1",
    )
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_MODEL", "qwen-local")
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_DISABLE_THINKING",
        True,
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '[{"step_number":1}]'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }

    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            return Response()

    class Runtime:
        db = object()

        def get_backend_metadata(self):
            return {"backend": "local_openclaw", "model_family": "qwen3.6:27B"}

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    prompt = "repair stale replace"
    captured = PlannerService._capture_stale_replace_repair_prompt(
        repair_prompt=prompt,
        reason="post_repair_stale_replace_fallback: stale_replace_ops_steps",
        session_id=1,
        task_id=2,
        repair_attempt_number=1,
        repair_prompt_build_seconds=0.123,
        malformed_output_chars=12,
        validation_error_chars=34,
        knowledge_context_chars=56,
        includes_project_context=True,
        includes_non_project_context=True,
    )

    result = asyncio.run(
        PlannerService._invoke_repair_prompt(
            Runtime(),
            prompt,
            repair_timeout=60,
            diagnostic_context={
                **captured,
                "stale_replace_repair_diagnostic": True,
            },
        )
    )

    diagnostic_dir = (
        tmp_path / "planning-stale-replace-repair" / captured["prompt_sha256_12"]
    )
    metadata = json.loads((diagnostic_dir / "prompt_metadata.json").read_text())
    gateway = json.loads(
        (diagnostic_dir / "direct_no_thinking_result.json").read_text()
    )
    assert result["backend"] == "direct_chat_completions"
    assert metadata["prompt_chars"] == len(prompt)
    assert metadata["repair_reason"].startswith("post_repair_stale_replace_fallback")
    assert gateway["status"] == "completed"
    assert gateway["finish_reason"] == "stop"
    assert gateway["content_null"] is False
    assert gateway["content_chars"] == len('[{"step_number":1}]')


def test_direct_planning_uses_runtime_ollama_model_for_hyphen_alias(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return Response()

    class Runtime:
        def get_backend_metadata(self):
            return {
                "backend": "direct_ollama",
                "model_family": "qwen3:8b-hybrid",
            }

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:11434/v1",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_MODEL",
        "qwen3-8b-hybrid",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_TIMEOUT_SECONDS",
        45,
    )

    result = asyncio.run(
        PlannerService._invoke_direct_no_thinking_planning(
            Runtime(),
            "plan me",
            timeout_budget_seconds=45,
        )
    )

    assert result["planning_direct"] is True
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["payload"]["model"] == "qwen3:8b-hybrid"


def test_local_openclaw_direct_planning_timeout_override_is_config_gated(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            return Response()

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
        120,
    )

    result = asyncio.run(
        PlannerService._invoke_direct_no_thinking_planning(
            Runtime(),
            "plan me",
            timeout_budget_seconds=300,
        )
    )

    assert captured["timeout"] == 120
    assert result["direct_planning_timeout_seconds"] == 120


def test_default_direct_planning_timeout_remains_repair_timeout(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            return Response()

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
        0,
    )

    result = asyncio.run(
        PlannerService._invoke_direct_no_thinking_planning(
            Runtime(),
            "plan me",
            timeout_budget_seconds=300,
        )
    )

    assert captured["timeout"] == 90
    assert result["direct_planning_timeout_seconds"] == 90


def test_direct_ollama_ignores_local_openclaw_direct_timeout_override(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            return Response()

    class Runtime:
        def get_backend_metadata(self):
            return {"backend": "direct_ollama", "model_family": "qwen3-coder:30b"}

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
        120,
    )

    result = asyncio.run(
        PlannerService._invoke_direct_no_thinking_planning(
            Runtime(),
            "plan me",
            timeout_budget_seconds=300,
        )
    )

    assert captured["timeout"] == 90
    assert result["direct_planning_timeout_seconds"] == 90


def test_direct_repair_uses_runtime_ollama_model_for_hyphen_alias(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '[{"step_number":1,"description":"ok","commands":[],"verification":null,"rollback":null,"expected_files":[]}]'
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return Response()

    class Runtime:
        db = object()

        def get_backend_metadata(self):
            return {
                "backend": "direct_ollama",
                "model_family": "qwen3:8b-hybrid",
            }

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", True)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_BASE_URL",
        "http://localhost:11434/v1",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_MODEL",
        "qwen3-8b-hybrid",
    )
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_DISABLE_THINKING",
        True,
    )

    result = asyncio.run(
        PlannerService._invoke_repair_prompt(
            Runtime(),
            "repair me",
            repair_timeout=45,
        )
    )

    assert result["backend"] == "direct_chat_completions"
    assert captured["payload"]["model"] == "qwen3:8b-hybrid"


def test_planning_repair_falls_back_to_openclaw_when_direct_fails(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}

    class Client:
        def __init__(self, timeout):
            captured["direct_timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError("direct unavailable")

    class Runtime:
        db = object()

        def get_backend_metadata(self):
            return {"backend": "local_openclaw"}

        async def invoke_prompt(self, prompt, **kwargs):
            captured["fallback_prompt"] = prompt
            captured["fallback_kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    monkeypatch.setattr(planner_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_ENABLED",
        True,
    )

    result = asyncio.run(
        PlannerService._invoke_repair_prompt(
            Runtime(),
            "repair me",
            repair_timeout=60,
        )
    )

    assert result["output"] == "[]"
    assert captured["direct_timeout"] == 60
    assert captured["fallback_prompt"] == "repair me"
    assert captured["fallback_kwargs"]["no_output_timeout_seconds"] == 60


def test_minimal_prompt_retry_flags_ultra_dense_prompt_without_changing_retry(
    tmp_path,
    monkeypatch,
):
    events = []
    captured = {}
    huge_prompt = "x" * (MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD * 4 + 1)

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    monkeypatch.setattr(
        PlannerService,
        "build_minimal_planning_prompt",
        staticmethod(lambda *args, **kwargs: huge_prompt),
    )

    PlannerService.retry_with_minimal_prompt(
        runtime_service=Runtime(),
        task_description="Build a small Python health checker",
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.ultra_dense_minimal_prompt_diagnostics"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="dense_planning_context",
        workflow_profile="default",
    )

    ultra_dense_events = [
        metadata
        for _, _, metadata in events
        if metadata.get("reason") == "ultra_dense_planning_context"
    ]

    assert ultra_dense_events
    assert ultra_dense_events[0]["ultra_dense_planning_context"] is True
    assert ultra_dense_events[0]["minimal_prompt_estimated_tokens"] > (
        MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
    )
    assert captured["prompt"] == huge_prompt
    assert captured["kwargs"]["diagnostic_label"] == "MINIMAL_PLANNING"


def test_minimal_planning_prompt_keeps_workflow_rules_for_existing_fullstack_workspace():
    prompt = PlannerService.build_minimal_planning_prompt(
        "Bring the existing frontend and backend to dev-ready state",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workflow_profile="fullstack_scaffold",
        workflow_phases=[
            "create_frontend_skeleton",
            "create_backend_skeleton",
            "wire_api_config",
            "verify_dev_startup",
        ],
        workspace_has_existing_files=True,
    )

    assert "Workflow profile: fullstack_scaffold" in prompt
    assert "Extend or verify existing files instead of re-scaffolding" in prompt
    assert "Never use parent-directory traversal like `../backend`" in prompt
    assert "`verification` must be a single shell string or null" in prompt
    assert "No heredocs, background processes" in prompt


def test_large_planning_context_is_compressed_before_first_attempt():
    state = type("State", (), {})()
    state.project_context = "Very long planning context " * 600
    state.plan = []
    state.current_step_index = 0
    state.completed_steps = []
    state.failed_steps = []
    state.debug_attempts = []
    state.changed_files = []

    compressed = _compress_project_context_for_planning(state)

    assert len(compressed) < len(state.project_context)
    assert "Very long planning context" in compressed
