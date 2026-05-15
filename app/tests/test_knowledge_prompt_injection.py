"""Tests for knowledge block injection into planning prompts."""

from __future__ import annotations

from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.context.assembly import _render_knowledge_block
from app.services.orchestration.planning.planner import PlannerService


def _make_ctx(items: list[KnowledgeItemRef]) -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=items,
        query="test query",
        trigger_phase="planning",
        retrieval_reason="test",
        confidence=0.9,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )


def _make_ref(
    *, title: str, content: str, knowledge_type: str = KnowledgeType.format_guide
) -> KnowledgeItemRef:
    return KnowledgeItemRef(
        id="abc",
        title=title,
        knowledge_type=knowledge_type,
        content=content,
        priority=0,
        confidence=0.9,
    )


def test_knowledge_block_contains_references_header_and_both_titles():
    items = [
        _make_ref(title="JSON Output Guide", content="Always return JSON."),
        _make_ref(title="Auth Module Example", content="Example auth task."),
    ]
    ctx = _make_ctx(items)
    block = _render_knowledge_block(ctx)

    assert "KNOWLEDGE REFERENCES" in block
    assert "JSON Output Guide" in block
    assert "Auth Module Example" in block


def test_no_knowledge_block_when_context_is_none():
    block = _render_knowledge_block(None)
    assert block == ""


def test_no_knowledge_block_when_items_empty():
    ctx = _make_ctx([])
    block = _render_knowledge_block(ctx)
    assert block == ""


def test_content_per_item_is_at_most_800_chars():
    long_content = "x" * 800
    items = [_make_ref(title="Big Doc", content=long_content)]
    ctx = _make_ctx(items)
    block = _render_knowledge_block(ctx)

    # Extract item content section — everything after the [1] header line
    lines = block.splitlines()
    content_lines = [ln for ln in lines if ln.startswith("x")]
    assert all(len(ln) <= 800 for ln in content_lines)


def test_minimal_planning_prompt_includes_knowledge_references(tmp_path):
    ctx = _make_ctx(
        [
            _make_ref(
                title="Workspace Reuse Guide",
                content="Inspect existing files before creating a nested project.",
            )
        ]
    )

    prompt = PlannerService.build_minimal_planning_prompt(
        "Update the existing app",
        tmp_path,
        workspace_has_existing_files=True,
        knowledge_context=ctx,
    )

    assert "KNOWLEDGE REFERENCES" in prompt
    assert "Workspace Reuse Guide" in prompt
    assert "Inspect existing files before creating a nested project." in prompt
