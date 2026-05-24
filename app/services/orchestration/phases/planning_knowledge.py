"""Knowledge retrieval helpers for planning-phase orchestration."""

from __future__ import annotations

from app.schemas.knowledge import KnowledgeContext
from app.services.orchestration.types import OrchestrationRunContext


def _retrieve_knowledge(
    ctx: OrchestrationRunContext,
    trigger_phase: str,
    knowledge_types: list[str],
    query: str | None = None,
    failure_signature: str | None = None,
) -> KnowledgeContext | None:
    """Retrieve knowledge context; returns None on any error so failures don't break the flow."""
    try:
        from app.config import settings
        from app.services.knowledge.knowledge_service import KnowledgeService

        svc = KnowledgeService(
            qdrant_url=settings.QDRANT_URL,
            collection_name=settings.QDRANT_COLLECTION_NAME,
        )
        knowledge_ctx = svc.retrieve(
            query=query or ctx.prompt or "",
            trigger_phase=trigger_phase,
            knowledge_types=knowledge_types,
            failure_signature=failure_signature,
            db=ctx.db,
        )
        ctx.logger.info(
            "[KNOWLEDGE] Retrieval phase=%s types=%s items=%d reason=%s "
            "matched_failure_memory=%s recommended_action=%s",
            trigger_phase,
            ",".join(knowledge_types),
            len(knowledge_ctx.retrieved_items),
            knowledge_ctx.retrieval_reason,
            knowledge_ctx.matched_failure_memory,
            knowledge_ctx.recommended_action.value,
        )
        return knowledge_ctx
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Retrieval skipped (%s): %s", trigger_phase, exc)
        return None


def _log_knowledge_usage(
    ctx: OrchestrationRunContext,
    knowledge_ctx: KnowledgeContext,
    *,
    used_in_prompt: bool,
) -> None:
    try:
        from app.services.knowledge import usage_log_service

        usage_log_service.log_usage(
            context=knowledge_ctx,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            used_in_prompt=used_in_prompt,
            db=ctx.db,
        )
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Usage log skipped: %s", exc)


def _retrieve_validation_repair_knowledge(
    ctx: OrchestrationRunContext,
    *,
    query: str,
    failure_signature: str | None = None,
    retrieve_knowledge=None,
    log_knowledge_usage=None,
) -> KnowledgeContext | None:
    retrieve = retrieve_knowledge or _retrieve_knowledge
    log_usage = log_knowledge_usage or _log_knowledge_usage
    knowledge_ctx = retrieve(
        ctx,
        trigger_phase="validation",
        knowledge_types=[
            "failure_memory",
            "format_guide",
            "debug_case",
        ],
        query=query,
        failure_signature=failure_signature,
    )
    if knowledge_ctx:
        log_usage(ctx, knowledge_ctx, used_in_prompt=True)
    return knowledge_ctx


def _looks_like_verification_only_task(
    title: str | None,
    description: str | None,
) -> bool:
    combined = f"{title or ''}\n{description or ''}".lower()
    verification_markers = (
        "verification command",
        "verification commands",
        "improve task verification",
        "strengthen verification",
        "checks prove",
        "content-aware checks",
        "file/content checks",
        "audit",
        "inspect current files",
        "no major implementation",
        "do not change page design",
        "do not change page design much",
    )
    if not any(marker in combined for marker in verification_markers):
        return False
    implementation_markers = (
        "create `",
        "create ",
        "add new ",
        "add a new ",
        "add seasonal",
        "add second",
        "adjust one content block",
        "adjust one css",
        "update css",
        "edit `",
        "edits `",
        "start task that edits",
    )
    return not any(marker in combined for marker in implementation_markers)
