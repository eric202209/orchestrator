"""Observable capture of the completion-repair configuration (§16).

RER-02A found this lane's configuration insufficiently frozen.  This module
makes the *existing* values observable.  It changes no configuration and
resolves nothing that the lane would not resolve itself.

Two layers are captured because they can differ:

``configured``
    Static values declared in code and environment: the enable flag, the
    declared role, the literal invocation options at the call site.
``resolved``
    What role resolution actually returns for the declared role at observation
    time -- backend, model family, adaptation profile.  Resolution requires a
    database read, so it is optional and its failure is recorded as
    ``UNRESOLVED`` rather than defaulted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session as DbSession

#: Source of truth for the enable trigger, read at call time by
#: ``completion_summary._generate_task_summary_with_fallback``.
ENABLE_ENV_VAR = "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"
ENABLE_TRUTHY_VALUES = frozenset({"1", "true", "yes"})

UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class CompletionRepairConfiguration:
    configured: dict[str, Any]
    resolved: dict[str, Any]
    evidence_source: dict[str, Any]
    notes: tuple[str, ...] = ()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "configured": dict(self.configured),
            "resolved": dict(self.resolved),
            "evidence_source": dict(self.evidence_source),
            "notes": list(self.notes),
        }


def _configured() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    from app.services.orchestration.policy import SUMMARY_TIMEOUT_SECONDS

    raw_flag = os.getenv(ENABLE_ENV_VAR, "")
    enabled = raw_flag.strip().lower() in ENABLE_TRUTHY_VALUES
    configured = {
        "enabled": enabled,
        "enable_env_var": ENABLE_ENV_VAR,
        "enable_env_raw": raw_flag,
        "trigger_conditions": (
            "task completion finalization AND "
            f"{ENABLE_ENV_VAR} in {sorted(ENABLE_TRUTHY_VALUES)}; "
            "any exception or empty output falls back to the deterministic "
            "summary without a second provider call"
        ),
        "declared_role": "repair",
        "session_prefix": "completion-summary",
        "source_brain": "local",
        "timeout_seconds": float(SUMMARY_TIMEOUT_SECONDS),
        "outer_wait_for_timeout_seconds": float(SUMMARY_TIMEOUT_SECONDS),
        "no_output_timeout_seconds": None,
        "retry_count": 0,
        "temperature": 0.0,
        "max_output_tokens": 512,
        "reasoning_enabled": False,
        "thinking_configuration": "disabled (reasoning_enabled=False)",
        "stream": False,
    }
    evidence_source = {
        "enabled": (
            "app/services/orchestration/phases/completion_summary.py"
            "::_generate_task_summary_with_fallback (os.getenv)"
        ),
        "invocation_options": (
            "app/services/orchestration/phases/completion_summary.py"
            "::_call_planning_lane (RuntimeInvocationOptions literal)"
        ),
        "timeout_seconds": (
            "app/services/orchestration/policy.py::SUMMARY_TIMEOUT_SECONDS"
        ),
        "role": (
            "app/services/orchestration/phases/completion_summary.py"
            "::_call_planning_lane (BackendRole.REPAIR)"
        ),
    }
    notes = [
        "retry_count=0: the lane issues one provider request; failure falls "
        "back to the deterministic summary rather than retrying.",
        "Configured/resolved divergence: settings COMPLETION_REPAIR_BACKEND and "
        "COMPLETION_REPAIR_MODEL exist and are resolved by "
        "BackendRole.COMPLETION_REPAIR, but completion_summary._call_planning_lane "
        "declares BackendRole.REPAIR, which resolves REPAIR_BACKEND instead. The "
        "settings named for this lane are therefore NOT the ones it uses. Both "
        "resolutions are captured below so the treatment is unambiguous. "
        "Recorded as an observation only; RR1 changes no configuration.",
    ]
    return configured, evidence_source, notes


def _resolve_role(db: DbSession, role_name: str) -> dict[str, Any]:
    from app.services.agents.agent_runtime import (
        BackendRole,
        resolve_runtime_configuration,
    )

    try:
        configuration = resolve_runtime_configuration(db, BackendRole(role_name))
    except Exception as exc:  # noqa: BLE001 - never default a resolved value
        return {
            "status": UNRESOLVED,
            "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    return {
        "status": "RESOLVED",
        "role": configuration.role.value,
        "backend": configuration.backend_name,
        "model_family": configuration.model_family,
        "adaptation_profile": configuration.adaptation_profile,
    }


def _resolved(db: DbSession | None) -> tuple[dict[str, Any], list[str]]:
    """Resolve the role the lane actually declares, and the one named for it."""

    if db is None:
        return {
            "status": UNRESOLVED,
            "reason": "no_database_context_supplied",
        }, []

    effective = _resolve_role(db, "repair")
    named = _resolve_role(db, "completion_repair")
    resolved = dict(effective)
    resolved["effective_role"] = "repair"
    resolved["role_named_for_this_lane"] = "completion_repair"
    resolved["role_named_for_this_lane_resolution"] = named
    resolved["effective_and_named_roles_agree"] = (
        effective.get("status") == "RESOLVED"
        and named.get("status") == "RESOLVED"
        and effective.get("backend") == named.get("backend")
        and effective.get("model_family") == named.get("model_family")
    )

    notes: list[str] = []
    if effective.get("status") != "RESOLVED":
        notes.append(
            "Resolved completion-repair configuration is UNRESOLVED, not absent."
        )
    elif not resolved["effective_and_named_roles_agree"]:
        notes.append(
            "The completion-repair lane's effective resolution differs from the "
            "resolution of the role named for it; the effective one governs."
        )
    return resolved, notes


def capture_completion_repair_configuration(
    db: DbSession | None = None,
) -> CompletionRepairConfiguration:
    """Return the configured and resolved completion-repair configuration."""

    configured, evidence_source, notes = _configured()
    resolved, resolved_notes = _resolved(db)
    return CompletionRepairConfiguration(
        configured=configured,
        resolved=resolved,
        evidence_source=evidence_source,
        notes=tuple(notes + resolved_notes),
    )
