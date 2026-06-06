"""
Fix B Class 1: verification-promoted assertion gate

_looks_like_safe_verification_command must not promote python -c assert forms
from the verification field into commands[].

Pattern being fixed:
  {
    "commands": [],
    "ops": [{"op": "replace_in_file", ...}],
    "verification": "python3 -c \"from module import fn; assert fn(x) == y\""
  }

Before fix: planner promotes verification into commands → validator flags brittle_inline_python.
After fix:  commands stays [], ops remain; validator sees no brittle command.

Contract: if the model places python -c assert directly in commands[], the validator
still rejects it — this fix only closes the promotion path.
"""

from __future__ import annotations

import json

import pytest

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.validator import ValidatorService


# ---------------------------------------------------------------------------
# 1. Promotion gate unit tests
# ---------------------------------------------------------------------------


def test_python_c_assert_form_is_not_promoted():
    cmd = "python3 -c \"from src.medium_cli.formatting import format_summary; assert format_summary(3, 2) == '3 tasks, 2 complete'\""
    assert PlannerService._looks_like_safe_verification_command(cmd) is False


def test_python_c_assert_with_multiple_statements_is_not_promoted():
    cmd = (
        'python3 -c "from src.medium_cli.store import TaskStore; '
        "s = TaskStore(); s.add('a', completed=True); s.add('b'); "
        "assert s.summary() == (2, 1); print('store.summary OK')\""
    )
    assert PlannerService._looks_like_safe_verification_command(cmd) is False


def test_python_c_assert_path_exists_is_not_promoted():
    cmd = "python3 -c \"from pathlib import Path; assert Path('notes.txt').exists()\""
    assert PlannerService._looks_like_safe_verification_command(cmd) is False


def test_python2_c_assert_is_not_promoted():
    cmd = 'python -c "from src.module import fn; assert fn(1) == 2"'
    assert PlannerService._looks_like_safe_verification_command(cmd) is False


def test_python_c_print_form_is_still_promoted():
    # print-based verification (no assert) must continue to be promoted
    cmd = (
        'python -c "import pathlib,sys; '
        "sys.exit(0 if 'Reliability Smoke 2' in pathlib.Path('README.md').read_text() else 1)\""
    )
    assert PlannerService._looks_like_safe_verification_command(cmd) is True


def test_python_m_pytest_is_still_promoted():
    assert (
        PlannerService._looks_like_safe_verification_command("python3 -m pytest -q")
        is True
    )


def test_pytest_is_still_promoted():
    assert PlannerService._looks_like_safe_verification_command("pytest tests/") is True


def test_npm_test_is_still_promoted():
    assert PlannerService._looks_like_safe_verification_command("npm test") is True


def test_assert_in_variable_name_does_not_block_promotion():
    # "assertTrue" is NOT the assert keyword; \bassert\s requires a following space
    cmd = 'python3 -c "import unittest; unittest.TestCase().assertTrue(True)"'
    assert PlannerService._looks_like_safe_verification_command(cmd) is True


# ---------------------------------------------------------------------------
# 2. Validator integration: ops-bearing step with assertion verification is clean
# ---------------------------------------------------------------------------


def test_ops_step_with_assertion_verification_passes_validation(tmp_path):
    """
    replace_in_file + assert verification → no brittle, no missing_commands.
    This is the medium_cli brittle pattern (sessions 533, 534, 536, 537).
    """
    plan = [
        {
            "step_number": 1,
            "description": "Implement format_summary",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/formatting.py",
                    "old": "raise NotImplementedError",
                    "new": 'return f"{total} tasks, {completed} complete"',
                }
            ],
            "verification": (
                'python3 -c "from src.medium_cli.formatting import format_summary; '
                "assert format_summary(3, 2) == '3 tasks, 2 complete'\""
            ),
            "expected_files": ["src/medium_cli/formatting.py"],
        },
        {
            "step_number": 2,
            "description": "Run tests",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Implement format_summary for medium CLI",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "brittle_inline_python" not in verdict.details.get(
        "brittle_command_subcodes", []
    )
    assert (
        "Plan contains brittle heredoc-heavy or malformed commands"
        not in verdict.reasons
    )
    assert 1 not in verdict.details.get("missing_commands_steps", [])


def test_multiple_ops_steps_with_assertion_verifications_all_clean(tmp_path):
    """Three-step plan where each step has replace_in_file + assert verification."""
    plan = [
        {
            "step_number": 1,
            "description": "Implement store.summary",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/store.py",
                    "old": "raise NotImplementedError",
                    "new": "return (len(self._tasks), len(self.completed()))",
                }
            ],
            "verification": (
                'python3 -c "from src.medium_cli.store import TaskStore; '
                "s = TaskStore(); s.add('a', completed=True); s.add('b'); "
                'assert s.summary() == (2, 1)"'
            ),
            "expected_files": ["src/medium_cli/store.py"],
        },
        {
            "step_number": 2,
            "description": "Implement format_summary",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/formatting.py",
                    "old": "raise NotImplementedError",
                    "new": 'return f"{total} tasks, {completed} complete"',
                }
            ],
            "verification": (
                'python3 -c "from src.medium_cli.formatting import format_summary; '
                "assert format_summary(3, 2) == '3 tasks, 2 complete'\""
            ),
            "expected_files": ["src/medium_cli/formatting.py"],
        },
        {
            "step_number": 3,
            "description": "Run full test suite",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Implement medium CLI summary feature",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "brittle_inline_python" not in verdict.details.get(
        "brittle_command_subcodes", []
    )
    assert (
        "Plan contains brittle heredoc-heavy or malformed commands"
        not in verdict.reasons
    )
    # All three steps must have no missing_commands
    missing = verdict.details.get("missing_commands_steps", [])
    assert 1 not in missing
    assert 2 not in missing


# ---------------------------------------------------------------------------
# 3. Validator still rejects python -c assert placed directly in commands[]
# ---------------------------------------------------------------------------


def test_assert_directly_in_commands_still_triggers_brittle(tmp_path):
    """
    The fix only closes the promotion path.
    assert placed directly in commands[] must still be rejected as brittle.
    """
    plan = [
        {
            "step_number": 1,
            "description": "Implement and verify inline",
            "commands": [
                'python3 -c "from src.medium_cli.formatting import format_summary; '
                "assert format_summary(3, 2) == '3 tasks, 2 complete'\""
            ],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/formatting.py",
                    "old": "raise NotImplementedError",
                    "new": 'return f"{total} tasks, {completed} complete"',
                }
            ],
            "verification": "python3 -m pytest -q",
            "expected_files": ["src/medium_cli/formatting.py"],
        },
        {
            "step_number": 2,
            "description": "Run tests",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Implement medium CLI summary feature",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "brittle_inline_python" in verdict.details.get(
        "brittle_command_subcodes", []
    )
    assert (
        "Plan contains brittle heredoc-heavy or malformed commands" in verdict.reasons
    )


# ---------------------------------------------------------------------------
# 4. Verification field remains present after fix (not stripped)
# ---------------------------------------------------------------------------


def test_verification_field_preserved_after_promotion_gate(tmp_path):
    """
    The fix prevents promotion; it does not strip the verification field.
    The step's verification annotation stays visible for diagnostics.
    """
    plan = [
        {
            "step_number": 1,
            "description": "Implement store.summary",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/store.py",
                    "old": "raise NotImplementedError",
                    "new": "return (len(self._tasks), len(self.completed()))",
                }
            ],
            "verification": (
                'python3 -c "from src.medium_cli.store import TaskStore; '
                's = TaskStore(); assert s.summary() == (0, 0)"'
            ),
            "expected_files": ["src/medium_cli/store.py"],
        },
        {
            "step_number": 2,
            "description": "Run tests",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
        },
    ]

    # The plan must not be flagged for missing_verification_steps on step 1
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Implement medium CLI summary feature",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert 1 not in verdict.details.get("missing_verification_steps", [])
