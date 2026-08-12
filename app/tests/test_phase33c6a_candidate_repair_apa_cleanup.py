"""Phase 33C-6A: Candidate Repair consumes APA-derived scope directly."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.coordinators.completion_coordinator import (
    _completion_candidate_scope,
)
from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
    _completion_repair_invalid_paths,
    repair_authorized_scope,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)


HASH = "a" * 64
AUTHORITY_HASH = "b" * 64


def _authority(
    *entries: tuple[str, GrantClass],
) -> AcceptedPathAuthority:
    grants = [
        PathGrant(
            path=declare(path),
            grant_class=grant_class,
            provenance=(
                GrantProvenance.ACCEPTED_PLAN
                if grant_class is not GrantClass.EXISTING_READONLY
                else GrantProvenance.SOURCE_GROUNDING
            ),
            baseline_content_hash=(
                None if grant_class is GrantClass.CREATION_AUTHORIZED else HASH
            ),
        )
        for path, grant_class in entries
    ]
    return AcceptedPathAuthority.create(
        accepted_plan_identity="c" * 64,
        workspace_identity="/tmp/phase33c6a-workspace",
        maximum_scope_digest=AUTHORITY_HASH,
        grants=grants,
    )


def _repair_step(path: str, *, op: str = "write_file") -> dict:
    payload = {"op": op, "path": path}
    if op in {"write_file", "append_file"}:
        payload["content"] = "repaired\n"
    return payload


def _apply(tmp_path: Path, authority: AcceptedPathAuthority, operation: dict):
    return _apply_completion_repair_ops_direct(
        [operation],
        tmp_path,
        repair_authorized_scope=repair_authorized_scope(authority),
    )


def test_repair_receives_apa_projection_without_legacy_details_key(
    tmp_path: Path,
) -> None:
    path = "app/allowed.py"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("before\n")
    authority = _authority((path, GrantClass.EXISTING_MUTABLE))
    validation = SimpleNamespace(details={"authorized_scope": [path]})

    assert (
        _completion_repair_invalid_paths(
            repair_step={"ops": [_repair_step(path)]},
            project_dir=tmp_path,
            repair_authorized_scope=repair_authorized_scope(authority),
        )
        == []
    )
    result = _apply(tmp_path, authority, _repair_step(path))

    assert result["success"] is True
    assert validation.details == {"authorized_scope": [path]}
    assert authority.authority_identity


@pytest.mark.parametrize("grant_class", [GrantClass.EXISTING_MUTABLE])
def test_existing_mutable_repair_is_allowed(
    tmp_path: Path, grant_class: GrantClass
) -> None:
    path = "app/mutable.py"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("before\n")

    result = _apply(tmp_path, _authority((path, grant_class)), _repair_step(path))

    assert result["success"] is True
    assert (tmp_path / path).read_text() == "repaired\n"


def test_creation_authorized_exact_path_is_allowed(tmp_path: Path) -> None:
    path = "app/generated/new.py"
    authority = _authority((path, GrantClass.CREATION_AUTHORIZED))

    result = _apply(tmp_path, authority, _repair_step(path))

    assert result["success"] is True
    assert (tmp_path / path).read_text() == "repaired\n"


def test_readonly_grant_cannot_be_mutated(tmp_path: Path) -> None:
    path = "app/readonly.py"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("protected\n")

    result = _apply(
        tmp_path, _authority((path, GrantClass.EXISTING_READONLY)), _repair_step(path)
    )

    assert result["success"] is False
    assert (tmp_path / path).read_text() == "protected\n"


@pytest.mark.parametrize(
    "requested",
    [
        "app/unrelated.py",
        "app/generated/other.py",
        "app/other.py",
        "App/allowed.py",
        "app/../allowed.py",
        "../app/allowed.py",
        "app/allowed.py/child.py",
    ],
)
def test_repair_scope_is_exact_and_lexically_fail_closed(
    tmp_path: Path, requested: str
) -> None:
    allowed = "app/allowed.py"
    (tmp_path / allowed).parent.mkdir(parents=True)
    (tmp_path / allowed).write_text("before\n")
    authority = _authority((allowed, GrantClass.EXISTING_MUTABLE))

    result = _apply(tmp_path, authority, _repair_step(requested))

    assert result["success"] is False
    assert (tmp_path / allowed).read_text() == "before\n"
    assert not (tmp_path / requested).exists()


def test_changeset_findings_and_expected_files_cannot_launder_repair_scope(
    tmp_path: Path,
) -> None:
    allowed = "app/allowed.py"
    unrelated = "app/unrelated.py"
    (tmp_path / allowed).parent.mkdir(parents=True)
    (tmp_path / allowed).write_text("before\n")
    authority = _authority((allowed, GrantClass.EXISTING_MUTABLE))
    # These are deliberately not inputs to the repair gate.  They model every
    # observation/request channel that previously could be mistaken for grant.
    completion_validation = SimpleNamespace(
        details={
            "authorized_scope": [allowed],
            "candidate_observed_paths": [unrelated],
            "verification_scope": [unrelated],
            "expected_files": [unrelated],
            "validator_findings": [f"please repair {unrelated}"],
        }
    )

    invalid = _completion_repair_invalid_paths(
        repair_step={"ops": [_repair_step(unrelated)]},
        project_dir=tmp_path,
        repair_authorized_scope=repair_authorized_scope(authority),
    )

    assert invalid == [unrelated]
    assert completion_validation.details["authorized_scope"] == [allowed]


def test_create_outside_apa_is_denied(tmp_path: Path) -> None:
    authority = _authority(("app/allowed.py", GrantClass.CREATION_AUTHORIZED))

    result = _apply(tmp_path, authority, _repair_step("app/ungranted.py"))

    assert result["success"] is False
    assert not (tmp_path / "app/ungranted.py").exists()


def test_delete_without_deletion_grant_is_denied(tmp_path: Path) -> None:
    path = "app/allowed.py"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("protected\n")
    authority = _authority((path, GrantClass.EXISTING_MUTABLE))

    result = _apply(tmp_path, authority, _repair_step(path, op="delete_file"))

    assert result["success"] is False
    assert (tmp_path / path).exists()


def test_repair_preserves_authority_and_candidate_identity_semantics() -> None:
    authority = _authority(("app/allowed.py", GrantClass.EXISTING_MUTABLE))
    before_identity = authority.authority_identity
    validation = SimpleNamespace(
        candidate_identity="sha256:candidate",
        details={"authorized_scope": ["app/allowed.py"]},
    )

    assert _completion_candidate_scope(validation) == ("app/allowed.py",)
    assert authority.authority_identity == before_identity
    assert validation.candidate_identity == "sha256:candidate"


def test_production_repair_path_has_no_legacy_candidate_authority_dependency() -> None:
    production_files = (
        Path("app/services/orchestration/coordinators/completion_coordinator.py"),
        Path("app/services/orchestration/phases/completion_flow.py"),
        Path("app/services/orchestration/phases/completion_repair.py"),
        Path("app/services/orchestration/validation/validator.py"),
    )
    source = "\n".join(path.read_text() for path in production_files)

    assert "candidate_authorized_paths" not in source
    assert "candidate_authorized_files" not in source
    assert "accepted_path_authority" in source
    assert "repair_authorized_scope" in source
