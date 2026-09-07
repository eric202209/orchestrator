"""Typed contracts for the bounded Grounding Coordinator lifecycle.

The coordinator contracts deliberately sit beside, rather than inside, the
PGI1 executor contracts.  They describe epistemic state and terminal
projection only; they do not grant planning, mutation, or execution authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from .contracts import (
    GroundingBudgetLimits,
    GroundingBudgetSnapshot,
    GroundingObservation,
    GroundingRequest,
    StructuralIdentity,
)


MAX_ASSESSMENT_RATIONALE_CHARS = 1000
MAX_TELEMETRY_VALUE_CHARS = 240


class GroundingAssessmentKind(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    TERMINAL_STOP = "TERMINAL_STOP"


class GroundingTerminalReason(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_GROUNDING = "INSUFFICIENT_GROUNDING"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_MODEL_REQUEST = "INVALID_MODEL_REQUEST"
    EXECUTOR_FAILURE = "EXECUTOR_FAILURE"
    SOURCE_VERSION_CHANGED = "SOURCE_VERSION_CHANGED"


class GroundingRequestStateSignalKind(str, Enum):
    DUPLICATE_TERMINAL_NOT_FOUND = "DUPLICATE_TERMINAL_NOT_FOUND"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _identity(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    return normalized


@dataclass(frozen=True, slots=True)
class GroundingAssessment:
    """One explicit provider assessment over already-recorded observations."""

    assessment_id: str
    grounding_run_id: str
    kind: GroundingAssessmentKind
    cited_observation_ids: tuple[str, ...] = ()
    cited_source_paths: tuple[str, ...] = ()
    cited_structural_identities: tuple[StructuralIdentity, ...] = ()
    rationale: str = ""
    unresolved_risk: bool = False
    next_action_digest: str | None = None
    terminal_reason: GroundingTerminalReason | None = None

    def __post_init__(self) -> None:
        _identity(self.assessment_id, "assessment_id")
        _identity(self.grounding_run_id, "grounding_run_id")
        if not isinstance(self.kind, GroundingAssessmentKind):
            raise ValueError("invalid grounding assessment kind")
        if not isinstance(self.unresolved_risk, bool):
            raise ValueError("unresolved_risk must be boolean")
        if len(self.rationale) > MAX_ASSESSMENT_RATIONALE_CHARS:
            raise ValueError("assessment rationale exceeds the bounded limit")
        if self.kind is GroundingAssessmentKind.SUFFICIENT:
            if self.terminal_reason not in (None, GroundingTerminalReason.SUFFICIENT):
                raise ValueError("SUFFICIENT cannot carry a failure terminal reason")
        elif self.kind is GroundingAssessmentKind.NEED_MORE_EVIDENCE:
            if self.terminal_reason is not None:
                raise ValueError("NEED_MORE_EVIDENCE cannot be terminal")
        elif self.terminal_reason is None:
            raise ValueError("TERMINAL_STOP requires a terminal reason")


@dataclass(frozen=True, slots=True)
class GroundingRequestStateSignal:
    """Mechanical request-state feedback, never a semantic replacement action."""

    kind: GroundingRequestStateSignalKind
    grounding_run_id: str
    action_digest: str
    source_observation_id: str


@dataclass(frozen=True, slots=True)
class GroundingTaskReference:
    task_id: str
    task_execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class GroundingRunConfig:
    """Per-run limits and identities supplied by the integration boundary."""

    grounding_run_id: str
    task_reference: GroundingTaskReference
    workspace_identity: str
    snapshot_identity: str
    max_steps: int
    max_provider_requests: int
    budget_limits: GroundingBudgetLimits = GroundingBudgetLimits()
    operator_task: str = ""
    orientation_advisory: Mapping[str, Any] = field(default_factory=dict)
    provider_name: str = "scripted_or_unbound"
    model_name: str = "unbound"
    snapshot_identity_supplier: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        _identity(self.grounding_run_id, "grounding_run_id")
        _identity(self.workspace_identity, "workspace_identity")
        _identity(self.snapshot_identity, "snapshot_identity")
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool):
            raise ValueError("max_steps must be an integer")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not isinstance(self.max_provider_requests, int) or isinstance(
            self.max_provider_requests, bool
        ):
            raise ValueError("max_provider_requests must be an integer")
        if self.max_provider_requests <= 0:
            raise ValueError("max_provider_requests must be positive")
        object.__setattr__(
            self, "orientation_advisory", _freeze(self.orientation_advisory)
        )


@dataclass(frozen=True, slots=True)
class GroundingCoordinatorState:
    """Facts-only immutable state accumulated by one coordinator run."""

    grounding_run_id: str
    task_reference: GroundingTaskReference
    workspace_identity: str
    snapshot_identity: str
    request_history: tuple[GroundingRequest, ...] = ()
    observation_history: tuple[GroundingObservation, ...] = ()
    assessment_history: tuple[GroundingAssessment, ...] = ()
    attempted_action_digests: tuple[str, ...] = ()
    discovered_source_paths: tuple[str, ...] = ()
    discovered_structural_identities: tuple[StructuralIdentity, ...] = ()
    source_versions: Mapping[str, str] = field(default_factory=dict)
    budget: GroundingBudgetSnapshot = GroundingBudgetSnapshot()
    remaining_budget: Mapping[str, int | None] = field(default_factory=dict)
    request_state_signals: tuple[GroundingRequestStateSignal, ...] = ()
    orientation_advisory: Mapping[str, Any] = field(default_factory=dict)
    termination_state: GroundingTerminalReason | None = None

    def __post_init__(self) -> None:
        _identity(self.grounding_run_id, "grounding_run_id")
        _identity(self.workspace_identity, "workspace_identity")
        _identity(self.snapshot_identity, "snapshot_identity")
        object.__setattr__(self, "source_versions", _freeze(self.source_versions))
        object.__setattr__(self, "remaining_budget", _freeze(self.remaining_budget))
        object.__setattr__(
            self, "orientation_advisory", _freeze(self.orientation_advisory)
        )


@dataclass(frozen=True, slots=True)
class GroundingDecisionContext:
    """Provider input: original task is separate from typed grounding state."""

    state: GroundingCoordinatorState
    rendered_grounding_state: str
    operator_task: str


@dataclass(frozen=True, slots=True)
class GroundingProposal:
    """One provider proposal, either an action plus assessment or terminal stop."""

    action_payload: Mapping[str, Any] | None = None
    assessment_kind: GroundingAssessmentKind | None = None
    cited_observation_ids: tuple[str, ...] = ()
    cited_source_paths: tuple[str, ...] = ()
    cited_structural_identities: tuple[StructuralIdentity, ...] = ()
    rationale: str = ""
    unresolved_risk: bool = False
    terminal_reason: GroundingTerminalReason | None = None

    def __post_init__(self) -> None:
        if self.action_payload is not None:
            object.__setattr__(self, "action_payload", _freeze(self.action_payload))
        if len(self.rationale) > MAX_ASSESSMENT_RATIONALE_CHARS:
            raise ValueError("proposal rationale exceeds the bounded limit")
        if not isinstance(self.unresolved_risk, bool):
            raise ValueError("unresolved_risk must be boolean")


@dataclass(frozen=True, slots=True)
class GroundingStateProjection:
    """Bounded result projection; raw evidence remains separately cited."""

    grounding_run_id: str
    task_reference: GroundingTaskReference
    workspace_identity: str
    snapshot_identity: str
    request_digests: tuple[str, ...]
    observation_ids: tuple[str, ...]
    observation_outcomes: tuple[str, ...]
    assessment_history: tuple[GroundingAssessment, ...]
    attempted_action_digests: tuple[str, ...]
    request_state_signals: tuple[GroundingRequestStateSignal, ...]
    discovered_source_paths: tuple[str, ...]
    discovered_structural_identities: tuple[StructuralIdentity, ...]
    source_versions: Mapping[str, str]
    remaining_budget: Mapping[str, int | None]
    termination_state: GroundingTerminalReason


@dataclass(frozen=True, slots=True)
class GroundingEvidence:
    """Bounded source evidence cited by a terminal assessment."""

    observation_id: str
    source_path: str
    source_version: str
    bounded_content: bytes = b""


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Immutable epistemic result.  It carries no Plan or mutation authority."""

    grounding_run_id: str
    terminal_reason: GroundingTerminalReason
    state_projection: GroundingStateProjection
    cited_observation_ids: tuple[str, ...] = ()
    cited_source_paths: tuple[str, ...] = ()
    cited_structural_identities: tuple[StructuralIdentity, ...] = ()
    source_versions: Mapping[str, str] = field(default_factory=dict)
    cited_source_evidence: tuple[GroundingEvidence, ...] = ()
    budget_trace: tuple[GroundingBudgetSnapshot, ...] = ()
    unresolved_risk: bool = True
    provider_model_telemetry: Mapping[str, Any] = field(default_factory=dict)
    grounding_diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> GroundingTerminalReason:
        return self.terminal_reason

    def __post_init__(self) -> None:
        _identity(self.grounding_run_id, "grounding_run_id")
        if not isinstance(self.terminal_reason, GroundingTerminalReason):
            raise ValueError("invalid grounding terminal reason")
        if not isinstance(self.unresolved_risk, bool):
            raise ValueError("unresolved_risk must be boolean")
        object.__setattr__(self, "source_versions", _freeze(self.source_versions))
        object.__setattr__(
            self, "provider_model_telemetry", _freeze(self.provider_model_telemetry)
        )
        object.__setattr__(
            self, "grounding_diagnostics", _freeze(self.grounding_diagnostics)
        )
