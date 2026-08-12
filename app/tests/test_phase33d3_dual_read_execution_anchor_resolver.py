"""Phase 33D-3 dual-read execution anchor resolver tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.services.orchestration.execution.anchor_resolver import (
    AMBIGUOUS,
    INVALID_AUTHORITY,
    NOT_FOUND,
    UNSUPPORTED_SELECTOR,
    VERSION_MISMATCH,
    exact_region_candidates,
)
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
    validate_file_op_shape,
)
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
    SourceRegionIdentityError,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
    materialize_planner_source_context,
    materialized_source_file,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import normalize_file_ops
from app.tests.phase33c4_test_helpers import executor_test_authority


def _run_legacy(tmp_path: Path, old: str, new: str):
    operation = {"op": "replace_in_file", "path": "target.txt", "old": old, "new": new}
    return ExecutorService.execute_file_ops(
        tmp_path,
        [operation],
        accepted_path_authority=executor_test_authority(tmp_path, [operation]),
    )


def test_pre_d3_legacy_literal_replacement_characterization(tmp_path):
    (tmp_path / "target.txt").write_text("before\n", encoding="utf-8")

    result = _run_legacy(tmp_path, "before", "after")

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "after\n"


def test_pre_d3_legacy_ambiguous_literal_fails_closed(tmp_path):
    (tmp_path / "target.txt").write_text("same\nsame\n", encoding="utf-8")

    result = _run_legacy(tmp_path, "same", "done")

    assert result["success"] is False
    assert "ambiguous" in result["output"]
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "same\nsame\n"


def test_pre_d3_legacy_regex_fallback_characterization(tmp_path):
    (tmp_path / "target.txt").write_text("item-17\n", encoding="utf-8")

    result = _run_legacy(tmp_path, r"item-\d+", "item-final")

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "item-final\n"


def test_pre_d3_legacy_python_patch_fallback_characterization(tmp_path):
    source = "def test_value():\n    assert 1 + 2 == 3\n"
    (tmp_path / "test_target.py").write_text(source, encoding="utf-8")
    operation = {
        "op": "replace_in_file",
        "path": "test_target.py",
        "old": "def test_value():\n    STALE_TEXT\n",
        "new": (
            "def test_value():\n"
            "    result = 1 + 2\n"
            "    assert result == 3\n"
            "    assert result > 0\n"
        ),
    }

    result = ExecutorService.execute_file_ops(
        tmp_path,
        [operation],
        accepted_path_authority=executor_test_authority(tmp_path, [operation]),
    )

    assert result["success"] is True
    assert "patch_helper" in result["output"]
    assert "assert result > 0" in (tmp_path / "test_target.py").read_text(
        encoding="utf-8"
    )


def _region_selector(
    tmp_path: Path,
    *,
    path: str = "target.txt",
    start_byte: int = 0,
    end_byte: int | None = None,
    expected_source_version: str | None = None,
    selected_region_sha256: str | None = None,
) -> SourceRegionIdentity:
    source_bytes = (tmp_path / path).read_bytes()
    if end_byte is None:
        end_byte = len(source_bytes)
    if selected_region_sha256 is None:
        selected_region_sha256 = hashlib.sha256(
            source_bytes[start_byte:end_byte]
        ).hexdigest()
    return SourceRegionIdentity.from_region(
        canonical_path=path,
        expected_source_version=(
            expected_source_version or current_source_version_identity(tmp_path / path)
        ),
        start_byte=start_byte,
        end_byte=end_byte,
        selected_region_sha256=selected_region_sha256,
    )


def _semantic_operation(
    tmp_path: Path,
    *,
    path: str = "target.txt",
    new: str = "after\n",
    **selector_kwargs,
) -> dict:
    selector = _region_selector(tmp_path, path=path, **selector_kwargs)
    return {
        "op": "replace_in_file",
        "path": path,
        "selector": selector.to_dict(),
        "new": new,
    }


def _run_semantic(tmp_path: Path, operation: dict):
    return ExecutorService.execute_file_ops(
        tmp_path,
        [operation],
        accepted_path_authority=executor_test_authority(tmp_path, [operation]),
    )


def _semantic_step(
    selector: SourceRegionIdentity, *, new: str = "after\n"
) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Replace the target region",
            "commands": [],
            "verification": "test -f target.txt",
            "rollback": None,
            "expected_files": ["target.txt"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "target.txt",
                    "selector": selector.to_dict(),
                    "new": new,
                }
            ],
        }
    ]


def test_dual_read_contract_accepts_only_the_two_replace_forms():
    selector = {
        "schema_version": "source-region/1",
        "canonical_path": "target.txt",
        "expected_source_version": "v1",
        "start_byte": 0,
        "end_byte": 1,
        "selected_region_sha256": hashlib.sha256(b"a").hexdigest(),
        "derivation_kind": "exact_region",
    }
    legacy = {
        "op": "replace_in_file",
        "path": "target.txt",
        "old": "a",
        "new": "b",
    }
    semantic = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": selector,
        "new": "b",
    }
    assert validate_file_op_shape(legacy)
    assert validate_file_op_shape(semantic)
    assert not validate_file_op_shape({**semantic, "old": "a"})
    assert not validate_file_op_shape(
        {"op": "replace_in_file", "path": "target.txt", "new": "b"}
    )
    assert not validate_file_op_shape(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "selector": selector,
        }
    )
    assert normalize_file_op_shape(semantic) == semantic


def test_semantic_selector_is_strict_immutable_and_deterministic():
    selector_payload = {
        "schema_version": "source-region/1",
        "canonical_path": "target.txt",
        "expected_source_version": "v1",
        "start_byte": 0,
        "end_byte": 1,
        "selected_region_sha256": hashlib.sha256(b"a").hexdigest(),
        "derivation_kind": "exact_region",
    }
    selector = SourceRegionIdentity.from_dict(selector_payload)
    assert selector.to_dict() == selector_payload
    assert (
        selector.selector_identity
        == SourceRegionIdentity.from_dict(
            dict(reversed(list(selector_payload.items())))
        ).selector_identity
    )
    with pytest.raises(SourceRegionIdentityError):
        SourceRegionIdentity.from_dict({**selector_payload, "extra": True})
    with pytest.raises(SourceRegionIdentityError):
        SourceRegionIdentity.from_dict({**selector_payload, "start_byte": -1})


def test_semantic_exact_region_replaces_without_old_and_emits_local_artifact(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    operation = _semantic_operation(tmp_path)

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_bytes() == b"after\n"
    assert result["files_changed"] == ["target.txt"]
    artifact = result["execution_mutation_artifacts"][0]
    assert artifact["canonical_path"] == "target.txt"
    assert artifact["operation"] == "replace_in_file"
    assert artifact["selected_region_start_byte"] == 0
    assert artifact["selected_region_end_byte"] == len(b"before\n")
    assert artifact["replacement_hash"] == hashlib.sha256(b"after\n").hexdigest()
    assert len(artifact["execution_mutation_identity"]) == 64


def test_semantic_selector_and_legacy_operation_produce_same_bytes_and_grant_class(
    tmp_path,
):
    legacy_root = tmp_path / "legacy"
    semantic_root = tmp_path / "semantic"
    legacy_root.mkdir()
    semantic_root.mkdir()
    (legacy_root / "target.txt").write_bytes(b"before\n")
    (semantic_root / "target.txt").write_bytes(b"before\n")
    legacy = {
        "op": "replace_in_file",
        "path": "target.txt",
        "old": "before\n",
        "new": "after\n",
    }
    semantic = _semantic_operation(semantic_root)

    legacy_result = _run_legacy(legacy_root, legacy["old"], legacy["new"])
    semantic_result = _run_semantic(semantic_root, semantic)

    assert legacy_result["success"] and semantic_result["success"]
    assert (legacy_root / "target.txt").read_bytes() == (
        semantic_root / "target.txt"
    ).read_bytes()
    legacy_authority = executor_test_authority(legacy_root, [legacy])
    semantic_authority = executor_test_authority(semantic_root, [semantic])
    assert (
        legacy_authority.grant_for(declare("target.txt")).grant_class
        is GrantClass.EXISTING_MUTABLE
    )
    assert (
        semantic_authority.grant_for(declare("target.txt")).grant_class
        is GrantClass.EXISTING_MUTABLE
    )


def test_semantic_version_mismatch_fails_before_mutation(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    selector = _region_selector(tmp_path)
    (tmp_path / "target.txt").write_bytes(b"changed\n")
    operation = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": selector.to_dict(),
        "new": "after\n",
    }

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result["resolver_status"] == VERSION_MISMATCH
    assert result["semantic_resolution"]["expected_source_version"] == (
        selector.expected_source_version
    )
    assert result["semantic_resolution"]["current_source_version"]
    assert (tmp_path / "target.txt").read_bytes() == b"changed\n"


def test_semantic_region_hash_mismatch_is_not_found_and_does_not_patch(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    selector = _region_selector(
        tmp_path,
        selected_region_sha256=hashlib.sha256(b"tampered").hexdigest(),
    )
    operation = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": selector.to_dict(),
        "new": "after\n",
    }

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result["resolver_status"] == NOT_FOUND
    assert (tmp_path / "target.txt").read_bytes() == b"before\n"


def test_semantic_mode_never_uses_legacy_regex_or_patch_fallback(tmp_path, monkeypatch):
    (tmp_path / "target.py").write_text(
        "def test_value():\n    assert 1\n", encoding="utf-8"
    )
    selector = _region_selector(
        tmp_path,
        path="target.py",
        selected_region_sha256=hashlib.sha256(b"missing").hexdigest(),
    )
    operation = {
        "op": "replace_in_file",
        "path": "target.py",
        "selector": selector.to_dict(),
        "new": "def test_value():\n    assert 2\n",
    }
    monkeypatch.setattr(
        "app.services.orchestration.execution.executor.try_deterministic_patch",
        lambda *args, **kwargs: pytest.fail(
            "semantic mode invoked legacy patch fallback"
        ),
    )

    result = ExecutorService.execute_file_ops(
        tmp_path,
        [operation],
        accepted_path_authority=executor_test_authority(tmp_path, [operation]),
    )

    assert result["resolver_status"] == NOT_FOUND


def test_semantic_unicode_offsets_are_utf8_bytes(tmp_path):
    source = "éclair = 1\n"
    (tmp_path / "target.txt").write_bytes(source.encode("utf-8"))
    operation = _semantic_operation(tmp_path, new="É", start_byte=0, end_byte=2)

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_bytes() == "Éclair = 1\n".encode("utf-8")


def test_semantic_crlf_and_no_final_newline_bytes_remain_exact(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\r\nlast")
    operation = _semantic_operation(tmp_path, new="after", start_byte=0, end_byte=6)

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_bytes() == b"after\r\nlast"


@pytest.mark.parametrize(
    "selector_kwargs",
    [
        {"start_byte": -1, "end_byte": 2},
        {"start_byte": 2, "end_byte": 1},
        {"start_byte": 0, "end_byte": 999},
    ],
)
def test_semantic_invalid_offsets_fail_closed(tmp_path, selector_kwargs):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    if (
        selector_kwargs["start_byte"] < 0
        or selector_kwargs["start_byte"] > selector_kwargs["end_byte"]
    ):
        with pytest.raises(SourceRegionIdentityError):
            _region_selector(tmp_path, **selector_kwargs)
        return
    result = _run_semantic(
        tmp_path,
        _semantic_operation(tmp_path, **selector_kwargs),
    )
    assert result["success"] is False
    assert result["resolver_status"] == NOT_FOUND


def test_semantic_non_boundary_utf8_offset_fails_closed(tmp_path):
    source_bytes = "é\n".encode("utf-8")
    (tmp_path / "target.txt").write_bytes(source_bytes)
    operation = _semantic_operation(
        tmp_path,
        start_byte=1,
        end_byte=2,
        selected_region_sha256=hashlib.sha256(source_bytes[1:2]).hexdigest(),
    )

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result["resolver_status"] == UNSUPPORTED_SELECTOR


def test_semantic_binary_source_fails_closed(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"\xff\x00binary")
    operation = _semantic_operation(tmp_path, end_byte=1)

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result["resolver_status"] == UNSUPPORTED_SELECTOR
    assert (tmp_path / "target.txt").read_bytes() == b"\xff\x00binary"


def test_selector_path_cannot_redirect_to_another_file(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"before\n")
    (tmp_path / "b.txt").write_bytes(b"before\n")
    selector = _region_selector(tmp_path, path="a.txt")
    operation = {
        "op": "replace_in_file",
        "path": "b.txt",
        "selector": selector.to_dict(),
        "new": "after\n",
    }

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result["resolver_status"] == INVALID_AUTHORITY
    assert (tmp_path / "a.txt").read_bytes() == b"before\n"
    assert (tmp_path / "b.txt").read_bytes() == b"before\n"


def test_case_alias_and_traversal_paths_fail_closed(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "real.py").write_bytes(b"before\n")
    selector = _region_selector(tmp_path, path="app/real.py")
    alias_operation = {
        "op": "replace_in_file",
        "path": "App/Real.py",
        "selector": selector.to_dict(),
        "new": "after\n",
    }
    alias_result = ExecutorService.execute_file_ops(
        tmp_path,
        [alias_operation],
        accepted_path_authority=executor_test_authority(tmp_path, [alias_operation]),
    )
    traversal_operation = {
        "op": "replace_in_file",
        "path": "../real.py",
        "selector": selector.to_dict(),
        "new": "after\n",
    }
    traversal_result = ExecutorService.execute_file_ops(
        tmp_path,
        [traversal_operation],
        accepted_path_authority=executor_test_authority(
            tmp_path, [traversal_operation]
        ),
    )

    assert alias_result["success"] is False
    assert traversal_result["success"] is False
    assert (tmp_path / "app" / "real.py").read_bytes() == b"before\n"


def test_selector_cannot_widen_apa_or_change_operation_class(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"before\n")
    (tmp_path / "b.txt").write_bytes(b"before\n")
    selector = _region_selector(tmp_path, path="a.txt")
    write_operation = {
        "op": "write_file",
        "path": "b.txt",
        "selector": selector.to_dict(),
        "content": "redirected",
    }
    delete_operation = {
        "op": "delete_file",
        "path": "b.txt",
        "selector": selector.to_dict(),
    }

    write_result = ExecutorService.execute_file_ops(
        tmp_path,
        [write_operation],
        accepted_path_authority=executor_test_authority(tmp_path, [write_operation]),
    )
    delete_result = ExecutorService.execute_file_ops(
        tmp_path,
        [delete_operation],
        accepted_path_authority=executor_test_authority(tmp_path, [delete_operation]),
    )

    assert write_result["success"] is False
    assert delete_result["success"] is False
    assert (tmp_path / "b.txt").read_bytes() == b"before\n"


def test_readonly_and_wrong_path_authority_fail_closed(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    selector = _region_selector(tmp_path)
    readonly = PathGrant(
        path=declare("target.txt"),
        grant_class=GrantClass.EXISTING_READONLY,
        provenance=GrantProvenance.SOURCE_GROUNDING,
        baseline_content_hash=hashlib.sha256(b"before\n").hexdigest(),
    )
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity="a" * 64,
        workspace_identity=str(tmp_path.resolve()),
        maximum_scope_digest="b" * 64,
        grants=(readonly,),
    )
    operation = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": selector.to_dict(),
        "new": "after\n",
    }

    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )

    assert result["success"] is False
    assert result["failure_category"] == "validation_failure"
    assert (tmp_path / "target.txt").read_bytes() == b"before\n"


def test_symlink_target_is_unsafe_and_external_file_is_unchanged(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"before\n")
    (tmp_path / "link.txt").symlink_to(outside)
    operation = {
        "op": "replace_in_file",
        "path": "link.txt",
        "selector": _region_selector(tmp_path, path="link.txt").to_dict(),
        "new": "after\n",
    }

    result = _run_semantic(tmp_path, operation)

    assert result["success"] is False
    assert result.get("failure_category") == "validation_failure"
    assert outside.read_bytes() == b"before\n"


def test_exact_region_candidate_helper_never_ranks_or_selects_a_best_match(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    selector = _region_selector(tmp_path)

    assert exact_region_candidates(b"before\n", selector) == (selector,)
    assert exact_region_candidates(b"before\nbefore\n", selector) == (selector,)
    assert AMBIGUOUS == "AMBIGUOUS"


def test_normalize_file_ops_preserves_canonical_semantic_shape(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    operation = _semantic_operation(tmp_path)

    normalized = normalize_file_ops([operation], tmp_path)

    assert normalized == [operation]


def test_semantic_plan_validation_cross_checks_accepted_source_version(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace target.txt",
        expected_paths=["target.txt"],
    )
    record = materialized_source_file(materialization, "target.txt")
    assert record is not None and record.version_identity
    selector = _region_selector(
        tmp_path,
        expected_source_version=record.version_identity,
    )
    accepted = ValidatorService.validate_plan(
        _semantic_step(selector),
        output_text="[]",
        task_prompt="replace target.txt",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    assert accepted.accepted, accepted.reasons

    mismatched_selector = _region_selector(
        tmp_path,
        expected_source_version="not-the-accepted-version",
    )
    rejected = ValidatorService.validate_plan(
        _semantic_step(mismatched_selector),
        output_text="[]",
        task_prompt="replace target.txt",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    assert rejected.rejected
    assert not rejected.repairable
    assert "semantic_replace_version_mismatches" in rejected.details
