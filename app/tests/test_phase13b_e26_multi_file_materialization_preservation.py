"""Phase 13B-E26: Multi-File Materialization Preservation Hardening tests.

Verifies that build_compact_stale_replace_repair_prompt:
- No longer contains a fixed "Return 3 steps" instruction.
- Explicitly requires preservation of all source-file materialization ops.
- Stays within the 6000-char hard budget for a medium_cli multi-file shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.planning import repair_prompts


# --- Fixtures ---


def _make_medium_cli_fixture(tmp_path: Path) -> Path:
    """medium_cli-shaped fixture with two source files: cli.py and formatting.py."""
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        '"""Medium CLI entry point."""\n'
        "\n"
        "from __future__ import annotations\n"
        "import argparse\n"
        "from medium_cli.formatting import format_output\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    parser = argparse.ArgumentParser(description="Medium CLI")\n'
        '    parser.add_argument("message", help="Message to display")\n'
        '    parser.add_argument("--uppercase", action="store_true")\n'
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv=None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    print(format_output(args.message, uppercase=args.uppercase))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "medium_cli" / "formatting.py").write_text(
        '"""Formatting helpers."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "\n"
        "def format_output(message: str, *, uppercase: bool = False) -> str:\n"
        "    return message.upper() if uppercase else message\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n"
        "from medium_cli.formatting import format_output\n"
        "\n"
        "\n"
        "def test_format_output_plain():\n"
        '    assert format_output("hello") == "hello"\n'
        "\n"
        "\n"
        "def test_format_output_uppercase():\n"
        '    assert format_output("hello", uppercase=True) == "HELLO"\n'
        "\n"
        "\n"
        "def test_main_plain(capsys):\n"
        '    assert main(["hello"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "hello"\n'
        "\n"
        "\n"
        "def test_main_uppercase(capsys):\n"
        '    assert main(["hello", "--uppercase"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "HELLO"\n',
        encoding="utf-8",
    )
    return tmp_path


# Multi-file rejected plan: stale replace on both cli.py and formatting.py
_MULTI_FILE_STALE_PLAN = json.dumps(
    [
        {
            "step_number": 1,
            "description": "Inspect workspace",
            "commands": ["ls src/medium_cli/"],
            "verification": None,
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Update CLI entry point",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/cli.py",
                    "old": "def main(argv=None):",
                    "new": "def main(argv: list[str] | None = None):",
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Update formatting module",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["src/medium_cli/formatting.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/formatting.py",
                    "old": "def format_output(msg):",
                    "new": "def format_output(message: str, *, uppercase: bool = False) -> str:",
                }
            ],
        },
        {
            "step_number": 4,
            "description": "Run tests",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [],
        },
    ]
)

_MULTI_FILE_STALE_REASONS = [
    "replace_in_file old text not found in workspace in steps [2, 3]",
    "stale_replace_ops_steps: use identifiers from current file excerpt",
]


# --- Case 1: Multi-file plan prompt contains preservation language ---


def test_multi_file_plan_prompt_contains_preservation_language(tmp_path):
    """Prompt rendered for a multi-file stale plan must include preservation language (E26 Case 1)."""
    _make_medium_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add type annotations to medium_cli cli.py and formatting.py.",
        malformed_output=_MULTI_FILE_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_MULTI_FILE_STALE_REASONS,
        prompt_profile="default",
    )
    assert (
        "source files" in prompt or "source-file materialization" in prompt
    ), "Prompt must reference source-file preservation for multi-file stale plans"


# --- Case 2: "Return 3 steps" absent ---


def test_stale_replace_prompt_no_longer_contains_return_3_steps(tmp_path):
    """The hard-coded 'Return 3 steps' instruction must not appear in the prompt (E26 Case 2)."""
    _make_medium_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add type annotations to medium_cli.",
        malformed_output=_MULTI_FILE_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_MULTI_FILE_STALE_REASONS,
        prompt_profile="default",
    )
    assert (
        "Return 3 steps" not in prompt
    ), "Fixed 'Return 3 steps' instruction must be removed; it collapses multi-file plans"


def test_stale_replace_prompt_no_3_steps_single_file(tmp_path):
    """'Return 3 steps' must also be absent for single-file stale plans (E26 Case 2 single-file)."""
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "def main(argv=None): pass\n", encoding="utf-8"
    )
    single_file_plan = json.dumps(
        [
            {
                "step_number": 1,
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "src/small_cli/cli.py",
                        "old": "MISSING",
                        "new": "def main(argv=None): return 0",
                    }
                ],
            }
        ]
    )
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Fix small_cli main.",
        malformed_output=single_file_plan,
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [1]"
        ],
        prompt_profile="default",
    )
    assert "Return 3 steps" not in prompt


# --- Case 3: Explicit source materialization / no-drop language ---


def test_prompt_references_preserving_source_materialization(tmp_path):
    """Prompt must explicitly reference preserving source materialization ops (E26 Case 3)."""
    _make_medium_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add type annotations to medium_cli.",
        malformed_output=_MULTI_FILE_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_MULTI_FILE_STALE_REASONS,
        prompt_profile="default",
    )
    assert (
        "materialization" in prompt
    ), "Prompt must reference source materialization preservation"


def test_prompt_references_not_dropping_source_files(tmp_path):
    """Prompt must state that dropping source-file operations is plan corruption (E26 Case 3)."""
    _make_medium_cli_fixture(tmp_path)
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add type annotations to medium_cli.",
        malformed_output=_MULTI_FILE_STALE_PLAN,
        project_dir=tmp_path,
        rejection_reasons=_MULTI_FILE_STALE_REASONS,
        prompt_profile="default",
    )
    assert (
        "plan corruption" in prompt
    ), "Prompt must label dropping source-file ops as plan corruption"


# --- Case 4: Budget check for medium_cli shape ---


def test_prompt_within_budget_medium_cli_shape(tmp_path):
    """Prompt must remain under REPAIR_PROMPT_MAX_CHARS for a medium_cli multi-file shape (E26 Case 4)."""
    project_dir = _make_medium_cli_fixture(tmp_path)
    cli_excerpt = (tmp_path / "src" / "medium_cli" / "cli.py").read_text(
        encoding="utf-8"
    )
    prompt = repair_prompts.build_compact_stale_replace_repair_prompt(
        task_description="Add type annotations to medium_cli cli.py and formatting.py.",
        malformed_output=_MULTI_FILE_STALE_PLAN,
        project_dir=project_dir,
        rejection_reasons=[
            "replace_in_file old text not found in workspace in steps [2, 3]",
            (
                "step 2 replace_in_file old text not found in src/medium_cli/cli.py. "
                "Use exact text from current file excerpt or choose a different operation. "
                f"Current file excerpt: {cli_excerpt}"
            ),
            "stale_replace_ops_steps: use identifiers from current file excerpt",
        ],
        prompt_profile="local_qwen_json_array",
    )
    assert len(prompt) <= repair_prompts.REPAIR_PROMPT_MAX_CHARS, (
        f"E26 medium_cli stale_replace prompt is {len(prompt)} chars "
        f"(budget: {repair_prompts.REPAIR_PROMPT_MAX_CHARS}). "
        "Multi-file preservation language may have exceeded budget."
    )
    assert "Return 3 steps" not in prompt
    assert "plan corruption" in prompt
