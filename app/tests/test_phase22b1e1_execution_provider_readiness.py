"""Phase 22B-1E1 execution and bounded-debug provider contract regressions."""

import pytest

from app.config import settings
from app.services.agents.agent_runtime import (
    BackendRole,
    RuntimeCapabilityError,
    validate_runtime_capabilities,
    validate_runtime_provider_contract,
)
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)


def test_execution_context_below_contract_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 8192)
    descriptor = get_backend_descriptor("local_openclaw")

    with pytest.raises(RuntimeCapabilityError) as exc_info:
        validate_runtime_capabilities(
            descriptor,
            BackendRole.EXECUTION,
            effective_context_tokens=8192,
        )

    assert exc_info.value.code == "provider_context_insufficient"
    assert "16000" in str(exc_info.value)


def test_execution_context_at_contract_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    descriptor = get_backend_descriptor("local_openclaw")

    result = validate_runtime_capabilities(
        descriptor,
        BackendRole.EXECUTION,
        effective_context_tokens=65536,
    )

    assert result["effective_context_tokens"] == 65536
    assert result["required_context_tokens"] == 16000


def test_execution_model_catalog_context_is_verified_before_dispatch(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    monkeypatch.setattr(
        "app.services.agents.agent_runtime._read_openclaw_model_catalog",
        lambda: [
            {
                "key": "ollama/qwen3-coder:30b",
                "contextWindow": 8192,
                "missing": False,
            }
        ],
    )

    with pytest.raises(RuntimeCapabilityError) as exc_info:
        validate_runtime_provider_contract(db_session, BackendRole.EXECUTION)

    assert exc_info.value.code == "provider_context_insufficient"


def test_execution_model_catalog_identity_and_context_are_retained(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    monkeypatch.setattr(
        "app.services.agents.agent_runtime._read_openclaw_model_catalog",
        lambda: [
            {
                "key": "ollama/qwen3-coder:30b",
                "contextWindow": 65536,
                "missing": False,
            }
        ],
    )

    result = validate_runtime_provider_contract(db_session, BackendRole.EXECUTION)

    assert result["backend"] == "local_openclaw"
    assert result["model"] == "qwen3-coder:30b"
    assert result["provider_model"] == "ollama/qwen3-coder:30b"
    assert result["effective_context_tokens"] == 65536


def test_debug_runtime_without_invocation_options_uses_role_endpoint(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "http://debug-gateway/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "debug-key")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "debug-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 200000)

    runtime = OpenAIChatCompletionsRuntime(
        db_session,
        session_id=None,
        runtime_configuration=type(
            "Config",
            (),
            {
                "backend_name": "openai_chat_completions",
                "role": BackendRole.DEBUG_REPAIR,
                "model_family": "debug-model",
            },
        )(),
    )

    assert runtime._invocation_base_url(None) == "http://debug-gateway/v1"
    assert runtime._invocation_api_key(None) == "debug-key"


def test_invalid_debug_endpoint_fails_closed_before_repair(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "not-a-url")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "debug-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 200000)

    with pytest.raises(RuntimeCapabilityError) as exc_info:
        validate_runtime_provider_contract(db_session, BackendRole.DEBUG_REPAIR)

    assert exc_info.value.code == "provider_endpoint_incompatible"


def test_public_openai_default_is_not_used_for_configured_debug_role(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "http://ai-gateway:8000/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 200000)

    runtime = OpenAIChatCompletionsRuntime(
        db_session,
        session_id=None,
        runtime_configuration=type(
            "Config",
            (),
            {
                "backend_name": "openai_chat_completions",
                "role": BackendRole.DEBUG_REPAIR,
                "model_family": "qwen-local",
            },
        )(),
    )

    assert "api.openai.com" not in runtime._invocation_base_url(None)
    assert runtime._invocation_base_url(None) == "http://ai-gateway:8000/v1"
