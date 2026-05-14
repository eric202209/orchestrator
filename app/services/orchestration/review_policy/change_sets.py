"""Review policy for task execution change sets.

This module owns governance outcomes. Validators and change-set builders provide
facts; this module maps those facts to auto-promote or hold-for-review behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

CHANGE_SET_REVIEW_POLICY_VERSION = "phase9m.change_set_review.v1"
_SUPPORTED_WORKSPACE_REVIEW_POLICIES = {
    "auto_publish_all",
    "hold_nontrivial",
    "hold_all",
}
_LOW_RISK_WORKFLOW_PROFILES = {
    "docs_only",
    "docs_static",
    "static_content",
    "static_site",
}
_SOURCE_RISK_WARNING_FLAGS = {
    "config_files_changed",
    "deleted_files",
    "dependency_files_changed",
    "more_than_10_changed_files",
}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _normalize_workspace_review_policy(value: str) -> str:
    policy = str(value or "").strip() or "hold_nontrivial"
    if policy not in _SUPPORTED_WORKSPACE_REVIEW_POLICIES:
        return "hold_nontrivial"
    return policy


def _normalize_evaluator_evidence(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not value:
        return {}
    return {
        key: value[key]
        for key in sorted(value)
        if key in {"confidence", "verdict", "risk_notes", "artifact_refs"}
    }


def decide_change_set_review(
    change_set: Optional[dict[str, Any]],
    *,
    workspace_review_policy: str,
    workflow_profile: Optional[str] = None,
    evaluator_evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return the governance decision for a task execution change set.

    The legacy fields are intentionally preserved because API/UI and persisted
    ``TaskExecutionChangeSet.review_decision`` consumers already read them.
    Evaluator evidence is shadow-only in this phase.
    """

    payload = change_set or {}
    policy = _normalize_workspace_review_policy(workspace_review_policy)
    warning_flags = _string_list(payload.get("warning_flags"))
    changed_count = _int_or_zero(payload.get("changed_count"))
    workflow_profile_name = str(workflow_profile or "").strip() or None
    evaluator = _normalize_evaluator_evidence(evaluator_evidence)

    held_for_review = policy == "hold_all" or (
        policy == "hold_nontrivial" and bool(warning_flags)
    )
    reason = None
    if held_for_review:
        reason = (
            "hold_all_review_required"
            if policy == "hold_all"
            else "nontrivial_change_set_review_required"
        )

    warning_allowed_by_profile = (
        held_for_review
        and policy == "hold_nontrivial"
        and workflow_profile_name in _LOW_RISK_WORKFLOW_PROFILES
        and not set(warning_flags).intersection(_SOURCE_RISK_WARNING_FLAGS)
    )
    if warning_allowed_by_profile:
        held_for_review = False
        reason = "low_risk_profile_warning_allowed"

    outcome = "hold_for_review" if held_for_review else "auto_promote"
    if warning_allowed_by_profile:
        outcome = "allow_with_warning"
    blocking_findings = warning_flags if held_for_review else []

    return {
        "workspace_review_policy": policy,
        "held_for_review": held_for_review,
        "reason": reason,
        "changed_count": changed_count,
        "warning_flags": warning_flags,
        "outcome": outcome,
        "policy_version": CHANGE_SET_REVIEW_POLICY_VERSION,
        "workflow_profile": workflow_profile_name,
        "blocking_findings": blocking_findings,
        "warning_findings": warning_flags,
        "evidence_refs": [],
        "evaluator_evidence": evaluator,
        "evaluator_influence": "shadow" if evaluator else "none",
    }


def build_operator_override_metadata(
    *,
    action: str,
    reason: str,
    task_execution_id: int,
    change_set: Optional[dict[str, Any]],
    operator: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build durable metadata for manual governance transitions."""

    payload = change_set or {}
    review_decision = payload.get("review_decision") or {}
    metadata = {
        "schema": "openclaw.review_policy.operator_override.v1",
        "action": action,
        "override_reason": reason,
        "operator": operator,
        "task_execution_id": task_execution_id,
        "change_set_id": payload.get("change_set_id"),
        "previous_outcome": review_decision.get("outcome"),
        "previous_held_for_review": review_decision.get("held_for_review"),
        "previous_reason": review_decision.get("reason"),
        "previous_review_decision": review_decision,
        "policy_version": review_decision.get("policy_version"),
        "overridden_at": datetime.now(UTC).isoformat(),
    }
    if extra:
        metadata.update(extra)
    return metadata
