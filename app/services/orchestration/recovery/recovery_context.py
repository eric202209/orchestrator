"""Phase 17C-2: canonical context for registry-owned recovery execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.reflection_evidence import ReflectionEvidence
from app.services.orchestration.recovery.recovery_policy import PolicyTable


def _runtime_profile() -> str:
    try:
        from app.config import settings

        return settings.RUNTIME_PROFILE
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class RecoveryContext:
    """Immutable carrier for all state needed by recovery strategies.

    RecoveryContext is a transport object only. Recovery behavior remains owned
    by concrete strategies and, for bounded execution recovery, by
    ExecutionRecoveryService.
    """

    project_dir: Any
    session_id: int
    task_id: int

    scope: str
    evidence: ExecutionRecoveryEvidence
    orchestration_state: Any

    step_index: Optional[int] = None
    parent_event_id: Optional[str] = None

    llm_callable: Optional[Callable[[str], str]] = None
    validator_callable: Optional[Callable[..., Any]] = None
    command_runner: Optional[Callable[..., Any]] = None

    policy_version: str = PolicyTable.VERSION
    runtime_profile: str = field(default_factory=_runtime_profile)

    reflection_result: Optional[ReflectionEvidence] = None
    working_memory: Optional[Any] = None
    human_guidance: Optional[Any] = None
    recovery_metadata: Optional[dict[str, Any]] = None
