#!/usr/bin/env python3
"""Scenario contract validation and regression-comparison support for the
orchestrator eval harness (Phase 30C, Program 4 — Evidence Harness
Hardening).

This module hardens the *existing* eval-harness tooling
(``scripts/evals/run_orchestrator_eval_slice.py`` and
``scripts/maintenance/score_orchestrator_eval_case.py``). It does not
introduce a new harness framework and it does not touch production
execution/planning/orchestration code — it only validates the manifest-case
and event/snapshot evidence shapes that harness already consumes, and
compares harness output for regressions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Scenario (manifest case) contract
# ---------------------------------------------------------------------------

# Fields every eval manifest case must define for the harness to score it.
CASE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"case_id", "category", "verifier", "required_events"}
)

# Fields a case may optionally define; present in the shipped manifest today.
CASE_OPTIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "purpose",
        "operator_prompt",
        "required_files",
        "forbidden_existing_files",
        "allowed_touched_prefixes",
        "expected_touched_files",
        "forbidden_touched_prefixes",
        "success_criteria",
        "workflow_stage",
        "allowed_events",
        "phase12b_role",
    }
)

# Fields every raw event-journal record must carry.
EVENT_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"event_id", "timestamp", "event_type", "session_id", "task_id"}
)

EVENT_OPTIONAL_FIELDS: frozenset[str] = frozenset({"parent_event_id", "details"})

# Report keys that are environment/wall-clock dependent, not evidence-derived.
# Excluded when checking that replay of identical evidence produces
# identical output.
DETERMINISTIC_EXCLUDE_KEYS: frozenset[str] = frozenset({"generated_at", "env_summary"})


class ScenarioContractError(ValueError):
    """Raised when a manifest case violates the required scenario contract."""


def validate_case_contract(case: dict[str, Any]) -> dict[str, Any]:
    """Validate a manifest case dict against the required/optional contract.

    Returns an explicit result — never silently accepts an unrecognized
    shape.
    """
    if not isinstance(case, dict):
        return {
            "missing_required": sorted(CASE_REQUIRED_FIELDS),
            "unknown_fields": [],
            "valid": False,
        }
    present = set(case.keys())
    missing_required = sorted(CASE_REQUIRED_FIELDS - present)
    unknown_fields = sorted(present - CASE_REQUIRED_FIELDS - CASE_OPTIONAL_FIELDS)
    verifier = case.get("verifier")
    if "verifier" not in missing_required and (
        not isinstance(verifier, dict) or not verifier.get("command")
    ):
        missing_required.append("verifier.command")
        missing_required.sort()
    return {
        "missing_required": missing_required,
        "unknown_fields": unknown_fields,
        "valid": not missing_required,
    }


def enforce_case_contract(case: dict[str, Any]) -> None:
    """Raise :class:`ScenarioContractError` if the case contract is violated.

    Unknown fields are tolerated (recorded elsewhere) since a forward-
    compatible manifest may add descriptive fields; missing required fields
    make the case unscoreable and must fail loudly.
    """
    result = validate_case_contract(case)
    if not result["valid"]:
        case_id = (
            case.get("case_id", "<unknown>") if isinstance(case, dict) else "<unknown>"
        )
        raise ScenarioContractError(
            f"scenario_contract_violation: case {case_id!r} missing required "
            f"fields: {result['missing_required']}"
        )


def validate_event_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one raw event-journal record's field shape."""
    if not isinstance(record, dict):
        return {
            "missing_required": sorted(EVENT_REQUIRED_FIELDS),
            "unknown_fields": [],
            "valid": False,
        }
    present = set(record.keys())
    missing_required = sorted(EVENT_REQUIRED_FIELDS - present)
    unknown_fields = sorted(present - EVENT_REQUIRED_FIELDS - EVENT_OPTIONAL_FIELDS)
    return {
        "missing_required": missing_required,
        "unknown_fields": unknown_fields,
        "valid": not missing_required,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_event_sequence(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect missing/unparseable/out-of-order timestamps across a run.

    Silent acceptance of an inconsistent evidence stream is not allowed —
    every anomaly is reported explicitly by index.
    """
    unparseable_timestamps: list[int] = []
    out_of_order: list[int] = []
    last_ts: datetime | None = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            unparseable_timestamps.append(index)
            continue
        parsed = _parse_timestamp(record.get("timestamp"))
        if parsed is None:
            unparseable_timestamps.append(index)
            continue
        if last_ts is not None and parsed < last_ts:
            out_of_order.append(index)
        else:
            last_ts = parsed
    return {
        "unparseable_timestamps": unparseable_timestamps,
        "out_of_order": out_of_order,
        "valid": not unparseable_timestamps and not out_of_order,
    }


def validate_event_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate field shape for every record plus overall timestamp order."""
    per_record = [validate_event_record(record) for record in records]
    record_violations = [
        {"index": index, **result}
        for index, result in enumerate(per_record)
        if not result["valid"]
    ]
    sequence = validate_event_sequence(records)
    return {
        "record_violations": record_violations,
        "sequence": sequence,
        "valid": not record_violations and sequence["valid"],
    }


# ---------------------------------------------------------------------------
# Deterministic replay / regression comparison support
# ---------------------------------------------------------------------------


def deterministic_slice(report: dict[str, Any]) -> dict[str, Any]:
    """Return the evidence-derived subset of a scorer report.

    Excludes wall-clock/host-dependent keys (``generated_at``,
    ``env_summary``) that are expected to vary across repeated runs even
    when the underlying evidence is byte-identical.
    """
    return {
        key: value
        for key, value in report.items()
        if key not in DETERMINISTIC_EXCLUDE_KEYS
    }


def _diff(
    baseline: Any, candidate: Any, path: str, differences: list[dict[str, Any]]
) -> None:
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        keys = sorted(set(baseline.keys()) | set(candidate.keys()))
        for key in keys:
            child_path = f"{path}.{key}" if path else key
            if key not in baseline:
                differences.append(
                    {"path": child_path, "baseline": None, "candidate": candidate[key]}
                )
            elif key not in candidate:
                differences.append(
                    {"path": child_path, "baseline": baseline[key], "candidate": None}
                )
            else:
                _diff(baseline[key], candidate[key], child_path, differences)
        return
    if isinstance(baseline, list) and isinstance(candidate, list):
        if baseline != candidate:
            differences.append(
                {"path": path, "baseline": baseline, "candidate": candidate}
            )
        return
    if baseline != candidate:
        differences.append({"path": path, "baseline": baseline, "candidate": candidate})


def compare_reports(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare two scorer reports for regressions, ignoring volatile keys."""
    differences: list[dict[str, Any]] = []
    _diff(
        deterministic_slice(baseline), deterministic_slice(candidate), "", differences
    )
    return {"match": not differences, "differences": differences}
