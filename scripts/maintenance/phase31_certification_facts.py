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
        repair_outcome_consistent=None,
        target_violation_repeated_after_repair=None,
        terminal_facts_contradictory=False,
        provider_identity=resolved_provider_identity,
        reason_hint=reason_hint,
    )
