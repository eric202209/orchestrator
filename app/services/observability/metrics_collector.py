"""Backend-authoritative operational metrics collected from DB state."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session as DbSession

from app.models import (
    KnowledgeUsageLog,
    LogEntry,
    Project,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_ROOT,
    PROMOTED_WORKSPACE_ARCHIVE_ROOT,
    REJECTED_CHANGE_ARCHIVE_ROOT,
    RETAINED_WORKSPACE_ARCHIVE_ROOT,
)
from app.services.orchestration.security_policy.retention_policy import (
    SNAPSHOT_MAX_AGE_DAYS,
    SNAPSHOT_MAX_COUNT,
)
from app.services.orchestration.security_policy.workspace_quota import (
    WORKSPACE_QUOTA_MAX_BYTES,
    check_workspace_size,
)

# Import terminal reason set so failure_class_distribution filters to real
# terminal failures only — not repair attempts, warnings, or diagnostics.
try:
    from scripts.failure_taxonomy import REPORT_TERMINAL_REASONS as _TERMINAL_REASONS
except Exception:
    _TERMINAL_REASONS: frozenset[str] = frozenset()  # type: ignore[misc]

SQLITE_FALLBACK_REASON = "sqlite_fallback_qdrant_or_embedding_unavailable"
MUTATION_LOCK_REASON = "project_mutation_lock_conflict"

_ARCHIVE_ROOTS = (
    AUTO_SNAPSHOT_ROOT,
    PROMOTED_WORKSPACE_ARCHIVE_ROOT,
    REJECTED_CHANGE_ARCHIVE_ROOT,
    RETAINED_WORKSPACE_ARCHIVE_ROOT,
)


def _parse_meta(value: Any) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.mean(values), 3)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    idx = max(0, int(len(values) * 0.95) - 1)
    return round(sorted(values)[idx], 3)


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class MetricsCollector:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def _cutoff(self, days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    # ------------------------------------------------------------------
    # Phase latency
    # ------------------------------------------------------------------

    def phase_latency(self, days: int = 1) -> dict[str, Any]:
        cutoff = self._cutoff(days)

        meta_rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )

        planning_durations: list[float] = []
        repair_durations: list[float] = []

        for (raw,) in meta_rows:
            meta = _parse_meta(raw)

            pd = meta.get("planning_duration")
            if pd is not None:
                try:
                    planning_durations.append(float(pd))
                except (TypeError, ValueError):
                    pass

            is_repair = (
                meta.get("retry") == "repair_prompt"
                or meta.get("attempt") == "repair"
                or int(meta.get("repair_attempts") or 0) > 0
            )
            if is_repair:
                rd = meta.get("duration_seconds")
                if rd is not None:
                    try:
                        repair_durations.append(float(rd))
                    except (TypeError, ValueError):
                        pass

        exec_rows = (
            self.db.query(TaskExecution.started_at, TaskExecution.completed_at)
            .filter(
                TaskExecution.status == TaskStatus.DONE,
                TaskExecution.started_at.isnot(None),
                TaskExecution.completed_at.isnot(None),
                TaskExecution.started_at >= cutoff,
            )
            .all()
        )
        exec_durations: list[float] = []
        for started, completed in exec_rows:
            if started and completed:
                try:
                    diff = (completed - started).total_seconds()
                    if diff >= 0:
                        exec_durations.append(diff)
                except Exception:
                    pass

        return {
            "planning": {
                "mean_seconds": _mean(planning_durations),
                "p95_seconds": _p95(planning_durations),
                "sample_count": len(planning_durations),
            },
            "execution": {
                "mean_seconds": _mean(exec_durations),
                "p95_seconds": _p95(exec_durations),
                "sample_count": len(exec_durations),
            },
            "repair": {
                "mean_seconds": _mean(repair_durations),
                "p95_seconds": _p95(repair_durations),
                "sample_count": len(repair_durations),
            },
        }

    # ------------------------------------------------------------------
    # Repair stats
    # ------------------------------------------------------------------

    def repair_stats(self, days: int = 1) -> dict[str, Any]:
        cutoff = self._cutoff(days)

        rows = (
            self.db.query(LogEntry.session_id, LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )

        sessions_with_repair: set[int] = set()
        sessions_repair_ok: set[int] = set()
        total_events = 0

        for session_id, raw in rows:
            meta = _parse_meta(raw)
            is_repair = (
                meta.get("retry") == "repair_prompt"
                or meta.get("attempt") == "repair"
                or int(meta.get("repair_attempts") or 0) > 0
            )
            if not is_repair:
                continue
            sessions_with_repair.add(session_id)
            total_events += 1
            if meta.get("repair_success") or meta.get("outcome") == "success":
                sessions_repair_ok.add(session_id)

        repair_count = len(sessions_with_repair)
        success_count = len(sessions_repair_ok)
        return {
            "sessions_with_repair": repair_count,
            "sessions_repair_succeeded": success_count,
            "repair_success_rate": (
                round(success_count / repair_count, 3) if repair_count > 0 else None
            ),
            "total_repair_events": total_events,
        }

    # ------------------------------------------------------------------
    # Retry distribution
    # ------------------------------------------------------------------

    def retry_distribution(self, days: int = 7) -> dict[str, Any]:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(TaskExecution.attempt_number)
            .filter(TaskExecution.created_at >= cutoff)
            .all()
        )
        dist: dict[int, int] = {}
        for (attempt,) in rows:
            dist[attempt] = dist.get(attempt, 0) + 1
        return {
            "distribution": {str(k): v for k, v in sorted(dist.items())},
            "total_executions": sum(dist.values()),
            "max_attempt": max(dist.keys()) if dist else 0,
        }

    # ------------------------------------------------------------------
    # Review policy outcomes
    # ------------------------------------------------------------------

    def review_policy_outcomes(self, days: int = 1) -> dict[str, Any]:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(TaskExecutionChangeSet.review_decision)
            .filter(
                TaskExecutionChangeSet.created_at >= cutoff,
                TaskExecutionChangeSet.review_decision.isnot(None),
            )
            .all()
        )
        counts: dict[str, int] = {}
        for (rd,) in rows:
            if not isinstance(rd, dict):
                continue
            outcome = rd.get("outcome")
            if outcome:
                counts[outcome] = counts.get(outcome, 0) + 1

        known = {"auto_promote", "hold_for_review", "allow_with_warning"}
        return {
            "auto_promote": counts.get("auto_promote", 0),
            "hold_for_review": counts.get("hold_for_review", 0),
            "allow_with_warning": counts.get("allow_with_warning", 0),
            "other": {k: v for k, v in counts.items() if k not in known},
        }

    # ------------------------------------------------------------------
    # Operator decisions
    # ------------------------------------------------------------------

    def operator_decisions(self, days: int = 1) -> dict[str, int]:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(TaskExecutionChangeSet.disposition, func.count())
            .filter(
                TaskExecutionChangeSet.disposition_at >= cutoff,
                TaskExecutionChangeSet.disposition.isnot(None),
            )
            .group_by(TaskExecutionChangeSet.disposition)
            .all()
        )
        return {disposition: count for disposition, count in rows}

    # ------------------------------------------------------------------
    # Rollback count (rejected dispositions)
    # ------------------------------------------------------------------

    def rollback_count(self, days: int = 1) -> int:
        cutoff = self._cutoff(days)
        return (
            self.db.query(func.count(TaskExecutionChangeSet.id))
            .filter(
                TaskExecutionChangeSet.disposition_at >= cutoff,
                TaskExecutionChangeSet.disposition == "rejected",
            )
            .scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # Mutation lock conflicts
    # ------------------------------------------------------------------

    def mutation_lock_conflicts(self, days: int = 1) -> int:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )
        count = 0
        for (raw,) in rows:
            meta = _parse_meta(raw)
            reason = meta.get("reason") or meta.get("terminal_reason") or ""
            if MUTATION_LOCK_REASON in reason:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Qdrant fallback count
    # ------------------------------------------------------------------

    def qdrant_fallback_count(self, days: int = 1) -> int:
        cutoff = self._cutoff(days)
        return (
            self.db.query(func.count(KnowledgeUsageLog.id))
            .filter(
                KnowledgeUsageLog.created_at >= cutoff,
                KnowledgeUsageLog.retrieval_reason.contains(SQLITE_FALLBACK_REASON),
            )
            .scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # OpenClaw timeout / no-output count
    # ------------------------------------------------------------------

    def openclaw_timeout_count(self, days: int = 1) -> int:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )
        count = 0
        for (raw,) in rows:
            meta = _parse_meta(raw)
            reason = str(meta.get("reason") or meta.get("terminal_reason") or "")
            if "openclaw_timeout" in reason or "no_output" in reason:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Failure class distribution
    # ------------------------------------------------------------------

    def failure_class_distribution(self, days: int = 30) -> list[dict[str, Any]]:
        """Top terminal failure reasons only — filters to KNOWN_TERMINAL_REASONS."""
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )
        counts: dict[str, int] = {}
        for (raw,) in rows:
            meta = _parse_meta(raw)
            reason = str(
                meta.get("reason") or meta.get("terminal_reason") or ""
            ).strip()
            if not reason:
                continue
            # Only count recognised terminal failure reasons; skip repair
            # attempts, warnings, planning diagnostics, and non-terminal events.
            if _TERMINAL_REASONS and reason not in _TERMINAL_REASONS:
                continue
            counts[reason] = counts.get(reason, 0) + 1

        sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{"reason": r, "count": c} for r, c in sorted_items[:20]]

    # ------------------------------------------------------------------
    # Security events (Phase 10D shadow audit)
    # ------------------------------------------------------------------

    def security_events_count(self, days: int = 7) -> int:
        """Count security audit events logged with [SECURITY] prefix."""
        cutoff = self._cutoff(days)
        return (
            self.db.query(func.count(LogEntry.id))
            .filter(
                LogEntry.message.contains("[SECURITY]"),
                LogEntry.created_at >= cutoff,
            )
            .scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # Storage stats
    # ------------------------------------------------------------------

    def storage_stats(self, projects: list[Project]) -> dict[str, Any]:
        per_project = []
        total_snapshot_bytes = 0
        total_archive_bytes = 0

        for proj in projects:
            if not proj.workspace_path:
                continue
            root = Path(proj.workspace_path)
            snap_bytes = _dir_bytes(root / AUTO_SNAPSHOT_ROOT)
            arch_bytes = sum(
                _dir_bytes(root / arc)
                for arc in (
                    PROMOTED_WORKSPACE_ARCHIVE_ROOT,
                    REJECTED_CHANGE_ARCHIVE_ROOT,
                    RETAINED_WORKSPACE_ARCHIVE_ROOT,
                )
            )
            total_snapshot_bytes += snap_bytes
            total_archive_bytes += arch_bytes
            quota_violation = check_workspace_size(root)
            per_project.append(
                {
                    "project_id": proj.id,
                    "project_name": proj.name,
                    "snapshot_bytes": snap_bytes,
                    "archive_bytes": arch_bytes,
                    "total_bytes": snap_bytes + arch_bytes,
                    "workspace_quota_violation": (
                        None
                        if quota_violation is None
                        else {
                            "kind": quota_violation.kind,
                            "value": quota_violation.value,
                            "limit": quota_violation.limit,
                        }
                    ),
                }
            )

        return {
            "total_snapshot_bytes": total_snapshot_bytes,
            "total_archive_bytes": total_archive_bytes,
            "total_bytes": total_snapshot_bytes + total_archive_bytes,
            "workspace_quota_max_bytes": WORKSPACE_QUOTA_MAX_BYTES,
            "snapshot_retention_max_count": SNAPSHOT_MAX_COUNT,
            "snapshot_retention_max_age_days": SNAPSHOT_MAX_AGE_DAYS,
            "per_project": per_project,
        }
