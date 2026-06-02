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
    Session,
    Task,
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
    # Task-1 product health
    # ------------------------------------------------------------------

    def task1_product_health(self, days: int = 7) -> dict[str, Any]:
        cutoff = self._cutoff(days)
        event_names = {
            "task1_bootstrap_contract_passed",
            "task1_bootstrap_contract_failed",
            "task1_execution_succeeded",
            "task1_execution_failed",
            "project_blocked_after_task1",
        }
        event_counters = {name: 0 for name in sorted(event_names)}
        event_rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )
        for (raw,) in event_rows:
            event_type = str(_parse_meta(raw).get("event_type") or "")
            if event_type in event_counters:
                event_counters[event_type] += 1

        first_tasks = (
            self.db.query(Task)
            .filter(
                Task.plan_position == 1,
                Task.created_at >= cutoff,
            )
            .all()
        )
        first_task_count = len(first_tasks)
        first_task_done = sum(
            1 for task in first_tasks if task.status == TaskStatus.DONE
        )
        first_task_failed = sum(
            1 for task in first_tasks if task.status == TaskStatus.FAILED
        )

        blocked_after_task1 = 0
        clean_project_completion = 0
        for task in first_tasks:
            later_tasks = (
                self.db.query(Task)
                .filter(
                    Task.project_id == task.project_id,
                    Task.plan_position.isnot(None),
                    Task.plan_position > 1,
                )
                .all()
            )
            if task.status == TaskStatus.FAILED and any(
                item.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}
                for item in later_tasks
            ):
                blocked_after_task1 += 1

            project_tasks = (
                self.db.query(Task)
                .filter(
                    Task.project_id == task.project_id,
                    Task.plan_position.isnot(None),
                )
                .all()
            )
            if project_tasks and all(
                item.status == TaskStatus.DONE for item in project_tasks
            ):
                clean_project_completion += 1

        contract_passed = event_counters["task1_bootstrap_contract_passed"]
        contract_failed = event_counters["task1_bootstrap_contract_failed"]
        contract_total = contract_passed + contract_failed
        execution_succeeded = event_counters["task1_execution_succeeded"]
        execution_failed = event_counters["task1_execution_failed"]
        execution_total = execution_succeeded + execution_failed

        return {
            "event_counters": event_counters,
            "first_task_count": first_task_count,
            "first_task_done": first_task_done,
            "first_task_failed": first_task_failed,
            "ordered_project_first_task_success_rate": (
                round(first_task_done / first_task_count, 3)
                if first_task_count
                else None
            ),
            "task1_bootstrap_contract_failure_rate": (
                round(contract_failed / contract_total, 3) if contract_total else None
            ),
            "task1_execution_failure_rate": (
                round(execution_failed / execution_total, 3)
                if execution_total
                else (
                    round(first_task_failed / first_task_count, 3)
                    if first_task_count
                    else None
                )
            ),
            "blocked_after_task1_rate": (
                round(blocked_after_task1 / first_task_count, 3)
                if first_task_count
                else None
            ),
            "clean_project_completion_rate": (
                round(clean_project_completion / first_task_count, 3)
                if first_task_count
                else None
            ),
            "task1_debug_repair_recovery_rate": None,
            "blocked_after_task1_count": blocked_after_task1,
            "clean_project_completion_count": clean_project_completion,
        }

    # ------------------------------------------------------------------
    # Phase 12R: Ordered-project product health
    # ------------------------------------------------------------------

    def ordered_project_health(self, days: int = 7) -> dict[str, Any]:
        """Phase 12R product-level health metrics for ordered projects.

        All task1_* aliases are preserved as output keys so existing
        consumers are not broken.  project_blocked_after_task1 is kept
        as an alias for project_blocked_after_bootstrap.
        """
        cutoff = self._cutoff(days)

        bootstrap_tasks = (
            self.db.query(Task)
            .filter(
                Task.plan_position == 1,
                Task.created_at >= cutoff,
            )
            .all()
        )

        bootstrap_total = len(bootstrap_tasks)
        bootstrap_success = sum(
            1 for t in bootstrap_tasks if t.status == TaskStatus.DONE
        )
        bootstrap_failure = sum(
            1 for t in bootstrap_tasks if t.status == TaskStatus.FAILED
        )

        project_blocked_count = 0
        ordered_completion_count = 0
        task2_success_count = 0
        task2_total = 0
        latency_samples: list[float] = []

        for bt in bootstrap_tasks:
            project_tasks = (
                self.db.query(Task)
                .filter(
                    Task.project_id == bt.project_id,
                    Task.plan_position.isnot(None),
                )
                .all()
            )

            if project_tasks and all(
                t.status == TaskStatus.DONE for t in project_tasks
            ):
                ordered_completion_count += 1

            later_tasks = [t for t in project_tasks if (t.plan_position or 0) > 1]
            if bt.status == TaskStatus.FAILED and any(
                t.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}
                for t in later_tasks
            ):
                project_blocked_count += 1

            task2 = next((t for t in project_tasks if t.plan_position == 2), None)
            if bt.status == TaskStatus.DONE and task2 is not None:
                task2_total += 1
                if task2.status == TaskStatus.DONE:
                    task2_success_count += 1
                if bt.completed_at and task2.started_at:
                    try:
                        delta = (task2.started_at - bt.completed_at).total_seconds()
                        if delta >= 0:
                            latency_samples.append(delta)
                    except Exception:
                        pass

        repair_applied = 0
        repair_rejected = 0
        vsm_count = 0
        vsm_by_type: dict[str, int] = {}

        log_rows = (
            self.db.query(LogEntry.log_metadata)
            .filter(
                LogEntry.log_metadata.isnot(None),
                LogEntry.created_at >= cutoff,
            )
            .all()
        )
        for (raw,) in log_rows:
            meta = _parse_meta(raw)
            event_type = str(meta.get("event_type") or "")
            if event_type == "repair_applied":
                repair_applied += 1
            elif event_type == "repair_rejected":
                repair_rejected += 1
            elif event_type == "verification_surface_mismatch":
                vsm_count += 1
                mtype = str(meta.get("mismatch_type") or "UNKNOWN")
                vsm_by_type[mtype] = vsm_by_type.get(mtype, 0) + 1

        repair_total = repair_applied + repair_rejected

        return {
            # --- Bootstrap task outcome ---
            "bootstrap_task_total": bootstrap_total,
            "bootstrap_task_success_count": bootstrap_success,
            "bootstrap_task_failure_count": bootstrap_failure,
            "bootstrap_task_success_rate": (
                round(bootstrap_success / bootstrap_total, 3)
                if bootstrap_total
                else None
            ),
            "bootstrap_task_failure_rate": (
                round(bootstrap_failure / bootstrap_total, 3)
                if bootstrap_total
                else None
            ),
            # --- Ordered project completion ---
            "ordered_project_completion_count": ordered_completion_count,
            "ordered_project_completion_rate": (
                round(ordered_completion_count / bootstrap_total, 3)
                if bootstrap_total
                else None
            ),
            # --- Blocked after bootstrap ---
            "project_blocked_after_bootstrap": project_blocked_count,
            "blocked_after_bootstrap_rate": (
                round(project_blocked_count / bootstrap_total, 3)
                if bootstrap_total
                else None
            ),
            # --- Task 2 continuation ---
            "task2_continuation_total": task2_total,
            "task2_continuation_success_count": task2_success_count,
            "task2_continuation_success_rate": (
                round(task2_success_count / task2_total, 3) if task2_total else None
            ),
            # --- Bootstrap → Task 2 latency ---
            "bootstrap_to_task2_continuation_latency": {
                "mean_seconds": _mean(latency_samples),
                "p95_seconds": _p95(latency_samples),
                "sample_count": len(latency_samples),
            },
            # --- Verification surface mismatches (0 until runtime wired) ---
            "verification_surface_mismatch_count": vsm_count,
            "verification_surface_mismatch_by_type": vsm_by_type,
            # --- Repair contract rejection ---
            "repair_contract_applied": repair_applied,
            "repair_contract_rejected": repair_rejected,
            "repair_contract_rejection_rate": (
                round(repair_rejected / repair_total, 3) if repair_total else None
            ),
            # Compatibility aliases — task1_* keys preserved for existing dashboard
            # consumers and eval scripts. Retire when: (1) all consumers confirmed
            # to use bootstrap_* keys; (2) no report schema references task1_* fields;
            # (3) trend-continuity tests pass after renaming.
            "project_blocked_after_task1": project_blocked_count,
            "blocked_after_task1_rate": (
                round(project_blocked_count / bootstrap_total, 3)
                if bootstrap_total
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Model lane distribution
    # ------------------------------------------------------------------

    def model_lane_distribution(self, days: int = 7) -> dict[str, Any]:
        cutoff = self._cutoff(days)
        rows = (
            self.db.query(Session.model_lane_label, Session.model_lane_metadata)
            .filter(Session.created_at >= cutoff, Session.deleted_at.is_(None))
            .all()
        )

        labels: dict[str, int] = {}
        capability_tiers: dict[str, int] = {}
        unknown_count = 0

        for label, metadata in rows:
            normalized_label = str(label or "").strip() or "unknown"
            labels[normalized_label] = labels.get(normalized_label, 0) + 1
            if normalized_label == "unknown":
                unknown_count += 1

            meta = metadata if isinstance(metadata, dict) else _parse_meta(metadata)
            tier = str(meta.get("capability_tier") or "").strip() or "unknown"
            capability_tiers[tier] = capability_tiers.get(tier, 0) + 1

        return {
            "total_sessions": len(rows),
            "labels": labels,
            "capability_tiers": capability_tiers,
            "unknown_count": unknown_count,
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
