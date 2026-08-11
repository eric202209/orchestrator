"""Phase 33C-2 — canonical path-authority primitive contract tests.

Deterministic and provider-free.  These tests pin the Phase 33B contract for
`app/services/orchestration/validation/path_authority.py`, which has zero
production callers at this phase boundary.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import unicodedata

import pytest

from app.services.orchestration.validation.path_authority import (
    MAX_OBSERVED_HASH_BYTES,
    MAX_PATH_LENGTH,
    MAX_PATH_SEGMENTS,
    MAX_SEGMENT_LENGTH,
    SCHEMA_VERSION,
    AcceptedPathAuthority,
    CanonicalPath,
    DeclarationContext,
    EntryType,
    GrantClass,
    GrantProvenance,
    PathDeclarationError,
    PathGrant,
    PathGrantError,
    PathObservation,
    TrustClass,
    classify_trust,
    declare,
    observe,
    paths_conflict,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
DIGEST = "c" * 64


def _grant(
    path: str,
    grant_class: GrantClass = GrantClass.EXISTING_MUTABLE,
    provenance: GrantProvenance = GrantProvenance.ACCEPTED_PLAN,
    baseline_content_hash: str | None = HASH_A,
) -> PathGrant:
    if grant_class is GrantClass.CREATION_AUTHORIZED:
        baseline_content_hash = None
    return PathGrant(
        path=declare(path),
        grant_class=grant_class,
        provenance=provenance,
        baseline_content_hash=baseline_content_hash,
    )


def _authority(*grants: PathGrant, plan: str = "plan-1", workspace: str = "ws-1"):
    return AcceptedPathAuthority.create(
        accepted_plan_identity=plan,
        workspace_identity=workspace,
        maximum_scope_digest=DIGEST,
        grants=grants,
    )


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "real.py",
        "app/real.py",
        "app/services/orchestration/validation/validator.py",
        "a:b/file.txt",
        "my file.txt",
        "app/.gitignore",
        "docs/README.md",
        "frontend/src/pages/SessionDetail.tsx",
    ],
)
def test_declare_accepts_ordinary_relative_paths(text: str) -> None:
    declared = declare(text)
    assert declared.value == text
    assert declared.segments == tuple(text.split("/"))


def test_declare_accepts_legal_posix_colon_filename() -> None:
    """`a:b/file.txt` is a legal POSIX name, not a Windows drive reference."""

    assert declare("a:b/file.txt").value == "a:b/file.txt"


@pytest.mark.parametrize(
    "text,code",
    [
        ("", "path_empty"),
        ("   ", "path_empty"),
        ("\t\n", "path_empty"),
        (" app/real.py", "path_untrimmed"),
        ("app/real.py ", "path_untrimmed"),
        ("app/ real.py", "path_segment_untrimmed"),
        ("app/real.py\n", "path_control_character"),
        ("app/re\x00al.py", "path_control_character"),
        ("app/re\tal.py", "path_control_character"),
        ("app\\real.py", "path_backslash_separator"),
        ("file://app/real.py", "path_uri_like"),
        ("https://example.com/x", "path_uri_like"),
        ("/etc/passwd", "path_absolute"),
        ("/", "path_absolute"),
        ("~/secrets", "path_home_expansion"),
        ("~", "path_home_expansion"),
        ("C:/windows/system32", "path_drive_letter"),
        ("C:", "path_drive_letter"),
        ("z:/x", "path_drive_letter"),
        ("app/", "path_trailing_separator"),
        ("app//real.py", "path_empty_segment"),
        ("./real.py", "path_traversal_segment"),
        ("app/./real.py", "path_traversal_segment"),
        ("../outside.py", "path_traversal_segment"),
        ("app/../real.py", "path_traversal_segment"),
        ("app/..", "path_traversal_segment"),
    ],
)
def test_declare_rejects_malformed_declarations(text: str, code: str) -> None:
    with pytest.raises(PathDeclarationError) as excinfo:
        declare(text)
    assert excinfo.value.code == code


def test_declare_rejects_non_string() -> None:
    with pytest.raises(PathDeclarationError) as excinfo:
        declare(None)
    assert excinfo.value.code == "path_not_string"


def test_declare_never_collapses_traversal() -> None:
    """`app/../real.py` must reject, never normalize to `real.py`."""

    with pytest.raises(PathDeclarationError):
        declare("app/../real.py")


def test_declare_path_length_bound() -> None:
    segment = "a" * 100
    ok = "/".join([segment] * 10)
    assert len(ok) <= MAX_PATH_LENGTH
    assert declare(ok).value == ok

    too_long = "a" * (MAX_PATH_LENGTH + 1)
    with pytest.raises(PathDeclarationError) as excinfo:
        declare(too_long)
    assert excinfo.value.code == "path_too_long"


def test_declare_segment_length_bound() -> None:
    assert declare("a" * MAX_SEGMENT_LENGTH).value == "a" * MAX_SEGMENT_LENGTH
    with pytest.raises(PathDeclarationError) as excinfo:
        declare("a" * (MAX_SEGMENT_LENGTH + 1))
    assert excinfo.value.code == "path_segment_too_long"


def test_declare_segment_count_bound() -> None:
    ok = "/".join(["a"] * MAX_PATH_SEGMENTS)
    assert len(declare(ok).segments) == MAX_PATH_SEGMENTS
    with pytest.raises(PathDeclarationError) as excinfo:
        declare("/".join(["a"] * (MAX_PATH_SEGMENTS + 1)))
    assert excinfo.value.code == "path_too_many_segments"


@pytest.mark.parametrize(
    "text",
    [
        ".git/config",
        ".GIT/config",
        ".agent/change-sets/1/x.json",
        ".openclaw/state.json",
        ".orchestrator/x",
        ".claude/settings.json",
    ],
)
def test_declare_rejects_protected_roots(text: str) -> None:
    with pytest.raises(PathDeclarationError) as excinfo:
        declare(text)
    assert excinfo.value.code == "path_protected_root"


@pytest.mark.parametrize("text", ["venv/bin/python3", "node_modules/x/index.js"])
def test_declare_accepts_toolchain_roots_as_declarations(text: str) -> None:
    """Toolchain roots are an ownership question, not a declaration-safety one.

    Rejecting them lexically would re-mix candidate declaration safety with
    trusted filesystem ownership classification — the exact separation Phase 33
    exists to establish.  They are declarable and classified as non-product.
    """

    assert declare(text).value == text
    assert classify_trust(declare(text)) is TrustClass.TRUSTED_TOOLCHAIN


def test_declare_rejects_bound_task_execution_root() -> None:
    context = DeclarationContext(task_execution_dir_name="task-42")
    assert declare("app/real.py", context=context).value == "app/real.py"
    with pytest.raises(PathDeclarationError) as excinfo:
        declare("task-42/app/real.py", context=context)
    assert excinfo.value.code == "path_task_execution_root"


def test_declare_task_execution_root_only_applies_when_context_supplies_it() -> None:
    assert declare("task-42/app/real.py").value == "task-42/app/real.py"


def test_declare_normalizes_unicode_to_nfc_deterministically() -> None:
    decomposed = "app/cafe\u0301.py"  # e + combining acute
    composed = "app/caf\u00e9.py"  # precomposed e-acute
    assert decomposed != composed

    assert declare(decomposed).value == composed
    assert declare(composed).value == composed
    assert declare(decomposed) == declare(composed)
    assert declare(decomposed).value == unicodedata.normalize("NFC", decomposed)


def test_declare_preserves_case() -> None:
    declared = declare("App/Real.py")
    assert declared.value == "App/Real.py"
    assert declared.segments == ("App", "Real.py")


def _record_filesystem_calls(monkeypatch, targets) -> list[tuple[str, bool]]:
    """Wrap filesystem entry points so calls are recorded but still delegate.

    Recording rather than raising keeps pytest's own fixture teardown and
    failure reporting working, which also use these primitives.  Each record is
    ``(name, follows_symlinks)``.
    """

    calls: list[tuple[str, bool]] = []

    for owner, name in targets:
        original = getattr(owner, name)

        def _wrapper(*args, __owner=owner, __name=name, __original=original, **kwargs):
            calls.append(
                (
                    f"{__owner.__name__}.{__name}",
                    bool(kwargs.get("follow_symlinks", True)),
                )
            )
            return __original(*args, **kwargs)

        monkeypatch.setattr(owner, name, _wrapper)

    return calls


_DECLARE_FORBIDDEN_CALLS = (
    (Path, "exists"),
    (Path, "stat"),
    (Path, "lstat"),
    (Path, "resolve"),
    (Path, "is_symlink"),
    (Path, "is_file"),
    (Path, "is_dir"),
    (Path, "open"),
    (Path, "iterdir"),
    (os, "stat"),
    (os, "lstat"),
    (os, "open"),
    (os, "listdir"),
    (os, "readlink"),
    (os, "scandir"),
)


def test_declare_performs_no_filesystem_access(monkeypatch) -> None:
    """Hard proof: declare() touches no filesystem entry point at all."""

    calls = _record_filesystem_calls(monkeypatch, _DECLARE_FORBIDDEN_CALLS)

    assert declare("app/real.py").value == "app/real.py"
    with pytest.raises(PathDeclarationError):
        declare("app/../real.py")
    with pytest.raises(PathDeclarationError):
        declare("venv/../etc/passwd")
    assert declare("does/not/exist.py").value == "does/not/exist.py"

    assert calls == []


def test_declaration_does_not_require_existence(tmp_path) -> None:
    declared = declare("does/not/exist.py")
    assert declared.value == "does/not/exist.py"
    assert not (tmp_path / "does/not/exist.py").exists()


def test_canonical_path_fold_key_and_parents() -> None:
    declared = declare("App/New/Module.py")
    assert isinstance(declared, CanonicalPath)
    assert declared.fold_key == "app/new/module.py"
    assert declared.parent_directories == ("App", "App/New")
    assert declare("root.py").parent_directories == ()


# ---------------------------------------------------------------------------
# Alias and conflict semantics
# ---------------------------------------------------------------------------


def test_case_aliased_grants_are_rejected() -> None:
    with pytest.raises(PathGrantError) as excinfo:
        _authority(_grant("App/Real.py"), _grant("app/real.py"))
    assert excinfo.value.code == "path_alias_conflict"


def test_case_alias_rule_does_not_lowercase_paths() -> None:
    authority = _authority(_grant("App/Real.py"))
    assert authority.grants[0].path.value == "App/Real.py"
    assert authority.to_dict()["grants"][0]["path"] == "App/Real.py"


def test_case_alias_rule_is_host_filesystem_independent() -> None:
    """The rule is computed from the declaration alone — no `stat`, no probe."""

    left = declare("App/Real.py")
    right = declare("app/real.py")
    assert left.fold_key == right.fold_key
    assert left.value != right.value
    assert paths_conflict(left, right) is True


def test_exact_duplicate_grant_is_deterministically_rejected() -> None:
    with pytest.raises(PathGrantError) as excinfo:
        _authority(_grant("app/real.py"), _grant("app/real.py"))
    assert excinfo.value.code == "duplicate_grant_path"


def test_nested_path_conflict_is_rejected() -> None:
    with pytest.raises(PathGrantError) as excinfo:
        _authority(_grant("a"), _grant("a/b.py"))
    assert excinfo.value.code == "path_conflict"


def test_nested_path_conflict_is_case_insensitive() -> None:
    with pytest.raises(PathGrantError) as excinfo:
        _authority(_grant("A"), _grant("a/b.py"))
    assert excinfo.value.code == "path_conflict"


def test_paths_conflict_rule() -> None:
    assert paths_conflict(declare("a"), declare("a")) is True
    assert paths_conflict(declare("a"), declare("a/b.py")) is True
    assert paths_conflict(declare("a/b.py"), declare("a")) is True
    assert paths_conflict(declare("a"), declare("ab")) is False
    assert paths_conflict(declare("a/b.py"), declare("a/c.py")) is False


def test_siblings_do_not_conflict() -> None:
    authority = _authority(_grant("app/one.py"), _grant("app/two.py"))
    assert len(authority.grants) == 2


# ---------------------------------------------------------------------------
# Parent materialization
# ---------------------------------------------------------------------------


def test_creation_grant_does_not_authorize_siblings_or_children() -> None:
    authority = _authority(_grant("app/new/module.py", GrantClass.CREATION_AUTHORIZED))
    target = declare("app/new/module.py")
    assert authority.authorizes(target, GrantClass.CREATION_AUTHORIZED) is True

    for other in ("app/other.py", "app/new/other.py", "app/new", "app"):
        declared = declare(other)
        assert authority.grant_for(declared) is None
        for grant_class in GrantClass:
            assert authority.authorizes(declared, grant_class) is False


def test_creation_parents_are_derived_not_stored_as_grants() -> None:
    authority = _authority(_grant("app/new/module.py", GrantClass.CREATION_AUTHORIZED))
    assert authority.creation_parent_directories() == ("app", "app/new")
    assert [grant.path.value for grant in authority.grants] == ["app/new/module.py"]
    assert authority.grant_for(declare("app/new")) is None


def test_non_creation_grants_contribute_no_parent_materialization() -> None:
    authority = _authority(_grant("app/new/module.py", GrantClass.EXISTING_MUTABLE))
    assert authority.creation_parent_directories() == ()


def test_grant_lookup_has_no_prefix_authority() -> None:
    authority = _authority(_grant("app/real.py"))
    assert authority.grant_for(declare("app/real.py")) is not None
    assert authority.grant_for(declare("app/real.py.bak")) is None
    assert authority.grant_for(declare("app")) is None


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def test_observe_missing_path(tmp_path) -> None:
    result = observe(tmp_path, declare("app/missing.py"))
    assert result.exists is False
    assert result.entry_type is EntryType.MISSING
    assert result.symlink_segment is False
    assert result.content_sha256 is None
    assert result.byte_length is None


def test_observe_regular_file_hash_and_length(tmp_path) -> None:
    target = tmp_path / "app"
    target.mkdir()
    (target / "real.py").write_bytes(b"print('x')\n")

    result = observe(tmp_path, declare("app/real.py"))
    assert result.exists is True
    assert result.entry_type is EntryType.REGULAR_FILE
    assert result.symlink_segment is False
    assert result.byte_length == len(b"print('x')\n")
    assert result.content_sha256 == hashlib.sha256(b"print('x')\n").hexdigest()
    assert result.trust_class is TrustClass.PRODUCT


def test_observe_directory(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    result = observe(tmp_path, declare("app"))
    assert result.exists is True
    assert result.entry_type is EntryType.DIRECTORY
    assert result.content_sha256 is None
    assert result.byte_length is None


def test_observe_final_segment_symlink_is_not_followed(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "link.py").symlink_to(outside)

    result = observe(tmp_path, declare("app/link.py"))
    assert result.symlink_segment is True
    assert result.entry_type is EntryType.SPECIAL
    assert result.content_sha256 is None
    assert result.byte_length is None


def test_observe_intermediate_symlink_segment_is_not_traversed(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0")
    (tmp_path / "some").symlink_to(outside, target_is_directory=True)

    result = observe(tmp_path, declare("some/passwd"))
    assert result.symlink_segment is True
    assert result.exists is False
    assert result.content_sha256 is None


def test_observe_dangling_symlink(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "gone.py").symlink_to(tmp_path / "never-existed")

    result = observe(tmp_path, declare("app/gone.py"))
    assert result.symlink_segment is True
    assert result.entry_type is EntryType.SPECIAL


def test_observe_outward_symlink_target_content_is_never_read(tmp_path) -> None:
    outside = tmp_path / "external-toolchain"
    outside.mkdir()
    (outside / "python3.12").write_text("#!/bin/sh\n")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "bin").mkdir()
    (tmp_path / "venv" / "bin" / "python3").symlink_to(outside / "python3.12")

    result = observe(tmp_path, declare("venv/bin/python3"))
    assert result.symlink_segment is True
    assert result.content_sha256 is None
    assert result.trust_class is TrustClass.TRUSTED_TOOLCHAIN
    # The symlink itself is untouched and still a symlink.
    assert (tmp_path / "venv" / "bin" / "python3").is_symlink()


def test_observe_special_entry(tmp_path) -> None:
    fifo = tmp_path / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError):  # pragma: no cover
        pytest.skip("FIFOs are unsupported on this platform")

    result = observe(tmp_path, declare("pipe"))
    assert result.exists is True
    assert result.entry_type is EntryType.SPECIAL
    assert result.content_sha256 is None


def test_observe_never_calls_a_resolving_primitive(monkeypatch, tmp_path) -> None:
    """observe() must never resolve or follow: only lstat/fstat are permitted."""

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "real.py").write_text("x")
    (tmp_path / "app" / "link.py").symlink_to(tmp_path / "app" / "real.py")

    calls = _record_filesystem_calls(
        monkeypatch,
        (
            (Path, "resolve"),
            (Path, "stat"),
            (Path, "exists"),
            (Path, "is_file"),
            (Path, "is_dir"),
            (os, "stat"),
            (os.path, "realpath"),
            (os, "readlink"),
        ),
    )

    assert observe(tmp_path, declare("app/real.py")).exists is True
    assert observe(tmp_path, declare("app/link.py")).symlink_segment is True
    assert observe(tmp_path, declare("app/missing.py")).exists is False

    names = [name for name, _ in calls]
    # Nothing that resolves, follows, or reads a link target may be called.
    for forbidden in (
        "Path.resolve",
        "Path.exists",
        "Path.is_file",
        "Path.is_dir",
        "posixpath.realpath",
        "os.readlink",
    ):
        assert forbidden not in names

    # `Path.lstat()` delegates to `Path.stat(follow_symlinks=False)` on this
    # Python, so stat calls are permitted only in their non-following form.
    assert calls, "expected observe() to stat the path"
    assert all(follows is False for _, follows in calls)


def test_observe_bounded_large_file_is_measured_but_not_hashed(
    monkeypatch, tmp_path
) -> None:
    """A file past the bound is deterministic evidence, not a partial digest."""

    import app.services.orchestration.validation.path_authority as module

    monkeypatch.setattr(module, "MAX_OBSERVED_HASH_BYTES", 8)
    payload = b"0123456789abcdef"
    (tmp_path / "big.bin").write_bytes(payload)

    result = observe(tmp_path, declare("big.bin"))
    assert result.exists is True
    assert result.entry_type is EntryType.REGULAR_FILE
    assert result.byte_length == len(payload)
    assert result.content_sha256 is None


def test_observe_hashes_a_file_exactly_at_the_bound(monkeypatch, tmp_path) -> None:
    import app.services.orchestration.validation.path_authority as module

    monkeypatch.setattr(module, "MAX_OBSERVED_HASH_BYTES", 8)
    payload = b"01234567"
    (tmp_path / "edge.bin").write_bytes(payload)

    result = observe(tmp_path, declare("edge.bin"))
    assert result.byte_length == 8
    assert result.content_sha256 == hashlib.sha256(payload).hexdigest()


def test_default_hash_bound_matches_harvested_phase29_value() -> None:
    assert MAX_OBSERVED_HASH_BYTES == 8 * 1024 * 1024


def test_observation_carries_no_authorization_field() -> None:
    fields = set(PathObservation.__dataclass_fields__)
    assert fields == {
        "path",
        "exists",
        "entry_type",
        "symlink_segment",
        "content_sha256",
        "byte_length",
        "trust_class",
    }
    for forbidden in ("authorized", "grant", "allowed", "permission"):
        assert not any(forbidden in name for name in fields)


def test_observation_is_immutable(tmp_path) -> None:
    result = observe(tmp_path, declare("app/missing.py"))
    with pytest.raises(Exception):
        result.exists = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("app/real.py", TrustClass.PRODUCT),
        ("docs/README.md", TrustClass.PRODUCT),
        (".github/workflows/ci.yml", TrustClass.PRODUCT),
        ("venv/bin/python3", TrustClass.TRUSTED_TOOLCHAIN),
        ("node_modules/react/index.js", TrustClass.TRUSTED_TOOLCHAIN),
        ("app/__pycache__/x.pyc", TrustClass.TRUSTED_TOOLCHAIN),
        (".gitignore", TrustClass.ORCHESTRATION_INTERNAL),
        ("BOOTSTRAP.md", TrustClass.ORCHESTRATION_INTERNAL),
        ("runtime.json", TrustClass.ORCHESTRATION_INTERNAL),
    ],
)
def test_trust_classification_reuses_existing_ownership_authority(
    path: str, expected: TrustClass
) -> None:
    assert classify_trust(declare(path)) is expected


def test_trust_classification_split_is_derived_from_the_single_authority() -> None:
    """No second exclusion table: the toolchain subset lives inside the existing one."""

    from app.services.workspace.workspace_paths import HYDRATION_EXCLUDED_NAMES
    from app.services.orchestration.validation.path_authority import (
        _TOOLCHAIN_EXCLUDED_NAMES,
    )

    assert _TOOLCHAIN_EXCLUDED_NAMES <= HYDRATION_EXCLUDED_NAMES


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def test_all_four_grant_classes_are_representable() -> None:
    assert {item.value for item in GrantClass} == {
        "existing_mutable",
        "existing_readonly",
        "creation_authorized",
        "deletion_authorized",
    }


def test_all_five_provenance_values_are_representable() -> None:
    assert {item.value for item in GrantProvenance} == {
        "task_explicit_scope",
        "source_grounding",
        "accepted_plan",
        "operator_authorized",
        "system_internal",
    }


@pytest.mark.parametrize(
    "grant_class",
    [
        GrantClass.EXISTING_MUTABLE,
        GrantClass.EXISTING_READONLY,
        GrantClass.DELETION_AUTHORIZED,
    ],
)
def test_grant_classes_requiring_a_baseline_hash(grant_class: GrantClass) -> None:
    grant = PathGrant(
        path=declare("app/real.py"),
        grant_class=grant_class,
        provenance=GrantProvenance.ACCEPTED_PLAN,
        baseline_content_hash=HASH_A,
    )
    assert grant.baseline_content_hash == HASH_A

    with pytest.raises(PathGrantError) as excinfo:
        PathGrant(
            path=declare("app/real.py"),
            grant_class=grant_class,
            provenance=GrantProvenance.ACCEPTED_PLAN,
            baseline_content_hash=None,
        )
    assert excinfo.value.code == "grant_baseline_hash_required"


def test_creation_grant_forbids_a_baseline_hash() -> None:
    grant = PathGrant(
        path=declare("app/new.py"),
        grant_class=GrantClass.CREATION_AUTHORIZED,
        provenance=GrantProvenance.SOURCE_GROUNDING,
        baseline_content_hash=None,
    )
    assert grant.baseline_content_hash is None

    with pytest.raises(PathGrantError) as excinfo:
        PathGrant(
            path=declare("app/new.py"),
            grant_class=GrantClass.CREATION_AUTHORIZED,
            provenance=GrantProvenance.SOURCE_GROUNDING,
            baseline_content_hash=HASH_A,
        )
    assert excinfo.value.code == "grant_baseline_hash_forbidden"


@pytest.mark.parametrize("bad_hash", ["", "abc", "A" * 64, "g" * 64, "a" * 63, 123])
def test_grant_rejects_malformed_baseline_hash(bad_hash) -> None:
    with pytest.raises(PathGrantError) as excinfo:
        PathGrant(
            path=declare("app/real.py"),
            grant_class=GrantClass.EXISTING_MUTABLE,
            provenance=GrantProvenance.ACCEPTED_PLAN,
            baseline_content_hash=bad_hash,
        )
    assert excinfo.value.code == "grant_baseline_hash_required"


def test_grant_rejects_undeclared_path() -> None:
    with pytest.raises(PathGrantError) as excinfo:
        PathGrant(
            path="app/real.py",  # type: ignore[arg-type]
            grant_class=GrantClass.EXISTING_MUTABLE,
            provenance=GrantProvenance.ACCEPTED_PLAN,
            baseline_content_hash=HASH_A,
        )
    assert excinfo.value.code == "grant_path_not_declared"


def test_observation_names_are_not_representable_as_provenance() -> None:
    """`execution_observed` / `change_set_observed` are observations, not authority."""

    values = {item.value for item in GrantProvenance}
    assert "execution_observed" not in values
    assert "change_set_observed" not in values

    for rejected in ("execution_observed", "change_set_observed"):
        with pytest.raises(ValueError):
            GrantProvenance(rejected)

    payload = _grant("app/real.py").payload()
    payload["provenance"] = "execution_observed"
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(
            {
                "schema_version": SCHEMA_VERSION,
                "authority_identity": "0" * 64,
                "accepted_plan_identity": "plan-1",
                "workspace_identity": "ws-1",
                "maximum_scope_digest": DIGEST,
                "grants": [payload],
            }
        )
    assert excinfo.value.code == "grant_provenance_invalid"


def test_grant_class_is_not_duplicated_in_provenance() -> None:
    values = {item.value for item in GrantProvenance}
    for redundant in (
        "source_grounding_existing",
        "source_grounding_creation_authorized",
        "accepted_plan_existing_target",
        "accepted_plan_creation_target",
    ):
        assert redundant not in values


def test_grant_is_immutable() -> None:
    grant = _grant("app/real.py")
    with pytest.raises(Exception):
        grant.grant_class = GrantClass.EXISTING_READONLY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Accepted path authority
# ---------------------------------------------------------------------------


def test_authority_fields() -> None:
    assert set(AcceptedPathAuthority.__dataclass_fields__) == {
        "authority_identity",
        "accepted_plan_identity",
        "workspace_identity",
        "maximum_scope_digest",
        "grants",
    }


def test_authority_identity_is_deterministic_across_reconstruction() -> None:
    first = _authority(_grant("app/real.py"), _grant("app/other.py"))
    second = _authority(_grant("app/real.py"), _grant("app/other.py"))
    assert first.authority_identity == second.authority_identity
    assert len(first.authority_identity) == 64


def test_authority_identity_is_independent_of_grant_input_ordering() -> None:
    forward = _authority(_grant("app/a.py"), _grant("app/b.py"), _grant("app/c.py"))
    reverse = _authority(_grant("app/c.py"), _grant("app/b.py"), _grant("app/a.py"))
    assert forward.authority_identity == reverse.authority_identity
    assert [g.path.value for g in forward.grants] == [
        g.path.value for g in reverse.grants
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_grant",
        "different_path",
        "different_grant_class",
        "different_baseline_hash",
        "different_provenance",
        "different_plan_identity",
        "different_workspace_identity",
    ],
)
def test_authority_identity_changes_for_semantic_change(mutation: str) -> None:
    base = _authority(_grant("app/real.py"))

    if mutation == "extra_grant":
        other = _authority(_grant("app/real.py"), _grant("app/second.py"))
    elif mutation == "different_path":
        other = _authority(_grant("app/renamed.py"))
    elif mutation == "different_grant_class":
        other = _authority(_grant("app/real.py", GrantClass.EXISTING_READONLY))
    elif mutation == "different_baseline_hash":
        other = _authority(_grant("app/real.py", baseline_content_hash=HASH_B))
    elif mutation == "different_provenance":
        other = _authority(
            _grant("app/real.py", provenance=GrantProvenance.SOURCE_GROUNDING)
        )
    elif mutation == "different_plan_identity":
        other = _authority(_grant("app/real.py"), plan="plan-2")
    else:
        other = _authority(_grant("app/real.py"), workspace="ws-2")

    assert other.authority_identity != base.authority_identity


def test_maximum_scope_digest_is_not_part_of_identity() -> None:
    first = _authority(_grant("app/real.py"))
    second = AcceptedPathAuthority.create(
        accepted_plan_identity="plan-1",
        workspace_identity="ws-1",
        maximum_scope_digest="d" * 64,
        grants=[_grant("app/real.py")],
    )
    assert first.authority_identity == second.authority_identity
    assert first.maximum_scope_digest != second.maximum_scope_digest


def test_empty_authority_is_valid_and_authorizes_nothing() -> None:
    authority = _authority()
    assert authority.grants == ()
    assert authority.grant_for(declare("app/real.py")) is None
    assert authority.creation_parent_directories() == ()


def test_authority_is_immutable() -> None:
    authority = _authority(_grant("app/real.py"))
    with pytest.raises(Exception):
        authority.authority_identity = "x" * 64  # type: ignore[misc]
    assert isinstance(authority.grants, tuple)


def test_authority_rejects_malformed_identities() -> None:
    for kwargs in (
        {"accepted_plan_identity": ""},
        {"accepted_plan_identity": "   "},
        {"workspace_identity": ""},
        {"accepted_plan_identity": None},
        {"workspace_identity": 7},
    ):
        with pytest.raises(PathGrantError):
            AcceptedPathAuthority.create(
                accepted_plan_identity=kwargs.get("accepted_plan_identity", "plan-1"),
                workspace_identity=kwargs.get("workspace_identity", "ws-1"),
                maximum_scope_digest=DIGEST,
                grants=[],
            )

    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.create(
            accepted_plan_identity="plan-1",
            workspace_identity="ws-1",
            maximum_scope_digest="not-a-digest",
            grants=[],
        )
    assert excinfo.value.code == "maximum_scope_digest_invalid"


def test_authority_carries_no_runtime_state() -> None:
    authority = _authority(_grant("app/real.py"))
    payload = authority.to_dict()
    serialized = json.dumps(payload)
    for forbidden in ("timestamp", "created_at", "object at 0x", "session", "model"):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_serialization_round_trip_preserves_identity() -> None:
    authority = _authority(
        _grant("app/real.py"),
        _grant("app/new.py", GrantClass.CREATION_AUTHORIZED),
        _grant("app/gone.py", GrantClass.DELETION_AUTHORIZED),
        _grant("docs/README.md", GrantClass.EXISTING_READONLY),
    )
    payload = json.loads(json.dumps(authority.to_dict()))
    restored = AcceptedPathAuthority.from_dict(payload)

    assert restored == authority
    assert restored.authority_identity == authority.authority_identity
    assert restored.to_dict() == authority.to_dict()


def test_serialization_is_json_safe() -> None:
    authority = _authority(_grant("app/caf\u00e9.py"))
    text = json.dumps(authority.to_dict())
    assert AcceptedPathAuthority.from_dict(json.loads(text)) == authority


def test_from_dict_recomputes_and_verifies_identity() -> None:
    authority = _authority(_grant("app/real.py"))
    payload = authority.to_dict()
    payload["authority_identity"] = "0" * 64

    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_identity_mismatch"


def test_from_dict_rejects_tampered_grant_content() -> None:
    """A grant edited in the persisted record no longer matches its identity."""

    authority = _authority(_grant("app/real.py"))
    payload = authority.to_dict()
    payload["grants"][0]["baseline_content_hash"] = HASH_B

    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_identity_mismatch"


def test_from_dict_rejects_smuggled_extra_grant() -> None:
    authority = _authority(_grant("app/real.py"))
    payload = authority.to_dict()
    payload["grants"].append(_grant("app/smuggled.py").payload())

    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_identity_mismatch"


@pytest.mark.parametrize(
    "key",
    [
        "schema_version",
        "authority_identity",
        "accepted_plan_identity",
        "workspace_identity",
        "maximum_scope_digest",
        "grants",
    ],
)
def test_from_dict_rejects_missing_required_fields(key: str) -> None:
    payload = _authority(_grant("app/real.py")).to_dict()
    payload.pop(key)
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_payload_invalid"


def test_from_dict_rejects_unknown_fields() -> None:
    payload = _authority(_grant("app/real.py")).to_dict()
    payload["surprise"] = True
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_payload_invalid"


def test_from_dict_rejects_unsupported_schema_version() -> None:
    payload = _authority(_grant("app/real.py")).to_dict()
    payload["schema_version"] = "accepted-path-authority/999"
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_schema_unsupported"


@pytest.mark.parametrize("payload", [None, [], "x", 7])
def test_from_dict_rejects_non_mapping(payload) -> None:
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "authority_payload_invalid"


def test_from_dict_rejects_malformed_grant_payloads() -> None:
    base = _authority(_grant("app/real.py")).to_dict()

    missing = json.loads(json.dumps(base))
    missing["grants"][0].pop("provenance")
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(missing)
    assert excinfo.value.code == "grant_payload_invalid"

    extra = json.loads(json.dumps(base))
    extra["grants"][0]["unexpected"] = 1
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(extra)
    assert excinfo.value.code == "grant_payload_invalid"

    bad_class = json.loads(json.dumps(base))
    bad_class["grants"][0]["grant_class"] = "existing_everything"
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(bad_class)
    assert excinfo.value.code == "grant_class_invalid"

    not_a_list = json.loads(json.dumps(base))
    not_a_list["grants"] = {"path": "app/real.py"}
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(not_a_list)
    assert excinfo.value.code == "authority_payload_invalid"


def test_from_dict_rejects_undeclarable_path() -> None:
    payload = _authority(_grant("app/real.py")).to_dict()
    payload["grants"][0]["path"] = "../outside.py"
    with pytest.raises(PathDeclarationError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "path_traversal_segment"


def test_from_dict_rejects_alias_conflict_on_load() -> None:
    payload = _authority(_grant("App/Real.py")).to_dict()
    payload["grants"].append(_grant("app/real.py").payload())
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "path_alias_conflict"


def test_from_dict_rejects_nested_conflict_on_load() -> None:
    payload = _authority(_grant("a")).to_dict()
    payload["grants"].append(_grant("a/b.py").payload())
    with pytest.raises(PathGrantError) as excinfo:
        AcceptedPathAuthority.from_dict(payload)
    assert excinfo.value.code == "path_conflict"


def test_from_dict_load_order_does_not_affect_identity() -> None:
    authority = _authority(_grant("app/a.py"), _grant("app/b.py"))
    payload = authority.to_dict()
    payload["grants"].reverse()
    assert (
        AcceptedPathAuthority.from_dict(payload).authority_identity
        == authority.authority_identity
    )
