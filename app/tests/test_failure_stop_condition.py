"""Tests for knowledge-driven halt condition in handle_task_failure."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.phases.failure_flow import _apply_knowledge_halt

_LOG = logging.getLogger(__name__)


def _make_knowledge_ctx(*, matched: bool) -> KnowledgeContext:
    items = (
        [
            KnowledgeItemRef(
                id="abc",
                title="Known DB Failure",
                knowledge_type=KnowledgeType.failure_memory,
                content="db connection drops under load.",
                priority=1,
                confidence=0.95,
            )
        ]
        if matched
        else []
    )
    return KnowledgeContext(
        retrieved_items=items,
        query="db error",
        trigger_phase="failure",
        retrieval_reason="signature_match",
        confidence=0.95 if matched else 0.0,
        matched_failure_memory=matched,
        recommended_action=(
            RecommendedAction.stop_retry if matched else RecommendedAction.none
        ),
    )


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.db = MagicMock()
    ctx.task = MagicMock()
    ctx.task.id = "task-1"
    ctx.project = MagicMock()
    ctx.project.id = "proj-1"
    ctx.orchestration_state = MagicMock()
    ctx.orchestration_state.current_phase = "execution"
    return ctx


def test_halt_when_matched_failure_memory_and_retry_count_ge_2():
    ctx = _make_ctx()
    exc = RuntimeError("db connection refused")

    with (
        patch("app.services.knowledge.knowledge_service.KnowledgeService") as MockKS,
        patch("app.services.knowledge.usage_log_service.log_usage"),
    ):
        MockKS.return_value.retrieve.return_value = _make_knowledge_ctx(matched=True)
        result = _apply_knowledge_halt(
            ctx=ctx,
            exc=exc,
            retry_count=2,
            session_id=1,
            task_id=1,
            logger=_LOG,
        )

    assert result is True
    ctx.db.add.assert_called_once()
    ctx.db.commit.assert_called()


def test_no_halt_when_matched_failure_memory_but_retry_count_lt_2():
    ctx = _make_ctx()
    exc = RuntimeError("db connection refused")

    with (
        patch("app.services.knowledge.knowledge_service.KnowledgeService") as MockKS,
        patch("app.services.knowledge.usage_log_service.log_usage"),
    ):
        MockKS.return_value.retrieve.return_value = _make_knowledge_ctx(matched=True)
        result = _apply_knowledge_halt(
            ctx=ctx,
            exc=exc,
            retry_count=1,
            session_id=1,
            task_id=1,
            logger=_LOG,
        )

    assert result is False
    ctx.db.add.assert_not_called()


def test_no_halt_when_not_matched_regardless_of_retry_count():
    ctx = _make_ctx()
    exc = RuntimeError("some random error")

    with (
        patch("app.services.knowledge.knowledge_service.KnowledgeService") as MockKS,
        patch("app.services.knowledge.usage_log_service.log_usage"),
    ):
        MockKS.return_value.retrieve.return_value = _make_knowledge_ctx(matched=False)
        result = _apply_knowledge_halt(
            ctx=ctx,
            exc=exc,
            retry_count=5,
            session_id=1,
            task_id=1,
            logger=_LOG,
        )

    assert result is False
    ctx.db.add.assert_not_called()
