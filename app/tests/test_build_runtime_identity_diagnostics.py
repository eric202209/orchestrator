"""Tests for build/runtime identity diagnostics hardening.

Covers the four fields added to build_identity_payload:
  - runtime_profile
  - timeout_settings
  - config_source_summary
  - stale_container_warning
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.services.observability.build_identity import (
    _config_source_summary,
    _stale_container_warning,
    _timeout_settings,
    build_identity_payload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


# ---------------------------------------------------------------------------
# runtime_profile
# ---------------------------------------------------------------------------


def test_build_identity_includes_runtime_profile(mem_db):
    payload = build_identity_payload(mem_db)
    assert "runtime_profile" in payload
    assert payload["runtime_profile"] in {
        "standard",
        "medium",
        "low_resource",
        "compact_local",
    }


def test_build_identity_runtime_profile_matches_settings(mem_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.observability.build_identity.settings.RUNTIME_PROFILE", "medium"
    )
    payload = build_identity_payload(mem_db)
    assert payload["runtime_profile"] == "medium"


# ---------------------------------------------------------------------------
# timeout_settings
# ---------------------------------------------------------------------------


def test_build_identity_includes_timeout_settings(mem_db):
    payload = build_identity_payload(mem_db)
    assert "timeout_settings" in payload
    ts = payload["timeout_settings"]
    for key in (
        "planning_repair_timeout_seconds",
        "planning_synthesis_timeout_seconds",
        "replan_synthesis_timeout_seconds",
        "ollama_planning_timeout_seconds",
        "planning_direct_local_openclaw_timeout_seconds",
    ):
        assert key in ts, f"missing: {key}"
        assert isinstance(ts[key], int)


def test_timeout_settings_reflects_settings_values(monkeypatch):
    monkeypatch.setattr(
        "app.services.observability.build_identity.settings.PLANNING_REPAIR_TIMEOUT_SECONDS",
        42,
    )
    monkeypatch.setattr(
        "app.services.observability.build_identity.settings.PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        77,
    )
    ts = _timeout_settings()
    assert ts["planning_repair_timeout_seconds"] == 42
    assert ts["planning_synthesis_timeout_seconds"] == 77


# ---------------------------------------------------------------------------
# config_source_summary
# ---------------------------------------------------------------------------


def test_build_identity_includes_config_source_summary(mem_db):
    payload = build_identity_payload(mem_db)
    assert "config_source_summary" in payload
    css = payload["config_source_summary"]
    assert "sources" in css
    assert "explicitly_set_env_vars" in css
    assert "active_legacy_aliases" in css
    assert "legacy_aliases_in_use" in css
    assert isinstance(css["sources"], list)
    assert isinstance(css["explicitly_set_env_vars"], list)
    assert isinstance(css["active_legacy_aliases"], list)
    assert isinstance(css["legacy_aliases_in_use"], bool)


def test_config_source_summary_detects_primary_env_var(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "local_openclaw")
    summary = _config_source_summary()
    assert "AGENT_BACKEND" in summary["explicitly_set_env_vars"]


def test_config_source_summary_detects_legacy_alias(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_AGENT_BACKEND", "local_openclaw")
    summary = _config_source_summary()
    assert summary["legacy_aliases_in_use"] is True
    assert any(
        "ORCHESTRATOR_AGENT_BACKEND" in entry
        for entry in summary["active_legacy_aliases"]
    )


def test_config_source_summary_no_legacy_aliases_by_default(monkeypatch):
    for alias in (
        "ORCHESTRATOR_AGENT_BACKEND",
        "ORCHESTRATOR_AGENT_MODEL_FAMILY",
        "PHASE7F_REPAIR_MODEL",
    ):
        monkeypatch.delenv(alias, raising=False)
    summary = _config_source_summary()
    legacy_for_these = [
        e
        for e in summary["active_legacy_aliases"]
        if any(
            a in e
            for a in (
                "ORCHESTRATOR_AGENT_BACKEND",
                "ORCHESTRATOR_AGENT_MODEL_FAMILY",
                "PHASE7F_REPAIR_MODEL",
            )
        )
    ]
    assert legacy_for_these == []


# ---------------------------------------------------------------------------
# stale_container_warning
# ---------------------------------------------------------------------------


def test_stale_container_warning_is_none_when_ok():
    assert _stale_container_warning("ok", "abc123", "abc123") is None


def test_stale_container_warning_is_none_when_unknown():
    assert _stale_container_warning("unknown", "unknown", None) is None


def test_stale_container_warning_non_null_when_stale():
    warning = _stale_container_warning("stale", "build-abc", "repo-xyz")
    assert warning is not None
    assert "build-abc" in warning
    assert "repo-xyz" in warning


def test_build_identity_stale_warning_absent_when_same_sha(mem_db, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_GIT_SHA", "same-sha")
    payload = build_identity_payload(mem_db, repo_sha_provider=lambda: "same-sha")
    assert payload["stale_container_check"] == "ok"
    assert payload["stale_container_warning"] is None


def test_build_identity_stale_warning_present_when_sha_differs(mem_db, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_GIT_SHA", "build-sha")
    payload = build_identity_payload(mem_db, repo_sha_provider=lambda: "repo-sha")
    assert payload["stale_container_check"] == "stale"
    warning = payload["stale_container_warning"]
    assert warning is not None
    assert "build-sha" in warning
    assert "repo-sha" in warning


# ---------------------------------------------------------------------------
# Shape completeness — all new fields are present alongside existing ones
# ---------------------------------------------------------------------------


def test_build_identity_shape_includes_all_new_fields(mem_db):
    payload = build_identity_payload(mem_db)
    new_fields = (
        "runtime_profile",
        "timeout_settings",
        "config_source_summary",
        "stale_container_warning",
    )
    for field in new_fields:
        assert field in payload, f"missing: {field}"
    existing_fields = (
        "git_sha",
        "build_time",
        "image_tag",
        "image_id",
        "planning_backend",
        "active_backend_lanes",
        "stale_container_check",
        "config_source",
    )
    for field in existing_fields:
        assert field in payload, f"regression — missing pre-existing field: {field}"
