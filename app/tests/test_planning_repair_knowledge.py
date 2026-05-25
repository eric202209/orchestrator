from __future__ import annotations

from pathlib import Path

from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.orchestration.planning.planner import (
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
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


def test_planning_repair_prompt_preserves_knowledge_when_structure_is_large(
    tmp_path,
):
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    for idx in range(120):
        (tmp_path / "src" / "ledger_app" / f"module_{idx}.py").write_text("")
    (tmp_path / "tests" / "test_calc.py").write_text("")

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Fix the ledger calculator refund handling.",
        malformed_output=(
            '[{"step_number":2,"ops":[{"op":"replace_in_file",'
            '"path":"src/ledger_app/calculator.py","old":"x","new":"y"}]}]'
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in src/ledger_app/calculator.py",
            "stale_replace_ops_steps: use identifiers from current file excerpt",
        ],
        knowledge_context=_knowledge_ctx(),
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "REPAIR KNOWLEDGE REFERENCES" in prompt
    assert "Planning repair produced non-runnable step" in prompt
    assert "PROJECT STRUCTURE CAPSULE" in prompt


def test_specialized_repair_prompt_preserves_knowledge_when_structure_is_large(
    tmp_path,
):
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    for idx in range(120):
        (tmp_path / "src" / "ledger_app" / f"module_{idx}.py").write_text("")

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Verify existing app source paths only.",
        malformed_output=(
            '[{"step_number":1,"commands":["cat missing.css"],'
            '"verification":"test -f missing.css"}]'
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "verification/review plan references source files that do not exist"
        ],
        knowledge_context=_knowledge_ctx(),
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "Verification-only repair mode." in prompt
    assert "REPAIR KNOWLEDGE REFERENCES" in prompt
    assert "Planning repair produced non-runnable step" in prompt
    assert "PROJECT STRUCTURE CAPSULE" in prompt
