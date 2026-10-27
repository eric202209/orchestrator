"""Offline Phase 40E-4 single-model routing identity regressions."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.services.agents.agent_runtime import (
    BackendRole,
    RuntimeCapabilityError,
    create_agent_runtime,
    resolve_runtime_configuration,
    validate_runtime_capabilities,
)
from app.services.agents.agent_backends import (
    ExecutionTopology,
    require_backend_descriptor,
)
from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import (
    InferenceRoutingPolicy,
    RoleRuntimeConfiguration,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.planning.planner import PlannerService


def _single_model_settings(monkeypatch) -> None:
    values = {
        "LOW_RESOURCE_SINGLE_MODEL": True,
        "AGENT_BACKEND": "openai_chat_completions",
        "PLANNING_BACKEND": "openai_chat_completions",
        "EXECUTION_BACKEND": "openai_chat_completions",
        "PLANNER_MODEL": "qwen-local",
        "EXECUTION_MODEL": "qwen-local",
        "AGENT_MODEL": "qwen-local",
        "PLANNING_DIRECT_BASE_URL": "http://vllm.test:8000/v1",
        "PLANNING_DIRECT_API_KEY": "single-model-key",
        "PLANNING_ADAPTATION_PROFILE": "ollama_default",
        "EXECUTION_ADAPTATION_PROFILE": "ollama_default",
        "EXECUTION_CONTEXT_TOKENS": 131072,
        "PLANNING_REPAIR_CONTEXT_TOKENS": 131072,
        "DEBUG_REPAIR_CONTEXT_TOKENS": 131072,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)


def test_single_model_profile_resolves_same_typed_identity_for_every_role(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)

    configurations = {
        role: resolve_runtime_configuration(db_session, role) for role in BackendRole
    }

    assert {cfg.routing_policy for cfg in configurations.values()} == {
        InferenceRoutingPolicy.SINGLE_MODEL
    }
    assert {cfg.backend_name for cfg in configurations.values()} == {
        "openai_chat_completions"
    }
    assert {cfg.provider_endpoint for cfg in configurations.values()} == {
        "http://vllm.test:8000/v1"
    }
    assert {cfg.model_family for cfg in configurations.values()} == {"qwen-local"}
    assert {cfg.fallback_enabled for cfg in configurations.values()} == {False}
    assert configurations[BackendRole.DEBUG_REPAIR].to_dict()["routing_policy"] == (
        "single_model"
    )


def test_single_model_profile_rejects_unscoped_legacy_runtime(db_session, monkeypatch):
    _single_model_settings(monkeypatch)

    with pytest.raises(RuntimeCapabilityError, match="explicit runtime role"):
        create_agent_runtime(db_session, session_id=None)


def test_single_model_profile_rejects_backend_substitution(db_session, monkeypatch):
    _single_model_settings(monkeypatch)

    with pytest.raises(RuntimeCapabilityError, match="canonical backend"):
        resolve_runtime_configuration(
            db_session,
            BackendRole.EXECUTION,
            backend_override="direct_ollama",
        )


def test_single_model_profile_rejects_nested_openclaw_backend(db_session, monkeypatch):
    _single_model_settings(monkeypatch)
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "local_openclaw")

    with pytest.raises(
        RuntimeCapabilityError, match="cannot require the local_openclaw"
    ):
        resolve_runtime_configuration(db_session, BackendRole.EXECUTION)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:pass@vllm.test:8000/v1",
        "http://vllm.test:8000/v1?token=secret",
        "http://vllm.test:8000/v1#fragment",
    ],
)
def test_single_model_endpoint_rejects_embedded_credentials_or_query(
    db_session, monkeypatch, endpoint
):
    _single_model_settings(monkeypatch)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", endpoint)

    with pytest.raises(RuntimeCapabilityError, match="without embedded credentials"):
        resolve_runtime_configuration(db_session, BackendRole.PLANNING)


class _Response:
    status_code = 200
    headers = {"x-request-id": "provider-request-7"}

    def __init__(self, body: dict):
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _AsyncClient:
    def __init__(self, response: _Response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return self.response


def _chat_response(model: str) -> dict:
    return {
        "id": "chatcmpl-7",
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


def test_single_model_request_records_correlated_route_and_response_identity(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)
    configuration = resolve_runtime_configuration(db_session, BackendRole.REPAIR)
    runtime = OpenAIChatCompletionsRuntime(
        db_session,
        session_id=12,
        task_id=34,
        runtime_configuration=configuration,
    )
    runtime.task_execution_id = 56
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(_Response(_chat_response("qwen-local"))),
    )

    output = asyncio.run(
        runtime._chat(
            system="system contract",
            user="return json",
            timeout_seconds=2,
            diagnostic_label="planning_repair",
            diagnostic_metadata={"stage": "planning_repair"},
        )
    )

    evidence = runtime._last_runtime_diagnostics
    assert output == "{}"
    assert evidence["request_id"]
    assert evidence["task_id"] == 34
    assert evidence["task_execution_id"] == 56
    assert evidence["logical_stage"] == "planning_repair"
    assert evidence["runtime_role"] == "repair"
    assert evidence["routing_policy"] == "single_model"
    assert evidence["provider_adapter"] == "openai_chat_completions"
    assert evidence["endpoint_identity"] == "http://vllm.test:8000/v1"
    assert evidence["configured_model"] == evidence["provider_response_model"]
    assert evidence["provider_response_id"] == "chatcmpl-7"
    assert evidence["provider_request_correlation_id"] == "provider-request-7"
    assert evidence["model_identity_status"] == "response_alias_match"
    assert evidence["fallback_enabled"] is False
    assert evidence["completion_status"] == "completed"
    assert len(evidence["prompt_sha256"]) == 64
    assert len(evidence["provider_response_sha256"]) == 64


def test_single_model_request_rejects_role_model_mismatch_before_dispatch(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)
    configuration = resolve_runtime_configuration(db_session, BackendRole.REPAIR)
    runtime = OpenAIChatCompletionsRuntime(
        db_session, session_id=None, runtime_configuration=configuration
    )
    runtime.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.REPAIR,
        backend_name="openai_chat_completions",
        model_family="unapproved-model",
        adaptation_profile="ollama_default",
        routing_policy=InferenceRoutingPolicy.SINGLE_MODEL,
        provider_endpoint="http://vllm.test:8000/v1",
        fallback_enabled=False,
    )
    dispatches = []
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **_kwargs: dispatches.append("constructed")
        or _AsyncClient(_Response(_chat_response("unapproved-model"))),
    )

    with pytest.raises(AgentRuntimeError, match="identity mismatch") as captured:
        asyncio.run(
            runtime._chat(
                system="system",
                user="prompt",
                timeout_seconds=2,
                invocation_options=RuntimeInvocationOptions(),
            )
        )

    assert "model" in captured.value.runtime_diagnostics["identity_mismatch_fields"]
    assert dispatches == []


def test_single_model_auth_does_not_inherit_generic_openai_credential(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "generic-openai-key")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_API_KEY", "")
    configuration = resolve_runtime_configuration(db_session, BackendRole.REPAIR)
    runtime = OpenAIChatCompletionsRuntime(
        db_session, session_id=None, runtime_configuration=configuration
    )

    assert runtime._invocation_api_key(RuntimeInvocationOptions()) == ""


def test_single_model_request_fails_closed_on_response_model_substitution(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)
    configuration = resolve_runtime_configuration(db_session, BackendRole.EXECUTION)
    runtime = OpenAIChatCompletionsRuntime(
        db_session, session_id=None, runtime_configuration=configuration
    )
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(_Response(_chat_response("different-model"))),
    )

    with pytest.raises(AgentRuntimeError, match="response identity") as captured:
        asyncio.run(
            runtime._chat(
                system="system",
                user="prompt",
                timeout_seconds=2,
                invocation_options=RuntimeInvocationOptions(),
            )
        )

    assert (
        captured.value.runtime_diagnostics["model_identity_status"]
        == "response_alias_mismatch_or_missing"
    )
    assert captured.value.runtime_diagnostics["completion_status"] == "failed"


def test_single_model_planning_repair_does_not_fall_back_to_parent_runtime(
    db_session, monkeypatch
):
    _single_model_settings(monkeypatch)
    fallback_attempts = []

    class ParentRuntime:
        db = db_session
        session_id = 12
        task_id = 34
        runtime_configuration = object()

        @staticmethod
        def get_backend_metadata():
            fallback_attempts.append("metadata")
            return {"backend": "local_openclaw"}

    class FailingRepairRuntime:
        backend_descriptor = type(
            "Descriptor", (), {"name": "openai_chat_completions"}
        )()

        async def invoke_prompt(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.services.agents.agent_runtime.create_agent_runtime",
        lambda *_args, **_kwargs: FailingRepairRuntime(),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(
            PlannerService._invoke_repair_prompt(
                ParentRuntime(), "repair prompt", 5, allow_registry_fallback=True
            )
        )

    assert fallback_attempts == []


def test_role_based_runtime_configuration_serialization_stays_compatible():
    configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="local_openclaw",
        model_family="local",
        adaptation_profile="openclaw_default",
    )

    assert configuration.to_dict() == {
        "role": "planning",
        "backend_name": "local_openclaw",
        "model_family": "local",
        "adaptation_profile": "openclaw_default",
    }


def test_single_model_chat_adapter_uses_orchestrator_owned_execution_without_native_tools():
    descriptor = require_backend_descriptor("openai_chat_completions")

    readiness = validate_runtime_capabilities(
        descriptor,
        BackendRole.EXECUTION,
        effective_context_tokens=131072,
        dispatch=False,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert readiness["provider_ready"] is True
    assert descriptor.capabilities.supports_tool_execution is False
    assert (
        descriptor.capabilities.missing_execution_capabilities(
            ExecutionTopology.STRUCTURED_ORCHESTRATOR
        )
        == []
    )
    assert descriptor.capabilities.missing_execution_capabilities(
        ExecutionTopology.AGENT_RUNTIME
    )
