"""Pure counterfactual policy simulation over replay reports.

This module is regression infrastructure only. It consumes deterministic replay
reports and produces bounded policy recommendations without reading runtime
state, emitting events, loading checkpoints, or mutating orchestration behavior.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List

from ..events.event_types import EventType
from ..policy import MAX_STEP_ATTEMPTS, PolicyProfile, get_policy_profile
from .replay import COMPATIBILITY_VERSION, REDUCER_VERSION, TRANSITION_EVENT_TYPES

SIMULATION_VERSION = "phase5a-sim-v1"
POLICY_FAMILY = "orchestration-safety"
POLICY_COMPATIBILITY_VERSION = "phase5a-policy-compat-v1"

MAX_POLICY_EVIDENCE_EVENTS = 12
MAX_POLICY_FINDINGS = 20
MAX_POLICY_REASON_CODES = 5
MAX_POLICY_EVIDENCE_GAPS = 10

_POLICY_CHECKSUM_CANONICAL_RESTORE_LABELS = {
    "balanced": "Restore only workspace-isolation failures by default",
    "strict": "Restore most orchestration failures to the pre-run snapshot",
    "recovery_friendly": (
        "Preserve more workspace state to support replay and operator recovery"
    ),
}

AUTHORITATIVE_POLICY_INPUTS = (
    "phase",
    "status",
    "retry_count",
    "repair_count",
    "current_step_index",
    "latest_failure_event_id",
    "latest_divergence_event_id",
    "validation_verdict_status_history",
    "intervention_status",
)

SUPPORTING_DIAGNOSTIC_INPUTS = (
    "integrity.confidence",
    "determinism.level",
    "drift_findings",
    "workspace_evidence.status",
    "compatibility_version",
    "reducer_version",
)

SIMULATION_SAFE_EVENT_TYPES = frozenset(
    {
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.STEP_STARTED,
        EventType.STEP_FINISHED,
        EventType.RETRY_ENTERED,
        EventType.VALIDATION_RESULT,
        EventType.REPAIR_GENERATED,
        EventType.REPAIR_APPLIED,
        EventType.REPAIR_REJECTED,
        EventType.COMPLETION_EVIDENCE_FAILED,
        EventType.CHECKPOINT_SAVED,
        EventType.CHECKPOINT_LOADED,
        EventType.CHECKPOINT_REDIRECTED,
        EventType.WAITING_FOR_INPUT,
        EventType.HUMAN_INTERVENTION_REQUESTED,
        EventType.HUMAN_INTERVENTION_REPLIED,
        EventType.DIVERGENCE_DETECTED,
        EventType.RESUME_WORKSPACE_DRIFT,
        EventType.WORKSPACE_CONTRACT_FAILED,
    }
)


def simulate_policy_from_replay(
    replay_report: Dict[str, Any],
    *,
    policy_profile: str = "balanced",
) -> Dict[str, Any]:
    """Return a bounded policy recommendation for a replay report."""

    profile = get_policy_profile(policy_profile)
    state = replay_report.get("state") or {}
    findings: List[Dict[str, Any]] = []
    reason_codes: List[str] = []
    evidence_event_ids: List[str] = []
    evidence_gaps: List[str] = []

    compatibility = _compatibility_report(replay_report)
    findings.extend(compatibility["findings"])
    evidence_gaps.extend(compatibility["evidence_gaps"])

    status = str(state.get("status") or "").lower()
    phase = str(state.get("phase") or "").lower()
    validation_history = [
        str(item).lower()
        for item in state.get("validation_verdict_status_history") or []
    ]
    latest_failure_event_id = state.get("latest_failure_event_id")
    latest_divergence_event_id = state.get("latest_divergence_event_id")

    if latest_failure_event_id:
        evidence_event_ids.append(str(latest_failure_event_id))
    if latest_divergence_event_id:
        evidence_event_ids.append(str(latest_divergence_event_id))

    action = "continue"
    confidence = "medium"

    integrity_confidence = (replay_report.get("integrity") or {}).get("confidence")
    determinism_level = (replay_report.get("determinism") or {}).get("level")
    if integrity_confidence == "failed" or determinism_level == "failed":
        action = "operator_review"
        confidence = "low"
        reason_codes.append("replay_integrity_failed")
    elif state.get("intervention_status") == "pending":
        action = "operator_intervention"
        confidence = "high"
        reason_codes.append("intervention_pending")
    elif status == "completed":
        action = "accept"
        confidence = "high"
        reason_codes.append("task_completed")
    elif status in {"rejected", "evidence_failed", EventType.REPAIR_REJECTED}:
        if profile.completion_repair_budget <= int(state.get("repair_count") or 0):
            action = "halt"
            reason_codes.append("repair_budget_exhausted")
        else:
            action = "repair"
            reason_codes.append("validation_repair_allowed")
        confidence = "high"
    elif "rejected" in validation_history:
        if profile.validation_severity == "high":
            action = "halt"
            reason_codes.append("strict_validation_rejection")
        else:
            action = "repair"
            reason_codes.append("validation_rejection")
        confidence = "high"
    elif status == "failed" or phase == "failure":
        if int(state.get("retry_count") or 0) >= MAX_STEP_ATTEMPTS:
            action = "halt"
            reason_codes.append("retry_budget_exhausted")
        else:
            action = "retry"
            reason_codes.append("failure_retry_allowed")
        confidence = "medium"
    elif latest_divergence_event_id:
        action = "operator_review"
        confidence = "medium"
        reason_codes.append("divergence_detected")
    else:
        reason_codes.append("no_policy_block")

    if determinism_level in {"bounded", "degraded"}:
        findings.append(
            {
                "type": "policy_determinism_bounded",
                "severity": "info",
                "summary": "Replay determinism constrains policy confidence",
            }
        )
    if (replay_report.get("workspace_evidence") or {}).get("status") in {
        "hash_differs_from_snapshot",
        "insufficient_evidence",
    }:
        findings.append(
            {
                "type": "workspace_evidence_diagnostic_only",
                "severity": "info",
                "summary": "Workspace drift is supporting evidence only",
            }
        )

    report = {
        "simulation_version": SIMULATION_VERSION,
        "policy": {
            "family": POLICY_FAMILY,
            "profile": profile.name,
            "version": f"{POLICY_FAMILY}:{profile.name}:v1",
            "checksum": _policy_checksum(_policy_checksum_payload(profile)),
        },
        "compatibility": compatibility,
        "policy_determinism": _policy_determinism(
            replay_report,
            compatibility=compatibility,
        ),
        "authoritative_inputs": {
            key: state.get(key) for key in AUTHORITATIVE_POLICY_INPUTS
        },
        "supporting_inputs": {
            "integrity_confidence": integrity_confidence,
            "determinism_level": determinism_level,
            "workspace_evidence_status": (
                replay_report.get("workspace_evidence") or {}
            ).get("status"),
            "drift_finding_count": len(replay_report.get("drift_findings") or []),
        },
        "recommendation": {
            "action": action,
            "confidence": confidence,
            "reason_codes": _bounded_unique(reason_codes, MAX_POLICY_REASON_CODES),
            "evidence_event_ids": _bounded_unique(
                evidence_event_ids,
                MAX_POLICY_EVIDENCE_EVENTS,
            ),
            "evidence_gaps": _bounded_unique(evidence_gaps, MAX_POLICY_EVIDENCE_GAPS),
        },
        "findings": findings[:MAX_POLICY_FINDINGS],
        "budgets": {
            "max_policy_evidence_events": MAX_POLICY_EVIDENCE_EVENTS,
            "max_policy_findings": MAX_POLICY_FINDINGS,
            "max_policy_reason_codes": MAX_POLICY_REASON_CODES,
            "max_policy_evidence_gaps": MAX_POLICY_EVIDENCE_GAPS,
        },
    }
    return report


def compare_policy_simulations(
    base_report: Dict[str, Any],
    candidate_report: Dict[str, Any],
) -> Dict[str, Any]:
    """Classify policy drift between two simulation reports."""

    base_action = (base_report.get("recommendation") or {}).get("action")
    candidate_action = (candidate_report.get("recommendation") or {}).get("action")
    base_reasons = tuple(
        (base_report.get("recommendation") or {}).get("reason_codes") or []
    )
    candidate_reasons = tuple(
        (candidate_report.get("recommendation") or {}).get("reason_codes") or []
    )
    base_confidence = (base_report.get("recommendation") or {}).get("confidence")
    candidate_confidence = (candidate_report.get("recommendation") or {}).get(
        "confidence"
    )

    if base_action != candidate_action:
        classification = "action_change"
    elif base_reasons != candidate_reasons:
        classification = "reason_change"
    elif base_confidence != candidate_confidence:
        classification = "confidence_change"
    else:
        classification = "equivalent"

    return {
        "classification": classification,
        "base_policy": base_report.get("policy"),
        "candidate_policy": candidate_report.get("policy"),
        "base_action": base_action,
        "candidate_action": candidate_action,
        "base_reason_codes": list(base_reasons),
        "candidate_reason_codes": list(candidate_reasons),
    }


def assert_policy_report_bounded(report: Dict[str, Any]) -> None:
    """Assertion helper for regression tests and fixture corpus checks."""

    recommendation = report.get("recommendation") or {}
    assert len(recommendation.get("evidence_event_ids") or []) <= (
        MAX_POLICY_EVIDENCE_EVENTS
    )
    assert len(report.get("findings") or []) <= MAX_POLICY_FINDINGS
    assert len(recommendation.get("reason_codes") or []) <= MAX_POLICY_REASON_CODES
    assert len(recommendation.get("evidence_gaps") or []) <= (MAX_POLICY_EVIDENCE_GAPS)


def _compatibility_report(replay_report: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    evidence_gaps: List[str] = []

    if replay_report.get("reducer_version") != REDUCER_VERSION:
        findings.append(
            {
                "type": "reducer_version_mismatch",
                "severity": "warning",
                "summary": "Replay report reducer version differs from simulator support",
            }
        )
        evidence_gaps.append("reducer_version")
    if replay_report.get("compatibility_version") != COMPATIBILITY_VERSION:
        findings.append(
            {
                "type": "compatibility_version_mismatch",
                "severity": "warning",
                "summary": "Replay compatibility version differs from simulator support",
            }
        )
        evidence_gaps.append("compatibility_version")

    unknown_event_types = (replay_report.get("integrity") or {}).get(
        "unknown_event_types"
    ) or []
    if unknown_event_types:
        findings.append(
            {
                "type": "unknown_event_types",
                "severity": "info",
                "event_types": list(unknown_event_types),
                "summary": "Replay ignored event types unknown to this reducer",
            }
        )
        evidence_gaps.extend(
            f"event_type:{event_type}" for event_type in unknown_event_types
        )

    malformed_count = (replay_report.get("integrity") or {}).get(
        "malformed_line_count",
        0,
    )
    if malformed_count:
        findings.append(
            {
                "type": "malformed_event_lines",
                "severity": "warning",
                "count": malformed_count,
                "summary": "Replay skipped malformed journal lines",
            }
        )
        evidence_gaps.append("malformed_jsonl")

    return {
        "version": POLICY_COMPATIBILITY_VERSION,
        "reducer_version_supported": REDUCER_VERSION,
        "replay_compatibility_supported": COMPATIBILITY_VERSION,
        "findings": findings[:MAX_POLICY_FINDINGS],
        "evidence_gaps": _bounded_unique(evidence_gaps, MAX_POLICY_EVIDENCE_GAPS),
    }


def _policy_determinism(
    replay_report: Dict[str, Any],
    *,
    compatibility: Dict[str, Any],
) -> Dict[str, Any]:
    nondeterministic_inputs: List[str] = []
    determinism_level = (replay_report.get("determinism") or {}).get("level")
    workspace_status = (replay_report.get("workspace_evidence") or {}).get("status")

    if determinism_level in {"bounded", "degraded", "failed"}:
        nondeterministic_inputs.append(f"replay_determinism:{determinism_level}")
    if workspace_status in {"hash_differs_from_snapshot", "insufficient_evidence"}:
        nondeterministic_inputs.append(f"workspace_evidence:{workspace_status}")
    nondeterministic_inputs.extend(compatibility.get("evidence_gaps") or [])

    if determinism_level == "failed":
        level = "failed"
    elif compatibility.get("findings"):
        level = "degraded"
    elif nondeterministic_inputs:
        level = "bounded"
    else:
        level = "strict"

    return {
        "level": level,
        "nondeterministic_inputs": _bounded_unique(
            nondeterministic_inputs,
            MAX_POLICY_EVIDENCE_GAPS,
        ),
    }


def _policy_checksum(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _policy_checksum_payload(profile: PolicyProfile) -> Dict[str, Any]:
    """Return the v1 compatibility payload used for stable policy checksums.

    The checksum is meant to identify behavioral policy semantics, not operator-
    facing wording.  Preserve the original v1 restore labels so copy updates do
    not churn every policy golden report.
    """

    payload = profile.to_dict()
    canonical_restore_label = _POLICY_CHECKSUM_CANONICAL_RESTORE_LABELS.get(
        profile.name
    )
    if canonical_restore_label:
        payload["restore_behavior_label"] = canonical_restore_label
        effects = payload.get("effects")
        if isinstance(effects, dict):
            effects["restore_behavior_label"] = canonical_restore_label
    return payload


def _bounded_unique(values: Iterable[Any], limit: int) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def assert_simulation_safe_vocabulary() -> None:
    """Pin the simulation event vocabulary to replay-known transition events."""

    assert SIMULATION_SAFE_EVENT_TYPES <= set(TRANSITION_EVENT_TYPES)
