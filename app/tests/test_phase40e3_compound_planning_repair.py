"""Offline regressions for compound Planning validator repair routing."""

from __future__ import annotations

import json

from app.services.orchestration.planning.repair_prompts import (
    build_compact_planning_repair_prompt,
)


def _replace_plan() -> str:
    return json.dumps(
        [
            {
                "step_number": 1,
                "description": "Update the existing function",
                "commands": ["python -m pytest -q tests/test_sample.py"],
                "verification": "python -m pytest -q tests/test_sample.py",
                "expected_files": ["pkg/sample.py"],
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "pkg/sample.py",
                        "old": "def current():\n    return 1\n",
                        "new": "def current():\n    return 2\n",
                    }
                ],
            }
        ]
    )


def test_compound_stale_syntax_and_command_failures_use_general_repair():
    rejection_reasons = [
        "Plan writes Python source with invalid syntax "
        "(python_source_syntax_invalid; pkg/sample.py line 1)",
        "stale_replace: replace_in_file.old_text is absent from the current source",
        "unsupported_step_command: structured runtime has no command tools",
    ]
    source_context = (
        "## EXECUTION CAPABILITY CONTRACT\n"
        "ops are limited to append_file, replace_in_file, and write_file; "
        "structured runtime has no command tools.\n"
        "## CURRENT SOURCE MATERIALIZATION\n"
        "pkg/sample.py: complete current source, hash=source-marker\n"
        "Legacy replace_in_file may use exact old/new from the supplied current source evidence."
    )

    prompt = build_compact_planning_repair_prompt(
        malformed_output=_replace_plan(),
        rejection_reasons=rejection_reasons,
        guidance_block=source_context,
        task_description="Fix the function while preserving its module and tests.",
    )

    assert "Stale replace repair mode." not in prompt
    assert "Compound validator repair" in prompt
    assert "correct every listed rejection" in prompt
    assert "python_source_syntax_invalid" in prompt
    assert "stale_replace" in prompt
    assert "unsupported_step_command" in prompt
    assert "Fix the function while preserving its module and tests." in prompt
    assert "source-marker" in prompt
    assert "execution capability" in prompt.lower()


def test_stale_replace_only_failure_keeps_specialized_repair():
    prompt = build_compact_planning_repair_prompt(
        malformed_output=_replace_plan(),
        rejection_reasons=[
            "stale_replace: replace_in_file.old_text is absent from the current source",
            "step 1 replace_in_file old text not found in pkg/sample.py",
        ],
        task_description="Make the requested localized function change.",
    )

    assert "Stale replace repair mode." in prompt
    assert "Correct only the rejected stale target operation" in prompt
    assert "Compound validator repair" not in prompt


def test_stale_marker_without_validator_reasons_does_not_claim_stale_only():
    prompt = build_compact_planning_repair_prompt(
        malformed_output=f"{_replace_plan()}\nstale_replace: evidence only",
        rejection_reasons=[],
        task_description="Make the requested localized function change.",
    )

    assert "Stale replace repair mode." not in prompt
    assert "Compound validator repair" not in prompt
