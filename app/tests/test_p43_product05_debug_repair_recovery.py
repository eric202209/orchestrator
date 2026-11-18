"""P43-PRODUCT-05 provider-free Debug Repair recovery controls."""

from pathlib import Path

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt_with_metadata,
    normalize_bounded_debug_repair_payload_detailed,
)


FAILURE_OUTPUT = """F..F..\n
_____________________ test_final_partial_page_is_returned ______________________

tests/test_pagination_regression.py:12: AssertionError
E       assert [8, 9] == [9]

____________________ test_empty_sequence_has_one_empty_page ____________________

tests/test_pagination_regression.py:34: ValueError
pagekit/paging.py:35: ValueError
"""


def _write_pagekit_fixture(project_dir: Path) -> None:
    (project_dir / "pagekit").mkdir()
    (project_dir / "tests").mkdir()
    (project_dir / "pagekit" / "__init__.py").write_text(
        "from pagekit.paging import Page, paginate\n",
        encoding="utf-8",
    )
    (project_dir / "pagekit" / "paging.py").write_text(
        "from typing import Any, Sequence\n\n"
        "def paginate(items: Sequence[Any], number: int, per_page: int):\n"
        "    return items\n",
        encoding="utf-8",
    )
    (project_dir / "tests" / "test_pagination_regression.py").write_text(
        "from pagekit import paginate\n\n"
        "def test_final_partial_page_is_returned():\n"
        "    assert paginate(list(range(10)), 3, 4).items == [9]\n",
        encoding="utf-8",
    )


def _envelope(project_dir: Path) -> DebugFeedbackEnvelope:
    return DebugFeedbackEnvelope(
        task_execution_id=404,
        task_id=314,
        step_index=3,
        failure_phase="execution",
        failed_command="python3 -m pytest tests/test_pagination_regression.py -q",
        stdout_excerpt=FAILURE_OUTPUT,
        stderr_excerpt=FAILURE_OUTPUT,
        pytest_excerpt=FAILURE_OUTPUT,
        changed_files=[],
        expected_files=[],
        workspace_path=str(project_dir),
        failure_class="pytest_failure",
    )


def test_debug_prompt_prefers_prior_repaired_source_over_package_root_target(
    tmp_path,
):
    _write_pagekit_fixture(tmp_path)
    result = build_bounded_debug_repair_prompt_with_metadata(
        _envelope(tmp_path),
        prior_source_paths=["pagekit/paging.py"],
    )

    contract_start = result.prompt.index("Debug source contract:")
    contract_end = result.prompt.index(
        "Source-context structured repair contract:", contract_start
    )
    contract = result.prompt[contract_start:contract_end]
    assert "- pagekit/paging.py" in contract
    assert "- pagekit/__init__.py" not in contract


def test_source_correction_is_admissible_but_test_correction_is_not(
    tmp_path,
):
    _write_pagekit_fixture(tmp_path)
    envelope = _envelope(tmp_path)
    source_op = {
        "op": "replace_in_file",
        "path": "pagekit/paging.py",
        "old": "    return items",
        "new": "    return list(items)",
    }
    test_op = {
        "op": "replace_in_file",
        "path": "tests/test_pagination_regression.py",
        "old": "== [9]",
        "new": "== [8, 9]",
    }

    source_only = normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "repair_type": "ops_fix",
                "ops": [source_op],
                "verification_command": "python3 -m pytest -q",
            }
        ],
        envelope=envelope,
        source_edit_context=True,
    )
    combined = normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "repair_type": "ops_fix",
                "ops": [source_op, test_op],
                "verification_command": "python3 -m pytest -q",
            }
        ],
        envelope=envelope,
        source_edit_context=True,
    )

    assert source_only.payload is not None
    assert combined.payload is None
    assert combined.rejection_reason == "unauthorized_source_path"
