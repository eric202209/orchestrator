"""Read-only analytics service for KnowledgeUsageLog drilldown."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import KnowledgeItem, KnowledgeUsageLog
from app.services.knowledge.knowledge_lifecycle_service import (
    KnowledgeLifecycleService,
    KnowledgeNotFoundError,
)

_lifecycle = KnowledgeLifecycleService()


class KnowledgeUsageDrilldownService:
    def get_usage_list(
        self,
        db: Session,
        knowledge_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        trigger_phase: Optional[str] = None,
        used_in_prompt: Optional[bool] = None,
        was_effective: Optional[bool] = None,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
    ) -> tuple[list[KnowledgeUsageLog], int]:
        _lifecycle.get(db, knowledge_id)  # raises KnowledgeNotFoundError if missing

        q = db.query(KnowledgeUsageLog).filter(
            KnowledgeUsageLog.knowledge_item_id == knowledge_id
        )

        if trigger_phase is not None:
            q = q.filter(KnowledgeUsageLog.trigger_phase == trigger_phase)
        if used_in_prompt is not None:
            q = q.filter(KnowledgeUsageLog.used_in_prompt == used_in_prompt)
        if was_effective is not None:
            q = q.filter(KnowledgeUsageLog.was_effective == was_effective)
        if session_id is not None:
            q = q.filter(KnowledgeUsageLog.session_id == session_id)
        if task_id is not None:
            q = q.filter(KnowledgeUsageLog.task_id == task_id)
        if created_after is not None:
            q = q.filter(KnowledgeUsageLog.created_at >= created_after)
        if created_before is not None:
            q = q.filter(KnowledgeUsageLog.created_at <= created_before)

        q = q.order_by(KnowledgeUsageLog.created_at.desc())
        total = q.count()
        items = q.offset((page - 1) * page_size).limit(page_size).all()
        return items, total

    def get_usage_summary(
        self,
        db: Session,
        knowledge_id: str,
    ) -> dict[str, Any]:
        _lifecycle.get(db, knowledge_id)  # raises KnowledgeNotFoundError if missing

        rows = (
            db.query(KnowledgeUsageLog)
            .filter(KnowledgeUsageLog.knowledge_item_id == knowledge_id)
            .all()
        )

        retrieval_count = len(rows)
        used_in_prompt_count = sum(1 for r in rows if r.used_in_prompt)
        effective_count = sum(1 for r in rows if r.was_effective)

        knowledge_hit_rate: Optional[float] = None
        if retrieval_count > 0:
            knowledge_hit_rate = used_in_prompt_count / retrieval_count

        effectiveness_rate: Optional[float] = None
        if used_in_prompt_count > 0:
            effectiveness_rate = effective_count / used_in_prompt_count

        confidences = [r.confidence for r in rows if r.confidence is not None]
        avg_confidence: Optional[float] = (
            sum(confidences) / len(confidences) if confidences else None
        )

        phase_distribution: dict[str, int] = {}
        for r in rows:
            phase_distribution[r.trigger_phase] = (
                phase_distribution.get(r.trigger_phase, 0) + 1
            )

        # Recent unique session/task IDs (up to 10, most recent first)
        seen_sessions: list[int] = []
        seen_tasks: list[int] = []
        for r in sorted(rows, key=lambda x: x.created_at or datetime.min, reverse=True):
            if r.session_id not in seen_sessions:
                seen_sessions.append(r.session_id)
            if r.task_id is not None and r.task_id not in seen_tasks:
                seen_tasks.append(r.task_id)
            if len(seen_sessions) >= 10 and len(seen_tasks) >= 10:
                break

        return {
            "knowledge_item_id": knowledge_id,
            "retrieval_count": retrieval_count,
            "used_in_prompt_count": used_in_prompt_count,
            "effective_count": effective_count,
            "knowledge_hit_rate": knowledge_hit_rate,
            "effectiveness_rate": effectiveness_rate,
            "avg_confidence": avg_confidence,
            "phase_distribution": phase_distribution,
            "recent_sessions": seen_sessions[:10],
            "recent_tasks": seen_tasks[:10],
        }
