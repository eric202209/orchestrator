"""KnowledgeAnalyticsService — Phase 15A-4.

Read-only knowledge utilization, prompt-injection, and effectiveness metrics.
Sources:
  - knowledge_usage_logs (retrieval, prompt use, effectiveness, confidence)
  - knowledge_items (title lookup)

Does not read the event journal. Does not write to any table.
Does not modify knowledge retrieval behavior. No runtime changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import KnowledgeItem, KnowledgeUsageLog

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

# Maximum items returned in top_items list.
_TOP_ITEMS_LIMIT = 10

# Minimum retrieval count for an item to appear in low_effectiveness_items.
_MIN_RETRIEVAL_THRESHOLD = 3

# Effectiveness rate below this threshold is considered "low".
_LOW_EFFECTIVENESS_THRESHOLD = 0.25


class KnowledgeAnalyticsService:
    """Computes knowledge utilization and effectiveness metrics.

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
        retrieval_count = self._retrieval_count(since)
        used_in_prompt_count = self._used_in_prompt_count(since)
        effective_count = self._effective_count(since)

        hit_rate: Optional[float] = (
            round(used_in_prompt_count / retrieval_count, 4)
            if retrieval_count > 0
            else None
        )
        effectiveness_rate: Optional[float] = (
            round(effective_count / used_in_prompt_count, 4)
            if used_in_prompt_count > 0
            else None
        )

        return {
            "retrieval_count": retrieval_count,
            "used_in_prompt_count": used_in_prompt_count,
            "knowledge_hit_rate": hit_rate,
            "effectiveness_rate": effectiveness_rate,
            "phase_utilization": self._phase_utilization(since),
            "top_items": self._top_items(since),
            "low_effectiveness_items": self._low_effectiveness_items(since),
        }

    def _filter(self, q, since: Optional[datetime]):
        if since is not None:
            q = q.filter(KnowledgeUsageLog.created_at >= since)
        return q

    def _retrieval_count(self, since: Optional[datetime]) -> int:
        q = self._filter(self._db.query(sa_func.count(KnowledgeUsageLog.id)), since)
        return q.scalar() or 0

    def _used_in_prompt_count(self, since: Optional[datetime]) -> int:
        q = self._filter(
            self._db.query(sa_func.count(KnowledgeUsageLog.id)).filter(
                KnowledgeUsageLog.used_in_prompt.is_(True)
            ),
            since,
        )
        return q.scalar() or 0

    def _effective_count(self, since: Optional[datetime]) -> int:
        q = self._filter(
            self._db.query(sa_func.count(KnowledgeUsageLog.id)).filter(
                KnowledgeUsageLog.used_in_prompt.is_(True),
                KnowledgeUsageLog.was_effective.is_(True),
            ),
            since,
        )
        return q.scalar() or 0

    def _phase_utilization(self, since: Optional[datetime]) -> Dict[str, int]:
        q = self._filter(
            self._db.query(
                KnowledgeUsageLog.trigger_phase,
                sa_func.count(KnowledgeUsageLog.id).label("cnt"),
            ).group_by(KnowledgeUsageLog.trigger_phase),
            since,
        )
        return {row.trigger_phase: row.cnt for row in q.all()}

    def _item_aggregates(self, since: Optional[datetime]):
        """Return one row per knowledge_item_id with aggregated metrics."""
        # used_count: rows where used_in_prompt IS TRUE
        _used_sum = sa_func.sum(
            case((KnowledgeUsageLog.used_in_prompt.is_(True), 1), else_=0)
        )
        # effective_count: rows where used_in_prompt IS TRUE AND was_effective IS TRUE
        _effective_sum = sa_func.sum(
            case(
                (
                    and_(
                        KnowledgeUsageLog.used_in_prompt.is_(True),
                        KnowledgeUsageLog.was_effective.is_(True),
                    ),
                    1,
                ),
                else_=0,
            )
        )
        # was_effective_data_count: rows where was_effective IS NOT NULL
        # (used as denominator in low_effectiveness_items to avoid false positives)
        _eff_data_sum = sa_func.sum(
            case((KnowledgeUsageLog.was_effective.isnot(None), 1), else_=0)
        )

        q = self._filter(
            self._db.query(
                KnowledgeUsageLog.knowledge_item_id,
                KnowledgeItem.title,
                sa_func.count(KnowledgeUsageLog.id).label("retrieval_count"),
                _used_sum.label("used_count"),
                _effective_sum.label("effective_count"),
                _eff_data_sum.label("eff_data_count"),
                sa_func.avg(KnowledgeUsageLog.confidence).label("avg_confidence"),
            )
            .outerjoin(
                KnowledgeItem,
                KnowledgeUsageLog.knowledge_item_id == KnowledgeItem.id,
            )
            .group_by(KnowledgeUsageLog.knowledge_item_id, KnowledgeItem.title),
            since,
        )
        return q.all()

    def _top_items(self, since: Optional[datetime]) -> List[Dict[str, Any]]:
        # Filter must be applied before .limit() — _filter() helper cannot be
        # used here because it appends after the query is already built.
        q = self._db.query(
            KnowledgeUsageLog.knowledge_item_id,
            KnowledgeItem.title,
            sa_func.count(KnowledgeUsageLog.id).label("retrieval_count"),
            sa_func.sum(
                case((KnowledgeUsageLog.used_in_prompt.is_(True), 1), else_=0)
            ).label("used_count"),
            sa_func.sum(
                case(
                    (
                        and_(
                            KnowledgeUsageLog.used_in_prompt.is_(True),
                            KnowledgeUsageLog.was_effective.is_(True),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("effective_count"),
            sa_func.avg(KnowledgeUsageLog.confidence).label("avg_confidence"),
        ).outerjoin(
            KnowledgeItem,
            KnowledgeUsageLog.knowledge_item_id == KnowledgeItem.id,
        )
        if since is not None:
            q = q.filter(KnowledgeUsageLog.created_at >= since)
        rows = (
            q.group_by(KnowledgeUsageLog.knowledge_item_id, KnowledgeItem.title)
            .order_by(sa_func.count(KnowledgeUsageLog.id).desc())
            .limit(_TOP_ITEMS_LIMIT)
            .all()
        )

        result = []
        for row in rows:
            rc = row.retrieval_count or 0
            uc = int(row.used_count or 0)
            ec = int(row.effective_count or 0)
            avg_conf = (
                round(float(row.avg_confidence), 4)
                if row.avg_confidence is not None
                else None
            )
            hit_rate = round(uc / rc, 4) if rc > 0 else None
            eff_rate = round(ec / uc, 4) if uc > 0 else None
            result.append(
                {
                    "knowledge_item_id": row.knowledge_item_id,
                    "title": row.title,
                    "retrieval_count": rc,
                    "used_in_prompt_count": uc,
                    "hit_rate": hit_rate,
                    "effectiveness_rate": eff_rate,
                    "avg_confidence": avg_conf,
                }
            )
        return result

    def _low_effectiveness_items(
        self, since: Optional[datetime]
    ) -> List[Dict[str, Any]]:
        """Items retrieved >= MIN_RETRIEVAL_THRESHOLD times with a low
        effectiveness rate, computed only over rows with non-null was_effective.

        Items with zero rows of was_effective signal are excluded to avoid
        false positives. If no items qualify, returns [].
        See docs/roadmap/phase15a-4-knowledge-analytics-service.md.
        """
        rows = self._item_aggregates(since)
        result = []
        for row in rows:
            rc = row.retrieval_count or 0
            if rc < _MIN_RETRIEVAL_THRESHOLD:
                continue
            eff_data = int(row.eff_data_count or 0)
            if eff_data == 0:
                # No was_effective signal — exclude to avoid false positives.
                continue
            ec = int(row.effective_count or 0)
            eff_rate = round(ec / eff_data, 4)
            if eff_rate >= _LOW_EFFECTIVENESS_THRESHOLD:
                continue
            uc = int(row.used_count or 0)
            avg_conf = (
                round(float(row.avg_confidence), 4)
                if row.avg_confidence is not None
                else None
            )
            result.append(
                {
                    "knowledge_item_id": row.knowledge_item_id,
                    "title": row.title,
                    "retrieval_count": rc,
                    "used_in_prompt_count": uc,
                    "effectiveness_rate": eff_rate,
                    "avg_confidence": avg_conf,
                }
            )
        # Worst first, then most-retrieved first for ties.
        result.sort(key=lambda x: (x["effectiveness_rate"], -x["retrieval_count"]))
        return result
