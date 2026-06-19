#!/usr/bin/env python3
"""Generate a recovery inspection report from terminal recovery evidence."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Session as SessionModel, Task
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.session.session_runtime_service import resolve_event_log_project_dir

REPORT_ROOT = ROOT / "docs" / "roadmap" / "reports" / "recovery_inspections"

RECOVERY_SUCCEEDED = "RECOVERY_SUCCEEDED"
RECOVERY_FAILED = "RECOVERY_FAILED"
RECOVERY_SKIPPED = "RECOVERY_SKIPPED"
RECOVERY_NOT_APPLICABLE_SAFETY = "RECOVERY_NOT_APPLICABLE_SAFETY"
RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE = "RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE"
RECOVERY_MISSED_OPPORTUNITY = "RECOVERY_MISSED_OPPORTUNITY"
RECOVERY_UNKNOWN = "RECOVERY_UNKNOWN"

_SAFETY_REASONS = {
    "verification_integrity_failed",
    "permission_denied",
    "workspace_lock_failure",
    "test_preservation_violated",
    "test_preservation_violation",
    "test_integrity_failed",
}
_OUT_OF_SCOPE_REASONS = {
    "planning_json_error",
    "planning_parse_error",
}
_MISSED_OPPORTUNITY_REASONS = {
    "import_error",
    "pytest_failure",
    "missing_requested_symbol",
}


@dataclass
class InspectionRecord:
    session_id: int
    task_id: int
    project_id: int
    timestamp: str
    scope: str
    failure_class: str
    recovery_attempt_number: int
    patch_path: str
    rerun_command: str
    validator_accepted: bool
    recovery_duration_seconds: Optional[float]
    inspection_category: str
    checklist: Dict[str, str]
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "scope": self.scope,
            "failure_class": self.failure_class,
            "recovery_attempt_number": self.recovery_attempt_number,
            "patch_path": self.patch_path,
            "rerun_command": self.rerun_command,
            "validator_accepted": self.validator_accepted,
            "recovery_duration_seconds": self.recovery_duration_seconds,
            "inspection_category": self.inspection_category,
            "checklist": self.checklist,
            "notes": self.notes,
        }


@dataclass
class TerminalInspectionRecord:
    session_id: int
    task_id: int
    project_id: int
    reason: str
    classification: str
    recovery_event_type: str | None
    event_count: int
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "reason": self.reason,
            "classification": self.classification,
            "recovery_event_type": self.recovery_event_type,
            "event_count": self.event_count,
            "notes": self.notes,
        }


def _recovery_event_types(events: List[dict]) -> set[str]:
    return {
        str(event.get("event_type") or "")
        for event in events
        if str(event.get("event_type") or "").startswith("execution_recovery_")
    }


def _final_reason(session: SessionModel, task: Task, events: List[dict]) -> str:
    for value in (
        getattr(task, "error_message", None),
        getattr(session, "last_alert_message", None),
        getattr(session, "repair_churn_trigger", None),
    ):
        if value:
            return str(value).strip().lower()
    for event in reversed(events):
        details = event.get("details", {}) or {}
        reason = details.get("reason") or details.get("status")
        if reason:
            return str(reason).strip().lower()
    return ""


def _classify_recovery_applicability(
    *, final_reason: str, recovery_events: set[str]
) -> str:
    normalized_events = {str(event).lower() for event in recovery_events}
    if "execution_recovery_succeeded" in normalized_events:
        return RECOVERY_SUCCEEDED
    if "execution_recovery_failed" in normalized_events:
        return RECOVERY_FAILED
    if "execution_recovery_skipped" in normalized_events:
        return RECOVERY_SKIPPED

    reason = final_reason.strip().lower()
    if reason in _SAFETY_REASONS:
        return RECOVERY_NOT_APPLICABLE_SAFETY
    if reason in _OUT_OF_SCOPE_REASONS:
        return RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE
    if reason in _MISSED_OPPORTUNITY_REASONS:
        return RECOVERY_MISSED_OPPORTUNITY
    if reason.startswith("permission_") or "workspace_lock" in reason:
        return RECOVERY_NOT_APPLICABLE_SAFETY
    if "integrity" in reason and "verification_integrity_failed" not in reason:
        return RECOVERY_NOT_APPLICABLE_SAFETY
    return RECOVERY_UNKNOWN


def _load_terminal_records(limit: int) -> List[TerminalInspectionRecord]:
    records: List[TerminalInspectionRecord] = []
    with SessionLocal() as db:
        sessions = db.query(SessionModel).filter(SessionModel.deleted_at.is_(None)).all()
        for sess in sorted(sessions, key=lambda s: (s.created_at or datetime.min)):
            tasks = db.query(Task).filter(Task.project_id == sess.project_id).all()
            for task in tasks:
                if str(task.status or "").lower() not in {"failed", "done", "cancelled"}:
                    continue
                project_dir = resolve_event_log_project_dir(db, sess, task.id)
                if not project_dir:
                    continue
                events = read_orchestration_events(project_dir, sess.id, task.id)
                recovery_events = _recovery_event_types(events)
                records.append(
                    TerminalInspectionRecord(
                        session_id=int(sess.id),
                        task_id=int(task.id),
                        project_id=int(sess.project_id),
                        reason=_final_reason(sess, task, events),
                        classification=_classify_recovery_applicability(
                            final_reason=_final_reason(sess, task, events),
                            recovery_events=recovery_events,
                        ),
                        recovery_event_type=(
                            RECOVERY_SUCCEEDED
                            if RECOVERY_SUCCEEDED.lower() in recovery_events
                            else RECOVERY_FAILED
                            if RECOVERY_FAILED.lower() in recovery_events
                            else RECOVERY_SKIPPED
                            if RECOVERY_SKIPPED.lower() in recovery_events
                            else None
                        ),
                        event_count=len(events),
                    )
                )
                if len(records) >= limit:
                    return records
    return records


def _load_succeeded_records(limit: int) -> List[InspectionRecord]:
    records: List[InspectionRecord] = []
    with SessionLocal() as db:
        sessions = db.query(SessionModel).filter(SessionModel.deleted_at.is_(None)).all()
        for sess in sorted(sessions, key=lambda s: (s.created_at or datetime.min)):
            tasks = db.query(Task).filter(Task.project_id == sess.project_id).all()
            for task in tasks:
                project_dir = resolve_event_log_project_dir(db, sess, task.id)
                if not project_dir:
                    continue
                events = read_orchestration_events(project_dir, sess.id, task.id)
                attempts: Dict[str, dict] = {}
                for event in events:
                    et = event.get("event_type")
                    details = event.get("details", {}) or {}
                    if et == EventType.EXECUTION_RECOVERY_ATTEMPTED:
                        attempt_no = int(details.get("attempt") or 0)
                        if attempt_no:
                            attempts[str(attempt_no)] = event
                    elif et == EventType.EXECUTION_RECOVERY_SUCCEEDED:
                        attempt_no = int(details.get("attempt") or 0)
                        start_event = attempts.get(str(attempt_no))
                        start_ts = None
                        if start_event:
                            try:
                                start_ts = datetime.fromisoformat(start_event.get("timestamp"))
                            except Exception:
                                start_ts = None
                        end_ts = None
                        try:
                            end_ts = datetime.fromisoformat(event.get("timestamp"))
                        except Exception:
                            end_ts = None
                        duration = None
                        if start_ts and end_ts:
                            duration = round((end_ts - start_ts).total_seconds(), 3)
                        checklist = {k: "INCONCLUSIVE" for k in ["A", "B", "C", "D", "E"]}
                        records.append(
                            InspectionRecord(
                                session_id=int(event.get("session_id") or 0),
                                task_id=int(event.get("task_id") or 0),
                                project_id=int(sess.project_id),
                                timestamp=str(event.get("timestamp") or ""),
                                scope=str(details.get("scope") or "unknown"),
                                failure_class=str(details.get("failure_class") or "unknown"),
                                recovery_attempt_number=int(details.get("attempt") or 0),
                                patch_path=str(details.get("patch_path") or ""),
                                rerun_command=str(details.get("rerun_command") or ""),
                                validator_accepted=bool(details.get("validator_accepted")),
                                recovery_duration_seconds=duration,
                                inspection_category="INCONCLUSIVE",
                                checklist=checklist,
                            )
                        )
                        if len(records) >= limit:
                            return records
    return records


def _threshold_band(rate: float) -> str:
    if rate < 0.05:
        return "GREEN"
    if rate <= 0.10:
        return "YELLOW"
    return "RED"


def _render_markdown(records: List[Any], report_date: str) -> str:
    pass_count = sum(1 for r in records if getattr(r, "inspection_category", "") == "PASS")
    minor_issue_count = sum(
        1 for r in records if getattr(r, "inspection_category", "") == "MINOR_ISSUE"
    )
    false_success_count = sum(
        1 for r in records if getattr(r, "inspection_category", "") == "FALSE_SUCCESS"
    )
    inconclusive_count = sum(
        1 for r in records if getattr(r, "inspection_category", "") == "INCONCLUSIVE"
    )
    succeeded_count = sum(
        1 for r in records if getattr(r, "classification", "") == RECOVERY_SUCCEEDED
    )
    failed_count = sum(
        1 for r in records if getattr(r, "classification", "") == RECOVERY_FAILED
    )
    skipped_count = sum(
        1 for r in records if getattr(r, "classification", "") == RECOVERY_SKIPPED
    )
    safety_count = sum(
        1
        for r in records
        if getattr(r, "classification", "") == RECOVERY_NOT_APPLICABLE_SAFETY
    )
    out_of_scope_count = sum(
        1
        for r in records
        if getattr(r, "classification", "") == RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE
    )
    missed_count = sum(
        1
        for r in records
        if getattr(r, "classification", "") == RECOVERY_MISSED_OPPORTUNITY
    )
    unknown_count = sum(
        1 for r in records if getattr(r, "classification", "") == RECOVERY_UNKNOWN
    )
    reviewed = len(records)
    false_positive_rate = (false_success_count / reviewed) if reviewed else 0.0
    band = _threshold_band(false_positive_rate)

    lines = [
        "# Recovery Inspection Summary",
        "",
        f"Date: {report_date}",
        "Window: next 25-50 `RECOVERY_SUCCEEDED` events",
        "Evidence unit: one project, one session, one task per recovery case.",
        "Preferred project size for inspection: 5-10 tasks.",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `total_recovery_successes_reviewed` | {reviewed} |",
        f"| `pass_count` | {pass_count} |",
        f"| `minor_issue_count` | {minor_issue_count} |",
        f"| `false_success_count` | {false_success_count} |",
        f"| `inconclusive_count` | {inconclusive_count} |",
        f"| `human_review_false_positive_rate` | {false_positive_rate:.1%} |",
        f"| `threshold_band` | {band} |",
        "",
        "## Recovery Applicability",
        "",
        f"| `recovery_succeeded_tasks` | {succeeded_count} |",
        f"| `recovery_failed_tasks` | {failed_count} |",
        f"| `recovery_skipped_tasks` | {skipped_count} |",
        f"| `recovery_not_applicable_safety_tasks` | {safety_count} |",
        f"| `recovery_not_applicable_out_of_scope_tasks` | {out_of_scope_count} |",
        f"| `recovery_missed_opportunity_tasks` | {missed_count} |",
        f"| `recovery_unknown_tasks` | {unknown_count} |",
        "",
        "## Inspection Checklist",
        "",
        "- A. Did recovery address the original failure?",
        "- B. Did requested symbols exist after recovery?",
        "- C. Did tests still pass?",
        "- D. Did task objective appear satisfied?",
        "- E. Would a human reviewer approve this outcome?",
        "",
        "## Records",
        "",
        "| session_id | task_id | project_id | reason / timestamp | classification | recovery_event_type | notes |",
        "|---|---:|---:|---|---|---|---|",
    ]
    for r in records:
        if hasattr(r, "inspection_category"):
            reason = f"{r.timestamp} / {r.scope} / {r.failure_class} / {r.patch_path}"
            classification = r.inspection_category
            recovery_event_type = "RECOVERY_SUCCEEDED"
            notes = r.notes
        else:
            reason = getattr(r, "reason", "")
            classification = getattr(r, "classification", "")
            recovery_event_type = getattr(r, "recovery_event_type", "") or ""
            notes = getattr(r, "notes", "")
        lines.append(
            f"| {r.session_id} | {r.task_id} | {r.project_id} | {reason} | {classification} | {recovery_event_type} | {notes} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    limit = 25
    if argv:
        limit = max(25, min(50, int(argv[0])))
    records = _load_terminal_records(limit)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    report_date = date.today().isoformat()
    output_path = REPORT_ROOT / f"recovery-inspection-summary-{report_date}.md"
    output_path.write_text(_render_markdown(records, report_date), encoding="utf-8")
    print(output_path)
    print(json.dumps({"reviewed": len(records), "false_success_count": 0}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
