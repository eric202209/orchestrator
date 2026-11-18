"""PHASE34-SMI1 — existing-source materialization boundary evidence tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import (
    ValidatorService,
    _source_operation_contract_issues,
)


SOURCE_PATH = "src/greeting/formatting.py"
TEST_PATH = "tests/test_formatting.py"
SOURCE = (
    "def format_customer_name(value: str) -> str:\n"
    '    """Normalize surrounding and repeated whitespace in a display name."""\n'
    "    return value\n"
)
TEST = "from src.greeting.formatting import format_customer_name\n"
TASK = "Normalize customer names and add regression coverage."


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "src" / "greeting").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / SOURCE_PATH).write_text(SOURCE, encoding="utf-8")
    (tmp_path / TEST_PATH).write_text(TEST, encoding="utf-8")
    return tmp_path


def _semantic_operation(project_dir: Path) -> dict:
    source = (project_dir / SOURCE_PATH).read_bytes()
    record = materialize_planner_source_context(
        project_dir, task_description=TASK, expected_paths=[SOURCE_PATH]
    ).file_map()[SOURCE_PATH]
    selector = SourceRegionIdentity.from_region(
        canonical_path=SOURCE_PATH,
        expected_source_version=record.version_identity,
        start_byte=0,
        end_byte=len(source),
        selected_region_sha256=hashlib.sha256(source).hexdigest(),
    )
    return {
        "op": "replace_in_file",
        "path": SOURCE_PATH,
        "selector": selector.to_dict(),
        "new": SOURCE.replace("    return value", '    return "normalized"'),
    }


def _plan(operations: list[dict]) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply grounded source and test changes",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [SOURCE_PATH, TEST_PATH],
            "ops": operations,
        }
    ]


def _issues(project_dir: Path, plan: list[dict], materialization) -> dict:
    return _source_operation_contract_issues(
        plan,
        task_text=TASK,
        project_dir=project_dir,
        source_materialization=materialization,
    )


def test_case_b_shaped_semantic_source_passes_but_unmaterialized_test_append_fails(
    tmp_path,
):
    project_dir = _workspace(tmp_path)
    source_materialization = materialize_planner_source_context(
        project_dir, task_description=TASK, expected_paths=[SOURCE_PATH]
    )
    source_operation = _semantic_operation(project_dir)
    test_append = {"op": "append_file", "path": TEST_PATH, "content": "\n"}

    semantic_only = ValidatorService.validate_plan(
        _plan([source_operation]),
        output_text=json.dumps(_plan([source_operation])),
        task_prompt=TASK,
        execution_profile="implementation",
        project_dir=project_dir,
        source_materialization=source_materialization,
    )
    # P43-PRODUCT-01: TASK asks for regression coverage, so the source-only
    # Plan is repairable for the missing test deliverable; the semantic source
    # operation itself still raises no source-materialization finding.
    assert semantic_only.repairable, semantic_only.reasons
    assert all(
        reason.startswith("Task explicitly requested regression or new tests")
        for reason in semantic_only.reasons
    ), semantic_only.reasons
    assert (
        semantic_only.details["requested_test_deliverable"]["code"]
        == "requested_test_deliverable_missing"
    )

    issues = _issues(
        project_dir, _plan([source_operation, test_append]), source_materialization
    )
    assert issues["semantic_replace_contract_issues"] == []
    assert issues["semantic_replace_version_mismatches"] == []
    assert issues["missing_source_materialization"] == [
        "step 1 op 2 (tests/test_formatting.py)"
    ]


def test_exact_materialization_of_both_existing_targets_accepts_the_full_shape(
    tmp_path,
):
    project_dir = _workspace(tmp_path)
    source_materialization = materialize_planner_source_context(
        project_dir,
        task_description=TASK,
        expected_paths=[SOURCE_PATH, TEST_PATH],
    )
    plan = _plan(
        [
            _semantic_operation(project_dir),
            {"op": "append_file", "path": TEST_PATH, "content": "\n"},
        ]
    )

    issues = _issues(project_dir, plan, source_materialization)
    assert issues["missing_source_materialization"] == []
    assert issues["semantic_replace_contract_issues"] == []
    assert issues["semantic_replace_version_mismatches"] == []


def test_missing_or_wrong_source_evidence_fails_closed(tmp_path):
    project_dir = _workspace(tmp_path)
    source_operation = _semantic_operation(project_dir)
    empty_materialization = materialize_planner_source_context(
        project_dir,
        task_description=TASK,
        expected_paths=[],
        supporting_paths=[],
    )
    missing = _issues(project_dir, _plan([source_operation]), empty_materialization)
    assert missing["semantic_replace_contract_issues"]

    wrong_path_operation = {
        **source_operation,
        "path": "src/greeting/other.py",
    }
    wrong_path = _issues(
        project_dir,
        _plan([wrong_path_operation]),
        materialize_planner_source_context(
            project_dir, task_description=TASK, expected_paths=[SOURCE_PATH]
        ),
    )
    assert wrong_path["semantic_replace_contract_issues"]
