from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.services.orchestration.policy_simulation import (
    MAX_POLICY_EVIDENCE_EVENTS,
    MAX_POLICY_EVIDENCE_GAPS,
    MAX_POLICY_FINDINGS,
    MAX_POLICY_REASON_CODES,
)
from app.services.orchestration.replay import AUTHORITATIVE_RECONSTRUCTED_FIELDS


GOLDEN_ROOT = Path(__file__).parent / "golden"


def semantic_replay_report(report: Dict[str, Any]) -> Dict[str, Any]:
    state = report.get("state") or {}
    integrity = report.get("integrity") or {}
    checkpoint_comparison = report.get("checkpoint_comparison") or {}
    return {
        "reducer_version": report.get("reducer_version"),
        "compatibility_version": report.get("compatibility_version"),
        "boundary": _selected_keys(
            report.get("boundary") or {},
            (
                "mode",
                "requested",
                "resolved",
                "resolved_event_id",
                "resolved_checkpoint_name",
                "resolved_snapshot_index",
            ),
        ),
        "state": {
            key: state.get(key)
            for key in AUTHORITATIVE_RECONSTRUCTED_FIELDS
            if key in state
        },
        "artifact_state": {
            "changed_files": state.get("changed_files") or [],
            "workspace_hashes": state.get("workspace_hashes") or [],
            "workspace_evidence_status": state.get("workspace_evidence_status"),
            "plan_step_count": state.get("plan_step_count"),
            "tool_event_types": [
                item.get("event_type") for item in state.get("tool_events") or []
            ],
            "reasoning_artifact_types": [
                item.get("artifact_type")
                for item in state.get("reasoning_artifacts") or []
            ],
        },
        "field_classification": report.get("field_classification"),
        "integrity": {
            "confidence": integrity.get("confidence"),
            "event_count_read": integrity.get("event_count_read"),
            "event_count_applied": integrity.get("event_count_applied"),
            "malformed_line_count": integrity.get("malformed_line_count"),
            "unknown_event_types": integrity.get("unknown_event_types") or [],
            "findings": _semantic_findings(integrity.get("findings") or []),
        },
        "determinism": report.get("determinism"),
        "drift_findings": _semantic_findings(report.get("drift_findings") or []),
        "checkpoint_comparison": (
            _selected_keys(
                checkpoint_comparison,
                ("status", "checkpoint_name", "mismatches"),
            )
            if checkpoint_comparison
            else None
        ),
        "workspace_evidence": _selected_keys(
            report.get("workspace_evidence") or {},
            ("status", "known_workspace_hash"),
        ),
    }


def semantic_policy_report(report: Dict[str, Any]) -> Dict[str, Any]:
    policy = report.get("policy") or {}
    compatibility = report.get("compatibility") or {}
    return {
        "simulation_version": report.get("simulation_version"),
        "policy": _selected_keys(
            policy,
            ("family", "profile", "version", "checksum"),
        ),
        "compatibility": {
            "version": compatibility.get("version"),
            "reducer_version_supported": compatibility.get("reducer_version_supported"),
            "replay_compatibility_supported": compatibility.get(
                "replay_compatibility_supported"
            ),
            "findings": _semantic_findings(compatibility.get("findings") or []),
            "evidence_gaps": compatibility.get("evidence_gaps") or [],
        },
        "policy_determinism": report.get("policy_determinism"),
        "authoritative_inputs": report.get("authoritative_inputs"),
        "supporting_inputs": report.get("supporting_inputs"),
        "recommendation": report.get("recommendation"),
        "findings": _semantic_findings(report.get("findings") or []),
        "budgets": report.get("budgets"),
    }


def assert_replay_matches_golden(
    fixture_id: str,
    report: Dict[str, Any],
) -> None:
    assert_semantic_report_matches_golden(
        GOLDEN_ROOT / "replay" / f"{fixture_id}.json",
        semantic_replay_report(report),
    )


def assert_policy_matches_golden(
    fixture_id: str,
    report: Dict[str, Any],
    *,
    profile: str,
) -> None:
    assert_semantic_report_matches_golden(
        GOLDEN_ROOT / "policy" / f"{fixture_id}.{profile}.json",
        semantic_policy_report(report),
    )


def assert_semantic_report_matches_golden(
    golden_path: Path,
    actual: Dict[str, Any],
) -> None:
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    if actual == expected:
        return

    expected_text = _pretty_json(expected).splitlines(keepends=True)
    actual_text = _pretty_json(actual).splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            expected_text,
            actual_text,
            fromfile=str(golden_path),
            tofile="actual",
        )
    )
    drift_summary = _compatibility_drift_summary(actual)
    raise AssertionError(
        f"Semantic report drift for {golden_path}.\n" f"{drift_summary}\n" f"{diff}"
    )


def assert_policy_budget_contract(report: Dict[str, Any]) -> None:
    recommendation = report.get("recommendation") or {}
    assert len(recommendation.get("evidence_event_ids") or []) <= (
        MAX_POLICY_EVIDENCE_EVENTS
    )
    assert len(report.get("findings") or []) <= MAX_POLICY_FINDINGS
    assert len(recommendation.get("reason_codes") or []) <= MAX_POLICY_REASON_CODES
    assert len(recommendation.get("evidence_gaps") or []) <= MAX_POLICY_EVIDENCE_GAPS


def _semantic_findings(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    semantic: List[Dict[str, Any]] = []
    for finding in findings:
        semantic.append(
            _selected_keys(
                finding,
                (
                    "type",
                    "severity",
                    "event_type",
                    "event_types",
                    "event_id",
                    "count",
                    "mismatches",
                ),
            )
        )
    return semantic


def _selected_keys(payload: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    return {key: payload.get(key) for key in keys if key in payload}


def _pretty_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _compatibility_drift_summary(actual: Dict[str, Any]) -> str:
    compatibility = actual.get("compatibility") or {}
    integrity = actual.get("integrity") or {}
    determinism = actual.get("determinism") or actual.get("policy_determinism") or {}
    finding_types = [
        item.get("type")
        for item in (
            compatibility.get("findings")
            or integrity.get("findings")
            or actual.get("findings")
            or []
        )
    ]
    return (
        "Compatibility drift: "
        f"compatibility={compatibility.get('version')}; "
        f"integrity={integrity.get('confidence')}; "
        f"determinism={determinism.get('level')}; "
        f"findings={finding_types}"
    )
