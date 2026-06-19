from __future__ import annotations

import json
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


def test_repair_knowledge_block_includes_first_item_only():
    block = _render_repair_knowledge_block(_knowledge_ctx())

    assert "REPAIR KNOWLEDGE REFERENCES" in block
    assert "Planning repair produced non-runnable step" in block
    assert "commands: []" in block
    # Only the first item is rendered (PLANNING_REPAIR_MAX_KNOWLEDGE_ITEMS=1).
    # The second debug_case item must be absent.
    assert "Use ops for package metadata rewrites" not in block


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
    # Only the first knowledge item should appear (PLANNING_REPAIR_MAX_KNOWLEDGE_ITEMS=1).
    assert "Use ops for package metadata rewrites" not in prompt


def test_planning_repair_prompt_preserves_knowledge_when_structure_is_large(
    tmp_path,
):
    # E14: With MAX_KNOWLEDGE_ITEMS=1, the smaller knowledge block (1 item) allows
    # the default fallback path to preserve knowledge WITHOUT needing the rescue
    # block that also reintroduced the structure capsule (with 2 items).  The
    # structure is dropped to stay under budget; knowledge is kept.
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


def test_planning_repair_prompt_fits_duplicate_stale_replace_source_context(
    tmp_path,
):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        '"""Tiny message-printing CLI used by the orchestrator eval fixture."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "\n"
        "\n"
        "def format_message(message: str) -> str:\n"
        "    return message\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    parser = argparse.ArgumentParser(description="Print a message.")\n'
        '    parser.add_argument("message", help="Message to print")\n'
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    print(format_message(args.message))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import build_parser, format_message, main\n"
        "\n"
        "\n"
        "def test_format_message_returns_message_by_default():\n"
        '    assert format_message("hello") == "hello"\n'
        "\n"
        "\n"
        "def test_parser_accepts_message():\n"
        '    args = build_parser().parse_args(["hello"])\n'
        '    assert args.message == "hello"\n'
        "\n"
        "\n"
        "def test_cli_prints_message(capsys):\n"
        '    assert main(["hello"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "hello"\n'
        "\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        '    assert main(["--uppercase", "hello"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "HELLO"\n',
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/small_cli/cli.py",
                    "old": "parser.add_argument('--uppercase')",
                    "new": "parser.add_argument('--uppercase', action='store_true')",
                }
            ],
        },
        {
            "step_number": 3,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/small_cli/cli.py",
                    "old": "print(args.message.upper())",
                    "new": "print(format_message(args.message))",
                }
            ],
        },
        {
            "step_number": 4,
            "commands": ["python -m pytest -q tests/test_cli.py"],
            "verification": "python -m pytest -q tests/test_cli.py",
        },
    ]
    stale_hints = PlannerService.stale_replace_repair_hints(plan, tmp_path)

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Add --uppercase to the existing small_cli argparse CLI.",
        malformed_output=json.dumps(plan),
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [2, 3]",
            *stale_hints,
        ],
        knowledge_context=_knowledge_ctx(),
    )

    assert len(stale_hints) == 1
    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "REPAIR KNOWLEDGE REFERENCES" in prompt
    assert "Planning repair produced non-runnable step" in prompt
    assert "Stale replace fixes" in prompt
    assert "Current file excerpt:" in prompt


def test_stale_replace_repair_over_budget_uses_compact_stale_prompt(
    tmp_path, monkeypatch
):
    from app.services.orchestration.planning import repair_prompts

    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        '"""Tiny message-printing CLI used by the orchestrator eval fixture."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "\n"
        "\n"
        "def format_message(message: str) -> str:\n"
        "    return message\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    parser = argparse.ArgumentParser(description="Print a message.")\n'
        '    parser.add_argument("message", help="Message to print")\n'
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    print(format_message(args.message))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/small_cli/cli.py",
                    "old": "parser.add_argument('--uppercase')",
                    "new": "parser.add_argument('--uppercase', action='store_true')",
                }
            ],
        }
    ]
    current_excerpt = (tmp_path / "src" / "small_cli" / "cli.py").read_text(
        encoding="utf-8"
    )

    compact_prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add --uppercase to the existing small_cli argparse CLI.",
        malformed_output=json.dumps(plan),
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [2]",
            (
                "step 2 replace_in_file old text not found in src/small_cli/cli.py. "
                "Use exact text from current file excerpt or choose a different "
                f"operation. Current file excerpt: {current_excerpt}"
            ),
            "stale_replace_ops_steps: use identifiers from current file excerpt "
            + ("extra validation context " * 80),
        ],
        prompt_profile="local_qwen_json_array",
    )
    prompt_cap = len(compact_prompt) + 20
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Add --uppercase to the existing small_cli argparse CLI.",
        malformed_output=json.dumps(plan),
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [2]",
            (
                "step 2 replace_in_file old text not found in src/small_cli/cli.py. "
                "Use exact text from current file excerpt or choose a different "
                f"operation. Current file excerpt: {current_excerpt}"
            ),
            "stale_replace_ops_steps: use identifiers from current file excerpt "
            + ("extra validation context " * 80),
        ],
        prompt_profile="local_qwen_json_array",
        knowledge_context=_knowledge_ctx(),
    )

    # E14: With MAX_KNOWLEDGE_ITEMS=1, the single knowledge item (~628 chars) is small
    # enough that the compact stale_replace prompt WITH knowledge fits under the patched
    # cap (compact_without_knowledge + 20).  Previously (2 items, ~1257 chars) knowledge
    # was always dropped in the compact fallback; now it is preserved.  The excerpt may
    # be truncated to make room; we do not assert on specific function signatures.
    assert len(prompt) <= prompt_cap
    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "Stale replace repair mode." in prompt
    assert "REPAIR KNOWLEDGE REFERENCES" in prompt
    assert "Planning repair produced non-runnable step" in prompt
    assert "src/small_cli/cli.py" in prompt
    assert "Current file excerpt for src/small_cli/cli.py" in prompt
    assert "Use a write_file op for `src/small_cli/cli.py`" in prompt
    assert "write_file.content and append_file.content must be JSON strings" in prompt


def test_compact_stale_replace_prompt_caps_current_file_excerpt(tmp_path):
    from app.services.orchestration.planning import repair_prompts

    (tmp_path / "src").mkdir()
    long_file = "\n".join(f"line_{index} = {index}" for index in range(400))
    (tmp_path / "src" / "cli.py").write_text(long_file, encoding="utf-8")
    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/cli.py",
                    "old": "missing old text",
                    "new": "replacement",
                }
            ],
        }
    ]

    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Update the CLI.",
        malformed_output=json.dumps(plan),
        project_dir=tmp_path,
        rejection_reasons=[
            "stale_replace_ops_steps: step 2 replace_in_file old text not found in src/cli.py",
            f"Current file excerpt: {long_file}",
        ],
        prompt_profile="local_qwen_json_array",
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "...<truncated current file excerpt>..." in prompt
    assert "line_0 = 0" in prompt
    assert "line_399 = 399" not in prompt


def test_non_stale_over_budget_behavior_uses_generic_compact_prompt(
    tmp_path, monkeypatch
):
    from app.services.orchestration.planning import repair_prompts

    malformed_output = json.dumps(
        {
            "payloads": [{"text": "remove me"}],
            "finalAssistantVisibleText": "x" * 12000,
            "projectContext": "project context must be stripped",
        }
    )
    rejection_reasons = ["schema rejected " + ("z" * 1000)] * 20
    compact_prompt = PlannerService.build_compact_planning_repair_prompt(
        malformed_output,
        rejection_reasons=rejection_reasons,
        prompt_profile="local_qwen_json_array",
    )
    prompt_cap = len(compact_prompt) + 20
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Build a page",
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=rejection_reasons,
        prompt_profile="local_qwen_json_array",
        knowledge_context=_knowledge_ctx(),
    )

    assert len(prompt) <= prompt_cap
    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "Repair this invalid plan into 3 to 4 executable steps." in prompt
    assert "Stale replace repair mode." not in prompt
    assert "Use a write_file op for" not in prompt


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


# --- E14: budget fix tests ---


def test_repair_knowledge_block_single_item_limit():
    """With MAX_KNOWLEDGE_ITEMS=1, only the first item renders (E14 regression guard)."""
    from app.services.orchestration.planning import repair_prompts

    assert repair_prompts.PLANNING_REPAIR_MAX_KNOWLEDGE_ITEMS == 1
    block = _render_repair_knowledge_block(_knowledge_ctx())
    assert "[1]" in block
    assert "[2]" not in block
    assert "Planning repair produced non-runnable step" in block
    assert "Use ops for package metadata rewrites" not in block


def test_repair_prompt_group_a_shape_fits_budget():
    """Repair prompt with Group A payload shape (665-char malformed, 240-char error,
    one knowledge item) must fit under the 6000-char hard budget.

    E13 Group A tasks failed because two knowledge items (1257 chars) pushed the
    compact fallback prompt from ~5231 to 6488 chars.  With one item the expected
    knowledge block is ~629 chars, targeting ~5860 chars total.
    """
    malformed_output = "x" * 665
    rejection_reasons = ["Validation error: " + "e" * 220]

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Implement a queue-latency handler.",
        malformed_output=malformed_output,
        project_dir=Path("/tmp/project"),
        rejection_reasons=rejection_reasons,
        knowledge_context=_knowledge_ctx(),
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS, (
        f"Group A-shaped prompt is {len(prompt)} chars, expected <= 6000. "
        "E14 constant change may not have taken effect."
    )
    assert "REPAIR KNOWLEDGE REFERENCES" in prompt


def test_repair_prompt_no_knowledge_context_works():
    """No knowledge context must produce a valid prompt under budget."""
    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Fix the broken step.",
        malformed_output='[{"step_number":1,"commands":[]}]',
        project_dir=Path("/tmp/project"),
        rejection_reasons=["commands must be a non-empty array"],
        knowledge_context=None,
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "REPAIR KNOWLEDGE REFERENCES" not in prompt


def test_repair_prompt_budget_rejection_still_fires(tmp_path, monkeypatch):
    """PlanningRepairBudgetExceeded must still be raised when the assembled prompt
    (after all fallbacks) exceeds the hard limit.  The E14 constant change must not
    suppress this safety gate.
    """
    import pytest
    from app.services.orchestration.planning import repair_prompts as repair_prompts_mod
    from app.services.orchestration.planning import planner as planner_mod
    from app.services.orchestration.planning.planner import PlanningRepairBudgetExceeded

    oversized_output = "z" * 12000
    rejection_reasons = ["schema violated " + "e" * 2000]

    # Lower the budget constant in both modules so even the compact fallback fails.
    monkeypatch.setattr(planner_mod, "PLANNING_REPAIR_PROMPT_MAX_CHARS", 200)
    monkeypatch.setattr(repair_prompts_mod, "PLANNING_REPAIR_PROMPT_MAX_CHARS", 200)

    with pytest.raises(PlanningRepairBudgetExceeded) as exc_info:
        PlannerService.repair_output(
            runtime_service=None,
            task_description="Build a feature",
            malformed_output=oversized_output,
            project_dir=tmp_path,
            timeout_seconds=300,
            logger=__import__("logging").getLogger("test"),
            emit_live=lambda *a, **kw: None,
            reason="json_parse_failed",
            rejection_reasons=rejection_reasons,
            knowledge_context=None,
        )

    assert "exceeded safe budget" in str(exc_info.value)
