"""Knowledge retrieval and ingestion service backed by Qdrant + SQLite."""

from __future__ import annotations

from typing import Optional

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeItem
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)

_EMBEDDING_DIM = 1536

# knowledge_type rank for budget sorting (lower = higher priority)
_TYPE_RANK: dict[str, int] = {
    KnowledgeType.failure_memory: 0,
    KnowledgeType.tool_contract: 1,
    KnowledgeType.format_guide: 2,
    KnowledgeType.debug_case: 3,
    KnowledgeType.task_example: 4,
    KnowledgeType.best_practice: 5,
    KnowledgeType.system_doc: 6,
}


class KnowledgeService:
    def __init__(
        self,
        qdrant_url: str = settings.QDRANT_URL,
        collection_name: str = settings.QDRANT_COLLECTION_NAME,
        embedding_model: str = settings.OPENAI_EMBEDDING_MODEL,
    ) -> None:
        # ":memory:" is a positional-only arg for the in-process store
        if qdrant_url == ":memory:":
            self._client = QdrantClient(":memory:")
        else:
            self._client = QdrantClient(url=qdrant_url)
        self._collection = collection_name
        self._embedding_model = embedding_model
        self._openai = OpenAI(
            api_key=settings.OPENAI_API_KEY or "no-key",
            max_retries=0,
            timeout=10.0,
        )
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, item: KnowledgeItem) -> None:
        """Embed and upsert a KnowledgeItem into Qdrant (idempotent by id)."""
        vector = self._embed(item.content)
        payload = {
            "knowledge_item_id": item.id,
            "knowledge_type": item.knowledge_type,
            "tags": item.tags or [],
            "tool_name": item.tool_name,
            "failure_signature": item.failure_signature,
            "project_scope": item.project_scope,
            "applies_to": item.applies_to or [],
            "priority": item.priority,
        }
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=item.id, vector=vector, payload=payload)],
        )

    def retrieve(
        self,
        query: str,
        trigger_phase: str,
        knowledge_types: list[str],
        failure_signature: Optional[str] = None,
        tool_name: Optional[str] = None,
        top_k: int = settings.KNOWLEDGE_MAX_ITEMS,
        db: Session = None,
    ) -> KnowledgeContext:
        if not self._has_indexed_points():
            return self._sqlite_fallback(trigger_phase, knowledge_types, top_k, db)

        try:
            vector = self._embed(query)
        except Exception:
            return self._sqlite_fallback(trigger_phase, knowledge_types, top_k, db)

        try:
            hits = self._search(
                vector=vector,
                trigger_phase=trigger_phase,
                knowledge_types=knowledge_types,
                failure_signature=failure_signature,
                tool_name=tool_name,
                top_k=top_k,
            )
        except Exception:
            return self._sqlite_fallback(trigger_phase, knowledge_types, top_k, db)

        if not hits:
            return self._build_context([], query, trigger_phase, "no_results")

        hit_scores: dict[str, float] = {h.id: h.score for h in hits}
        ids = list(hit_scores.keys())

        items: list[KnowledgeItem] = []
        if db is not None:
            items = db.query(KnowledgeItem).filter(KnowledgeItem.id.in_(ids)).all()
        # Preserve scores alongside items for budget sorting
        scored = [(item, hit_scores.get(item.id, 0.0)) for item in items]
        scored = self._apply_budget(scored, failure_signature)

        reason = (
            "failure_signature_match" if failure_signature else "semantic_retrieval"
        )
        return self._build_context(scored, query, trigger_phase, reason)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _search(
        self,
        vector: list[float],
        trigger_phase: str,
        knowledge_types: list[str],
        failure_signature: Optional[str],
        tool_name: Optional[str],
        top_k: int,
    ) -> list:
        if failure_signature:
            conditions = [
                FieldCondition(
                    key="failure_signature", match=MatchValue(value=failure_signature)
                )
            ]
            if tool_name:
                conditions.append(
                    FieldCondition(key="tool_name", match=MatchValue(value=tool_name))
                )
            result = self._client.query_points(
                collection_name=self._collection,
                query=vector,
                query_filter=Filter(must=conditions),
                limit=top_k,
            )
            hits = result.points
            if hits:
                return hits
            # Semantic fallback — no filter

        conditions = []
        if knowledge_types:
            conditions.append(
                FieldCondition(
                    key="knowledge_type", match=MatchAny(any=knowledge_types)
                )
            )
        conditions.append(
            FieldCondition(key="applies_to", match=MatchAny(any=[trigger_phase, "all"]))
        )
        result = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=Filter(must=conditions) if conditions else None,
            limit=top_k,
        )
        return result.points

    def _has_indexed_points(self) -> bool:
        try:
            return (
                int(self._client.count(collection_name=self._collection).count or 0) > 0
            )
        except Exception:
            return True

    def _apply_budget(
        self,
        scored: list[tuple[KnowledgeItem, float]],
        failure_signature: Optional[str],
    ) -> list[tuple[KnowledgeItem, float]]:
        def sort_key(pair: tuple[KnowledgeItem, float]) -> tuple:
            item, score = pair
            exact_match = (
                1
                if (failure_signature and item.failure_signature == failure_signature)
                else 0
            )
            type_rank = _TYPE_RANK.get(item.knowledge_type, 99)
            return (-item.priority, -exact_match, type_rank, -score)

        scored = sorted(scored, key=sort_key)
        # Apply item cap
        scored = scored[: settings.KNOWLEDGE_MAX_ITEMS]
        # Apply total char cap — drop trailing items (never mid-item)
        total = 0
        result = []
        for pair in scored:
            item_len = len(pair[0].content)
            if total + item_len > settings.KNOWLEDGE_MAX_TOTAL_CHARS and result:
                break
            result.append(pair)
            total += item_len
        return result

    def _truncate(self, content: str) -> str:
        max_chars = settings.KNOWLEDGE_CONTENT_MAX_CHARS
        if len(content) <= max_chars:
            return content
        # No mid-word cut
        truncated = content[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated

    def _sqlite_fallback(
        self,
        trigger_phase: str,
        knowledge_types: list[str],
        top_k: int,
        db: Session,
    ) -> KnowledgeContext:
        reason = "sqlite_fallback_qdrant_or_embedding_unavailable"
        if db is None:
            return self._build_context([], None, trigger_phase, reason)
        rows = (
            db.query(KnowledgeItem)
            .filter(KnowledgeItem.knowledge_type.in_(knowledge_types))
            .order_by(KnowledgeItem.priority.desc(), KnowledgeItem.updated_at.desc())
            .all()
        )
        filtered = [
            r
            for r in rows
            if r.applies_to and (trigger_phase in r.applies_to or "all" in r.applies_to)
        ][:top_k]
        scored = [(item, 0.3) for item in filtered]
        scored = self._apply_budget(scored, None)
        return self._build_context(scored, None, trigger_phase, reason)

    def _embed(self, text: str) -> list[float]:
        response = self._openai.embeddings.create(
            model=self._embedding_model, input=text
        )
        return response.data[0].embedding

    def _build_context(
        self,
        scored: list[tuple[KnowledgeItem, float]],
        query: Optional[str],
        trigger_phase: str,
        reason: str,
    ) -> KnowledgeContext:
        refs: list[KnowledgeItemRef] = []
        matched_failure_memory = False
        for item, score in scored:
            refs.append(
                KnowledgeItemRef(
                    id=item.id,
                    title=item.title,
                    knowledge_type=item.knowledge_type,
                    content=self._truncate(item.content),
                    priority=item.priority,
                    confidence=round(score, 4),
                )
            )
            if item.knowledge_type == KnowledgeType.failure_memory:
                matched_failure_memory = True

        overall_confidence = (
            round(sum(r.confidence for r in refs) / len(refs), 4) if refs else 0.0
        )

        recommended_action = RecommendedAction.none
        if refs:
            top_type = refs[0].knowledge_type
            if top_type == KnowledgeType.failure_memory:
                recommended_action = RecommendedAction.stop_retry
            elif top_type == KnowledgeType.tool_contract:
                recommended_action = RecommendedAction.use_tool_contract
            elif top_type == KnowledgeType.format_guide:
                recommended_action = RecommendedAction.adjust_format
            elif top_type in (KnowledgeType.debug_case, KnowledgeType.best_practice):
                recommended_action = RecommendedAction.review_failure

        return KnowledgeContext(
            retrieved_items=refs,
            query=query,
            trigger_phase=trigger_phase,  # type: ignore[arg-type]
            retrieval_reason=reason,
            confidence=overall_confidence,
            matched_failure_memory=matched_failure_memory,
            recommended_action=recommended_action,
        )

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=_EMBEDDING_DIM, distance=Distance.COSINE
                ),
            )
