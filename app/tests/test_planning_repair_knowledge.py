from __future__ import annotations

from pathlib import Path

from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    _render_repair_knowledge_block,
)


def _knowledge_ctx() -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id="failure-1",
                title="Planning repair produced non-runnable step",
                knowledge_type=KnowledgeType.failure_memory,
                content=(
                    "A prior package metadata task failed because repaired planning "
                    "output added a final step with commands: []. Keep final "
                    "verification runnable with node -e or python -m."
                ),
                priority=10,
                confidence=0.95,
            ),
            KnowledgeItemRef(
                id="debug-1",
                title="Use ops for package metadata rewrites",
                knowledge_type=KnowledgeType.debug_case,
                content="Prefer write_file ops for package.json and README edits.",
                priority=5,
                confidence=0.7,
            ),
        ],
        query="Plan validation failed after repair",
        trigger_phase="validation",
        retrieval_reason="failure_signature_match",
        confidence=0.9,
        matched_failure_memory=True,
        recommended_action=RecommendedAction.review_failure,
    )


def test_repair_knowledge_block_includes_failure_memory_and_debug_case():
    block = _render_repair_knowledge_block(_knowledge_ctx())

    assert "REPAIR KNOWLEDGE REFERENCES" in block
    assert "Planning repair produced non-runnable step" in block
    assert "Use ops for package metadata rewrites" in block
    assert "commands: []" in block


def test_planning_repair_prompt_includes_bounded_knowledge_context():
    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Update package metadata and README.",
        malformed_output='[{"step_number":4,"commands":[]}]',
        project_dir=Path("/tmp/project"),
        rejection_reasons=["Plan contains steps without runnable commands"],
        knowledge_context=_knowledge_ctx(),
    )

    assert "REPAIR KNOWLEDGE REFERENCES" in prompt
    assert "Planning repair produced non-runnable step" in prompt
    assert "commands: []" in prompt
    assert len(prompt) <= 6000
