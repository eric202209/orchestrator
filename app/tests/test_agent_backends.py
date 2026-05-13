from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
)
from app.services.model_adaptation import resolve_adaptation_profile


def test_default_backend_descriptor_is_local_openclaw():
    descriptor = get_backend_descriptor(None)

    assert descriptor.name == "local_openclaw"
    assert descriptor.health.status in {"ready", "degraded"}
    assert descriptor.capabilities.supports_planning is True
    assert descriptor.capabilities.supports_checkpoint_resume is True


def test_unknown_backend_is_rejected():
    try:
        get_backend_descriptor("future_backend")
    except UnsupportedAgentBackendError as exc:
        assert "Unsupported orchestration backend" in str(exc)
        return

    raise AssertionError("Expected UnsupportedAgentBackendError")


def test_supported_backends_contains_registered_future_metadata():
    descriptors = list_supported_backends()
    names = [descriptor.name for descriptor in descriptors]

    assert "local_openclaw" in names
    assert "docker_openclaw" in names
    assert "openai_responses_api" in names
    docker_backend = next(
        descriptor for descriptor in descriptors if descriptor.name == "docker_openclaw"
    )
    assert docker_backend.implemented is True
    assert docker_backend.config.transport_mode == "docker_cli"
    assert docker_backend.health.status in {"ready", "degraded"}
    backend = next(
        descriptor
        for descriptor in descriptors
        if descriptor.name == "openai_responses_api"
    )
    assert backend.implemented is True
    assert backend.config.transport_mode == "api"
    assert backend.config.supported_prompt_format == "structured_prompt_envelope"
    assert backend.config.prompt_dialect == "responses_json"
    assert backend.config.tool_call_shape == "responses_tools"
    assert backend.health.status in {"ready", "degraded"}


def test_resolve_adaptation_profile_prefers_matching_backend_and_model_family():
    profile = resolve_adaptation_profile(
        backend="openai_responses_api",
        model_family="gpt-5.5",
        preferred_name="openai_responses_structured",
    )

    assert profile.backend == "openai_responses_api"
    assert profile.name == "openai_responses_structured"
    assert profile.prompt_dialect == "responses_json"
