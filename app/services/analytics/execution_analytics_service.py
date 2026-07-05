"""ExecutionAnalyticsService — Phase 15A-5.

Read-only execution timing, queue latency, token usage, backend usage, and
phase-duration metrics.

DB sources  (windowed by created_at):
  - task_executions: duration, queue latency, tokens, backend distribution

Event-journal source (windowed by phase_finished timestamp):
  - PHASE_STARTED / PHASE_FINISHED event pairs → phase_duration_seconds

Does not write to any table. Does not emit events. No runtime behavior changes.

Percentile note:
  SQLite does not have native PERCENTILE_CONT. p50/p95 are computed in Python
  over all qualifying queue_latency_seconds values in the window.

Phase duration note:
  PHASE_STARTED events are matched to the next PHASE_FINISHED event for the
  same phase name (FIFO within each session/task). Events with a missing or
  unparseable timestamp are skipped. Unmatched starts are discarded.
  See docs/roadmap/phase15a-5-execution-analytics-service.md for details.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import TaskExecution
from app.services.orchestration.events.event_types import EventType

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

_PHASE_STARTED = EventType.PHASE_STARTED
_PHASE_FINISHED = EventType.PHASE_FINISHED


# ── helpers ────────────────────────────────────────────────────────────────────


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _percentile(values: List[float], p: float) -> Optional[float]:
    """Linear-interpolation percentile over a sorted list."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return round(s[0], 4)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return round(s[-1], 4)
    frac = idx - lo
    return round(s[lo] + frac * (s[hi] - s[lo]), 4)


def tally_phase_events(
    events: List[Dict[str, Any]],
) -> List[Tuple[str, float, datetime]]:
    """Match PHASE_STARTED / PHASE_FINISHED pairs and return completed phases.

    Returns a list of (phase_name, duration_seconds, finished_at) tuples.
    Matching is FIFO per phase name. Negative durations are discarded.
    Events missing a phase name or a parseable timestamp are skipped.
    """
    pending: Dict[str, List[datetime]] = {}
    results: List[Tuple[str, float, datetime]] = []

    for event in events:
        try:
            et = event.get("event_type", "")
            phase = event.get("phase")
            if not phase:
                continue
            ts = _parse_ts(event.get("timestamp"))
            if ts is None:
                continue

            if et == _PHASE_STARTED:
                pending.setdefault(phase, []).append(ts)
            elif et == _PHASE_FINISHED:
                starts = pending.get(phase, [])
                if starts:
                    start_ts = starts.pop(0)
                    d = (ts - start_ts).total_seconds()
                    if d >= 0:
                        results.append((phase, d, ts))
        except Exception:
            continue

    return results


# ── service ────────────────────────────────────────────────────────────────────


class ExecutionAnalyticsService:
    """Computes execution timing, resource, and phase-duration metrics.

    Instantiate with a SQLAlchemy session; call compute() to get the full
    metrics response dict. All queries are SELECT-only.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def compute(self) -> Dict[str, Any]:
        now = datetime.now(UTC)
        phase_windows = self._collect_phase_duration_windows(now)
        windows: Dict[str, Any] = {}
        for label, days in _WINDOW_DAYS.items():
            since = (now - timedelta(days=days)) if days is not None else None
            windows[label] = self._compute_window(since, phase_windows[label])
        return {
            "windows": windows,
            "generated_at": now.isoformat(),
            "metrics_version": 1,
        }

    # ── private helpers ────────────────────────────────────────────────────────

    def _compute_window(
        self,
        since: Optional[datetime],
        phase_durations: Dict[str, Any],
    ) -> Dict[str, Any]:
        latencies = self._queue_latencies(since)
        return {
            "execution_count": self._execution_count(since),
            "mean_execution_duration_seconds": self._mean_execution_duration(since),
            "queue_latency_p50_seconds": _percentile(latencies, 50),
            "queue_latency_p95_seconds": _percentile(latencies, 95),
            "tokens_in_total": self._token_total(since, TaskExecution.tokens_in),
            "tokens_out_total": self._token_total(since, TaskExecution.tokens_out),
            "backend_distribution": self._backend_distribution(since),
            "phase_duration_seconds": phase_durations,
        }

    def _base(self, since: Optional[datetime]):
        q = self._db.query(TaskExecution)
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)
        return q

    def _execution_count(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(TaskExecution.id))
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)
        return q.scalar() or 0

    def _mean_execution_duration(self, since: Optional[datetime]) -> Optional[float]:
        q = self._db.query(TaskExecution.started_at, TaskExecution.completed_at).filter(
            TaskExecution.started_at.isnot(None),
            TaskExecution.completed_at.isnot(None),
        )
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)

        durations: List[float] = []
        for row in q.all():
            try:
                d = (row.completed_at - row.started_at).total_seconds()
                if d >= 0:
                    durations.append(d)
            except Exception:
                continue

        if not durations:
            return None
        return round(sum(durations) / len(durations), 4)

    def _queue_latencies(self, since: Optional[datetime]) -> List[float]:
        q = self._db.query(TaskExecution.queue_latency_seconds).filter(
            TaskExecution.queue_latency_seconds.isnot(None)
        )
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)
        return [row.queue_latency_seconds for row in q.all()]

    def _token_total(self, since: Optional[datetime], col) -> int:
        q = self._db.query(sa_func.sum(sa_func.coalesce(col, 0)))
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)
        result = q.scalar()
        return int(result) if result is not None else 0

    def _backend_distribution(self, since: Optional[datetime]) -> Dict[str, int]:
        q = self._db.query(
            TaskExecution.backend_id,
            sa_func.count(TaskExecution.id).label("cnt"),
        ).group_by(TaskExecution.backend_id)
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)

        result: Dict[str, int] = {}
        for row in q.all():
            key = (row.backend_id or "").strip() or "unknown"
            result[key] = result.get(key, 0) + row.cnt
        return result

    def _collect_phase_duration_windows(
        self, now: datetime
    ) -> Dict[str, Dict[str, Any]]:
        """Walk all non-deleted sessions/tasks, read events, collect phase durations.

        Returns per-window aggregates: {label: {phase: {count, mean_seconds}}}.
        Events are bucketed by the PHASE_FINISHED timestamp.
        """
        from app.services.orchestration.state.persistence import (
            read_orchestration_events,
        )
        from app.services.analytics.event_journal_targets import (
            load_event_journal_targets,
        )

        thresholds: Dict[str, Optional[datetime]] = {
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "all_time": None,
        }
        # per_window[label][phase] = list of duration floats
        per_window: Dict[str, Dict[str, List[float]]] = {
            label: {} for label in thresholds
        }

        for target in load_event_journal_targets(self._db):
            try:
                events = read_orchestration_events(
                    target.project_dir,
                    target.session_id,
                    target.task_id,
                )
            except Exception:
                continue

            for phase_name, duration, finished_at in tally_phase_events(events):
                for label, threshold in thresholds.items():
                    if threshold is not None and finished_at < threshold:
                        continue
                    bucket = per_window[label]
                    bucket.setdefault(phase_name, []).append(duration)

        # Collapse lists to {count, mean_seconds}
        def _aggregate(durations_map: Dict[str, List[float]]) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for phase, durations in durations_map.items():
                if not durations:
                    continue
                out[phase] = {
                    "count": len(durations),
                    "mean_seconds": round(sum(durations) / len(durations), 4),
                }
            return out

        return {label: _aggregate(per_window[label]) for label in thresholds}
