"""Phase 28Q Planning Provider seam and selection regression tests."""

from pathlib import Path

import pytest

from app.config import settings
from app.services.planning.providers import (
    PlanningArtifactKind,
    PlanningProvider,
    PlanningProviderExecutionError,
    PlanningRequest,
    PlanningRuntimeOptions,
    ProviderFailureOrigin,
    ReasoningControls,
    SamplingControls,
    UnsupportedPlanningProviderError,
    create_planning_provider,
    list_planning_provider_names,
)
from app.services.planning.providers.openclaw import OpenClawPlanningProvider
from app.services.planning.structured_task_plan_stage import (
    build_protocol_v2_stage_definitions,
)


def _request(
    artifact_kind: PlanningArtifactKind = PlanningArtifactKind.PLANNING_BRIEF,
) -> PlanningRequest:
    return PlanningRequest(
        artifact_kind=artifact_kind,
        prompt="unchanged protocol prompt",
        protocol_input={"protocol_version": "v2"},
        runtime_options=PlanningRuntimeOptions(timeout_seconds=360),
        reasoning=ReasoningControls(enabled=False),
        sampling=SamplingControls(temperature=0),
        project_id=17,
        metadata={"manifest_id": "manifest:17"},
    )


def test_provider_interface_request_and_capabilities_are_provider_neutral():
    provider = OpenClawPlanningProvider(None)
    request = _request()

    assert isinstance(provider, PlanningProvider)
    assert request.artifact_kind is PlanningArtifactKind.PLANNING_BRIEF
    assert request.protocol_input == {"protocol_version": "v2"}
    assert request.reasoning.enabled is False
    assert request.sampling.temperature == 0
    assert "openclaw" not in request.__dataclass_fields__

    capabilities = provider.capabilities.to_dict()
    assert capabilities["supports_reasoning_control"] is True
    assert capabilities["supports_timeout_control"] is True
    assert capabilities["supports_prompt_ownership"] is False
    assert capabilities["supports_request_ownership"] is False
    assert capabilities["supports_response_format"] is False
    assert capabilities["supports_seed"] is False
    assert capabilities["supports_top_p"] is False


def test_selection_defaults_to_openclaw_and_registers_direct_adapter(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "PLANNING_PROVIDER", "openclaw")

    provider = create_planning_provider(db_session)

    assert isinstance(provider, OpenClawPlanningProvider)
    assert provider.name == "openclaw"
    assert list_planning_provider_names() == (
        "direct_openai_compatible",
        "openclaw",
    )


def test_selection_rejects_unimplemented_future_provider(db_session, monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_PROVIDER", "direct-openai-compatible")

    with pytest.raises(UnsupportedPlanningProviderError, match="Unsupported"):
        create_planning_provider(db_session)


def test_default_v2_graph_injects_one_provider_into_both_stages(db_session):
    provider = OpenClawPlanningProvider(db_session)

    brief_stage, task_plan_stage = build_protocol_v2_stage_definitions(
        db_session, planning_provider=provider
    )

    assert brief_stage.provider is provider
    assert task_plan_stage.provider is provider


def test_planning_stages_do_not_import_openclaw_or_runtime_transport():
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "app/services/planning/planning_brief_stage.py",
        "app/services/planning/structured_task_plan_stage.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "openclaw" not in source.lower()
        assert "invoke_runtime_prompt" not in source
        assert "BackendRole" not in source


def test_openclaw_adapter_normalizes_response_metadata(monkeypatch):
    def fake_invoke(_db, prompt, **kwargs):
        assert prompt == "unchanged protocol prompt"
        assert kwargs["role"].value == "planning"
        assert {key: value for key, value in kwargs.items() if key != "role"} == {
            "project_id": 17,
            "source_brain": "local",
            "timeout_seconds": 360,
            "session_prefix": "planning-brief",
        }
        return {
            "status": "completed",
            "output": '{"candidate":true}',
            "openclaw_version": "2026.4.10",
            "token_usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
            "runtime_diagnostics": {
                "diagnostic_category": "provider_success",
                "duration_seconds": 1.25,
                "backend": "local_openclaw",
                "model_family": "qwen",
                "adaptation_profile": "qwen_compact_json",
                "role": "planning",
            },
        }

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )

    response = OpenClawPlanningProvider(None).generate(_request())

    assert response.candidate_text == '{"candidate":true}'
    assert response.provider_name == "openclaw"
    assert response.provider_version == "2026.4.10"
    assert response.latency_seconds == 1.25
    assert response.diagnostics.category == "provider_success"
    assert "invocation" not in response.diagnostics.details
    assert response.runtime_metadata.runtime_name == "local_openclaw"
    assert response.runtime_metadata.model == "qwen"
    assert response.token_usage.total_tokens == 18


def test_openclaw_adapter_normalizes_runtime_failure(monkeypatch):
    def fake_invoke(*_args, **_kwargs):
        error = RuntimeError("bounded timeout")
        error.provider_failure_classification = "provider_timeout"
        raise error

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )

    with pytest.raises(PlanningProviderExecutionError) as caught:
        OpenClawPlanningProvider(None).generate(_request())

    assert caught.value.classification == "provider_timeout"
    assert caught.value.origin is ProviderFailureOrigin.INVOCATION
