"""Langfuse observability backend — delegates to ObservabilityService.

This file is kept for backwards compatibility. All new code should import
from ``app.services.observability`` directly or use the
``ObservabilityService`` class.

The implementation lives in :mod:`app.services.observability.tracing`.
"""

from __future__ import annotations

# Re-export everything from the service module so that existing import paths
# (e.g., ``from app.services.observability.langfuse import ...``) continue
# to work without modification.
from app.services.observability.tracing import (
    _ObservationHandle,
    ObservabilityService,
    build_text_trace_payload,
    flush,
    flush_langfuse,
    is_tracing_enabled,
    langfuse_tracing_enabled,
    reset_for_tests,
    reset_langfuse_client_for_tests,
    start_langfuse_observation,
    start_observation,
    update_langfuse_observation,
)

__all__ = [
    "ObservabilityService",
    "build_text_trace_payload",
    "flush",
    "flush_langfuse",
    "is_tracing_enabled",
    "langfuse_tracing_enabled",
    "reset_for_tests",
    "reset_langfuse_client_for_tests",
    "start_langfuse_observation",
    "start_observation",
    "update_langfuse_observation",
]
