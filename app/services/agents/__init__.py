"""Agent backend and runtime integrations."""

from .agent_backends import (
    BackendCapabilities,
    BackendConfigMetadata,
    BackendDescriptor,
    BackendHealth,
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
    require_backend_descriptor,
)
from .agent_runtime import (
    create_agent_runtime,
    invoke_runtime_prompt,
    runtime_reports_context_overflow,
)
from .interfaces import (
    AgentRuntime,
    AgentRuntimeError,
    RuntimeBackendResult,
    UnsupportedCapabilityError,
)

__all__ = [
    "BackendCapabilities",
    "BackendConfigMetadata",
    "BackendDescriptor",
    "BackendHealth",
    "UnsupportedAgentBackendError",
    "get_backend_descriptor",
    "list_supported_backends",
    "require_backend_descriptor",
    "AgentRuntime",
    "AgentRuntimeError",
    "RuntimeBackendResult",
    "UnsupportedCapabilityError",
    "create_agent_runtime",
    "invoke_runtime_prompt",
    "runtime_reports_context_overflow",
]
