"""Tests for local_openclaw runtime practicality: timeout warning, config defaults,
config-source reporting, and the /ops/planning-config endpoint field.
"""

from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# Startup warning: warn_local_openclaw_timeout
# ---------------------------------------------------------------------------


def test_warning_fires_for_local_openclaw_when_timeout_is_zero(caplog, monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert any("below the safe threshold" in r.message for r in caplog.records)


def test_warning_fires_for_local_openclaw_below_safe_threshold(caplog, monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 60
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert any("below the safe threshold" in r.message for r in caplog.records)


def test_warning_suppressed_when_timeout_at_safe_level(caplog, monkeypatch):
    from app import config as config_module
    from app.config import LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings,
        "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
        LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS,
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert not any("below the safe threshold" in r.message for r in caplog.records)


def test_warning_suppressed_when_timeout_is_validated_value(caplog, monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert not any("below the safe threshold" in r.message for r in caplog.records)


def test_warning_suppressed_for_non_local_openclaw_backend(caplog, monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert not any("below the safe threshold" in r.message for r in caplog.records)


def test_warning_uses_planning_backend_over_agent_backend(caplog, monkeypatch):
    """PLANNING_BACKEND takes priority; if it's local_openclaw the warning applies."""
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", "local_openclaw")
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0
    )

    with caplog.at_level(logging.WARNING, logger="app.config"):
        config_module.warn_local_openclaw_timeout()

    assert any("below the safe threshold" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_default_direct_local_openclaw_timeout_is_240():
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS == 240


def test_safe_threshold_constant_is_120():
    from app.config import LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS

    assert LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS == 120


def test_validated_timeout_constant_is_240():
    from app.config import LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS

    assert LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS == 240


# ---------------------------------------------------------------------------
# Config-source reporting in _timeout_settings
# ---------------------------------------------------------------------------


def test_timeout_config_sources_present_in_timeout_settings():
    from app.services.build_identity import _timeout_settings

    ts = _timeout_settings()
    assert "timeout_config_sources" in ts
    sources = ts["timeout_config_sources"]
    assert "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS" in sources
    assert "PLANNING_REPAIR_TIMEOUT_SECONDS" in sources


def test_timeout_config_source_is_default_when_no_env_var(monkeypatch):
    monkeypatch.delenv("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PLANNING_REPAIR_TIMEOUT_SECONDS", raising=False)

    from app.services.build_identity import _timeout_settings

    ts = _timeout_settings()
    sources = ts["timeout_config_sources"]
    assert sources["PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS"] == "default"
    assert sources["PLANNING_REPAIR_TIMEOUT_SECONDS"] == "default"


def test_timeout_config_source_is_env_when_env_var_set(monkeypatch):
    monkeypatch.setenv("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("PLANNING_REPAIR_TIMEOUT_SECONDS", "240")

    from app.services.build_identity import _timeout_settings

    ts = _timeout_settings()
    sources = ts["timeout_config_sources"]
    assert sources["PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS"] == "env"
    assert sources["PLANNING_REPAIR_TIMEOUT_SECONDS"] == "env"


def test_timeout_settings_existing_int_keys_still_present():
    """Adding timeout_config_sources must not remove existing int-valued keys."""
    from app.services.build_identity import _timeout_settings

    ts = _timeout_settings()
    for key in (
        "planning_repair_timeout_seconds",
        "planning_synthesis_timeout_seconds",
        "replan_synthesis_timeout_seconds",
        "ollama_planning_timeout_seconds",
        "planning_direct_local_openclaw_timeout_seconds",
    ):
        assert key in ts, f"missing: {key}"
        assert isinstance(ts[key], int), f"{key} is not int"


# ---------------------------------------------------------------------------
# /ops/planning-config endpoint field (unit-level — no HTTP stack)
# ---------------------------------------------------------------------------


def test_planning_config_shape(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )
    monkeypatch.setattr(config_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr(
        config_module.settings, "PLANNING_REPAIR_DISABLE_THINKING", True
    )
    monkeypatch.delenv("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PLANNING_REPAIR_TIMEOUT_SECONDS", raising=False)

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)

    assert "planning_backend" in result
    assert "planning_direct_local_openclaw_timeout_seconds" in result
    assert "planning_repair_timeout_seconds" in result
    assert "thinking_disabled" in result
    assert "local_openclaw_timeout_warning" in result
    assert "computed_at" in result


def test_planning_config_no_warning_when_timeout_safe(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    assert result["local_openclaw_timeout_warning"] is None


def test_planning_config_warning_present_when_timeout_low(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0
    )

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    assert result["local_openclaw_timeout_warning"] is not None
    assert "safe threshold" in result["local_openclaw_timeout_warning"]


def test_planning_config_no_warning_for_non_local_openclaw(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "direct_ollama")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0
    )

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    assert result["local_openclaw_timeout_warning"] is None


def test_planning_config_direct_timeout_has_validated_value_field(monkeypatch):
    from app import config as config_module
    from app.config import LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    dt = result["planning_direct_local_openclaw_timeout_seconds"]
    assert dt["value"] == 240
    assert dt["validated_value"] == LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS


def test_planning_config_source_is_default_when_no_env(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )
    monkeypatch.delenv("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PLANNING_REPAIR_TIMEOUT_SECONDS", raising=False)

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    assert (
        result["planning_direct_local_openclaw_timeout_seconds"]["source"] == "default"
    )
    assert result["planning_repair_timeout_seconds"]["source"] == "default"


def test_planning_config_source_is_env_when_set(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "PLANNING_BACKEND", None)
    monkeypatch.setattr(config_module.settings, "AGENT_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        config_module.settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 240
    )
    monkeypatch.setenv("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("PLANNING_REPAIR_TIMEOUT_SECONDS", "240")

    from app.api.v1.endpoints.ops import ops_planning_config

    result = ops_planning_config(current_user=None)
    assert result["planning_direct_local_openclaw_timeout_seconds"]["source"] == "env"
    assert result["planning_repair_timeout_seconds"]["source"] == "env"
