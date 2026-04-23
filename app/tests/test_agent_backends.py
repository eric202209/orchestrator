from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
)


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
    assert "openai_responses_api" in names
    planned = next(
        descriptor
        for descriptor in descriptors
        if descriptor.name == "openai_responses_api"
    )
    assert planned.health.status == "not_implemented"
    assert planned.health.errors
