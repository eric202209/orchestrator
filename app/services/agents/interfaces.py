"""Provider-neutral runtime interfaces shared across agent backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol


class AgentRuntimeError(Exception):
    """Backend-neutral runtime failure raised by provider adapters."""


class UnsupportedCapabilityError(AgentRuntimeError):
    """Raised when the active backend does not support a requested capability."""


@dataclass(frozen=True)
class ContextWindowPolicy:
    """Declarative context budget policy exposed by a runtime adapter."""

    max_input_tokens: Optional[int]
    overflow_strategy: str
    compaction_strategy: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetryStrategy:
    """Preferred retry behavior for one backend/model family."""

    planning: str
    execution: str
    completion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBackendResult:
    """Normalized result shape returned by runtime backend adapters."""

    backend_id: str
    role: str
    success: bool
    exit_reason: str
    output: Optional[str]
    duration_seconds: float
    failure_category: Optional[str] = None
    terminal_reason: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    token_source: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentInterfaceDescriptor:
    """Backend/model-specific contract used by orchestration flows."""

    backend: str
    model_family: str
    planning_prompt_template: str
    execution_prompt_template: str
    prompt_dialect: str
    tool_capability_map: dict[str, bool] = field(default_factory=dict)
    tool_shape: str = "shell_text"
    preferred_retry_strategy: RetryStrategy = field(
        default_factory=lambda: RetryStrategy(
            planning="single_retry_minimal_prompt",
            execution="single_retry_compact_prompt",
            completion="single_retry_repair_step",
        )
    )
    context_window_policy: ContextWindowPolicy = field(
        default_factory=lambda: ContextWindowPolicy(
            max_input_tokens=None,
            overflow_strategy="retry_compact",
            compaction_strategy="context_summary",
        )
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["preferred_retry_strategy"] = self.preferred_retry_strategy.to_dict()
        payload["context_window_policy"] = self.context_window_policy.to_dict()
        return payload


class BackendHealthCheck(Protocol):
    """Contract for a backend health probe.

    Each backend registered in the backend registry must supply a callable
    matching this protocol so health aggregation works transport-agnostically.
    """

    def __call__(self, descriptor: Any) -> Any:
        """Run the health check and return a BackendHealth result."""
        ...


class AgentRuntime(Protocol):
    """Minimal runtime contract shared by orchestration entrypoints."""

    backend_descriptor: Any

    async def create_session(
        self, task_description: str, context: Optional[dict[str, Any]] = None
    ) -> str: ...

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Any = None,
        *,
        diagnostic_label: Optional[str] = None,
        diagnostic_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    async def pause_session(self) -> None: ...

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str: ...

    async def stop_session(self) -> None: ...

    async def get_session_context(self) -> dict[str, Any]: ...

    async def invoke_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 180,
        source_brain: str = "local",
        session_prefix: str = "planning",
        isolate_workspace_context: bool = False,
        no_output_timeout_seconds: Optional[int] = None,
    ) -> dict[str, Any]: ...

    def get_backend_metadata(self) -> dict[str, Any]: ...

    def describe_interface(self) -> AgentInterfaceDescriptor: ...

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool: ...
