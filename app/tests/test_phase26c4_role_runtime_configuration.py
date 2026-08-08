"""Focused Phase 26C-4 Stage A role configuration compatibility tests."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.agents.agent_runtime import (
    BackendRole,
    RuntimeCapabilityError,
    UnsupportedRuntimeProfileError,
    create_agent_runtime,
    resolve_runtime_configuration,
    runtime_provider_role_matrix,
)
from app.services.agents.providers.ollama_adapter import OllamaRuntime
from app.services.agents.providers.openai_adapter import OpenAIResponsesRuntime
from app.services.agents.runtime_configuration import (
    RoleRuntimeConfiguration,
    RuntimeConfiguration,
)
from app.services.workspace.system_settings import (
    ADAPTATION_PROFILE_KEY,
    AGENT_BACKEND_KEY,
    AGENT_MODEL_FAMILY_KEY,
    get_setting_value,
    set_setting_value,
)


ALL_ROLES = tuple(BackendRole)


def _set_a0_compatible_settings(db_session, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", None)
    monkeypatch.setattr(settings, "REPAIR_BACKEND", None)
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", None)
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", None)
    monkeypatch.setattr(settings, "PLANNER_MODEL", "")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "")
    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "execution-model")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "repair-model")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 200000)
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "completion-model")
    for setting_name in (
        "PLANNING_ADAPTATION_PROFILE",
        "EXECUTION_ADAPTATION_PROFILE",
        "REPAIR_ADAPTATION_PROFILE",
        "DEBUG_REPAIR_ADAPTATION_PROFILE",
        "COMPLETION_REPAIR_ADAPTATION_PROFILE",
    ):
        monkeypatch.setattr(settings, setting_name, None)
    set_setting_value(db_session, AGENT_BACKEND_KEY, "local_openclaw")
    set_setting_value(db_session, AGENT_MODEL_FAMILY_KEY, "global-model")
    set_setting_value(db_session, ADAPTATION_PROFILE_KEY, "openclaw_default")


def test_role_runtime_configuration_is_canonical_and_alias_is_compatible():
    assert RuntimeConfiguration is RoleRuntimeConfiguration
    configuration = RoleRuntimeConfiguration(
        role="planning",
        backend_name="local_openclaw",
        model_family="planner-model",
        adaptation_profile="openclaw_default",
    )

    assert configuration.role is BackendRole.PLANNING
    assert configuration.to_dict() == {
        "role": "planning",
        "backend_name": "local_openclaw",
        "model_family": "planner-model",
        "adaptation_profile": "openclaw_default",
    }


def test_every_explicit_role_resolves_a_complete_configuration(db_session, monkeypatch):
    _set_a0_compatible_settings(db_session, monkeypatch)

    for role in ALL_ROLES:
        configuration = resolve_runtime_configuration(db_session, role)
        assert configuration.role is role
        assert configuration.backend_name == "local_openclaw"
        assert configuration.model_family
        assert configuration.adaptation_profile


def test_a0_planning_configuration_tuple_is_unchanged(db_session, monkeypatch):
    _set_a0_compatible_settings(db_session, monkeypatch)
    set_setting_value(db_session, AGENT_MODEL_FAMILY_KEY, "qwen3.6:27B")

    configuration = resolve_runtime_configuration(db_session, BackendRole.PLANNING)

    assert configuration.to_dict() == {
        "role": "planning",
        "backend_name": "local_openclaw",
        "model_family": "qwen3.6:27B",
        "adaptation_profile": "openclaw_default",
    }


def test_direct_ollama_execution_resolver_matches_legacy_adapter_selection(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "")
    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "ollama-execution")

    legacy_runtime = OllamaRuntime(db_session, session_id=None)
    legacy_runtime.backend_role = BackendRole.EXECUTION.value
    legacy_model = legacy_runtime._model
    legacy_profile = legacy_runtime._adaptation_profile().name

    configuration = resolve_runtime_configuration(db_session, BackendRole.EXECUTION)

    assert configuration.backend_name == "direct_ollama"
    assert configuration.model_family == legacy_model
    assert configuration.adaptation_profile == legacy_profile


def test_repair_and_debug_repair_keep_direct_path_model_backend_fallbacks(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "REPAIR_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", None)
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "repair-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "")

    repair = resolve_runtime_configuration(db_session, BackendRole.REPAIR)
    debug = resolve_runtime_configuration(db_session, BackendRole.DEBUG_REPAIR)

    assert repair.to_dict() == {
        "role": "repair",
        "backend_name": "direct_ollama",
        "model_family": "repair-model",
        "adaptation_profile": "ollama_default",
    }
    assert debug.to_dict() == {
        "role": "debug_repair",
        "backend_name": "direct_ollama",
        "model_family": "repair-model",
        "adaptation_profile": "ollama_default",
    }


def test_completion_repair_explicit_model_is_role_owned(db_session, monkeypatch):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "qwen3-coder:30b")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "qwen-local")

    configuration = resolve_runtime_configuration(
        db_session, BackendRole.COMPLETION_REPAIR
    )

    assert configuration.to_dict() == {
        "role": "completion_repair",
        "backend_name": "direct_ollama",
        "model_family": "qwen3-coder:30b",
        "adaptation_profile": "ollama_default",
    }


def test_role_inventory_reports_completion_repair_from_runtime_resolver(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "qwen3-coder:30b")

    matrix = runtime_provider_role_matrix(db_session)
    completion = matrix["completion_repair"]

    assert completion["backend"] == "direct_ollama"
    assert completion["role"] == "completion_repair"
    assert completion["provider_ready"] is True
    assert completion["model"] == "qwen3-coder:30b"
    assert completion["adaptation_profile"] == "ollama_default"


def test_completion_repair_blank_model_fails_closed_without_cross_lane_fallback(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "execution-model")
    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "ollama-model")

    with pytest.raises(RuntimeCapabilityError, match="COMPLETION_REPAIR_MODEL"):
        resolve_runtime_configuration(db_session, BackendRole.COMPLETION_REPAIR)


@pytest.mark.parametrize(
    ("role", "backend", "profile_setting", "profile"),
    [
        (
            BackendRole.EXECUTION,
            "direct_ollama",
            "EXECUTION_ADAPTATION_PROFILE",
            "ollama_default",
        ),
        (
            BackendRole.REPAIR,
            "direct_ollama",
            "REPAIR_ADAPTATION_PROFILE",
            "ollama_default",
        ),
        (
            BackendRole.DEBUG_REPAIR,
            "direct_ollama",
            "DEBUG_REPAIR_ADAPTATION_PROFILE",
            "ollama_default",
        ),
        (
            BackendRole.COMPLETION_REPAIR,
            "direct_ollama",
            "COMPLETION_REPAIR_ADAPTATION_PROFILE",
            "ollama_default",
        ),
    ],
)
def test_explicit_role_profile_is_resolved_and_wins_over_global(
    db_session, monkeypatch, role, backend, profile_setting, profile
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, f"{role.name}_BACKEND", backend)
    monkeypatch.setattr(settings, profile_setting, profile)

    configuration = resolve_runtime_configuration(db_session, role)

    assert configuration.adaptation_profile == profile


def test_supplied_configuration_wins_over_legacy_adapter_fallbacks(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "legacy-ollama-model")

    configuration = RoleRuntimeConfiguration(
        role=BackendRole.EXECUTION,
        backend_name="openai_responses_api",
        model_family="supplied-model",
        adaptation_profile="openai_responses_default",
    )
    runtime = OpenAIResponsesRuntime(
        db_session, session_id=None, runtime_configuration=configuration
    )

    assert runtime._model_name() == "supplied-model"
    metadata = runtime.get_backend_metadata()
    assert metadata["model_family"] == "supplied-model"
    assert metadata["adaptation_profile"] == "openai_responses_default"
    assert metadata["runtime_configuration"] == configuration.to_dict()


def test_factory_uniformly_attaches_configuration_and_metadata(db_session, monkeypatch):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "REPAIR_BACKEND", "local_openclaw")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "planner-model")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "execution-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "debug-model")
    monkeypatch.setattr(settings, "COMPLETION_REPAIR_MODEL", "completion-model")

    for role in ALL_ROLES:
        runtime = create_agent_runtime(db_session, session_id=None, role=role)
        configuration = runtime.runtime_configuration
        assert isinstance(configuration, RoleRuntimeConfiguration)
        assert configuration.role is role
        assert configuration.backend_name
        assert configuration.model_family
        assert configuration.adaptation_profile
        metadata = runtime.get_backend_metadata()
        assert metadata["role"] == role.value
        assert metadata["runtime_configuration"] == configuration.to_dict()


def test_roleless_factory_preserves_legacy_unscoped_metadata(db_session, monkeypatch):
    _set_a0_compatible_settings(db_session, monkeypatch)
    runtime = create_agent_runtime(db_session, session_id=None)

    assert runtime.runtime_configuration is None
    assert "runtime_configuration" not in runtime.get_backend_metadata()
    assert "role" not in runtime.get_backend_metadata()


def test_backend_override_changes_backend_but_uses_role_resolution(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "local_openclaw")

    configuration = resolve_runtime_configuration(
        db_session,
        BackendRole.EXECUTION,
        backend_override="direct_ollama",
    )

    assert configuration.backend_name == "direct_ollama"
    assert configuration.model_family == "execution-model"
    assert configuration.adaptation_profile == "ollama_default"


def test_explicit_incompatible_role_profile_fails_closed_without_persistence(
    db_session, monkeypatch
):
    _set_a0_compatible_settings(db_session, monkeypatch)
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "direct_ollama")
    monkeypatch.setattr(settings, "EXECUTION_ADAPTATION_PROFILE", "openclaw_default")

    with pytest.raises(UnsupportedRuntimeProfileError):
        resolve_runtime_configuration(db_session, BackendRole.EXECUTION)

    assert (
        get_setting_value(db_session, "orchestrator_execution_adaptation_profile")
        is None
    )
