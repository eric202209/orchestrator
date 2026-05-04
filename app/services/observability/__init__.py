"""Observability integrations and helpers.

Public API:
* ObservabilityService — new, explicit service class (recommended for new code)
* Module-level functions — backwards-compatible shims (existing code continues to work)
"""

from .tracing import ObservabilityService
from .tracing import (
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
    # Service class (preferred)
    "ObservabilityService",
    # Canonical names
    "is_tracing_enabled",
    "start_observation",
    "build_text_trace_payload",
    "flush",
    "reset_for_tests",
    # Backwards-compatible aliases
    "langfuse_tracing_enabled",
    "start_langfuse_observation",
    "update_langfuse_observation",
    "flush_langfuse",
    "reset_langfuse_client_for_tests",
]
