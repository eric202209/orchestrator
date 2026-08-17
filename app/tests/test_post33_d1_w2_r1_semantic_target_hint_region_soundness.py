"""POST33-D1-W2-R1 semantic target-region soundness regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
    SemanticTargetContractError,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService


PATH = "app/schemas/pagination.py"
SOURCE = '''"""Shared pagination abstractions.

Every paginated endpoint uses Page[T] for the response envelope
and paginate() to execute the bounded SQL query.
"""

from typing import Any


def paginate(query: Any) -> dict:
    return {"items": query.all()}
'''

EXECUTABLE_SOURCE = '''"""Helpers for pagination."""


def paginate(query=None):
    return query


def run(query):
    return paginate()
'''

FUNCTION_DOCSTRING_SOURCE = '''def run():
    """Call paginate() from this function's documentation."""
    return True
'''

CLASS_DOCSTRING_SOURCE = '''class Runner:
    """Call paginate() from this class's documentation."""

    def run(self):
        return True
'''

COMMENT_SOURCE = """# Call paginate() from this source comment.


def run():
    return True
"""

REGULAR_STRING_SOURCE = """value = "paginate()"
"""


def _write_source(root: Path, source: str, *, relative_path: str = PATH) -> bytes:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = source.encode("utf-8")
    target.write_bytes(source_bytes)
    return source_bytes


def _materialize(
    root: Path,
    source: str,
    *,
    task: str,
    relative_path: str = PATH,
    expected_paths: list[str] | None = None,
    supporting_paths: list[str] | None = None,
):
    _write_source(root, source, relative_path=relative_path)
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=(
            expected_paths if expected_paths is not None else [relative_path]
        ),
        supporting_paths=(supporting_paths if supporting_paths is not None else []),
    )


def _task(relative_path: str = PATH, hint: str = "paginate()") -> str:
    return f"Replace the exact call `{hint}` in {relative_path}."


def _semantic_plan(target_id: str, *, new: str, relative_path: str = PATH):
    return [
        {
            "step_number": 1,
            "description": "Apply the requested bounded source change",
            "commands": [],
            "verification": f"python -m py_compile {relative_path}",
            "rollback": None,
            "expected_files": [relative_path],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": relative_path,
                    "target_id": target_id,
                    "new": new,
                }
            ],
        }
    ]


def test_unique_module_docstring_match_is_not_issued_as_semantic_target(
    tmp_path: Path,
):
    target = tmp_path / PATH
    target.parent.mkdir(parents=True)
    target.write_text(SOURCE, encoding="utf-8", newline="")

    task = f"Replace the exact call `paginate()` in {PATH}."
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=[PATH],
    )
    item = materialization.file_map()[PATH]
    inventory = build_semantic_target_inventory(materialization)
    source_bytes = SOURCE.encode("utf-8")
    selected_start = source_bytes.index(b"paginate()")
    selected_end = selected_start + len(b"paginate()")

    assert item.target_match_count == 1
    assert (item.target_match_start, item.target_match_end) == (
        selected_start,
        selected_end,
    )
    assert source_bytes[selected_start:selected_end] == b"paginate()"
    assert item.target_region_eligibility_reason == "python_module_docstring"
    assert inventory.handles == ()


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (FUNCTION_DOCSTRING_SOURCE, "python_function_docstring"),
        (CLASS_DOCSTRING_SOURCE, "python_class_docstring"),
        (COMMENT_SOURCE, "python_comment"),
    ],
)
def test_python_documentation_and_comment_matches_are_not_issued(
    tmp_path: Path, source: str, reason: str
):
    materialization = _materialize(tmp_path, source, task=_task())
    item = materialization.file_map()[PATH]

    assert item.target_match_count == 1
    assert item.target_region_eligibility_reason == reason
    assert build_semantic_target_inventory(materialization).handles == ()


def test_unique_executable_call_is_preserved_through_d5_d4_apa_and_d3(
    tmp_path: Path,
):
    materialization = _materialize(tmp_path, EXECUTABLE_SOURCE, task=_task())
    inventory = build_semantic_target_inventory(materialization)

    assert len(inventory.handles) == 1
    item = materialization.file_map()[PATH]
    assert item.target_match_count == 1
    assert item.target_region_eligibility_reason is None

    target_id = inventory.handles[0].target_id
    normalized = normalize_provider_semantic_intents(
        _semantic_plan(target_id, new="paginate(query)"),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    operation = normalized[0]["ops"][0]
    selector = SourceRegionIdentity.from_dict(operation["selector"])
    source_bytes = EXECUTABLE_SOURCE.encode("utf-8")
    start = source_bytes.index(b"paginate()")
    end = start + len(b"paginate()")
    assert (selector.start_byte, selector.end_byte) == (start, end)
    assert selector.selected_region_sha256

    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt=_task(),
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    authority = accepted_path_authority_from_verdict(verdict)
    assert verdict.accepted, verdict.reasons
    assert authority is not None

    result = ExecutorService.execute_file_ops(
        tmp_path, normalized[0]["ops"], accepted_path_authority=authority
    )
    assert result["success"] is True, result
    assert (tmp_path / PATH).read_text(encoding="utf-8") == EXECUTABLE_SOURCE.replace(
        "paginate()", "paginate(query)", 1
    )


def test_executable_expression_and_regular_string_literal_remain_eligible(
    tmp_path: Path,
):
    expression_source = "value = (paginate())\n"
    expression = _materialize(tmp_path, expression_source, task=_task())
    expression_item = expression.file_map()[PATH]
    assert expression_item.target_region_eligibility_reason is None
    assert len(build_semantic_target_inventory(expression).handles) == 1

    string = _materialize(tmp_path, REGULAR_STRING_SOURCE, task=_task())
    string_item = string.file_map()[PATH]
    assert string_item.target_region_eligibility_reason is None
    assert len(build_semantic_target_inventory(string).handles) == 1


def test_duplicate_executable_matches_remain_ambiguous(tmp_path: Path):
    source = "value = paginate()\nother = paginate()\n"
    materialization = _materialize(tmp_path, source, task=_task())
    item = materialization.file_map()[PATH]

    assert item.target_match_count == 2
    assert item.target_region_eligibility_reason is None
    assert build_semantic_target_inventory(materialization).handles == ()


def test_docstring_and_executable_duplicate_remains_ambiguous_without_searching(
    tmp_path: Path,
):
    source = '"""Documentation mentions paginate()."""\n\nvalue = paginate()\n'
    materialization = _materialize(tmp_path, source, task=_task())
    item = materialization.file_map()[PATH]

    assert item.target_match_count == 2
    assert item.target_region_eligibility_reason == "python_module_docstring"
    assert build_semantic_target_inventory(materialization).handles == ()


def test_markdown_call_hint_is_not_issued_as_executable_prose_target(
    tmp_path: Path,
):
    relative_path = "README.md"
    source = "This documentation mentions paginate().\n"
    materialization = _materialize(
        tmp_path,
        source,
        task=_task(relative_path),
        relative_path=relative_path,
    )
    item = materialization.file_map()[relative_path]

    assert item.target_match_count == 1
    assert (
        item.target_region_eligibility_reason == "documentation_prose_materialization"
    )
    assert build_semantic_target_inventory(materialization).handles == ()


def test_unicode_and_crlf_executable_region_keeps_exact_byte_identity(
    tmp_path: Path,
):
    source = "# préface\r\nvalue = paginate()\r\n"
    materialization = _materialize(tmp_path, source, task=_task())
    inventory = build_semantic_target_inventory(materialization)
    assert len(inventory.handles) == 1

    normalized = normalize_provider_semantic_intents(
        _semantic_plan(inventory.handles[0].target_id, new="page()"),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    selector = SourceRegionIdentity.from_dict(normalized[0]["ops"][0]["selector"])
    source_bytes = source.encode("utf-8")
    start = source_bytes.index(b"paginate()")
    end = start + len(b"paginate()")
    assert (selector.start_byte, selector.end_byte) == (start, end)
    assert source_bytes[start:end] == b"paginate()"


def test_readonly_out_of_scope_and_unsafe_targets_remain_fail_closed(
    tmp_path: Path,
):
    readonly = _materialize(
        tmp_path,
        EXECUTABLE_SOURCE,
        task="Replace the exact call `paginate()`.",
        expected_paths=[],
        supporting_paths=[PATH],
    )
    assert build_semantic_target_inventory(readonly).handles == ()

    in_scope = _materialize(tmp_path, EXECUTABLE_SOURCE, task=_task())
    assert (
        build_semantic_target_inventory(in_scope, task_scope=["other.py"]).handles == ()
    )

    outside = tmp_path / "outside.py"
    outside.write_text(EXECUTABLE_SOURCE, encoding="utf-8")
    target = tmp_path / PATH
    target.unlink()
    target.symlink_to(outside)
    unsafe = materialize_planner_source_context(
        tmp_path,
        task_description=_task(),
        expected_paths=[PATH],
    )
    assert build_semantic_target_inventory(unsafe).handles == ()


def test_stale_source_version_remains_fail_closed(tmp_path: Path):
    materialization = _materialize(tmp_path, EXECUTABLE_SOURCE, task=_task())
    inventory = build_semantic_target_inventory(materialization)
    assert len(inventory.handles) == 1
    (tmp_path / PATH).write_bytes(EXECUTABLE_SOURCE.replace("query", "value").encode())

    with pytest.raises(SemanticTargetContractError, match="version_mismatch"):
        normalize_provider_semantic_intents(
            _semantic_plan(inventory.handles[0].target_id, new="page()"),
            inventory=inventory,
            project_dir=tmp_path,
            source_materialization=materialization,
        )
