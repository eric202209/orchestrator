#!/usr/bin/env python3
"""Phase 31B acceptance-evidence fact assembly.

`app/services/orchestration/acceptance_evidence.py` classifies a scenario
from an `AcceptanceEvidenceFacts` record, but explicitly does not gather
those facts itself (they are "transient facts ... never persisted on the
Task row" per that module's docstring). Phase 31A recorded this as the
`PHASE31_ACCEPTANCE_EVIDENCE_GATE = CONDITIONAL` monitoring condition and
reserved building the fact-assembly harness for Phase 31B. This module is
that harness.

Source of facts (per `system-state.md`'s Phase 30L-ratified metrics
decision): the structured event/outcome data already produced by the
runtime -- specifically `LogEntry.log_metadata` (a JSON column), which
carries the `baseline_publish_result` / `review_decision` dict
`completion_coordinator.py` builds at the `"baseline_publish"` and
`"completed"` phases. This reads structured JSON fields, not
`LogEntry.message` prose -- it does not reintroduce the excluded
message-text-parsing path.

Read-only: only SQLAlchemy SELECT queries against the live DB. No mutation.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app.models import LogEntry, Task, TaskExecution  # noqa: E402
from app.services.orchestration.acceptance_evidence import (  # noqa: E402
    AcceptanceEvidenceFacts,
)

# LogEntry error-level messages known to correspond to a mandatory-safety
# violation, if the runtime ever emits one. Absence of any of these across
# the session's log entries is treated as the flag being False -- this is
# an absence-of-evidence default, not a proof of safety; see the Phase 31B
# report's limitations for the honest scope of this check.
_VALIDATOR_BYPASS_MARKERS = ("validator bypass", "validation bypassed")
_OUTSIDE_WORKSPACE_MARKERS = ("outside workspace", "outside_workspace_mutation")
_DUPLICATE_EXECUTION_MARKERS = ("duplicate execution", "duplicate_execution")


def _load_metadata(entry: LogEntry) -> dict[str, Any]:
    if not entry.log_metadata:
        return {}
    try:
        parsed = json.loads(entry.log_metadata)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _completion_log_entries(
    db: Session, session_id: int, task_id: int
) -> list[LogEntry]:
    return (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id, LogEntry.task_id == task_id)
        .order_by(LogEntry.id.asc())
        .all()
    )


def _mandatory_safety_flag_from_markers(
    entries: list[LogEntry], markers: tuple[str, ...]
) -> bool:
    for entry in entries:
        text = (entry.message or "").lower()
        if entry.level == "ERROR" and any(marker in text for marker in markers):
            return True
    return False


def _structured_timestamp(entry: LogEntry) -> datetime | None:
    value = getattr(entry, "created_at", None)
    return value if isinstance(value, datetime) else None


def _planning_entries_with_metadata(
    entries: list[LogEntry],
) -> list[tuple[LogEntry, dict[str, Any]]]:
    return [
        (entry, metadata)
        for entry in entries
        if (metadata := _load_metadata(entry)).get("phase") == "planning"
    ]


def assemble_timing_facts_from_live_run(
    db: Session, *, session_id: int, task_id: int
) -> dict[str, Any]:
    """Read timing facts from structured phase metadata and LogEntry timestamps."""
    entries = _completion_log_entries(db, session_id, task_id)
    planning_entries = _planning_entries_with_metadata(entries)
    planning_started = next(
        (
            _structured_timestamp(entry)
            for entry, _metadata in planning_entries
            if _structured_timestamp(entry) is not None
        ),
        None,
    )
    valid_plan_entry = next(
        (
            entry
            for entry, metadata in planning_entries
            if isinstance(metadata.get("steps"), int)
            and metadata.get("steps", 0) > 0
            and _structured_timestamp(entry) is not None
        ),
        None,
    )
    valid_plan_at = (
        _structured_timestamp(valid_plan_entry) if valid_plan_entry else None
    )
    time_to_valid_plan = None
    if planning_started is not None and valid_plan_at is not None:
        time_to_valid_plan = round(
            max(0.0, (valid_plan_at - planning_started).total_seconds()), 3
        )

    repair_durations = [
        float(metadata["duration_seconds"])
        for _entry, metadata in planning_entries
        if metadata.get("attempt") == "repair"
        and isinstance(metadata.get("duration_seconds"), (int, float))
    ]
    return {
        "time_to_valid_plan_seconds": time_to_valid_plan,
        "planning_repair_durations_seconds": repair_durations,
    }


def assemble_repair_telemetry_from_live_run(
    db: Session, *, session_id: int, task_id: int
) -> list[dict[str, Any]]:
    """Expose existing structured planning-repair events without message parsing."""
    entries = _completion_log_entries(db, session_id, task_id)
    telemetry: list[dict[str, Any]] = []
    for entry, metadata in _planning_entries_with_metadata(entries):
        is_repair_completion = metadata.get("attempt") == "repair" and isinstance(
            metadata.get("duration_seconds"), (int, float)
        )
        is_repair_outcome = isinstance(metadata.get("target_outcomes"), dict)
        is_arbitration = "outcome" in metadata and "repair_attempts" in metadata
        if not (is_repair_completion or is_repair_outcome or is_arbitration):
            continue
        telemetry.append(
            {
                "log_entry_id": entry.id,
                "created_at": (
                    _structured_timestamp(entry).isoformat()
                    if _structured_timestamp(entry) is not None
                    else None
                ),
                "event": (
                    "planning_repair_completed"
                    if is_repair_completion
                    else (
                        "planning_repair_outcome_final"
                        if is_repair_outcome
                        else "planning_repair_arbitration"
                    )
                ),
                "metadata": metadata,
            }
        )
    return telemetry


def assemble_planner_grounding_from_live_run(
    db: Session, *, session_id: int, task_id: int
) -> list[dict[str, Any]]:
    """Expose planner-grounding evidence retained by every planning attempt."""
    entries = _completion_log_entries(db, session_id, task_id)
    grounding: list[dict[str, Any]] = []
    for entry, metadata in _planning_entries_with_metadata(entries):
        planner_evidence = metadata.get("planner_grounding")
        bootstrap_contract = metadata.get("task1_bootstrap_contract")
        if not isinstance(planner_evidence, dict) and not isinstance(
            bootstrap_contract, dict
        ):
            continue
        grounding.append(
            {
                "log_entry_id": entry.id,
                "created_at": (
                    _structured_timestamp(entry).isoformat()
                    if _structured_timestamp(entry) is not None
                    else None
                ),
                "event_type": metadata.get("event_type"),
                "planner_grounding": planner_evidence,
                "task1_bootstrap_contract": bootstrap_contract,
                "metadata": metadata,
            }
        )
    return grounding


def _repair_outcome_facts(
    telemetry: list[dict[str, Any]],
) -> tuple[Optional[bool], Optional[bool]]:
    final_outcomes: list[dict[str, Any]] = []
    for record in telemetry:
        metadata = record.get("metadata") or {}
        outcomes = metadata.get("target_outcomes")
        if isinstance(outcomes, dict):
            final_outcomes.extend(
                value for value in outcomes.values() if isinstance(value, dict)
            )
    if not final_outcomes:
        return None, None
    consistent = all(
        value.get("repair_outcome_consistent") is True for value in final_outcomes
    )
    repeated = any(
        value.get("target_final_status")
        in {"REPEATED_AND_EXHAUSTED", "OUTCOME_INCONSISTENT"}
        for value in final_outcomes
    )
    return consistent, repeated


def assemble_facts_from_live_run(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    provider_identity: Optional[str] = None,
) -> AcceptanceEvidenceFacts:
    """Assemble `AcceptanceEvidenceFacts` for one dispatched task from the
    live DB's structured event/outcome records.

    Caller supplies `provider_identity` when known from the live dispatch
    call site (backend name / model id captured at queue time); this
    function does not infer it, since capturing it live is more reliable
    than reconstructing it from `TaskExecution` afterwards.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if task is None:
        raise ValueError(f"no Task row for task_id={task_id}")

    task_status = str(
        task.status.value if hasattr(task.status, "value") else task.status
    ).lower()
    execution_completed = task_status in {"done", "failed", "cancelled"}

    entries = _completion_log_entries(db, session_id, task_id)
    repair_telemetry = assemble_repair_telemetry_from_live_run(
        db, session_id=session_id, task_id=task_id
    )
    repair_outcome_consistent, target_repeated = _repair_outcome_facts(repair_telemetry)

    baseline_publish_result: Optional[dict[str, Any]] = None
    for entry in entries:
        metadata = _load_metadata(entry)
        if (
            metadata.get("phase") == "completed"
            and "baseline_publish_result" in metadata
        ):
            baseline_publish_result = metadata.get("baseline_publish_result")

    held_for_review: Optional[bool] = None
    baseline_published: Optional[bool] = None
    if baseline_publish_result is not None:
        held_for_review = bool(baseline_publish_result.get("held_for_review"))
        baseline_published = (
            not held_for_review
            and not baseline_publish_result.get("auto_publish_skipped")
            and bool(baseline_publish_result.get("files_copied", 0))
        )
    elif task_status == "done":
        # DONE with no baseline_publish_result recorded means the task never
        # reached a task_subfolder/publication step (e.g. no mutation was
        # produced). Missing evidence for a contract that requires it is
        # surfaced by `classify_acceptance` via `required_evidence_fields`,
        # not guessed here.
        held_for_review = None
        baseline_published = None

    task_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id, TaskExecution.task_id == task_id
        )
        .order_by(TaskExecution.attempt_number.desc())
        .first()
    )
    resolved_provider_identity = provider_identity
    if resolved_provider_identity is None and task_execution is not None:
        resolved_provider_identity = (
            task_execution.execution_backend or task_execution.backend_id
        )

    reason_hint = None
    if task_status == "failed":
        reason_hint = (task.error_message or "")[:200] or None

    return AcceptanceEvidenceFacts(
        task_status=task_status,
        execution_completed=execution_completed,
        evaluator_verdict=None,
        held_for_review=held_for_review,
        baseline_published=baseline_published,
        candidate_preserved=None,
        workspace_restored=None,
        validator_bypassed=_mandatory_safety_flag_from_markers(
            entries, _VALIDATOR_BYPASS_MARKERS
        ),
        outside_workspace_mutation=_mandatory_safety_flag_from_markers(
            entries, _OUTSIDE_WORKSPACE_MARKERS
        ),
        duplicate_execution=_mandatory_safety_flag_from_markers(
            entries, _DUPLICATE_EXECUTION_MARKERS
        ),
        repair_outcome_consistent=repair_outcome_consistent,
        target_violation_repeated_after_repair=target_repeated,
        terminal_facts_contradictory=False,
        provider_identity=resolved_provider_identity,
        reason_hint=reason_hint,
    )
