"""Langfuse tracing helpers should fail open and emit compact payloads.

Tests exercise both the new ObservabilityService API and the backwards
compatible module-level shims.
"""

from __future__ import annotations

import types

from app.config import settings
from app.services.observability import (
    ObservabilityService,
    build_text_trace_payload,
    flush_langfuse,
    is_tracing_enabled,
    langfuse_tracing_enabled,
    reset_for_tests,
    reset_langfuse_client_for_tests,
    start_langfuse_observation,
    start_observation,
    update_langfuse_observation,
)
from app.services.observability.langfuse import (
    ObservabilityService as ObsServiceFromLangfuse,
    build_text_trace_payload as bttp_langfuse,
    flush_langfuse as fl_langfuse,
    reset_langfuse_client_for_tests as reset_langfuse_client_for_tests_direct,
    start_langfuse_observation as slo_langfuse,
    update_langfuse_observation as ulo_langfuse,
)

# -----------------------------------------------------------------------
# Module-level (backwards-compatible) API
# -----------------------------------------------------------------------


def test_build_text_trace_payload_truncates_large_values():
    payload = build_text_trace_payload("x" * 700, max_preview_chars=20)

    assert payload == {
        "preview": ("x" * 20) + "...",
        "chars": 700,
        "lines": 1,
    }


def test_langfuse_helpers_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    reset_langfuse_client_for_tests()

    with start_langfuse_observation(name="disabled-span") as observation:
        assert observation is None
        update_langfuse_observation(observation, output={"status": "ok"})

    flush_langfuse()


def test_langfuse_helpers_emit_when_sdk_available(monkeypatch):
    captured = {
        "init": None,
        "start": None,
        "updates": [],
        "flushed": False,
    }

    class FakeObservation:
        def update(self, **kwargs):
            captured["updates"].append(kwargs)

    class FakeContextManager:
        def __enter__(self):
            return FakeObservation()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLangfuse:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def start_as_current_observation(self, **kwargs):
            captured["start"] = kwargs
            return FakeContextManager()

        def flush(self):
            captured["flushed"] = True

    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(settings, "LANGFUSE_BASE_URL", "http://localhost:3001")
    monkeypatch.setattr(settings, "LANGFUSE_ENVIRONMENT", "test")
    monkeypatch.setitem(
        __import__("sys").modules,
        "langfuse",
        types.SimpleNamespace(Langfuse=FakeLangfuse),
    )
    reset_langfuse_client_for_tests()

    with start_langfuse_observation(
        name="unit-test-span",
        as_type="generation",
        input={"preview": "hello"},
        metadata={"task_id": 7},
        model="gpt-test",
    ) as observation:
        update_langfuse_observation(
            observation,
            output={"status": "completed"},
            usage_details={"input": 10, "output": 20},
        )

    flush_langfuse()

    assert captured["init"]["public_key"] == "pk-test"
    assert captured["init"]["secret_key"] == "sk-test"
    assert captured["init"]["base_url"] == "http://localhost:3001"
    assert captured["start"]["name"] == "unit-test-span"
    assert captured["start"]["as_type"] == "generation"
    assert captured["start"]["model"] == "gpt-test"
    assert captured["updates"] == [
        {
            "output": {"status": "completed"},
            "usage_details": {"input": 10, "output": 20},
        }
    ]
    assert captured["flushed"] is True


# -----------------------------------------------------------------------
# ObservabilityService class API
# -----------------------------------------------------------------------


def test_observability_service_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")

    svc = ObservabilityService()
    assert not svc.is_enabled()

    with svc.observation(name="noop-span") as obs:
        assert obs is None

    svc.flush()  # must not raise


def test_observability_service_emits_when_configured(monkeypatch):
    captured = {"observations": [], "flushed": False}

    class FakeObs:
        def update(self, **kwargs):
            captured["observations"].append(kwargs)

    class FakeCM:
        def __enter__(self):
            return FakeObs()

        def __exit__(self, *args):
            pass

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def start_as_current_observation(self, **kwargs):
            return FakeCM()

        def flush(self):
            captured["flushed"] = True

    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(settings, "LANGFUSE_BASE_URL", "")
    monkeypatch.setattr(settings, "LANGFUSE_ENVIRONMENT", "test")
    monkeypatch.setitem(
        __import__("sys").modules,
        "langfuse",
        types.SimpleNamespace(Langfuse=FakeClient),
    )

    svc = ObservabilityService()
    assert svc.is_enabled()

    with svc.observation(name="svc-span") as obs:
        assert obs is not None
        obs.update(output={"x": 1})

    svc.flush()
    assert captured["flushed"] is True


def test_observability_service_sdk_not_installed(monkeypatch):
    """When SDK is missing, service falls back to no-op mode."""
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk")

    # Block langfuse import so service falls back to no-op
    monkeypatch.setitem(__import__("sys").modules, "langfuse", None)

    svc = ObservabilityService()
    assert svc.is_enabled()

    # Must not raise even though SDK is missing
    with svc.observation(name="missing-sdk") as obs:
        assert obs is None
    svc.flush()


# -----------------------------------------------------------------------
# Backwards-compat: langfuse.py re-exports
# -----------------------------------------------------------------------


def test_langfuse_module_reexports_work():
    """Ensure langfuse.py re-exports the same symbols."""
    assert bttp_langfuse is build_text_trace_payload
    assert fl_langfuse is flush_langfuse
    assert reset_langfuse_client_for_tests_direct is reset_for_tests
    assert slo_langfuse is start_langfuse_observation
    assert ulo_langfuse is update_langfuse_observation
    assert ObsServiceFromLangfuse is ObservabilityService


# -----------------------------------------------------------------------
# Cross-cutting: aliases resolve to same callable
# -----------------------------------------------------------------------


def test_aliases_resolve_to_same_callable():
    assert is_tracing_enabled is langfuse_tracing_enabled
    assert start_observation is start_langfuse_observation


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


def test_update_on_none_observation():
    """update_langfuse_observation must be safe when passed None."""
    update_langfuse_observation(
        None,
        output={"status": "ok"},
        level="INFO",
        status_message="fine",
    )  # must not raise


def test_build_text_trace_payload_edge_cases():
    assert build_text_trace_payload(None) is None
    assert build_text_trace_payload("") is None
    assert build_text_trace_payload("   ") is None

    payload = build_text_trace_payload("hello")
    assert payload["preview"] == "hello"
    assert payload["chars"] == 5
    assert payload["lines"] == 1


def test_is_tracing_enabled_false_by_default(monkeypatch):
    """LANGFUSE_ENABLED defaults to False and tracing is off."""
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    reset_for_tests()
    assert not is_tracing_enabled()
