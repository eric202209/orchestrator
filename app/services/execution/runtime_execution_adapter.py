"""Small provider-neutral adapter boundary for Phase 29C-6B/29C-9.

The adapter is intentionally narrower than the legacy AgentRuntime contract.
It may return bounded candidate bytes directly at the trusted completion
boundary for Phase 29C-9; it never returns a path or URL and has no authority
to mutate Phase 29 lifecycle, attempt, lease, or outcome rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


RUNTIME_PROGRESS_STATES = frozenset(
    {
        "runtime_starting",
        "provider_request_active",
        "provider_stream_active",
        "tool_execution_active",
        "post_processing",
        "result_persistence_active",
    }
)
RUNTIME_OUTCOME_STATUSES = frozenset(
    {"candidate_completed", "attempt_failed", "attempt_cancelled"}
)
MAX_ADAPTER_TEXT = 1024
MAX_ADAPTER_REFERENCE = 512
MAX_ADAPTER_DIAGNOSTICS_BYTES = 4096
MAX_ADAPTER_CANDIDATE_BYTES = 1_048_576


@dataclass(frozen=True)
class RuntimeProgress:
    """One bounded liveness/progress signal from the adapter."""

    state: str
    sequence: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class RuntimeExecutionCommand:
    """Immutable invocation identity supplied to an adapter."""

    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    dispatch_intent_id: int
    runtime_lease_id: int
    runtime_start_id: int
    broker_task_id: str
    worker_instance_id: str
    ownership_fencing_token: int
    runtime_adapter_name: str
    adapter_version: str | None
    execution_mode: str
    configuration_hash: str
    provider_request_id: str | None = None


@dataclass(frozen=True)
class RuntimeExecutionResult:
    """Bounded adapter result with optional completion-time candidate bytes."""

    completion_kind: str
    output_reference: str | None = None
    output_hash: str | None = None
    candidate_bytes: bytes | None = None
    candidate_media_type: str | None = None
    provider_request_id: str | None = None
    usage_summary: Mapping[str, Any] | None = None
    diagnostics: Mapping[str, Any] | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    sanitized_detail: str | None = None
    exception_type: str | None = None


class ExecutionRuntimeAdapter(Protocol):
    """Injectable external-runtime seam owned by neither lifecycle nor policy."""

    def execute(
        self,
        command: RuntimeExecutionCommand,
        heartbeat: Callable[[RuntimeProgress], None],
        authority_check: Callable[[], None],
        cancellation_check: Callable[[], bool],
    ) -> RuntimeExecutionResult:
        """Invoke one bounded runtime operation outside the DB transaction."""


@dataclass
class DeterministicExecutionRuntimeAdapter:
    """No-op adapter for focused authority-boundary certification.

    It emits at most the supplied bounded progress sequence and returns the
    supplied result.  It does not access a database, workspace, provider, or
    lifecycle service.
    """

    result: RuntimeExecutionResult = field(
        default_factory=lambda: RuntimeExecutionResult(
            completion_kind="candidate_completed",
            output_reference="runtime://deterministic-candidate",
        )
    )
    progress: tuple[RuntimeProgress, ...] = ()
    calls: int = 0
    last_command: RuntimeExecutionCommand | None = None

    def execute(
        self,
        command: RuntimeExecutionCommand,
        heartbeat: Callable[[RuntimeProgress], None],
        authority_check: Callable[[], None],
        cancellation_check: Callable[[], bool],
    ) -> RuntimeExecutionResult:
        self.calls += 1
        self.last_command = command
        authority_check()
        for progress in self.progress:
            heartbeat(progress)
            authority_check()
        if cancellation_check():
            return RuntimeExecutionResult(
                completion_kind="attempt_cancelled",
                failure_category="caller_cancelled",
                failure_code="runtime_cancellation_observed",
                sanitized_detail="runtime cancellation was observed",
            )
        return self.result


__all__ = [
    "DeterministicExecutionRuntimeAdapter",
    "ExecutionRuntimeAdapter",
    "MAX_ADAPTER_DIAGNOSTICS_BYTES",
    "MAX_ADAPTER_CANDIDATE_BYTES",
    "MAX_ADAPTER_REFERENCE",
    "MAX_ADAPTER_TEXT",
    "RUNTIME_OUTCOME_STATUSES",
    "RUNTIME_PROGRESS_STATES",
    "RuntimeExecutionCommand",
    "RuntimeExecutionResult",
    "RuntimeProgress",
]
