"""Write KnowledgeUsageLog rows from a KnowledgeContext."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import KnowledgeUsageLog
from app.schemas.knowledge import KnowledgeContext


def log_usage(
    context: KnowledgeContext,
    session_id: int,
    task_id: Optional[int],
    used_in_prompt: bool,
    db: Session,
) -> None:
    """Write one KnowledgeUsageLog row per item in context.retrieved_items."""
    for rank, item_ref in enumerate(context.retrieved_items):
        log = KnowledgeUsageLog(
            session_id=session_id,
            task_id=task_id,
            knowledge_item_id=item_ref.id,
            trigger_phase=context.trigger_phase,
            retrieval_reason=context.retrieval_reason,
            retrieval_query=context.query,
            confidence=item_ref.confidence,
            rank=rank,
            used_in_prompt=used_in_prompt,
            was_effective=None,
        )
        db.add(log)
    db.commit()
