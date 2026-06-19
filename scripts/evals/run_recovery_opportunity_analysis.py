#!/usr/bin/env python3
"""Analyze why recent failed tasks did or did not present Phase 13B recovery opportunities."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import json
import sys
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Session as SessionModel, Task, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.session.session_runtime_service import resolve_event_log_project_dir

REPORT_ROOT = ROOT / "docs" / "roadmap" / "reports"

RECOVERY_EVENT_TYPES = {
    EventType.EXECUTION_RECOVERY_ATTEMPTED,
    EventType.EXECUTION_RECOVERY_SUCCEEDED,
    EventType.EXECUTION_RECOVERY_FAILED,
    EventType.EXECUTION_RECOVERY_SKIPPED,
}


@dataclass
class FailureRecord:
    task_id: int
    project_id: int
    session_id: int | None
    task_title: str
    failure_category: str
    classification: str
    phase13b_entered: bool
    recovery_succeeded: bool
    recovery_failed: bool
    recovery_skipped: bool
    recovery_event_count: int
    error_message: str
    session_status: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "task_title": self.task_title,
            "failure_category": self.failure_category,
            "classification": self.classification,
            "phase13b_entered": self.phase13b_entered,
            "recovery_succeeded": self.recovery_succeeded,
            "recovery_failed": self.recovery_failed,
            "recovery_skipped": self.recovery_skipped,
            "recovery_event_count": self.recovery_event_count,
            "error_message": self.error_message,
            "session_status": self.session_status,
            "timestamp": self.timestamp,
        }


def _normalize(text: str | None) -> str:
    return str(text or "").strip().lower()


def _recovery_event_summary(events: list[dict[str, Any]]) -> tuple[int, bool, bool, bool]:
    event_types = [str(event.get("event_type") or "") for event in events]
    return (
        sum(1 for name in event_types if name in RECOVERY_EVENT_TYPES),
        EventType.EXECUTION_RECOVERY_SUCCEEDED in event_types,
        EventType.EXECUTION_RECOVERY_FAILED in event_types,
        EventType.EXECUTION_RECOVERY_SKIPPED in event_types,
    )


def classify_failed_task(*, task: Task, session: SessionModel | None, events: list[dict[str, Any]]) -> str:
    error_message = _normalize(getattr(task, "error_message", None))
    session_message = _normalize(getattr(session, "last_alert_message", None) if session else None)
    combined = " | ".join(filter(None, [error_message, session_message]))

    # Planning repair timeout/budget must be checked before generic timeout.
    if "planning repair timed out" in combined or "planning repair prompt exceeded safe budget" in combined:
        return "PLANNING_REPAIR_TIMEOUT_OR_BUDGET"

    if "bootstrap contract" in combined:
        return "BOOTSTRAP_REJECTION"
    if ("capacity" in combined) and ("backend" in combined):
        return "BACKEND_CAPACITY"
    if "timed out" in combined or "timeout" in combined:
        return "EXECUTION_TIMEOUT"
    if "verification_integrity_failed" in combined:
        return "VALIDATION_SAFETY_STOP"
    if "completion validation" in combined or "test_preservation" in combined or "integrity" in combined:
        return "VALIDATION_SAFETY_STOP"

    # Canonical writer lock must be checked before generic permission check.
    if "active canonical-root writer" in combined or "mutation.lock" in combined:
        return "CANONICAL_WRITER_LOCK_CONFLICT"

    if "permission" in combined or "policy" in combined or "workspace lock" in combined or "workspace_lock" in combined:
        return "PERMISSION_OR_POLICY"

    # Refined failure taxonomy (splits the old OTHER bucket).

    if "planning json parse failed" in combined or "failed to parse planning" in combined or "malformed json" in combined:
        return "PLANNING_JSON_OR_PARSE_FAILURE"

    if any(
        needle in combined
        for needle in (
            "planning repair still produced invalid commands",
            "plan validation failed after repair",
            "planning failed 3 time(s)",
            "planning failed 2 time(s)",
            "placeholder_only_steps",
            "test_assertion_loss_ops_steps",
            "post_repair_brittle_commands",
            "post_repair_stale_replace_fallback",
            "brittle heredoc-heavy",
            "plan contains brittle",
            "expected files without materializing",
            "placeholder or stub implementations",
        )
    ):
        return "PLANNING_REPAIR_CONTRACT_FAILURE"

    if any(
        needle in combined
        for needle in (
            "did not include concrete source materialization",
            "moved or removed required source materialization",
            "removed materializing source operations",
            "missing concrete source materialization",
            "no src/ or package implementation",
        )
    ):
        return "SOURCE_MATERIALIZATION_FAILURE"

    if any(
        needle in combined
        for needle in (
            "retry requires checkpoint resume",
            "workspace restore failed",
            "workspace remained dirty",
        )
    ):
        return "WORKSPACE_RESTORE_OR_DIRTY_STATE"

    if any(
        needle in combined
        for needle in (
            "phase 7f debug repair budget exhausted",
            "invalid bounded debug repair output",
            "invalid phase 7f debug repair output",
        )
    ):
        return "PHASE7F_DEBUG_REPAIR_FAILURE"

    if any(
        needle in combined
        for needle in (
            "replace_in_file old text not found",
            "op missing required keys",
            "must contain keys",
            "raw keys mismatch",
        )
    ):
        return "OPERATION_CONTRACT_FAILURE"

    # Phase 13B-relevant terminal failures.
    if any(
        needle in combined
        for needle in ("import_error", "pytest_failure", "missing_requested_symbol")
    ):
        return "RECOVERY_ELIGIBLE_FAILURE"

    # Event-driven fallback.
    if any(event.get("event_type") in RECOVERY_EVENT_TYPES for event in events):
        return "RECOVERY_ELIGIBLE_FAILURE"

    return "OTHER_UNCLASSIFIED"


def _recent_failed_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        tasks = (
            db.query(Task)
            .filter(Task.status == TaskStatus.FAILED)
            .order_by(Task.completed_at.desc().nullslast(), Task.updated_at.desc().nullslast(), Task.id.desc())
            .limit(limit)
            .all()
        )
        results: list[dict[str, Any]] = []
        for task in tasks:
            session = (
                db.query(SessionModel)
                .filter(SessionModel.project_id == task.project_id)
                .order_by(SessionModel.id.desc())
                .first()
            )
            session_id = session.id if session else None
            project_dir = resolve_event_log_project_dir(db, session, task.id) if session else None
            events: list[dict[str, Any]] = []
            if project_dir and session_id is not None:
                events = read_orchestration_events(project_dir, session_id, task.id)
            recovery_event_count, rec_succeeded, rec_failed, rec_skipped = _recovery_event_summary(events)
            classification = classify_failed_task(task=task, session=session, events=events)
            results.append(
                FailureRecord(
                    task_id=task.id,
                    project_id=task.project_id,
                    session_id=session_id,
                    task_title=task.title,
                    failure_category=_normalize(task.error_message)[:120] or _normalize(getattr(session, "last_alert_message", None))[:120],
                    classification=classification,
                    phase13b_entered=recovery_event_count > 0,
                    recovery_succeeded=rec_succeeded,
                    recovery_failed=rec_failed,
                    recovery_skipped=rec_skipped,
                    recovery_event_count=recovery_event_count,
                    error_message=str(task.error_message or "")[:500],
                    session_status=_normalize(getattr(session, "status", None)) if session else "",
                    timestamp=str(task.completed_at or task.updated_at or task.created_at or ""),
                ).to_dict()
            )
        return results


_ORDERED_CATEGORIES = [
    ("RECOVERY_ELIGIBLE_FAILURE", "recovery_eligible_failures"),
    ("BOOTSTRAP_REJECTION", "bootstrap_rejections"),
    ("EXECUTION_TIMEOUT", "execution_timeouts"),
    ("BACKEND_CAPACITY", "backend_capacity_failures"),
    ("VALIDATION_SAFETY_STOP", "validation_safety_stops"),
    ("PERMISSION_OR_POLICY", "permission_policy_failures"),
    ("PLANNING_JSON_OR_PARSE_FAILURE", "planning_json_or_parse_failures"),
    ("PLANNING_REPAIR_CONTRACT_FAILURE", "planning_repair_contract_failures"),
    ("PLANNING_REPAIR_TIMEOUT_OR_BUDGET", "planning_repair_timeout_or_budget_failures"),
    ("SOURCE_MATERIALIZATION_FAILURE", "source_materialization_failures"),
    ("WORKSPACE_RESTORE_OR_DIRTY_STATE", "workspace_restore_or_dirty_state_failures"),
    ("CANONICAL_WRITER_LOCK_CONFLICT", "canonical_writer_lock_conflicts"),
    ("PHASE7F_DEBUG_REPAIR_FAILURE", "phase7f_debug_repair_failures"),
    ("OPERATION_CONTRACT_FAILURE", "operation_contract_failures"),
    ("OTHER_UNCLASSIFIED", "other_unclassified_failures"),
]

_BOTTLENECK_LABEL = {
    "PLANNING_REPAIR_CONTRACT_FAILURE": "A. Planning repair quality",
    "PLANNING_REPAIR_TIMEOUT_OR_BUDGET": "B. Planning repair timeout/budget",
    "SOURCE_MATERIALIZATION_FAILURE": "C. Source materialization",
    "WORKSPACE_RESTORE_OR_DIRTY_STATE": "D. Workspace restore/dirty-state",
    "CANONICAL_WRITER_LOCK_CONFLICT": "D. Canonical writer lock state",
    "BACKEND_CAPACITY": "E. Backend capacity",
    "EXECUTION_TIMEOUT": "E. Execution timeout",
    "PHASE7F_DEBUG_REPAIR_FAILURE": "E. Phase 7F debug repair failures",
    "OPERATION_CONTRACT_FAILURE": "F. Operation contract execution",
    "BOOTSTRAP_REJECTION": "D. Bootstrap contract strictness",
    "VALIDATION_SAFETY_STOP": "A. Validation safety stops",
    "PLANNING_JSON_OR_PARSE_FAILURE": "A. Planning JSON/parse quality",
}


def _render_markdown(records: list[dict[str, Any]], report_date: str) -> str:
    counts = Counter(record["classification"] for record in records)
    total_failures = len(records)
    recovery_eligible_failures = counts["RECOVERY_ELIGIBLE_FAILURE"]
    opportunity_rate = (recovery_eligible_failures / total_failures) if total_failures else 0.0
    phase13b_entered = sum(1 for r in records if r["phase13b_entered"])
    phase13b_recovered = sum(1 for r in records if r["recovery_succeeded"])
    phase13b_failed = sum(1 for r in records if r["recovery_failed"])
    phase13b_missed = sum(
        1 for r in records if r["classification"] == "RECOVERY_ELIGIBLE_FAILURE" and not r["phase13b_entered"]
    )

    # Top-5 repeated failure signatures (first 80 chars of normalized error).
    sig_counter: Counter[str] = Counter()
    for record in records:
        sig = str(record.get("failure_category") or "").strip()[:80]
        if sig:
            sig_counter[sig] += 1
    top_sigs = sig_counter.most_common(5)

    # Top-3 blockers by count.
    blocker_counts = [
        (counts[cat], _BOTTLENECK_LABEL[cat], cat)
        for cat, _ in _ORDERED_CATEGORIES
        if cat in _BOTTLENECK_LABEL and counts[cat] > 0
    ]
    blocker_counts.sort(key=lambda x: x[0], reverse=True)
    top_blockers = blocker_counts[:3]

    lines = [
        "# Phase 13B Recovery Opportunity Analysis (E4 Refined Taxonomy)",
        "",
        f"Date: {report_date}",
        f"Sample window: last {total_failures} failed tasks",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `total_failures` | {total_failures} |",
        f"| `recovery_eligible_failures` | {recovery_eligible_failures} |",
        f"| `recovery_opportunity_rate` | {opportunity_rate:.1%} |",
        "",
        "## Failure Category Breakdown (Refined Taxonomy)",
        "",
        "| Category | Count |",
        "|---|---|",
    ]
    for cat, metric in _ORDERED_CATEGORIES:
        lines.append(f"| `{cat}` | {counts[cat]} |")

    lines.extend([
        "",
        "## Phase 13B Coverage",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `entered_phase13b` | {phase13b_entered} |",
        f"| `recovered` | {phase13b_recovered} |",
        f"| `failed_recovery` | {phase13b_failed} |",
        f"| `missed_recovery` | {phase13b_missed} |",
        "",
        "## Top 5 Repeated Failure Signatures",
        "",
        "| Count | Signature |",
        "|---|---|",
    ])
    for sig, cnt in top_sigs:
        lines.append(f"| {cnt} | {sig} |")

    lines.extend([
        "",
        "## Top 3 Blockers Preventing Phase 13B Entry",
        "",
        "| Rank | Bottleneck | Category | Count |",
        "|---|---|---|---|",
    ])
    for rank, (cnt, label, cat) in enumerate(top_blockers, 1):
        lines.append(f"| {rank} | {label} | `{cat}` | {cnt} |")

    lines.extend([
        "",
        "## Dominant Failure Class",
        "",
        f"- `{counts.most_common(1)[0][0] if counts else 'OTHER_UNCLASSIFIED'}`",
        "",
        "## Sample Records",
        "",
        "| task_id | project_id | session_id | classification | failure_category | phase13b_entered | recovery_events | error_message |",
        "|---|---:|---:|---|---|---|---:|---|",
    ])
    for record in records:
        lines.append(
            f"| {record['task_id']} | {record['project_id']} | {record['session_id'] or ''} | {record['classification']} | {record['failure_category']} | {str(record['phase13b_entered']).lower()} | {record['recovery_event_count']} | {record['error_message']} |"
        )

    lines.extend([
        "",
        "## Recommendation",
        "",
        _recommendation(counts),
    ])
    return "\n".join(lines) + "\n"


def _recommendation(counts: Counter[str]) -> str:
    total = sum(counts.values())
    if not total:
        return "No failed-task sample available."

    # Identify top category.
    top_cat = counts.most_common(1)[0][0] if counts else None

    # Joint workspace+lock check (D).
    workspace_lock = counts["WORKSPACE_RESTORE_OR_DIRTY_STATE"] + counts["CANONICAL_WRITER_LOCK_CONFLICT"]

    ordered = [
        (counts["PLANNING_REPAIR_CONTRACT_FAILURE"], "A. Planning repair quality is the dominant bottleneck — tighten repair contract validation and prompt guidance."),
        (counts["PLANNING_REPAIR_TIMEOUT_OR_BUDGET"], "B. Planning repair timeout/budget is the dominant bottleneck — reduce knowledge context size or increase budget."),
        (counts["SOURCE_MATERIALIZATION_FAILURE"], "C. Source materialization is the dominant bottleneck — verify that repair always writes src/ files."),
        (workspace_lock, "D. Workspace restore / lock state is the dominant bottleneck — investigate checkpoint resume and lock-release paths."),
        (counts["BACKEND_CAPACITY"] + counts["EXECUTION_TIMEOUT"], "E. Backend capacity or execution timeout is the dominant bottleneck — scale workers or increase timeout budget."),
        (counts["OPERATION_CONTRACT_FAILURE"], "F. Operation contract execution is the dominant bottleneck — fix replace_in_file stale-text and op-schema errors."),
    ]
    ordered.sort(key=lambda x: x[0], reverse=True)
    return ordered[0][1]


def main() -> int:
    report_date = date.today().strftime("%Y%m%d")
    records = _recent_failed_tasks(limit=50)
    output_path = REPORT_ROOT / f"phase13b-recovery-opportunity-analysis-{report_date}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(records, report_date), encoding="utf-8")
    print(output_path)
    print(json.dumps({"records": len(records), "output": str(output_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
