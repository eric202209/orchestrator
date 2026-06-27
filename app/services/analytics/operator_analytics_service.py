"""OperatorAnalyticsService — Phase 15A-6.

Read-only operator interaction metrics.

DB sources:
  - intervention_requests : request counts, response latency, type distribution
  - sessions              : terminal counts, pause/resume/stop counts

Window anchors:
  - intervention_requests metrics → intervention_requests.created_at
  - session action counts (pause/resume/stop) → paused_at / resumed_at / stopped_at
  - sessions_with_intervention → intervention_requests.created_at (distinct session_ids)
  - terminal_sessions → sessions.created_at
  - sessions_without_intervention = max(0, terminal_sessions - sessions_with_intervention)
    (different anchors — see docs/roadmap/phase15a-6-operator-analytics-service.md §Limitations)

phase_intervention_distribution is always {} — InterventionRequest has no phase column
and the spec says not to infer.

Does not write to any table. Does not emit events. No runtime behavior changes.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import InterventionRequest, Session as SessionModel

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

_TERMINAL_STATUSES = ("completed", "stopped")


class OperatorAnalyticsService:
    """Computes operator interaction metrics.

    Instantiate with a SQLAlchemy session; call compute() to get the full
    metrics response dict. All queries are SELECT-only.
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
        requests = self._intervention_requests(since)
        responses = self._intervention_responses(since)
        response_rate = round(responses / requests, 4) if requests > 0 else None

        latencies = self._response_latencies(since)
        mean_resp = round(sum(latencies) / len(latencies), 4) if latencies else None
        median_resp = round(statistics.median(latencies), 4) if latencies else None

        terminal = self._terminal_sessions(since)
        with_ivt = self._sessions_with_intervention(since)
        without_ivt = max(0, terminal - with_ivt)
        autonomy_rate = round(without_ivt / terminal, 4) if terminal > 0 else None

        return {
            "intervention_requests": requests,
            "intervention_responses": responses,
            "intervention_response_rate": response_rate,
            "mean_response_seconds": mean_resp,
            "median_response_seconds": median_resp,
            "sessions_with_intervention": with_ivt,
            "sessions_without_intervention": without_ivt,
            "autonomy_rate": autonomy_rate,
            "pause_count": self._pause_count(since),
            "resume_count": self._resume_count(since),
            "stop_count": self._stop_count(since),
            "intervention_type_distribution": self._type_distribution(since),
            "phase_intervention_distribution": {},
        }

    def _intervention_requests(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(InterventionRequest.id))
        if since is not None:
            q = q.filter(InterventionRequest.created_at >= since)
        return q.scalar() or 0

    def _intervention_responses(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(InterventionRequest.id)).filter(
            InterventionRequest.replied_at.isnot(None)
        )
        if since is not None:
            q = q.filter(InterventionRequest.created_at >= since)
        return q.scalar() or 0

    def _response_latencies(self, since: Optional[datetime]) -> List[float]:
        q = self._db.query(
            InterventionRequest.created_at, InterventionRequest.replied_at
        ).filter(InterventionRequest.replied_at.isnot(None))
        if since is not None:
            q = q.filter(InterventionRequest.created_at >= since)

        latencies: List[float] = []
        for row in q.all():
            try:
                d = (row.replied_at - row.created_at).total_seconds()
                if d >= 0:
                    latencies.append(d)
            except Exception:
                continue
        return latencies

    def _sessions_with_intervention(self, since: Optional[datetime]) -> int:
        """Distinct session count from intervention_requests in the window."""
        q = self._db.query(
            sa_func.count(sa_func.distinct(InterventionRequest.session_id))
        )
        if since is not None:
            q = q.filter(InterventionRequest.created_at >= since)
        return q.scalar() or 0

    def _terminal_sessions(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(SessionModel.id)).filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.status.in_(_TERMINAL_STATUSES),
        )
        if since is not None:
            q = q.filter(SessionModel.created_at >= since)
        return q.scalar() or 0

    def _pause_count(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(SessionModel.id)).filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.paused_at.isnot(None),
        )
        if since is not None:
            q = q.filter(SessionModel.paused_at >= since)
        return q.scalar() or 0

    def _resume_count(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(SessionModel.id)).filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.resumed_at.isnot(None),
        )
        if since is not None:
            q = q.filter(SessionModel.resumed_at >= since)
        return q.scalar() or 0

    def _stop_count(self, since: Optional[datetime]) -> int:
        q = self._db.query(sa_func.count(SessionModel.id)).filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.stopped_at.isnot(None),
        )
        if since is not None:
            q = q.filter(SessionModel.stopped_at >= since)
        return q.scalar() or 0

    def _type_distribution(self, since: Optional[datetime]) -> Dict[str, int]:
        q = self._db.query(
            InterventionRequest.intervention_type,
            sa_func.count(InterventionRequest.id).label("cnt"),
        ).group_by(InterventionRequest.intervention_type)
        if since is not None:
            q = q.filter(InterventionRequest.created_at >= since)
        return {(row.intervention_type or "unknown"): row.cnt for row in q.all()}
