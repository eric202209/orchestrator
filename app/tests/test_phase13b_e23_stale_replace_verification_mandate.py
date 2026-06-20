"""Phase 13B-E23: Stale-Replace Verification Mandate tests.

Verifies that build_compact_stale_replace_repair_prompt contains a hard
consequence-linked verification mandate rather than soft guidance, and that
the prompt remains within the 6000-char budget for realistic fixture shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.planning import repair_prompts


_STALE_PLAN = json.dumps(
    [
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
)

_STALE_REASONS = [
    "replace_in_file old text not found in workspace in steps [2]",
    "stale_replace_ops_steps: use identifiers from current file excerpt",
]


def _make_python_cli_fixture(tmp_path: Path) -> Path:
    """E20 python_cli-shaped fixture: src/small_cli/cli.py + tests/test_cli.py."""
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        '"""Tiny message-printing CLI."""\n'
        "\n"
        "from __future__ import annotations\n"
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
        "def test_format_message():\n"
        '    assert format_message("hello") == "hello"\n'
        "\n"
        "\n"
        "def test_cli_prints_message(capsys):\n"
        '    assert main(["hello"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "hello"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_compact_stale_replace_prompt_has_verification_mandate(tmp_path):
    """The hard mandate phrase must appear in the compact stale_replace prompt (E23)."""
    _make_python_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add --uppercase flag to small_cli.",
        malformed_output=_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_STALE_REASONS,
        prompt_profile="default",
    )
    assert (
        'REQUIRED: the final step MUST include a non-empty "verification" field'
        in prompt
    )


def test_compact_stale_replace_prompt_mandate_is_consequence_linked(tmp_path):
    """The mandate must state the rejection consequence (E23 hard-link requirement)."""
    _make_python_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add --uppercase flag to small_cli.",
        malformed_output=_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_STALE_REASONS,
        prompt_profile="default",
    )
    assert "will cause this repaired plan to be rejected" in prompt


def test_compact_stale_replace_prompt_old_soft_guidance_absent(tmp_path):
    """The old soft guidance phrase must NOT appear in the compact stale_replace prompt."""
    _make_python_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add --uppercase flag to small_cli.",
        malformed_output=_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_STALE_REASONS,
        prompt_profile="default",
    )
    assert "Keep simple scalar verification on final pytest/test steps" not in prompt


def test_compact_stale_replace_prompt_mandate_fits_budget_python_cli_shape(tmp_path):
    """Prompt must remain under the 6000-char hard budget for the E20 python_cli shape."""
    _make_python_cli_fixture(tmp_path)
    current_excerpt = (tmp_path / "src" / "small_cli" / "cli.py").read_text(
        encoding="utf-8"
    )
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add --uppercase flag to small_cli.",
        malformed_output=_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [2]",
            (
                "step 2 replace_in_file old text not found in src/small_cli/cli.py. "
                "Use exact text from current file excerpt or choose a different "
                f"operation. Current file excerpt: {current_excerpt}"
            ),
            "stale_replace_ops_steps: use identifiers from current file excerpt",
        ],
        prompt_profile="local_qwen_json_array",
    )
    assert len(prompt) <= repair_prompts.REPAIR_PROMPT_MAX_CHARS, (
        f"E23 python_cli-shaped stale_replace prompt is {len(prompt)} chars "
        f"(budget: {repair_prompts.REPAIR_PROMPT_MAX_CHARS}). "
        "Verification mandate may have pushed the prompt over budget."
    )
    assert (
        'REQUIRED: the final step MUST include a non-empty "verification" field'
        in prompt
    )


def test_compact_stale_replace_prompt_mandate_present_in_truncated_path(tmp_path):
    """Mandate must survive excerpt truncation (compact budget-compressed path)."""
    _make_python_cli_fixture(tmp_path)
    # Inject an oversized excerpt to force truncation inside _compose
    long_excerpt = "\n".join(f"line_{i} = {i}" for i in range(300))
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Update the CLI.",
        malformed_output=_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=[
            "stale_replace_ops_steps: step 2 replace_in_file old text not found",
            f"Current file excerpt: {long_excerpt}",
        ],
        prompt_profile="local_qwen_json_array",
    )
    assert len(prompt) <= repair_prompts.REPAIR_PROMPT_MAX_CHARS
    assert (
        'REQUIRED: the final step MUST include a non-empty "verification" field'
        in prompt
    )


def test_stale_replace_mandate_absent_from_generic_compact_prompt():
    """The mandate is scoped to stale_replace path; generic compact prompt must not contain it."""
    prompt = repair_prompts.build_compact_planning_repair_prompt(
        malformed_output='[{"step_number":1,"commands":[]}]',
        rejection_reasons=["commands must be a non-empty array"],
        prompt_profile="default",
    )
    assert (
        'REQUIRED: the final step MUST include a non-empty "verification" field'
        not in prompt
    )
    assert "will cause this repaired plan to be rejected" not in prompt
    assert len(prompt) <= repair_prompts.REPAIR_PROMPT_MAX_CHARS
