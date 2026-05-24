from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.models import Project, Session as SessionModel
from app.config import settings
from app.services.model_adaptation import resolve_adaptation_profile


def test_default_backend_descriptor_is_local_openclaw():
    descriptor = get_backend_descriptor(None)

    assert descriptor.name == "local_openclaw"
    assert descriptor.health.status in {"ready", "degraded"}
    assert descriptor.capabilities.supports_planning is True
    assert descriptor.capabilities.supports_checkpoint_resume is True
    assert descriptor.lane_traits.structured_output_reliability == "variable"
    assert descriptor.to_dict()["lane_traits"]["configured_available"] in {
        True,
        False,
    }


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
    assert backend.lane_traits.evidence_following == "strong"
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


def test_openclaw_cli_args_are_parsed_into_resolved_command(
    db_session, monkeypatch, tmp_path
):
    cli_path = tmp_path / "openclaw"
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o755)
    monkeypatch.setattr(settings, "OPENCLAW_CLI_PATH", str(cli_path))
    monkeypatch.setattr(settings, "OPENCLAW_CLI_ARGS", '--profile "load test" --json')

    project = Project(name="CLI Args Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(name="CLI Args Session", project_id=project.id)
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = OpenClawSessionService(db_session, session.id)

    assert service._resolve_openclaw_command() == [
        str(cli_path),
        "--profile",
        "load test",
        "--json",
    ]
