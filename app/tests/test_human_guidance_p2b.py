"""Tests for HG-P2b — guidance-aware planning validation."""

from __future__ import annotations

from typing import Dict, List

import pytest

from app.services.human_guidance.plan_validator import (
    _extract_plan_write_content,
    validate_plan_against_guidance,
)


def _make_write_step(content: str, step_number: int = 1) -> Dict:
    return {
        "step_number": step_number,
        "description": f"step {step_number}",
        "ops": [{"op": "write_file", "path": "foo.py", "content": content}],
        "commands": [],
    }


def _make_replace_step(new_text: str, step_number: int = 1) -> Dict:
    return {
        "step_number": step_number,
        "description": f"step {step_number}",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "foo.py",
                "old": "old text",
                "new": new_text,
            }
        ],
        "commands": [],
    }


def _guidance(msg: str, gid: int = 1) -> Dict:
    return {"id": gid, "message": msg, "scope": "project"}


# --- _extract_plan_write_content ---


def test_extract_empty_plan():
    assert _extract_plan_write_content([]) == ""


def test_extract_write_file_content():
    steps = [_make_write_step("def foo(x = []):\n    pass")]
    content = _extract_plan_write_content(steps)
    assert "def foo" in content
    assert "x = []" in content


def test_extract_replace_in_file_new():
    steps = [_make_replace_step("x = None\n")]
    content = _extract_plan_write_content(steps)
    assert "x = None" in content


def test_extract_skips_replace_old_text():
    step = {
        "step_number": 1,
        "description": "replace",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "foo.py",
                "old": "x = []  # old mutable default",
                "new": "x = None",
            }
        ],
        "commands": [],
    }
    content = _extract_plan_write_content([step])
    # old text is NOT extracted; new text IS
    assert "x = None" in content
    assert "old mutable default" not in content


def test_extract_multiple_steps():
    steps = [
        _make_write_step("def a(): pass", step_number=1),
        _make_write_step("def b(): pass", step_number=2),
    ]
    content = _extract_plan_write_content(steps)
    assert "def a" in content
    assert "def b" in content


# --- validate_plan_against_guidance: mutable_default ---


def test_mutable_default_violation_detected():
    steps = [_make_write_step("def add(x: list = []) -> list:\n    pass")]
    guidance = [_guidance("Never use mutable default arguments. Use None instead.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert len(violations) == 1
    assert "mutable_default" in violations[0]
    assert "= []" in violations[0]


def test_mutable_default_dict_violation_detected():
    steps = [_make_write_step("def fn(opts = {}) -> None:\n    pass")]
    guidance = [_guidance("Use None and initialize inside. Mutable default forbidden.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert len(violations) == 1
    assert "mutable_default" in violations[0]


def test_mutable_default_compliant_none_default():
    # Compliant: None default, body initialises with list() (not = [])
    steps = [
        _make_write_step(
            "def add(x=None):\n    if x is None:\n        x = list()\n    return x"
        )
    ]
    guidance = [_guidance("Never use mutable default arguments. Use None instead.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert violations == []


# --- validate_plan_against_guidance: stdout_vs_logging ---


def test_stdout_violation_import_logging():
    steps = [_make_write_step("import logging\nlogger = logging.getLogger(__name__)")]
    guidance = [_guidance("All runtime output must go to stdout. Never use logging.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert len(violations) >= 1
    assert "stdout_vs_logging" in violations[0]


def test_stdout_violation_getLogger():
    steps = [_make_write_step("logger = logging.getLogger(__name__)\nlogger.info('x')")]
    guidance = [_guidance("Use print() for runtime reporting. Never use logging.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert any("stdout_vs_logging" in v for v in violations)


def test_stdout_compliant_print():
    steps = [_make_write_step("def report(msg):\n    print(msg)")]
    guidance = [_guidance("All output to stdout. Use print(). Never use logging.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert violations == []


# --- guidance not matching pattern ---


def test_unrelated_guidance_no_violation():
    steps = [_make_write_step("def add(x = []) -> list:\n    pass")]
    guidance = [_guidance("Always add type hints to public functions.")]
    violations = validate_plan_against_guidance(steps, guidance)
    assert violations == []


def test_empty_plan_no_violation():
    guidance = [_guidance("Never use mutable default arguments.")]
    violations = validate_plan_against_guidance([], guidance)
    assert violations == []


def test_empty_guidance_no_violation():
    steps = [_make_write_step("def add(x = []):\n    pass")]
    violations = validate_plan_against_guidance(steps, [])
    assert violations == []


# --- multiple guidance entries ---


def test_multiple_violations_multiple_guidance():
    steps = [
        _make_write_step(
            "import logging\ndef add(x = []):\n    logger = logging.getLogger(__name__)\n    pass"
        )
    ]
    guidance = [
        _guidance("Never use mutable default arguments. Use None.", gid=1),
        _guidance("All output to stdout. Never use logging.", gid=2),
    ]
    violations = validate_plan_against_guidance(steps, guidance)
    patterns = [v.split(":")[0] for v in violations]
    assert "mutable_default" in patterns
    assert "stdout_vs_logging" in patterns


def test_violation_message_contains_guidance_text():
    steps = [_make_write_step("def fn(items = []):\n    pass")]
    guidance_msg = (
        "Never use mutable default arguments. Use None and initialize inside."
    )
    guidance = [_guidance(guidance_msg)]
    violations = validate_plan_against_guidance(steps, guidance)
    assert len(violations) == 1
    assert guidance_msg in violations[0]


# --- replace_in_file new text is scanned ---


def test_replace_in_file_new_text_scanned():
    step = _make_replace_step("def fn(items = []):\n    pass")
    guidance = [_guidance("Never use mutable default arguments. Use None.")]
    violations = validate_plan_against_guidance([step], guidance)
    assert len(violations) == 1
    assert "mutable_default" in violations[0]


# --- plan with no write ops ---


def test_plan_with_only_commands_no_violation():
    step = {
        "step_number": 1,
        "description": "run tests",
        "ops": [],
        "commands": ["pytest"],
    }
    guidance = [_guidance("Never use mutable default arguments.")]
    violations = validate_plan_against_guidance([step], guidance)
    assert violations == []
