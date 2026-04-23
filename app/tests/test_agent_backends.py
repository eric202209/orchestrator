from app.services.agents.agent_backends import (
    get_backend_descriptor,
    list_supported_backends,
)


def test_default_backend_descriptor_is_local_openclaw():
    descriptor = get_backend_descriptor(None)

    assert descriptor.name == "local_openclaw"
    assert descriptor.available is True
    assert descriptor.capabilities.supports_planning is True
    assert descriptor.capabilities.supports_checkpoint_resume is True


def test_unknown_backend_falls_back_to_local_openclaw():
    descriptor = get_backend_descriptor("future_backend")

    assert descriptor.name == "local_openclaw"


def test_supported_backends_contains_local_openclaw():
    names = [descriptor.name for descriptor in list_supported_backends()]

    assert "local_openclaw" in names
