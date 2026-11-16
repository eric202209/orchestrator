"""P42-V17-G provider-free regressions: Discovery generation contract coverage.

PHASE34-PGR1 bounded Planning generation (reasoning disabled, bounded output
budget) on ``openai_chat_completions`` but deliberately excluded read-only
Discovery. TaskExecution 394 then timed out in Discovery with no ``max_tokens``
and reasoning enabled. Discovery now reuses the canonical Planning contract.

Every request here is captured at the httpx boundary; no provider is called.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    _GENERIC_SYSTEM,
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    InferenceRoutingPolicy,
    RoleRuntimeConfiguration,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.planning import planner as planner_module
from app.services.orchestration.planning.discovery_contract_capture import (
    DISCOVERY_ACTION_SCHEMA,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    build_discovery_prompt,
    run_discovery_stage,
)
from app.services.orchestration.planning.repository_orientation import (
    derive_repository_orientation,
)

GATEWAY = "http://ai-gateway.test:8000/v1"
MODEL = "qwen-local"
TASK_PROMPT = "Inspect the existing implementation."
PROJECT_CONTEXT = "A tiny deterministic Python fixture."


def _expected_budget() -> int:
    options = PlannerService.planning_generation_invocation_options(
        _FakeRuntimeService("openai_chat_completions")
    )
    assert options is not None
    return int(options.max_output_tokens)


class _FakeRuntimeService:
    def __init__(self, backend: str) -> None:
        self._backend = backend

    def get_backend_metadata(self) -> dict:
        return {"backend": self._backend, "model_family": MODEL}


def _client(monkeypatch, *, content=None, raises=None) -> dict:
    captured: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self) -> None:
            self.content = json.dumps(
                {"model": MODEL, "choices": [{"message": {"content": content}}]}
            ).encode()

        @property
        def text(self) -> str:
            return self.content.decode()

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return json.loads(self.content)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update({"url": url, "json": json})
            if raises is not None:
                raise raises
            return _FakeResponse()

    monkeypatch.setattr(openai_chat_adapter.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


def _settings(monkeypatch, tmp_path: Path, *, l1: bool) -> None:
    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", l1)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", GATEWAY)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_BASE_URL", GATEWAY)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_TEMPERATURE", 0.1)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_TOP_P", None)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_REPEAT_PENALTY", None)
    monkeypatch.setattr(
        planner_module, "OPENCLAW_PLANNING_LOCK_PATH", tmp_path / "planning.lock"
    )
    if l1:
        for name in ("AGENT_BACKEND", "PLANNING_BACKEND", "EXECUTION_BACKEND"):
            monkeypatch.setattr(settings, name, "openai_chat_completions")
        for name in ("PLANNING_DIRECT_MODEL", "PLANNER_MODEL", "EXECUTION_MODEL"):
            monkeypatch.setattr(settings, name, MODEL)


def _runtime(db, *, l1: bool) -> OpenAIChatCompletionsRuntime:
    extra = (
        {
            "routing_policy": InferenceRoutingPolicy.SINGLE_MODEL,
            "provider_endpoint": GATEWAY,
            "fallback_enabled": False,
        }
        if l1
        else {}
    )
    return OpenAIChatCompletionsRuntime(
        db,
        session_id=None,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.PLANNING,
            backend_name="openai_chat_completions",
            model_family=MODEL,
            adaptation_profile="ollama_default",
            **extra,
        ),
    )


def _ctx(tmp_path: Path, runtime) -> SimpleNamespace:
    return SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=runtime,
        prompt=TASK_PROMPT,
        orchestration_state=SimpleNamespace(
            project_context=PROJECT_CONTEXT, project_dir=tmp_path
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )


def _run(tmp_path: Path, runtime):
    return run_discovery_stage(
        ctx=_ctx(tmp_path, runtime),
        planning_timeout_seconds=240,
        extract_structured_text=lambda value: str(value),
        planner_service=PlannerService,
        emit_phase_event=lambda *args, **kwargs: None,
    )


def _assert_bounded_generation(payload: dict) -> None:
    assert payload["max_tokens"] == _expected_budget()
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["enable_thinking"] is False
    assert payload["think"] is False


def test_normal_mode_discovery_wire_contract(db_session, tmp_path, monkeypatch):
    _settings(monkeypatch, tmp_path, l1=False)
    # Planning's own response schema must never be applied to Discovery.
    monkeypatch.setattr(settings, "PLANNING_JSON_SCHEMA_MODE_ENABLED", True)
    captured = _client(monkeypatch, content='{"action":"stop"}')

    observation = _run(tmp_path, _runtime(db_session, l1=False))

    assert observation.action == "stop"
    payload = captured["json"]
    assert captured["url"] == f"{GATEWAY}/chat/completions"
    assert payload["model"] == MODEL
    assert payload["stream"] is False
    assert payload["temperature"] == 0.1
    _assert_bounded_generation(payload)
    expected_prompt = build_discovery_prompt(
        TASK_PROMPT,
        PROJECT_CONTEXT,
        derive_repository_orientation(
            tmp_path, TASK_PROMPT, explicit_paths=(), include_import_neighbors=True
        ),
    )
    assert payload["messages"] == [
        {"role": "system", "content": _GENERIC_SYSTEM},
        {"role": "user", "content": expected_prompt},
    ]
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "response_format" not in payload
    # Logical deadline unchanged: min(planning budget, Discovery cap).
    assert captured["client_kwargs"]["timeout"] == 120


def test_l1_discovery_keeps_schema_and_gains_bounds(db_session, tmp_path, monkeypatch):
    _settings(monkeypatch, tmp_path, l1=True)
    captured = _client(monkeypatch, content='{"action":"read_file","path":"answer.py"}')
    (tmp_path / "answer.py").write_text("def answer():\n    return 42\n")

    observation = _run(tmp_path, _runtime(db_session, l1=True))

    assert observation.action == "read_file"
    payload = captured["json"]
    _assert_bounded_generation(payload)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "discovery_action",
            "strict": True,
            "schema": DISCOVERY_ACTION_SCHEMA,
        },
    }


@pytest.mark.parametrize("l1", [False, True])
def test_unsupported_runtime_keeps_previous_discovery_options(
    tmp_path, monkeypatch, l1
):
    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", l1)
    captured: dict[str, object] = {}

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "completed", "output": '{"action":"stop"}'}

    for backend in ("local_openclaw", "direct_ollama"):
        captured.clear()
        run_discovery_stage(
            ctx=_ctx(tmp_path, _FakeRuntimeService(backend)),
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_Planner,
            emit_phase_event=lambda *args, **kwargs: None,
        )
        if not l1:
            assert "invocation_options" not in captured
        else:
            assert captured["invocation_options"] == RuntimeInvocationOptions(
                extra_provider_options={"response_format": {"type": "json_object"}},
                response_schema=DISCOVERY_ACTION_SCHEMA,
            )


@pytest.mark.parametrize(
    ("client_kwargs", "reason"),
    [
        ({"raises": httpx.ReadTimeout("slow")}, "discovery_provider_failed"),
        ({"raises": RuntimeError("boom")}, "discovery_provider_failed"),
        ({"content": ""}, "discovery_output_empty"),
        ({"content": "Here is the action: stop"}, "discovery_output_not_json"),
        ({"content": '{"action":"write_file"}'}, "discovery_action_unsupported"),
    ],
)
def test_bounded_discovery_still_fails_closed(
    db_session, tmp_path, monkeypatch, client_kwargs, reason
):
    _settings(monkeypatch, tmp_path, l1=False)
    captured = _client(monkeypatch, **client_kwargs)
    ctx = _ctx(tmp_path, _runtime(db_session, l1=False))

    with pytest.raises(DiscoveryContractError, match=reason) as excinfo:
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=240,
            extract_structured_text=lambda value: str(value),
            planner_service=PlannerService,
            emit_phase_event=lambda *args, **kwargs: None,
        )

    _assert_bounded_generation(captured["json"])
    assert getattr(ctx, "read_only_observation", None) is None
    if isinstance(client_kwargs.get("raises"), httpx.ReadTimeout):
        assert excinfo.value.provider_failure_classification == "provider_timeout"
