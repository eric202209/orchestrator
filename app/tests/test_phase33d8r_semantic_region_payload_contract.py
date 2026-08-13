"""Phase 33D-8R semantic selector/payload contract regressions."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.candidate_checks import (
    validate_candidate_delta,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService


PATH = "app/services/project/lifecycle.py"
SOURCE = '''"""Lifecycle helpers."""

from app.models import Project

# A surrounding comment must survive a semantic replacement.


def before() -> str:
    return "before"


def is_project_retired(project: Project) -> bool:
    return project.status == "retired"


def after() -> str:
    return "after"
'''


def _materialize(root: Path, task: str, source: str = SOURCE):
    target = root / PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8", newline="")
    return materialize_planner_source_context(
        root, task_description=task, expected_paths=[PATH]
    )


def _plan(target_id: str, replacement: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Replace the exact selected source region.",
            "commands": [],
            "verification": f"python -m py_compile {PATH}",
            "rollback": None,
            "expected_files": [PATH],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": PATH,
                    "target_id": target_id,
                    "new": replacement,
                }
            ],
        }
    ]


def test_attempt18_function_definition_hint_is_ineligible_without_function_bounds(
    tmp_path,
):
    materialization = _materialize(
        tmp_path,
        f"Replace the bounded implementation beginning with "
        f"`def is_project_retired(project: Project) -> bool:` in {PATH}.",
    )
    item = materialization.file_map()[PATH]

    assert item.target_match_count == 1
    assert item.target_hint_type == "symbol"
    assert item.target_match_end - item.target_match_start == len(
        b"def is_project_retired(project: Project) -> bool:"
    )
    assert len(item.content.encode("utf-8")) == len(SOURCE.encode("utf-8"))
    assert build_semantic_target_inventory(materialization).handles == ()


def test_exact_snippet_selector_replaces_only_exact_region_and_preserves_surroundings(
    tmp_path,
):
    old = 'return project.status == "retired"'
    new = 'return project.status == "archived"'
    materialization = _materialize(tmp_path, f"Replace `{old}` in {PATH}.")
    inventory = build_semantic_target_inventory(materialization)

    assert len(inventory.handles) == 1
    normalized = normalize_provider_semantic_intents(
        _plan(inventory.handles[0].target_id, new),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    operation = normalized[0]["ops"][0]
    selector = SourceRegionIdentity.from_dict(operation["selector"])
    source_bytes = SOURCE.encode("utf-8")
    start = source_bytes.index(old.encode("utf-8"))
    end = start + len(old.encode("utf-8"))

    assert (selector.start_byte, selector.end_byte) == (start, end)
    assert source_bytes[:start] == (tmp_path / PATH).read_bytes()[:start]
    assert source_bytes[end:] == (tmp_path / PATH).read_bytes()[end:]

    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt=f"Replace `{old}` in {PATH}.",
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

    expected = SOURCE.replace(old, new, 1)
    actual = (tmp_path / PATH).read_text(encoding="utf-8")
    assert result["success"] is True, result
    assert actual == expected
    assert actual.startswith(SOURCE[:start])
    assert actual.endswith(SOURCE[end:])
    assert "from app.models import Project" in actual
    assert "def before" in actual and "def after" in actual
    assert "# A surrounding comment" in actual
    assert actual.endswith("\n")

    candidate = validate_candidate_delta(
        project_dir=tmp_path,
        change_set={
            "added_files": [],
            "modified_files": [PATH],
            "deleted_files": [],
        },
        plan=normalized,
        task_prompt=f"Replace `{old}` in {PATH}.",
    )
    assert candidate.findings == ()
    assert any("black --check" in command for command in candidate.commands_run)
    assert any("flake8" in command for command in candidate.commands_run)


def test_exact_snippet_byte_boundaries_preserve_unicode_crlf_and_final_newline(
    tmp_path,
):
    old = 'return project.status == "retired"'
    new = 'return project.status == "archived"'
    source = (
        "# préface\r\n"
        "from app.models import Project\r\n\r\n"
        "def is_project_retired(project: Project) -> bool:\r\n"
        f"    {old}\r\n\r\n"
        "# suffix\r\n"
    )
    materialization = _materialize(tmp_path, f"Replace `{old}` in {PATH}.", source)
    inventory = build_semantic_target_inventory(materialization)
    normalized = normalize_provider_semantic_intents(
        _plan(inventory.handles[0].target_id, new),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    selector = SourceRegionIdentity.from_dict(normalized[0]["ops"][0]["selector"])
    source_bytes = source.encode("utf-8")
    start = source_bytes.index(old.encode("utf-8"))
    end = start + len(old.encode("utf-8"))
    assert (selector.start_byte, selector.end_byte) == (start, end)

    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt=f"Replace `{old}` in {PATH}.",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    result = ExecutorService.execute_file_ops(
        tmp_path,
        normalized[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )
    actual = (tmp_path / PATH).read_bytes()
    assert result["success"] is True, result
    assert actual == source_bytes.replace(old.encode(), new.encode(), 1)
    assert actual.startswith("# préface\r\n".encode("utf-8"))
    assert actual.endswith(b"# suffix\r\n")
