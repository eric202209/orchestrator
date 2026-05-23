"""Knowledge retrieval and ingestion service backed by Qdrant + SQLite."""

from __future__ import annotations

import re
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


def _resolve_provider() -> str:
    p = settings.EMBEDDING_PROVIDER.strip().lower()
    if p == "auto":
        return "openai" if settings.OPENAI_API_KEY.strip() else "ollama"
    return p


def _resolve_dim(provider: str) -> int:
    if settings.EMBEDDING_DIM > 0:
        return settings.EMBEDDING_DIM
    return 768 if provider == "ollama" else 1536


def _knowledge_type_values(knowledge_types: list[str]) -> list[str]:
    return [str(getattr(item, "value", item)) for item in knowledge_types]


def _applies_to_candidates(trigger_phase: str, knowledge_types: list[str]) -> list[str]:
    candidates = [trigger_phase, "all"]
    type_values = set(_knowledge_type_values(knowledge_types))
    if trigger_phase == "validation" and type_values.intersection(
        {KnowledgeType.failure_memory.value, KnowledgeType.debug_case.value}
    ):
        candidates.insert(1, "failure")
    return list(dict.fromkeys(candidates))


def _matches_task_type_gate(
    item: KnowledgeItem,
    *,
    query: Optional[str],
    failure_signature: Optional[str],
) -> bool:
    """Keep task-family guides from leaking into unrelated planning prompts."""

    tags = {str(tag or "").strip().lower() for tag in (item.tags or [])}
    if "static-site" not in tags:
        return True

    signature = (failure_signature or "").strip().lower()
    if signature:
        return any(
            marker in signature
            for marker in (
                "static",
                "html",
                "css",
                "svg",
                "asset",
                "expected_files",
                "materialization",
            )
        )

    text = f"{query or ''} {item.failure_signature or ''}".lower()
    if _explicitly_excludes_static_site(text):
        return False

    static_markers = (
        "static site",
        "static-site",
        "plain html",
        "index.html",
        "css/style.css",
        "stylesheet link",
        "svg",
        ".html",
        ".css",
    )
    return any(marker in text for marker in static_markers)


def _explicitly_excludes_static_site(text: str) -> bool:
    """Treat negated static-site mentions as exclusion, not task-family signal."""

    patterns = (
        r"\bnot\s+(?:a\s+)?static[-\s]site\b",
        r"\bnot\s+static[-\s]site\b",
        r"\bno\s+static[-\s]site\b",
        r"\bwithout\s+static[-\s]site\b",
        r"\bdo\s+not\s+[^.;\n]{0,120}\bstatic[-\s]site\b",
        r"\bdon't\s+[^.;\n]{0,120}\bstatic[-\s]site\b",
        r"\bdo\s+not\s+[^.;\n]{0,120}\bindex\.html\b",
        r"\bdon't\s+[^.;\n]{0,120}\bindex\.html\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


class KnowledgeService:
    def __init__(
        self,
        qdrant_url: str = settings.QDRANT_URL,
        collection_name: str = settings.QDRANT_COLLECTION_NAME,
        embedding_model: str = "",
        embedding_dim: int = 0,
    ) -> None:
        # ":memory:" is a positional-only arg for the in-process store
        if qdrant_url == ":memory:":
            self._client = QdrantClient(":memory:")
        else:
            self._client = QdrantClient(url=qdrant_url, check_compatibility=False)
        self._collection = collection_name
        self._qdrant_available = True

        provider = _resolve_provider()
        if provider == "ollama":
            base_url = settings.OLLAMA_BASE_URL.rstrip("/") + "/v1"
            self._embedding_model = embedding_model or settings.OLLAMA_EMBEDDING_MODEL
            self._embed_client = OpenAI(
                api_key="ollama",
                base_url=base_url,
                max_retries=0,
                timeout=30.0,
            )
        else:
            self._embedding_model = embedding_model or settings.OPENAI_EMBEDDING_MODEL
            self._embed_client = OpenAI(
                api_key=settings.OPENAI_API_KEY or "no-key",
                max_retries=0,
                timeout=10.0,
            )

        self._embedding_dim = (
            embedding_dim if embedding_dim > 0 else _resolve_dim(provider)
        )
        try:
            self._ensure_collection()
        except Exception:
            self._qdrant_available = False

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
            return self._sqlite_fallback(
                trigger_phase,
                knowledge_types,
                top_k,
                db,
                query=query,
                failure_signature=failure_signature,
            )

        try:
            vector = self._embed(query)
        except Exception:
            return self._sqlite_fallback(
                trigger_phase,
                knowledge_types,
                top_k,
                db,
                query=query,
                failure_signature=failure_signature,
            )

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
            return self._sqlite_fallback(
                trigger_phase,
                knowledge_types,
                top_k,
                db,
                query=query,
                failure_signature=failure_signature,
            )

        if not hits:
            return self._build_context([], query, trigger_phase, "no_results")

        hit_scores: dict[str, float] = {h.id: h.score for h in hits}
        ids = list(hit_scores.keys())

        items: list[KnowledgeItem] = []
        if db is not None:
            items = db.query(KnowledgeItem).filter(KnowledgeItem.id.in_(ids)).all()
        # Preserve scores alongside items for budget sorting
        scored = [
            (item, hit_scores.get(item.id, 0.0))
            for item in items
            if _matches_task_type_gate(
                item, query=query, failure_signature=failure_signature
            )
        ]
        if not scored and items:
            return self._build_context(
                [], query, trigger_phase, "filtered_by_task_type_gate"
            )
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
                    key="knowledge_type",
                    match=MatchAny(any=_knowledge_type_values(knowledge_types)),
                )
            )
        conditions.append(
            FieldCondition(
                key="applies_to",
                match=MatchAny(
                    any=_applies_to_candidates(trigger_phase, knowledge_types)
                ),
            )
        )
        result = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=Filter(must=conditions) if conditions else None,
            limit=top_k,
        )
        return result.points

    def _has_indexed_points(self) -> bool:
        if not self._qdrant_available:
            return False
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
            return (-exact_match, type_rank, -score, -item.priority)

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
        query: Optional[str] = None,
        failure_signature: Optional[str] = None,
    ) -> KnowledgeContext:
        reason = "sqlite_fallback_qdrant_or_embedding_unavailable"
        if db is None:
            return self._build_context([], query, trigger_phase, reason)
        rows = (
            db.query(KnowledgeItem)
            .filter(
                KnowledgeItem.knowledge_type.in_(
                    _knowledge_type_values(knowledge_types)
                )
            )
            .order_by(KnowledgeItem.priority.desc(), KnowledgeItem.updated_at.desc())
            .all()
        )
        applies_to_candidates = _applies_to_candidates(trigger_phase, knowledge_types)
        filtered = [
            r
            for r in rows
            if r.applies_to
            and any(phase in r.applies_to for phase in applies_to_candidates)
            and _matches_task_type_gate(
                r, query=query, failure_signature=failure_signature
            )
        ]
        scored = [
            (item, self._sqlite_fallback_score(item, query, failure_signature))
            for item in filtered
        ]
        scored = self._apply_budget(scored, failure_signature)[:top_k]
        return self._build_context(scored, query, trigger_phase, reason)

    def _sqlite_fallback_score(
        self,
        item: KnowledgeItem,
        query: Optional[str],
        failure_signature: Optional[str],
    ) -> float:
        score = 0.3
        item_signature = (item.failure_signature or "").strip().lower()
        normalized_failure = (failure_signature or "").strip().lower()
        normalized_query = (query or "").strip().lower()

        if item_signature:
            if normalized_failure and (
                item_signature in normalized_failure
                or normalized_failure in item_signature
            ):
                return 1.0
            if normalized_query and item_signature in normalized_query:
                score = max(score, 0.95)

        haystack_parts = [
            item.title or "",
            item.content or "",
            item.failure_signature or "",
            " ".join(str(tag) for tag in (item.tags or [])),
        ]
        haystack = " ".join(haystack_parts).lower()
        query_terms = {
            term.strip(".,:;()[]'\"")
            for term in normalized_query.split()
            if len(term.strip(".,:;()[]'\"")) >= 4
        }
        if query_terms:
            overlap = sum(1 for term in query_terms if term in haystack)
            if overlap > 0:
                score = max(score, min(0.9, 0.35 + (overlap * 0.05)))
        return score

    def _embed(self, text: str) -> list[float]:
        response = self._embed_client.embeddings.create(
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
                    size=self._embedding_dim, distance=Distance.COSINE
                ),
            )
