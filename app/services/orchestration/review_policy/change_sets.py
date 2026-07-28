"""Review policy for task execution change sets.

This module owns governance outcomes. Validators and change-set builders provide
facts; this module maps those facts to auto-promote or hold-for-review behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Mapping
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
    "secret_path_write",
    "security_high_risk_command",
}
_REGISTERED_REVIEW_EXPECTATIONS = {
    "REVIEW_NOT_APPLICABLE",
    "REVIEW_NOT_REQUIRED",
    "REVIEW_REQUIRED",
}
_REGISTERED_PUBLICATION_EXPECTATIONS = {
    "PUBLICATION_ALLOWED",
    "PUBLICATION_FORBIDDEN",
    "PUBLICATION_NOT_REQUIRED",
    "PUBLICATION_REQUIRED",
}


def _contract_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _resolve_registered_contract(
    planner_contract: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve explicit review/publication intent without guessing.

    The Phase 31 runner carries the registered outcome bindings alongside the
    planner contract.  Legacy planner payloads do not contain those bindings
    and remain governed by the existing workspace policy.  A partial or
    contradictory binding is deliberately distinguishable from absence so the
    completion path can fail closed.
    """

    payload = _contract_mapping(planner_contract)
    registered_scenario = _contract_mapping(payload.get("registered_scenario_contract"))
    review = _contract_mapping(payload.get("review_contract"))
    publication = _contract_mapping(payload.get("publication_contract"))
    registered_review = _contract_mapping(registered_scenario.get("review_contract"))
    registered_publication = _contract_mapping(
        registered_scenario.get("publication_contract")
    )
    has_outcome_fields = any(
        (
            payload.get("review_expectation") is not None,
            payload.get("publication_expectation") is not None,
            review,
            publication,
            registered_review,
            registered_publication,
        )
    )
    if not has_outcome_fields:
        return {"resolution": "absent", "policy_source": "workspace_review_policy"}

    review_expectation = review.get("expectation")
    publication_expectation = publication.get("expectation")
    registered_review_expectation = registered_review.get("expectation")
    registered_publication_expectation = registered_publication.get("expectation")
    if not review or not publication or not registered_scenario:
        return {
            "resolution": "ambiguous",
            "policy_source": "registered_certification_contract",
            "reason": "registered_contract_ambiguous",
        }

    values = {
        "review_expectation": {
            value
            for value in (
                payload.get("review_expectation"),
                review_expectation,
                registered_review_expectation,
            )
            if value is not None
        },
        "publication_expectation": {
            value
            for value in (
                payload.get("publication_expectation"),
                publication_expectation,
                registered_publication_expectation,
            )
            if value is not None
        },
    }
    scenario_ids = {
        value
        for value in (
            payload.get("scenario_id"),
            registered_scenario.get("scenario_id"),
            review.get("scenario_id"),
            publication.get("scenario_id"),
            registered_review.get("scenario_id"),
            registered_publication.get("scenario_id"),
        )
        if value is not None
    }
    if (
        len(values["review_expectation"]) != 1
        or len(values["publication_expectation"]) != 1
        or len(scenario_ids) > 1
    ):
        return {
            "resolution": "ambiguous",
            "policy_source": "registered_certification_contract",
            "reason": "registered_contract_ambiguous",
        }

    review_expectation = next(iter(values["review_expectation"]), None)
    publication_expectation = next(iter(values["publication_expectation"]), None)
    if (
        review_expectation not in _REGISTERED_REVIEW_EXPECTATIONS
        or publication_expectation not in _REGISTERED_PUBLICATION_EXPECTATIONS
    ):
        return {
            "resolution": "ambiguous",
            "policy_source": "registered_certification_contract",
            "reason": "registered_contract_ambiguous",
        }

    return {
        "resolution": "valid",
        "policy_source": "registered_certification_contract",
        "contract_id": payload.get("contract_id"),
        "contract_version": payload.get("contract_version")
        or registered_scenario.get("specification_version"),
        "scenario_id": next(iter(scenario_ids), None),
        "review_expectation": review_expectation,
        "publication_expectation": publication_expectation,
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
    template_review_policy: Optional[dict[str, Any]] = None,
    planner_contract: Optional[Mapping[str, Any]] = None,
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
    registered_policy = _resolve_registered_contract(planner_contract)
    review_required_before_contract = held_for_review = policy == "hold_all" or (
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

    # Template review policy overrides — applied after workspace policy.
    # hold_if conditions have highest priority; auto_promote_if can release a hold.
    template_signal: dict[str, Any] = {}
    if template_review_policy:
        from app.services.orchestration.workflow_templates import (
            _AUTO_PROMOTE_CONDITIONS,
            _HOLD_CONDITIONS,
        )

        wf_set = set(warning_flags)
        hold_if = template_review_policy.get("hold_if") or []
        auto_promote_if = template_review_policy.get("auto_promote_if") or []
        auto_promote_eligible = template_review_policy.get(
            "auto_promote_eligible", True
        )
        allowed_ops = template_review_policy.get("allowed_ops") or []

        # auto_promote_eligible=False: always hold regardless of conditions.
        if auto_promote_eligible is False:
            held_for_review = True
            warning_allowed_by_profile = False
            reason = "template_auto_promote_not_eligible"
            template_signal = {
                "triggered_hold_conditions": ["auto_promote_eligible_false"],
                "failed_auto_promote_conditions": [],
            }
        else:
            # Unknown condition names fail-closed: unknown hold_if → triggers hold;
            # unknown auto_promote_if → blocks auto-promote.
            triggered_hold = [
                c for c in hold_if if _HOLD_CONDITIONS.get(c, lambda _: True)(wf_set)
            ]
            failed_promote = [
                c
                for c in auto_promote_if
                if not _AUTO_PROMOTE_CONDITIONS.get(c, lambda _: False)(wf_set)
            ]
            template_signal = {
                "triggered_hold_conditions": triggered_hold,
                "failed_auto_promote_conditions": failed_promote,
            }
            if triggered_hold:
                held_for_review = True
                warning_allowed_by_profile = False
                reason = "template_hold_condition_triggered"
            elif not failed_promote and held_for_review and policy == "hold_nontrivial":
                held_for_review = False
                reason = "template_auto_promote_conditions_met"

        # allowed_ops enforcement: read-only templates must not produce mutations.
        # Always surface the violation even when already held for another reason,
        # so the operator signal is never hidden behind a generic hold.
        _read_only_template = bool(allowed_ops) and set(allowed_ops) == {"read_file"}
        if _read_only_template and changed_count > 0:
            held_for_review = True
            warning_allowed_by_profile = False
            reason = "template_allowed_ops_violation"
            template_signal = {
                **template_signal,
                "allowed_ops_violation": True,
            }

    stronger_safety_override = False
    if registered_policy["resolution"] == "ambiguous":
        held_for_review = True
        warning_allowed_by_profile = False
        reason = registered_policy["reason"]
        stronger_safety_override = True
    elif registered_policy["resolution"] == "valid":
        registered_review_expectation = registered_policy["review_expectation"]
        registered_publication_expectation = registered_policy[
            "publication_expectation"
        ]
        source_risk_findings = sorted(
            set(warning_flags).intersection(_SOURCE_RISK_WARNING_FLAGS)
        )
        stronger_safety_override = bool(
            source_risk_findings
            or policy == "hold_all"
            or template_signal.get("triggered_hold_conditions")
            or template_signal.get("allowed_ops_violation")
            or template_review_policy
            and template_review_policy.get("auto_promote_eligible") is False
        )
        if registered_review_expectation == "REVIEW_REQUIRED":
            held_for_review = True
            warning_allowed_by_profile = False
            reason = "registered_review_required"
        elif registered_review_expectation in {
            "REVIEW_NOT_APPLICABLE",
            "REVIEW_NOT_REQUIRED",
        }:
            if source_risk_findings:
                held_for_review = True
                reason = "mandatory_safety_review_required"
            elif policy == "hold_all":
                held_for_review = True
                reason = "hold_all_review_required"
            elif not template_signal.get("triggered_hold_conditions") and not (
                template_signal.get("allowed_ops_violation")
                or template_review_policy
                and template_review_policy.get("auto_promote_eligible") is False
            ):
                held_for_review = False
                reason = "registered_review_not_required"

    publication_expectation = registered_policy.get("publication_expectation")
    publication_allowed = publication_expectation not in {
        "PUBLICATION_FORBIDDEN",
        "PUBLICATION_NOT_REQUIRED",
    }
    publication_required = (
        publication_expectation == "PUBLICATION_REQUIRED"
        if registered_policy["resolution"] == "valid"
        else None
    )
    if (
        registered_policy["resolution"] == "valid"
        and not publication_allowed
        and not held_for_review
    ):
        reason = (
            "publication_forbidden"
            if publication_expectation == "PUBLICATION_FORBIDDEN"
            else "publication_not_required"
        )

    outcome = "hold_for_review" if held_for_review else "auto_promote"
    if warning_allowed_by_profile:
        outcome = "allow_with_warning"
    elif not publication_allowed and not held_for_review:
        outcome = "no_publication_required"
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
        "template_signal": template_signal,
        "contract_resolution": registered_policy["resolution"],
        "policy_source": registered_policy["policy_source"],
        "registered_contract_id": registered_policy.get("contract_id"),
        "registered_contract_version": registered_policy.get("contract_version"),
        "registered_scenario_id": registered_policy.get("scenario_id"),
        "registered_review_expectation": registered_policy.get("review_expectation"),
        "registered_publication_expectation": registered_policy.get(
            "publication_expectation"
        ),
        "review_required_before_contract": review_required_before_contract,
        "stronger_safety_override": stronger_safety_override,
        "publication_required": publication_required,
        "publication_allowed": publication_allowed,
        "publication_eligible": bool(publication_allowed and not held_for_review),
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
