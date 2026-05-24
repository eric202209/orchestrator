"""Tests for startup configuration validation (validate_runtime_secrets)."""

from __future__ import annotations

import pytest
from dataclasses import replace


def _call_validate(
    monkeypatch, *, secret_key="strong-unique-key", backend="local_openclaw"
):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", secret_key)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", backend)
    config_module.validate_runtime_secrets()


def test_validate_passes_with_valid_secret_and_default_backend(monkeypatch):
    from app import config as config_module
    from app.services.agents import agent_backends
    from app.services.agents.agent_backends import BackendHealth

    original = agent_backends.require_backend_descriptor("local_openclaw")
    monkeypatch.setattr(
        agent_backends,
        "require_backend_descriptor",
        lambda name: replace(
            original,
            health=BackendHealth(
                available=True,
                ready=True,
                status="ready",
                errors=[],
                warnings=[],
            ),
        ),
    )

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", "strong-unique-key")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")

    config_module.validate_runtime_secrets()


def test_settings_defuses_accidental_app_db_override():
    from app.config import DEFAULT_DATABASE_URL, Settings

    settings = Settings(DATABASE_URL="sqlite:///app.db")

    assert settings.DATABASE_URL == DEFAULT_DATABASE_URL


def test_settings_anchors_relative_sqlite_database_to_project_root():
    from app.config import BASE_DIR, Settings

    settings = Settings(DATABASE_URL="sqlite:///./orchestrator.db")

    assert settings.DATABASE_URL == f"sqlite:///{BASE_DIR / 'orchestrator.db'}"


def test_settings_accepts_short_runtime_config_names():
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        SECRET_KEY="strong-unique-key",
        AGENT_BACKEND="local_openclaw",
        AGENT_MODEL="local",
        PLANNING_REPAIR_ENABLED=False,
        LANGFUSE_ENABLED=True,
        WORKSPACE_REVIEW_POLICY="hold_all",
    )

    assert settings.AGENT_BACKEND == "local_openclaw"
    assert settings.AGENT_MODEL == "local"
    assert settings.PLANNING_REPAIR_ENABLED is False
    assert settings.LANGFUSE_ENABLED is True
    assert settings.WORKSPACE_REVIEW_POLICY == "hold_all"


def test_settings_keeps_legacy_orchestrator_env_aliases():
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        SECRET_KEY="strong-unique-key",
        ORCHESTRATOR_AGENT_BACKEND="openai_responses_api",
        ORCHESTRATOR_AGENT_MODEL_FAMILY="gpt-5",
        ORCHESTRATOR_PLANNING_REPAIR_DIRECT_ENABLED=False,
        ORCHESTRATOR_LANGFUSE_ENABLED=True,
        ORCHESTRATOR_FORCE_INLINE_PLANNING=True,
        ORCHESTRATOR_WORKSPACE_REVIEW_POLICY="auto_publish_all",
    )

    assert settings.AGENT_BACKEND == "openai_responses_api"
    assert settings.AGENT_MODEL == "gpt-5"
    assert settings.PLANNING_REPAIR_ENABLED is False
    assert settings.LANGFUSE_ENABLED is True
    assert settings.INLINE_PLANNING is True
    assert settings.WORKSPACE_REVIEW_POLICY == "auto_publish_all"


def test_settings_rejects_unknown_workspace_review_policy():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="WORKSPACE_REVIEW_POLICY"):
        Settings(
            _env_file=None,
            SECRET_KEY="strong-unique-key",
            WORKSPACE_REVIEW_POLICY="always_merge",
        )


def test_settings_applies_medium_runtime_profile_caps():
    from app.config import Settings

    settings = Settings(_env_file=None, RUNTIME_PROFILE="medium")

    assert settings.PLANNING_REPAIR_TIMEOUT_SECONDS == 60
    assert settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS == 120
    assert settings.REPLAN_SYNTHESIS_TIMEOUT_SECONDS == 38
    assert settings.KNOWLEDGE_MAX_ITEMS == 2
    assert settings.KNOWLEDGE_MAX_TOTAL_CHARS == 1400
    assert settings.MAX_PLAN_STEPS == 6


def test_settings_applies_low_resource_runtime_profile_caps():
    from app.config import Settings

    settings = Settings(_env_file=None, RUNTIME_PROFILE="low_resource")

    assert settings.PLANNING_REPAIR_TIMEOUT_SECONDS == 45
    assert settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS == 90
    assert settings.REPLAN_SYNTHESIS_TIMEOUT_SECONDS == 30
    assert settings.KNOWLEDGE_MAX_ITEMS == 1
    assert settings.KNOWLEDGE_MAX_TOTAL_CHARS == 800
    assert settings.MAX_PLAN_STEPS == 3


def test_settings_applies_compact_local_runtime_profile_caps():
    from app.config import Settings

    settings = Settings(_env_file=None, RUNTIME_PROFILE="compact_local")

    assert settings.PLANNING_REPAIR_TIMEOUT_SECONDS == 45
    assert settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS == 90
    assert settings.REPLAN_SYNTHESIS_TIMEOUT_SECONDS == 30
    assert settings.KNOWLEDGE_MAX_ITEMS == 1
    assert settings.KNOWLEDGE_MAX_TOTAL_CHARS == 800
    assert settings.MAX_PLAN_STEPS == 3


def test_settings_rejects_unknown_runtime_profile():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="RUNTIME_PROFILE"):
        Settings(_env_file=None, RUNTIME_PROFILE="oversized")


def test_settings_allows_import_without_secret_key():
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.SECRET_KEY == ""


def test_validate_raises_on_empty_secret_key(monkeypatch):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _call_validate(monkeypatch, secret_key="")


def test_validate_raises_on_unknown_backend(monkeypatch):
    with pytest.raises(RuntimeError, match="AGENT_BACKEND"):
        _call_validate(monkeypatch, backend="nonexistent_backend")


def test_validate_raises_when_openai_backend_lacks_api_key(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", "strong-unique-key")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(config_module.settings, "OPENAI_API_KEY", "")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        config_module.validate_runtime_secrets()


def test_validate_passes_when_openai_backend_has_api_key(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", "strong-unique-key")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(config_module.settings, "OPENAI_API_KEY", "sk-test-key-abc123")

    config_module.validate_runtime_secrets()


def test_validate_raises_when_openai_backend_has_whitespace_only_api_key(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", "strong-unique-key")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(config_module.settings, "OPENAI_API_KEY", "   ")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        config_module.validate_runtime_secrets()


def test_validate_raises_when_backend_is_registered_but_unimplemented(monkeypatch):
    with pytest.raises(RuntimeError, match="not implemented yet"):
        _call_validate(monkeypatch, backend="remote_openclaw_gateway")


def test_validate_raises_clear_error_for_unknown_role_backend(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "SECRET_KEY", "strong-unique-key")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(config_module.settings, "OPENAI_API_KEY", "sk-test-key-abc123")
    monkeypatch.setattr(config_module.settings, "EXECUTION_BACKEND", "missing_backend")

    with pytest.raises(RuntimeError, match="EXECUTION_BACKEND='missing_backend'"):
        config_module.validate_runtime_secrets()
