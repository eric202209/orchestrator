"""Provider-independent Protocol v2 planning generation seam."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable


PROVIDER_RUNTIME_FAILURES = frozenset(
    {
        "provider_timeout",
        "provider_process_failure",
        "provider_result_missing",
        "provider_result_ambiguous",
    }
)
PlanningCandidateText = str | bytes | Mapping[str, Any] | None


class PlanningArtifactKind(str, Enum):
    """Semantic candidate requested from a Planning Provider."""

    PLANNING_BRIEF = "planning_brief"
    STRUCTURED_TASK_PLAN = "structured_task_plan"


class ProviderFailureOrigin(str, Enum):
    """Provider-neutral point at which generation failed."""

    INVOCATION = "invocation"
    FAILED_RESULT = "failed_result"


@dataclass(frozen=True)
class ReasoningControls:
    """Requested model reasoning behavior."""

    enabled: bool | None = None


@dataclass(frozen=True)
class SamplingControls:
    """Requested provider-independent sampling behavior."""

    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None


@dataclass(frozen=True)
class PlanningRuntimeOptions:
    """Bounded execution controls shared by all Planning Providers."""

    timeout_seconds: int
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("planning provider timeout must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("planning provider max output tokens must be positive")


@dataclass(frozen=True)
class PlanningRequest:
    """Complete provider-neutral request for one Protocol v2 candidate."""

    artifact_kind: PlanningArtifactKind
    prompt: str
    protocol_input: Mapping[str, Any]
    runtime_options: PlanningRuntimeOptions
    reasoning: ReasoningControls = field(default_factory=ReasoningControls)
    sampling: SamplingControls = field(default_factory=SamplingControls)
    project_id: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("planning provider prompt must not be empty")


@dataclass(frozen=True)
class ProviderDiagnostics:
    """Bounded provider-neutral diagnostics for one generation."""

    category: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderTokenUsage:
    """Optional normalized token accounting."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ExecutionMetadata:
    """Normalized execution identity without transport payload details."""

    runtime_name: str | None = None
    model: str | None = None
    adaptation_profile: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanningResponse:
    """Provider-independent result for one semantic planning candidate."""

    candidate_text: PlanningCandidateText
    provider_name: str
    provider_version: str | None
    diagnostics: ProviderDiagnostics
    latency_seconds: float
    completion_metadata: Mapping[str, Any] = field(default_factory=dict)
    token_usage: ProviderTokenUsage | None = None
    runtime_metadata: ExecutionMetadata = field(default_factory=ExecutionMetadata)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Features a Planning Provider can enforce at its seam."""

    supports_reasoning_control: bool
    supports_response_format: bool
    supports_tool_calling: bool
    supports_deterministic_sampling: bool
    supports_prompt_ownership: bool
    supports_request_ownership: bool
    supports_streaming: bool
    supports_cancellation: bool
    supports_timeout_control: bool
    supports_structured_output: bool
    supports_seed: bool
    supports_top_p: bool
    supports_health_endpoint: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderHealth:
    """Normalized readiness state for provider selection and diagnostics."""

    available: bool
    ready: bool
    status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderRuntimeInformation:
    """Configured runtime identity exposed without adapter-specific objects."""

    provider_name: str
    provider_version: str | None
    runtime_name: str | None
    model: str | None
    adaptation_profile: str | None
    details: Mapping[str, Any] = field(default_factory=dict)


class PlanningProviderExecutionError(RuntimeError):
    """Stable provider failure consumed by Protocol v2 planning stages."""

    def __init__(
        self,
        *,
        classification: str,
        detail: str,
        origin: ProviderFailureOrigin,
        diagnostics: ProviderDiagnostics | None = None,
    ) -> None:
        self.classification = classification
        self.detail = str(detail or classification)[:500]
        self.origin = origin
        self.diagnostics = diagnostics
        super().__init__(self.detail)


@runtime_checkable
class PlanningProvider(Protocol):
    """Deep interface used by every Protocol v2 semantic planning stage."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str | None: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def health(self) -> ProviderHealth: ...

    def runtime_information(self) -> ProviderRuntimeInformation: ...

    def generate(self, request: PlanningRequest) -> PlanningResponse: ...


__all__ = [
    "ExecutionMetadata",
    "PlanningArtifactKind",
    "PlanningCandidateText",
    "PlanningProvider",
    "PlanningProviderExecutionError",
    "PlanningRequest",
    "PlanningResponse",
    "PlanningRuntimeOptions",
    "PROVIDER_RUNTIME_FAILURES",
    "ProviderCapabilities",
    "ProviderDiagnostics",
    "ProviderFailureOrigin",
    "ProviderHealth",
    "ProviderRuntimeInformation",
    "ProviderTokenUsage",
    "ReasoningControls",
    "SamplingControls",
]
