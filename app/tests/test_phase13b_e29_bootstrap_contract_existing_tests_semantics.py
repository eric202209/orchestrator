"""Phase 13B-E29: Bootstrap Contract Existing-Tests Semantics Fix tests.

Verifies the two-part semantic split in task_bootstrap_contract.py:

  - existing_project_tests_present: existing tests are verification assets;
    no test file materialization required.
  - explicit_code_test_intent (even when existing tests are present): task
    explicitly requests new tests; test file materialization still required.

See: docs/roadmap/phase13b-e28-bootstrap-contract-conflict-design-analysis.md
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.orchestration.planning.task_bootstrap_contract import (
    BootstrapTaskType,
    EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT,
    EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT,
    _expected_test_reason,
    _has_explicit_new_test_writing_intent,
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.validation.validator import ValidatorService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    *,
    ops: list[dict[str, Any]] | None = None,
    commands: list[str] | None = None,
    verification: str | None = "python3 -m pytest -q",
    expected_files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "step_number": 1,
        "description": "Implementation step",
        "commands": commands if commands is not None else [],
        "verification": verification,
        "rollback": None,
        "expected_files": expected_files if expected_files is not None else [],
        "ops": ops if ops is not None else [],
    }


def _write_source_op(path: str = "src/money/money.py") -> dict[str, Any]:
    return {
        "op": "write_file",
        "path": path,
        "content": (
            "def format_amount(value: float) -> str:\n" "    return f'${value:.2f}'\n"
        ),
    }


# ---------------------------------------------------------------------------
# Case 1: Existing-test project + source-fix + source ops + pytest → PASS
# ---------------------------------------------------------------------------


def test_existing_tests_source_fix_with_verification_passes(tmp_path):
    """E29 Case 1: source-fix task on existing-test project must PASS when plan
    has source ops + pytest verification (no test file ops required)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text(
        "from money.money import format_amount\n\n"
        "def test_format_dollar():\n"
        "    assert format_amount(1.5) == '$1.50'\n"
    )

    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[_write_source_op()],
                expected_files=["src/money/money.py"],
                verification="python3 -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt=(
            "Fix the existing money formatter in src/money/money.py so the "
            "existing tests pass. Edit only that source file. "
            "Do not create new files. Do not edit tests. "
            "Verify with python3 -m pytest -q."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert (
        verdict.accepted
    ), f"Expected PASS but got violations: {contract['violation_codes']}"
    assert (
        contract["expected_test_reason"]
        == EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT
    )
    assert (
        "task1_bootstrap_missing_expected_test_files" not in contract["violation_codes"]
    )


# ---------------------------------------------------------------------------
# Case 2: Existing-test project + source-fix + no verification → FAIL on
#         verification, NOT on test-file materialization
# ---------------------------------------------------------------------------


def test_existing_tests_source_fix_no_verification_fails_on_verification_only(tmp_path):
    """E29 Case 2: missing verification must be the failure cause, not missing
    test file ops, for source-fix tasks on existing-test projects."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text(
        "def test_placeholder():\n    assert True\n"
    )

    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[_write_source_op()],
                expected_files=["src/money/money.py"],
                verification=None,
            )
        ],
        output_text="[]",
        task_prompt=(
            "Fix the existing money formatter in src/money/money.py so the "
            "existing tests pass. Edit only that source file."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert (
        "task1_bootstrap_missing_required_verification" in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_missing_expected_test_files" not in contract["violation_codes"]
    )


# ---------------------------------------------------------------------------
# Case 3: Existing-test project + explicit test-writing task + no test ops
#         → FAIL with task1_bootstrap_missing_expected_test_files
# ---------------------------------------------------------------------------


def test_existing_tests_explicit_test_intent_still_requires_test_materialization(
    tmp_path,
):
    """E29 Case 3: when the task explicitly asks for new tests AND existing tests
    are present, test file materialization is still required."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text(
        "def test_existing():\n    assert True\n"
    )

    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[_write_source_op("src/medium_cli/cli.py")],
                expected_files=["src/medium_cli/cli.py"],
                verification="python3 -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt=(
            "Add the summary command to this Python CLI with unit tests. "
            "Verify with python3 -m pytest -q."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert (
        contract["expected_test_reason"]
        == EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT
    )
    assert "task1_bootstrap_missing_expected_test_files" in contract["violation_codes"]


# ---------------------------------------------------------------------------
# Case 4: New project + explicit test-writing task + no test ops → FAIL
#         (unchanged behavior — no existing tests)
# ---------------------------------------------------------------------------


def test_new_project_explicit_test_intent_still_requires_test_materialization(tmp_path):
    """E29 Case 4: new project with explicit test-writing intent still requires
    test file ops (pre-existing behavior unchanged)."""
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[_write_source_op()],
                expected_files=["src/money/money.py"],
                verification="python3 -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt="Build the first slice with unit tests for the money formatter.",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert (
        contract["expected_test_reason"]
        == EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT
    )
    assert "task1_bootstrap_missing_expected_test_files" in contract["violation_codes"]


# ---------------------------------------------------------------------------
# Case 5: _expected_test_reason() — existing tests + explicit new-test intent
#         → EXPLICIT_CODE_TEST_INTENT
# ---------------------------------------------------------------------------


def test_expected_test_reason_existing_plus_explicit_writing_intent_returns_explicit():
    """E29 Case 5: prompt asking to write new tests ('with unit tests') overrides
    existing-tests reason and returns EXPLICIT_CODE_TEST_INTENT."""
    reason = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt="Implement the feature with unit tests.",
        all_paths={"src/app.py"},
        existing_files={"tests/test_existing.py"},
        source_candidates=["src/app.py"],
    )
    assert reason == EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT


def test_has_explicit_new_test_writing_intent_matches_write_verb_plus_tests():
    """E29 Case 5a: strict intent helper matches 'add tests', 'with unit tests', etc."""
    assert _has_explicit_new_test_writing_intent(
        "Implement the feature with unit tests."
    )
    assert _has_explicit_new_test_writing_intent("Add tests for the new parser module.")
    assert _has_explicit_new_test_writing_intent("Include test coverage for the CLI.")


# ---------------------------------------------------------------------------
# Case 6: _expected_test_reason() — existing tests + no explicit new-test intent
#         → EXISTING_PROJECT_TESTS_PRESENT
# ---------------------------------------------------------------------------


def test_expected_test_reason_existing_tests_source_fix_prompt_returns_existing():
    """E29 Case 6: source-fix prompts ('existing tests pass', 'verify with pytest')
    must NOT be misclassified as explicit new-test-writing intent."""
    # tiny_money-shaped prompt
    reason_money = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt=(
            "Fix the existing money formatter in src/money/money.py so the "
            "existing tests pass. Edit only that source file. "
            "Do not create new files. Do not edit tests. "
            "Verify with python3 -m pytest -q."
        ),
        all_paths={"src/money/money.py"},
        existing_files={"tests/test_money.py"},
        source_candidates=["src/money/money.py"],
    )
    assert reason_money == EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT

    # medium_cli-shaped prompt
    reason_cli = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt=(
            "Add the summary command to this Python CLI. Keep the change scoped "
            "to the existing src/ and tests/ files. "
            "Verify with python3 -m pytest -q."
        ),
        all_paths={"src/medium_cli/cli.py"},
        existing_files={
            "tests/test_cli.py",
            "tests/test_store.py",
            "tests/test_summary.py",
        },
        source_candidates=["src/medium_cli/cli.py"],
    )
    assert reason_cli == EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT


def test_has_explicit_new_test_writing_intent_rejects_verify_commands_and_existing_refs():
    """E29 Case 6a: strict intent helper must NOT fire for verification-command
    mentions of pytest or references to the existing test suite."""
    assert not _has_explicit_new_test_writing_intent(
        "Fix the formatter. Verify with python3 -m pytest -q."
    )
    assert not _has_explicit_new_test_writing_intent(
        "Fix the existing money formatter so the existing tests pass."
    )
    assert not _has_explicit_new_test_writing_intent(
        "Keep the change scoped to the existing src/ and tests/ files."
    )


# ---------------------------------------------------------------------------
# Case 7: Existing-test project + source-fix task must still require source
#         implementation evidence
# ---------------------------------------------------------------------------


def test_existing_tests_source_fix_still_requires_implementation_evidence(tmp_path):
    """E29 Case 7: suppressing test-file materialization for existing-test
    projects must not suppress source implementation evidence enforcement."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text(
        "def test_placeholder():\n    assert True\n"
    )

    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[],
                commands=["cat src/money/money.py"],
                expected_files=[],
                verification="python3 -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt=(
            "Fix the existing money formatter in src/money/money.py. "
            "Do not create new files."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_missing_expected_test_files" not in contract["violation_codes"]
    )
