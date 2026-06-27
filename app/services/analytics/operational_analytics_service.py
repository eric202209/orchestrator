"""OperationalAnalyticsService — Phase 15A-2.

Read-only metrics over sessions, tasks, and task_executions.
Does not read event journals, knowledge tables, or intervention tables.
Does not write to any table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel, Task, TaskExecution, TaskStatus

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

# Terminal task execution statuses used in first-attempt rate denominator.
_TERMINAL_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)


class OperationalAnalyticsService:
    """Computes high-level operational metrics from existing DB state.

    Instantiate with a SQLAlchemy session; call compute() to get the
    full metrics response dict. All queries are SELECT-only.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def compute(self) -> Dict[str, Any]:
        now = datetime.now(UTC)
        windows: Dict[str, Any] = {}
        for label, days in _WINDOW_DAYS.items():
            since = (now - timedelta(days=days)) if days is not None else None
            windows[label] = self._compute_window(since)
        return {
            "windows": windows,
            "generated_at": now.isoformat(),
            "metrics_version": 1,
        }

    # ── private helpers ────────────────────────────────────────────────────────

    def _compute_window(self, since: Optional[datetime]) -> Dict[str, Any]:
        s = self._session_metrics(since)
        t = self._task_metrics(since)
        return {
            "session_success_rate": s["success_rate"],
            "first_attempt_success_rate": t["first_attempt_success_rate"],
            "failure_category_distribution": self._failure_category_distribution(since),
            "sessions_started": s["started"],
            "sessions_completed": s["completed"],
            "sessions_failed": s["failed"],
        }

    def _session_metrics(self, since: Optional[datetime]) -> Dict[str, Any]:
        q = self._db.query(SessionModel).filter(SessionModel.deleted_at.is_(None))
        if since is not None:
            q = q.filter(SessionModel.created_at >= since)
        sessions = q.all()

        started = sum(1 for s in sessions if s.started_at is not None)
        completed = sum(1 for s in sessions if s.status == "completed")
        # "stopped" is the non-successful terminal state in the sessions model.
        failed = sum(1 for s in sessions if s.status == "stopped")

        terminal = completed + failed
        success_rate: Optional[float] = (
            round(completed / terminal, 4) if terminal > 0 else None
        )
        return {
            "started": started,
            "completed": completed,
            "failed": failed,
            "success_rate": success_rate,
        }

    def _task_metrics(self, since: Optional[datetime]) -> Dict[str, Any]:
        q = self._db.query(TaskExecution).filter(
            TaskExecution.attempt_number == 1,
            TaskExecution.status.in_(_TERMINAL_STATUSES),
        )
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)

        first_attempts = q.all()
        total = len(first_attempts)
        succeeded = sum(1 for ex in first_attempts if ex.status == TaskStatus.DONE)
        rate: Optional[float] = round(succeeded / total, 4) if total > 0 else None
        return {"first_attempt_success_rate": rate}

    def _failure_category_distribution(
        self, since: Optional[datetime]
    ) -> Dict[str, int]:
        q = (
            self._db.query(
                TaskExecution.failure_category,
                sa_func.count(TaskExecution.id).label("cnt"),
            )
            .filter(TaskExecution.failure_category.isnot(None))
            .group_by(TaskExecution.failure_category)
            .order_by(sa_func.count(TaskExecution.id).desc())
        )
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)
        return {row.failure_category: row.cnt for row in q.all()}
