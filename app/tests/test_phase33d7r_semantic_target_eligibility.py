"""Phase 33D-7R provider-free semantic target eligibility proof."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    TARGET_HINT_ABSENT,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.path_authority import declare
from app.services.orchestration.validation.validator import ValidatorService


RELATIVE_PATH = "app/services/project/name_formatter.py"
ATTEMPT_17_TASK = (
    "Make billing/invoice_total become billing invoice total in " f"{RELATIVE_PATH}."
)
SUPPORTED_TASK = (
    'Replace the exact snippet `if "-" in text or "_" in text:` in ' f"{RELATIVE_PATH}."
)
SOURCE = '''"""Helpers for keeping display names human-readable."""

import re


def humanize_display_name(value: str) -> str:
    """Convert slug-like display names into space-separated names."""
    text = (value or "").strip()
    if not text:
        return text

    if " " in text:
        return re.sub(r"\\s+", " ", text).strip()

    if "-" in text or "_" in text:
        text = re.sub(r"[-_]+", " ", text)

    return re.sub(r"\\s+", " ", text).strip()
'''
REPLACED_SOURCE = SOURCE.replace(
    'if "-" in text or "_" in text:',
    'if "-" in text or "_" in text or "/" in text:',
)


def _write_source(root: Path, source: str = SOURCE) -> None:
    path = root / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _materialize(
    root: Path,
    *,
    task: str = SUPPORTED_TASK,
    source: str = SOURCE,
    expected_paths: list[str] | None = None,
    supporting_paths: list[str] | None = None,
):
    _write_source(root, source)
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=(
            expected_paths if expected_paths is not None else [RELATIVE_PATH]
        ),
        supporting_paths=(supporting_paths if supporting_paths is not None else []),
    )


def _step(operation: dict, *, path: str = RELATIVE_PATH) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the requested bounded source change",
            "commands": [],
            "verification": f"python -m py_compile {path}",
            "rollback": None,
            "expected_files": [path],
            "ops": [operation],
        }
    ]


def _semantic_response(target_id: str, *, path: str = RELATIVE_PATH) -> list[dict]:
    return _step(
        {
            "op": "replace_in_file",
            "path": path,
            "target_id": target_id,
            "new": 'if "-" in text or "_" in text or "/" in text:',
        },
        path=path,
    )


def _normalize(raw_plan, root: Path, materialization):
    inventory = build_semantic_target_inventory(materialization)
    return normalize_provider_semantic_intents(
        raw_plan,
        inventory=inventory,
        project_dir=root,
        source_materialization=materialization,
    )


def test_attempt17_shape_reproduces_zero_handles_at_first_eligibility_gate(
    tmp_path,
):
    materialization = _materialize(
        tmp_path,
        task=ATTEMPT_17_TASK,
        expected_paths=[RELATIVE_PATH],
    )
    item = materialization.files[0]
    inventory = build_semantic_target_inventory(materialization)

    eligibility_evidence = {
        "status_existing": item.status == SOURCE_STATUS_EXISTING,
        "expected": item.expected,
        "path_in_expected_scope": item.relative_path == RELATIVE_PATH,
        "version_lineage": bool(item.version_identity),
        "content_lineage": bool(item.content_hash),
        "primary_target_region": {
            "hint_status": item.target_hint_status,
            "match_count": item.target_match_count,
            "included": item.target_included,
            "span_count": len(item.spans),
        },
        "bounded_source": {
            "source_length": item.source_length,
            "included_source_bytes": item.included_source_bytes,
            "truncated": item.truncated,
        },
        "issued_target_handles": len(inventory.handles),
    }

    assert eligibility_evidence["status_existing"] is True
    assert eligibility_evidence["expected"] is True
    assert eligibility_evidence["path_in_expected_scope"] is True
    assert eligibility_evidence["version_lineage"] is True
    assert eligibility_evidence["content_lineage"] is True
    assert eligibility_evidence["primary_target_region"] == {
        "hint_status": TARGET_HINT_ABSENT,
        "match_count": 0,
        "included": False,
        "span_count": 1,
    }
    assert eligibility_evidence["bounded_source"] == {
        "source_length": len(SOURCE.encode("utf-8")),
        "included_source_bytes": len(SOURCE.encode("utf-8")),
        "truncated": False,
    }
    assert eligibility_evidence["issued_target_handles"] == 0


def test_supported_existing_python_replace_proves_full_semantic_pipeline_without_old(
    tmp_path,
):
    materialization = _materialize(tmp_path)
    inventory = build_semantic_target_inventory(materialization)
    assert len(inventory.handles) == 1
    target_id = inventory.handles[0].target_id
    assert (
        target_id
        == build_semantic_target_inventory(materialization).handles[0].target_id
    )

    normalized = _normalize(_semantic_response(target_id), tmp_path, materialization)
    operation = normalized[0]["ops"][0]
    assert set(operation) == {"op", "path", "selector", "new"}
    assert "old" not in operation
    selector = SourceRegionIdentity.from_dict(operation["selector"])
    assert selector.canonical_path.value == RELATIVE_PATH

    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt=SUPPORTED_TASK,
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    authority = accepted_path_authority_from_verdict(verdict)
    assert verdict.accepted, verdict.reasons
    assert authority is not None
    assert authority.grant_for(declare(RELATIVE_PATH)) is not None
    reloaded_authority = type(authority).from_dict(
        json.loads(json.dumps(authority.to_dict()))
    )
    assert reloaded_authority.authority_identity == authority.authority_identity

    reloaded_plan = json.loads(json.dumps(normalized))
    result = ExecutorService.execute_file_ops(
        tmp_path,
        reloaded_plan[0]["ops"],
        accepted_path_authority=reloaded_authority,
    )
    assert result["success"] is True, result
    assert result["files_changed"] == [RELATIVE_PATH]
    assert result["execution_mutation_artifacts"][0]["operation"] == "replace_in_file"
    assert (tmp_path / RELATIVE_PATH).read_text(encoding="utf-8") == REPLACED_SOURCE
    assert '"old"' not in json.dumps(reloaded_plan)


def test_target_eligibility_negative_controls_fail_closed(tmp_path):
    zero_target = _materialize(tmp_path, task=ATTEMPT_17_TASK)
    assert build_semantic_target_inventory(zero_target).handles == ()

    ambiguous_source = (
        SOURCE
        + "\n\ndef another_name(value: str) -> str:\n"
        + '    if "-" in text or "_" in text:\n'
        + "        return value\n"
    )
    ambiguous = _materialize(tmp_path, source=ambiguous_source)
    assert build_semantic_target_inventory(ambiguous).handles == ()

    readonly = _materialize(
        tmp_path,
        task="Replace `humanize_display_name`.",
        expected_paths=[],
        supporting_paths=[RELATIVE_PATH],
    )
    assert readonly.files[0].expected is False
    assert build_semantic_target_inventory(readonly).handles == ()

    outside = tmp_path / "outside.py"
    outside.write_text(SOURCE, encoding="utf-8")
    target = tmp_path / RELATIVE_PATH
    target.unlink()
    target.symlink_to(outside)
    unsafe = materialize_planner_source_context(
        tmp_path,
        task_description=SUPPORTED_TASK,
        expected_paths=[RELATIVE_PATH],
        supporting_paths=[],
    )
    assert build_semantic_target_inventory(unsafe).handles == ()

    in_scope = _materialize(tmp_path)
    assert (
        build_semantic_target_inventory(in_scope, task_scope=["other.py"]).handles == ()
    )


def test_invented_stale_mismatched_case_and_traversal_ids_fail_closed(tmp_path):
    materialization = _materialize(tmp_path)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id

    with pytest.raises(SemanticTargetContractError) as invented:
        _normalize(_semantic_response("tgt_invented"), tmp_path, materialization)
    assert invented.value.code == "unknown_target_id"

    (tmp_path / RELATIVE_PATH).write_text(SOURCE + "\n# changed\n", encoding="utf-8")
    newer = _materialize(tmp_path, source=SOURCE + "\n# changed\n")
    with pytest.raises(SemanticTargetContractError) as stale:
        _normalize(_semantic_response(target_id), tmp_path, newer)
    assert stale.value.code == "unknown_target_id"

    current = _materialize(tmp_path)
    current_id = build_semantic_target_inventory(current).handles[0].target_id
    with pytest.raises(SemanticTargetContractError) as mismatch:
        _normalize(
            _semantic_response(current_id, path=RELATIVE_PATH.upper()),
            tmp_path,
            current,
        )
    assert mismatch.value.code == "target_id_path_mismatch"

    with pytest.raises(SemanticTargetContractError) as traversal:
        _normalize(
            _semantic_response(current_id, path="../" + RELATIVE_PATH),
            tmp_path,
            current,
        )
    assert traversal.value.code == "target_path_invalid"
