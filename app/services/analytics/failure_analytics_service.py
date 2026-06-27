"""FailureAnalyticsService — Phase 15A-3.

Read-only failure, recovery, and churn metrics over existing data.
Sources:
  - sessions.repair_churn_stopped (DB)
  - task_executions.failure_category / status (DB)
  - orchestration event journal (EXECUTION_RECOVERY_* events)

Does not write to any table. Does not emit events. No runtime behavior changes.

Window note for event-journal fields:
  Events carry a "timestamp" field (ISO 8601). This service filters them per
  window using that field. Events with a missing or unparseable timestamp are
  counted only in the all_time window, not in 7d or 30d.
  See docs/roadmap/phase15a-3-failure-analytics-service.md for details.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import Session as SessionModel, Task, TaskExecution
from app.services.orchestration.events.event_types import EventType

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

_RECOVERY_ATTEMPT = EventType.EXECUTION_RECOVERY_ATTEMPTED
_RECOVERY_SUCCESS = EventType.EXECUTION_RECOVERY_SUCCEEDED
_RECOVERY_FAILED = EventType.EXECUTION_RECOVERY_FAILED


def _parse_event_timestamp(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _empty_event_bucket() -> Dict[str, int]:
    return {
        "recovery_attempts": 0,
        "recovery_successes": 0,
        "recovery_failures": 0,
        "budget_exhaustion_count": 0,
    }


class FailureAnalyticsService:
    """Computes failure, recovery, and churn metrics from existing DB state
    and orchestration event journals.

    Instantiate with a SQLAlchemy session; call compute() to get the full
    metrics response dict. All queries are SELECT-only.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def compute(self) -> Dict[str, Any]:
        now = datetime.now(UTC)
        event_windows = self._collect_event_journal_windows(now)
        windows: Dict[str, Any] = {}
        for label, days in _WINDOW_DAYS.items():
            since = (now - timedelta(days=days)) if days is not None else None
            windows[label] = self._compute_window(since, event_windows[label])
        return {
            "windows": windows,
            "generated_at": now.isoformat(),
            "metrics_version": 1,
        }

    # ── private helpers ────────────────────────────────────────────────────────

    def _compute_window(
        self,
        since: Optional[datetime],
        event_totals: Dict[str, int],
    ) -> Dict[str, Any]:
        attempts = event_totals["recovery_attempts"]
        successes = event_totals["recovery_successes"]
        success_rate: Optional[float] = (
            round(successes / attempts, 4) if attempts > 0 else None
        )
        return {
            "recovery_attempts": attempts,
            "recovery_successes": successes,
            "recovery_failures": event_totals["recovery_failures"],
            "recovery_success_rate": success_rate,
            "budget_exhaustion_count": event_totals["budget_exhaustion_count"],
            "churn_guard_activations": self._churn_guard_activations(since),
            "failure_category_distribution": self._failure_category_distribution(since),
            # Per-category recovery outcomes require correlating ATTEMPTED events
            # with their outcome events by a shared execution ID, which the current
            # event schema does not expose. Returning {} until that field is added.
            "failure_category_recovery": {},
        }

    def _churn_guard_activations(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(SessionModel.id)).filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.repair_churn_stopped.is_(True),
        )
        if since is not None:
            q = q.filter(SessionModel.created_at >= since)
        result = q.scalar()
        return int(result) if result is not None else 0

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

    def _collect_event_journal_windows(
        self, now: datetime
    ) -> Dict[str, Dict[str, int]]:
        """Walk all non-deleted sessions/tasks and tally recovery events per window.

        Events are bucketed by their "timestamp" field. Events with a missing
        or unparseable timestamp are counted only in all_time.
        """
        from app.services.orchestration.state.persistence import (
            read_orchestration_events,
        )
        from app.services.session.session_runtime_service import (
            resolve_event_log_project_dir,
        )

        thresholds: Dict[str, Optional[datetime]] = {
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "all_time": None,
        }
        per_window: Dict[str, Dict[str, int]] = {
            label: _empty_event_bucket() for label in thresholds
        }

        try:
            sessions = (
                self._db.query(SessionModel)
                .filter(SessionModel.deleted_at.is_(None))
                .all()
            )
        except Exception:
            return per_window

        for sess in sessions:
            try:
                tasks = (
                    self._db.query(Task)
                    .filter(Task.project_id == sess.project_id)
                    .all()
                )
            except Exception:
                continue

            for task in tasks:
                try:
                    project_dir = resolve_event_log_project_dir(self._db, sess, task.id)
                    if not project_dir:
                        continue
                    events = read_orchestration_events(project_dir, sess.id, task.id)
                except Exception:
                    continue

                for event in events:
                    try:
                        et = event.get("event_type", "")
                        if et not in (
                            _RECOVERY_ATTEMPT,
                            _RECOVERY_SUCCESS,
                            _RECOVERY_FAILED,
                        ):
                            continue

                        event_ts = _parse_event_timestamp(event.get("timestamp"))

                        for label, threshold in thresholds.items():
                            if threshold is not None:
                                if event_ts is None or event_ts < threshold:
                                    continue

                            bucket = per_window[label]
                            if et == _RECOVERY_ATTEMPT:
                                bucket["recovery_attempts"] += 1
                            elif et == _RECOVERY_SUCCESS:
                                bucket["recovery_successes"] += 1
                            elif et == _RECOVERY_FAILED:
                                bucket["recovery_failures"] += 1
                                details = event.get("details") or {}
                                if details.get("budget_exhausted"):
                                    bucket["budget_exhaustion_count"] += 1
                    except Exception:
                        continue

        return per_window
