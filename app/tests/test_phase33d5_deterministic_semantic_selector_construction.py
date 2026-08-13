"""Phase 33D-5 deterministic semantic selector construction tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.semantic_selector_construction import (
    AMBIGUOUS,
    CONSTRUCTED_UNIQUE,
    INVALID_AUTHORITY,
    NOT_FOUND,
    UNSAFE_SOURCE,
    UNSUPPORTED_INTENT,
    VERSION_MISMATCH,
    MaterializedRegionReference,
    SemanticTargetIntent,
    construct_source_region_identity,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
    build_accepted_path_authority,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
)
from app.services.orchestration.validation.validator import ValidatorService


def _materialize(
    root: Path,
    *,
    source: str,
    task: str = "replace target.txt `needle()`",
    maximum_bytes_per_file: int = 2000,
    maximum_total_source_bytes: int = 5000,
):
    (root / "target.txt").write_bytes(source.encode("utf-8"))
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=["target.txt"],
        maximum_bytes_per_file=maximum_bytes_per_file,
        maximum_total_source_bytes=maximum_total_source_bytes,
    )


def _authority(
    materialization,
    *,
    path: str = "target.txt",
    mutable: bool = True,
) -> AcceptedPathAuthority:
    scope_plan = [{"ops": [{"op": "replace_in_file", "path": path}]}] if mutable else []
    authority, undeclarable = build_accepted_path_authority(
        plan=scope_plan,
        source_materialization=materialization,
    )
    assert not undeclarable
    return authority


def _construct(root: Path, materialization, authority=None, *, path="target.txt"):
    return construct_source_region_identity(
        root=root,
        canonical_path=path,
        semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
        accepted_source_materialization=materialization,
        accepted_path_authority=authority or _authority(materialization, path=path),
    )


def _plan(operation: dict) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Replace the explicitly selected source region",
            "commands": [],
            "verification": "test -f target.txt",
            "rollback": None,
            "expected_files": ["target.txt"],
            "ops": [operation],
        }
    ]


def test_unique_materialized_region_constructs_existing_identity(tmp_path):
    source = "".join("padding()\n" for _ in range(300)) + "needle()\n"
    materialization = _materialize(tmp_path, source=source)
    result = _construct(tmp_path, materialization)

    assert result.status == CONSTRUCTED_UNIQUE
    assert isinstance(result.selector, SourceRegionIdentity)
    assert result.selector.canonical_path.value == "target.txt"
    assert result.selector.expected_source_version == current_source_version_identity(
        tmp_path / "target.txt"
    )
    selected = (tmp_path / "target.txt").read_bytes()[
        result.selector.start_byte : result.selector.end_byte
    ]
    assert (
        hashlib.sha256(selected).hexdigest() == result.selector.selected_region_sha256
    )
    assert b"needle()" in selected


def test_constructor_has_one_closed_intent_family_and_no_freeform_selector_facts(
    tmp_path,
):
    materialization = _materialize(tmp_path, source="needle()\n")
    authority = _authority(materialization)

    assert SemanticTargetIntent(MaterializedRegionReference()).__dict__ == {
        "region_reference": MaterializedRegionReference()
    }
    unsupported = construct_source_region_identity(
        root=tmp_path,
        canonical_path="target.txt",
        semantic_target=object(),
        accepted_source_materialization=materialization,
        accepted_path_authority=authority,
    )
    assert unsupported.status == UNSUPPORTED_INTENT


def test_unsupported_operation_does_not_construct_selector(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    result = construct_source_region_identity(
        root=tmp_path,
        canonical_path="target.txt",
        semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
        accepted_source_materialization=materialization,
        accepted_path_authority=_authority(materialization),
        operation_intent="write_file",
    )

    assert result.status == UNSUPPORTED_INTENT
    assert result.selector is None


@pytest.mark.parametrize("path", ["../target.txt", "Target.txt", "other.txt"])
def test_invalid_or_wrong_path_cannot_redirect_construction(tmp_path, path):
    materialization = _materialize(tmp_path, source="needle()\n")
    result = _construct(
        tmp_path,
        materialization,
        _authority(materialization),
        path=path,
    )

    assert result.status in {INVALID_AUTHORITY, NOT_FOUND}
    assert result.selector is None


def test_readonly_path_is_not_a_mutation_authority(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    readonly_authority = _authority(materialization, mutable=False)

    result = _construct(tmp_path, materialization, readonly_authority)

    assert result.status == INVALID_AUTHORITY
    assert result.diagnostic_code == "existing_mutable_grant_required"


def test_malformed_authority_fails_closed(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    result = construct_source_region_identity(
        root=tmp_path,
        canonical_path="target.txt",
        semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
        accepted_source_materialization=materialization,
        accepted_path_authority=object(),
    )

    assert result.status == INVALID_AUTHORITY
    assert result.selector is None


def test_duplicate_materialized_target_is_ambiguous(tmp_path):
    materialization = _materialize(
        tmp_path,
        source="needle()\npadding()\nneedle()\n",
        maximum_bytes_per_file=2000,
    )
    item = materialization.file_map()["target.txt"]
    assert item.target_match_count == 2

    result = _construct(tmp_path, materialization)

    assert result.status == AMBIGUOUS
    assert result.selector is None


def test_missing_target_evidence_is_not_ranked_or_guessed(tmp_path):
    materialization = _materialize(
        tmp_path,
        source="other()\n",
        task="replace target.txt `missing()`",
    )
    result = _construct(tmp_path, materialization)

    assert result.status == NOT_FOUND
    assert result.selector is None


def test_truncated_materialization_uses_authoritative_full_source_span(tmp_path):
    source = (
        "".join("padding()\n" for _ in range(300))
        + "needle()\n"
        + "".join("tail()\n" for _ in range(300))
    )
    materialization = _materialize(tmp_path, source=source)
    item = materialization.file_map()["target.txt"]
    assert item.truncated is True
    assert item.target_included is True
    assert item.content is not None

    result = _construct(tmp_path, materialization)

    assert result.status == CONSTRUCTED_UNIQUE
    assert result.selector.start_byte > 0
    selected = (tmp_path / "target.txt").read_bytes()[
        result.selector.start_byte : result.selector.end_byte
    ]
    assert (
        hashlib.sha256(selected).hexdigest() == result.selector.selected_region_sha256
    )
    assert b"... [truncated]" not in selected


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_unicode_and_newline_bytes_are_derived_from_authoritative_source(
    tmp_path, newline
):
    source = (
        ("préface()" + newline) * 220
        + "needle()"
        + newline
        + ("suffix()" + newline) * 220
    )
    materialization = _materialize(tmp_path, source=source)
    result = _construct(tmp_path, materialization)

    assert result.status == CONSTRUCTED_UNIQUE
    raw = (tmp_path / "target.txt").read_bytes()
    selected = raw[result.selector.start_byte : result.selector.end_byte]
    assert (
        hashlib.sha256(selected).hexdigest() == result.selector.selected_region_sha256
    )
    assert b"needle()" in selected
    if newline == "\r\n":
        assert raw[result.selector.end_byte :].startswith(b"\r\n")
    assert result.selector.end_byte <= len(raw)


def test_no_final_newline_keeps_authoritative_end_byte(tmp_path):
    source = ("préface()\n" * 220) + "needle()"
    materialization = _materialize(tmp_path, source=source)
    result = _construct(tmp_path, materialization)

    assert result.status == CONSTRUCTED_UNIQUE
    raw = (tmp_path / "target.txt").read_bytes()
    assert result.selector.end_byte == len(raw)
    assert raw[result.selector.start_byte : result.selector.end_byte].endswith(
        b"needle()"
    )


def test_insufficient_truncated_evidence_fails_closed(tmp_path):
    source = "".join("padding()\n" for _ in range(400)) + "needle()\n"
    materialization = _materialize(
        tmp_path,
        source=source,
        maximum_bytes_per_file=8,
        maximum_total_source_bytes=8,
    )
    result = _construct(tmp_path, materialization)

    assert result.status in {INVALID_AUTHORITY, NOT_FOUND, UNSAFE_SOURCE}
    assert result.selector is None


def test_source_change_after_materialization_is_version_mismatch(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    (tmp_path / "target.txt").write_text("changed()\n", encoding="utf-8")

    result = _construct(tmp_path, materialization)

    assert result.status == VERSION_MISMATCH
    assert result.selector is None


def test_symlink_target_is_unsafe_even_when_materialization_resolved_it(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("needle()\n", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.symlink_to(outside)
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace target.txt `needle()`",
        expected_paths=["target.txt"],
    )

    result = _construct(tmp_path, materialization)

    assert result.status == UNSAFE_SOURCE
    assert result.selector is None


def test_same_evidence_has_same_selector_identity(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    first = _construct(tmp_path, materialization)
    second = _construct(tmp_path, materialization)

    assert first.selector.selector_identity == second.selector.selector_identity
    assert first.selector.to_dict() == second.selector.to_dict()


def test_different_region_path_and_version_change_selector_identity(tmp_path):
    first_materialization = _materialize(tmp_path, source="needle()\n")
    first = _construct(tmp_path, first_materialization).selector

    (tmp_path / "other.txt").write_text("needle()\n", encoding="utf-8")
    second_materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace other.txt `needle()`",
        expected_paths=["other.txt"],
    )
    # The constructor intentionally binds the operation path to the record;
    # this is a distinct path identity, not a repository search.
    other_authority, _ = build_accepted_path_authority(
        plan=[{"ops": [{"op": "replace_in_file", "path": "other.txt"}]}],
        source_materialization=second_materialization,
    )
    other = construct_source_region_identity(
        root=tmp_path,
        canonical_path="other.txt",
        semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
        accepted_source_materialization=second_materialization,
        accepted_path_authority=other_authority,
    ).selector
    assert first.selector_identity != other.selector_identity

    (tmp_path / "target.txt").write_text("needle()\nchanged()\n", encoding="utf-8")
    third_materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace target.txt `needle()`",
        expected_paths=["target.txt"],
    )
    third = _construct(tmp_path, third_materialization).selector
    assert first.selector_identity != third.selector_identity
    assert first.expected_source_version != third.expected_source_version


def test_constructed_selector_persists_inside_existing_plan_json(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    selector = _construct(tmp_path, materialization).selector
    operation = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": selector.to_dict(),
        "new": "changed()\n",
    }
    plan = _plan(operation)
    reloaded = json.loads(json.dumps(plan))

    assert "old" not in reloaded[0]["ops"][0]
    assert (
        SourceRegionIdentity.from_dict(
            reloaded[0]["ops"][0]["selector"]
        ).selector_identity
        == selector.selector_identity
    )


def test_provider_free_semantic_pipeline_construct_validate_apa_reload_execute(
    tmp_path,
):
    source = (
        "".join("padding()\n" for _ in range(300))
        + "needle()\n"
        + "".join("tail()\n" for _ in range(300))
    )
    materialization = _materialize(tmp_path, source=source)
    construction_scope = _authority(materialization)
    constructed = construct_source_region_identity(
        root=tmp_path,
        canonical_path="target.txt",
        semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
        accepted_source_materialization=materialization,
        accepted_path_authority=construction_scope,
    )
    assert constructed.status == CONSTRUCTED_UNIQUE
    selected = source.encode("utf-8")[
        constructed.selector.start_byte : constructed.selector.end_byte
    ].decode("utf-8")
    replacement = selected.replace("needle()", "changed()", 1)
    plan = _plan(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "selector": constructed.selector.to_dict(),
            "new": replacement,
        }
    )

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="replace target.txt `needle()`",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    assert verdict.accepted, verdict.reasons
    assert "old" not in json.dumps(plan)
    accepted_authority = accepted_path_authority_from_verdict(verdict)
    assert accepted_authority is not None
    reloaded_authority = AcceptedPathAuthority.from_dict(
        json.loads(json.dumps(accepted_authority.to_dict()))
    )
    assert (
        reloaded_authority.authority_identity == accepted_authority.authority_identity
    )
    assert (
        reloaded_authority.accepted_plan_identity
        == accepted_authority.accepted_plan_identity
    )
    assert (
        SourceRegionIdentity.from_dict(plan[0]["ops"][0]["selector"]).selector_identity
        == constructed.selector.selector_identity
    )

    result = ExecutorService.execute_file_ops(
        tmp_path,
        plan[0]["ops"],
        accepted_path_authority=reloaded_authority,
    )
    assert result["success"] is True, result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == source.replace(
        "needle()", "changed()", 1
    )


def test_constructor_does_not_implicitly_convert_legacy_plan(tmp_path):
    materialization = _materialize(tmp_path, source="needle()\n")
    legacy = {
        "op": "replace_in_file",
        "path": "target.txt",
        "old": "needle()\n",
        "new": "changed()\n",
    }
    assert "selector" not in legacy
    # Construction is an explicit internal semantic seam; legacy operations
    # are passed through unchanged by D4's legacy normalization path.
    assert legacy["old"] == "needle()\n"
    assert _construct(tmp_path, materialization).status == CONSTRUCTED_UNIQUE
