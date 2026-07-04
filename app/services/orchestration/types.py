"""Shared orchestration types."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, List, Optional, Union

from app.services.agents.interfaces import AgentRuntime
from app.services.orchestration.policy import PolicyProfile, get_policy_profile


@dataclass
class ValidationVerdict:
    """Deterministic validation result for plans, steps, or completion."""

    stage: str
    status: str
    profile: str
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    used_small_model: bool = False
    confidence: Optional[str] = None

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

    @property
    def validator_rule_ids(self) -> List[str]:
        return [
            str(rule_id)
            for rule_id in (self.details.get("validator_rule_ids") or [])
            if str(rule_id).strip()
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "profile": self.profile,
            "reasons": list(self.reasons),
            "validator_rule_ids": self.validator_rule_ids,
            "details": dict(self.details),
            "used_small_model": self.used_small_model,
            "confidence": self.confidence,
        }


class _PlanOutcomeBase:
    """Delegate properties so callers can access verdict fields directly."""

    verdict: ValidationVerdict

    @property
    def accepted(self) -> bool:
        return self.verdict.accepted

    @property
    def rejected(self) -> bool:
        return self.verdict.rejected

    @property
    def repairable(self) -> bool:
        return self.verdict.repairable

    @property
    def warning(self) -> bool:
        return self.verdict.warning

    @property
    def status(self) -> str:
        return self.verdict.status

    @property
    def reasons(self) -> List[str]:
        return self.verdict.reasons

    @property
    def details(self) -> Dict[str, Any]:
        return self.verdict.details

    def to_dict(self) -> Dict[str, Any]:
        return self.verdict.to_dict()


@dataclass
class PlanAccepted(_PlanOutcomeBase):
    verdict: ValidationVerdict


@dataclass
class PlanRepairRequired(_PlanOutcomeBase):
    verdict: ValidationVerdict


@dataclass
class PlanRejected(_PlanOutcomeBase):
    verdict: ValidationVerdict


PlanOutcome = Union[PlanAccepted, PlanRepairRequired, PlanRejected]


@dataclass
class ReasoningArtifact:
    """Machine-checkable control-plane artifact inserted before execution."""

    intent: str
    workspace_facts: List[str] = field(default_factory=list)
    planned_actions: List[str] = field(default_factory=list)
    verification_plan: List[str] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "workspace_facts": list(self.workspace_facts),
            "planned_actions": list(self.planned_actions),
            "verification_plan": list(self.verification_plan),
        }


@dataclass
class FailureEnvelope:
    """Normalized failure payload shared across retries, telemetry, and UI."""

    session_id: int
    task_id: int
    phase: str
    root_cause: str
    step_index: Optional[int] = None
    model_id: str = ""
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    stderr: str = ""
    cost: Dict[str, Any] = field(default_factory=dict)
    token_count: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "step_index": self.step_index,
            "model_id": self.model_id,
            "input": dict(self.input),
            "output": dict(self.output),
            "stderr": self.stderr,
            "cost": dict(self.cost),
            "token_count": dict(self.token_count),
            "root_cause": self.root_cause,
        }

    def to_prompt_block(self, *, max_chars: int = 2200) -> str:
        payload = self.to_dict()
        payload["stderr"] = str(payload.get("stderr") or "")[:1200]
        text = json.dumps(payload, indent=2, ensure_ascii=True)
        return "[EXECUTION_ERROR]\n" + text[:max_chars]


def classify_failure_root_cause(
    *,
    error_message: str,
    verification_output: str = "",
    tool_failures: Optional[List[str]] = None,
) -> str:
    combined = "\n".join(
        part
        for part in [
            str(error_message or ""),
            str(verification_output or ""),
            "\n".join(tool_failures or []),
        ]
        if part
    ).lower()
    if not combined.strip():
        return "unknown"
    if "context window exceeded" in combined or "context exceeded" in combined:
        return "context_overflow"
    if "json" in combined and (
        "parse" in combined or "schema" in combined or "malformed" in combined
    ):
        return "malformed_prompt_output"
    if "permission denied" in combined:
        return "permission_denied"
    if (
        "workspace" in combined
        or "path escapes" in combined
        or "absolute path" in combined
        or "outside" in combined
    ):
        return "path_contract"
    if "verification command failed" in combined or "validation failed" in combined:
        return "validation_failure"
    if "session_instance_changed" in combined or "duplicate execution" in combined:
        return "dispatch_contention"
    if "tool" in combined and "failed" in combined:
        return "tool_failure"
    return "unknown"


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
    runtime_service: AgentRuntime
    task_service: Any
    logger: Any
    emit_live: Callable[..., None]
    error_handler: Any
    policy_profile_name: str = "balanced"
    validation_severity: str = "standard"
    completion_repair_budget: int = 1
    workflow_profile: str = "default"
    workflow_stage: Optional[str] = None
    task_execution_id: Optional[int] = None
    restore_workspace_snapshot_if_needed: Optional[Callable[..., Any]] = None
    planning_backend: str = "all"
    execution_backend: str = "all"
    guidance_backend: str = "all"
    guidance_model_name: str = "unknown"
    guidance_model_family: str = "all"

    @property
    def policy_profile(self) -> PolicyProfile:
        return get_policy_profile(self.policy_profile_name)

    @property
    def session_instance_id(self) -> Optional[str]:
        if not self.session:
            return None
        return getattr(self.session, "instance_id", None)
