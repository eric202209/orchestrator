"""Shared orchestration types."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ValidationVerdict:
    """Deterministic validation result for plans, steps, or completion."""

    stage: str
    status: str
    profile: str
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    used_small_model: bool = False

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "warning"}

    @property
    def warning(self) -> bool:
        return self.status == "warning"

    @property
    def repairable(self) -> bool:
        return self.status == "repair_required"

    @property
    def rejected(self) -> bool:
        return self.status == "rejected"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "profile": self.profile,
            "reasons": list(self.reasons),
            "details": dict(self.details),
            "used_small_model": self.used_small_model,
        }


@dataclass
class OrchestrationRunContext:
    """Shared runtime context for orchestration flows."""

    db: Any
    session: Any
    project: Any
    task: Any
    session_task_link: Any
    session_id: int
    task_id: int
    prompt: str
    timeout_seconds: int
    execution_profile: str
    validation_profile: str
    runs_in_canonical_baseline: bool
    orchestration_state: Any
    openclaw_service: Any
    task_service: Any
    logger: Any
    emit_live: Callable[..., None]
    error_handler: Any
    policy_profile_name: str = "balanced"
    validation_severity: str = "standard"
    completion_repair_budget: int = 1
    restore_workspace_snapshot_if_needed: Optional[Callable[[str], Any]] = None

    @property
    def session_instance_id(self) -> Optional[str]:
        if not self.session:
            return None
        return getattr(self.session, "instance_id", None)
