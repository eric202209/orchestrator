"""Phase 33C-5 Candidate Validator authority/observation separation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_observed_paths,
    select_candidate_verification,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from app.services.orchestration.validation.validator import ValidatorService


_HASH = "0" * 64


def _plan(*paths: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Implement the requested files",
            "expected_files": list(paths),
            "verification": "python -m pytest tests/test_app.py",
        }
    ]


def _authority(
    root: Path,
    plan: list[dict],
    grants: list[tuple[str, GrantClass]],
) -> AcceptedPathAuthority:
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(root.resolve()),
        maximum_scope_digest=_HASH,
        grants=[
            PathGrant(
                path=declare(path),
                grant_class=grant_class,
                provenance=GrantProvenance.ACCEPTED_PLAN,
                baseline_content_hash=(
                    _HASH if grant_class is not GrantClass.CREATION_AUTHORIZED else None
                ),
            )
            for path, grant_class in grants
        ],
    )


def _validate(
    root: Path,
    plan: list[dict],
    change_set: dict,
    authority: AcceptedPathAuthority,
):
    return ValidatorService.validate_task_completion(
        project_dir=root,
        plan=plan,
        task_prompt="Implement the requested files",
        execution_profile="full_lifecycle",
        workflow_stage="implementation",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "candidate_delta_required": True,
            "change_set": change_set,
        },
        accepted_path_authority=authority,
    )


def _write(root: Path, relative: str, content: str = "value = 1\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_change_set_is_observation_only_and_cannot_widen_authority(
    tmp_path: Path,
) -> None:
    plan = _plan("app/allowed.py")
    _write(tmp_path, "app/allowed.py")
    _write(tmp_path, "app/invented.py")
    authority = _authority(
        tmp_path, plan, [("app/allowed.py", GrantClass.EXISTING_MUTABLE)]
    )

    result = _validate(
        tmp_path,
        plan,
        {
            "added_files": [],
            "modified_files": ["app/invented.py"],
            "deleted_files": [],
        },
        authority,
    )

    assert candidate_observed_paths(
        {"added_files": [], "modified_files": ["app/invented.py"], "deleted_files": []}
    ) == ("app/invented.py",)
    assert result.details["authorized_scope"] == ["app/allowed.py"]
    assert result.details["observed_scope"] == ["app/invented.py"]
    assert result.details["candidate_authorized_paths"] == ["app/allowed.py"]
    assert result.status == "rejected"
    assert result.repairable is False
    assert any(
        finding.rule_id == "candidate_observed_scope_outside_accepted_authority"
        for finding in result.findings
    )


def test_attempt_16_invented_tsx_path_fails_closed_and_is_not_repairable(
    tmp_path: Path,
) -> None:
    accepted = "frontend/src/pages/SessionDetail.tsx"
    invented = "frontend/src/components/session/SessionDetail.tsx"
    plan = _plan(accepted)
    _write(tmp_path, accepted, "export default function SessionDetail() {}\n")
    _write(tmp_path, invented, "export default function SessionDetail() {}\n")
    authority = _authority(tmp_path, plan, [(accepted, GrantClass.EXISTING_MUTABLE)])

    result = _validate(
        tmp_path,
        plan,
        {"added_files": [], "modified_files": [invented], "deleted_files": []},
        authority,
    )

    assert result.status == "rejected"
    assert result.repairable is False
    assert result.details["candidate_authorized_paths"] == [accepted]
    assert result.details["observed_scope"] == [invented]
    assert invented not in result.details["candidate_authorized_paths"]
    assert result.details["candidate_authority_invariant_failed"] is True
    assert not any(
        finding.repairable
        for finding in result.findings
        if finding.source == "accepted_path_authority"
    )


def test_authorized_mutation_missing_from_delta_stays_in_verification_scope(
    tmp_path: Path,
) -> None:
    expected = "app/expected.py"
    plan = _plan(expected)
    _write(tmp_path, expected)
    authority = _authority(tmp_path, plan, [(expected, GrantClass.EXISTING_MUTABLE)])

    result = _validate(
        tmp_path,
        plan,
        {"added_files": [], "modified_files": [], "deleted_files": []},
        authority,
    )

    assert result.status in {"accepted", "warning"}
    assert result.details["authorized_scope"] == [expected]
    assert result.details["observed_scope"] == []
    assert result.details["verification_scope"] == [expected]
    assert result.details["missing_authorized_paths"] == [expected]


def test_read_only_grant_is_not_mutation_authority_or_expectation(
    tmp_path: Path,
) -> None:
    mutable = "app/allowed.py"
    readonly = "docs/context.md"
    plan = _plan(mutable, readonly)
    _write(tmp_path, mutable)
    _write(tmp_path, readonly, "context\n")
    authority = _authority(
        tmp_path,
        plan,
        [
            (mutable, GrantClass.EXISTING_MUTABLE),
            (readonly, GrantClass.EXISTING_READONLY),
        ],
    )

    result = _validate(
        tmp_path,
        plan,
        {"added_files": [], "modified_files": [mutable], "deleted_files": []},
        authority,
    )

    assert result.status in {"accepted", "warning"}
    assert result.details["authorized_scope"] == [mutable]
    assert result.details["verification_scope"] == [mutable]
    assert readonly not in result.details["candidate_authorized_paths"]
    assert readonly not in result.details["missing_authorized_paths"]


def test_expected_files_cannot_widen_authorized_scope(tmp_path: Path) -> None:
    mutable = "app/allowed.py"
    expected_context = "docs/context.md"
    plan = _plan(mutable, expected_context)
    _write(tmp_path, mutable)
    _write(tmp_path, expected_context, "context\n")
    authority = _authority(
        tmp_path,
        plan,
        [
            (mutable, GrantClass.EXISTING_MUTABLE),
            (expected_context, GrantClass.EXISTING_READONLY),
        ],
    )

    result = _validate(
        tmp_path,
        plan,
        {"added_files": [], "modified_files": [mutable], "deleted_files": []},
        authority,
    )

    assert result.details["authorized_scope"] == [mutable]
    assert result.details["verification_scope"] == [mutable]
    assert expected_context not in result.details["candidate_authorized_paths"]


def test_creation_and_mixed_mutations_use_only_apa_grants(tmp_path: Path) -> None:
    existing = "app/existing.py"
    created = "app/new.py"
    plan = _plan(existing, created)
    _write(tmp_path, existing)
    _write(tmp_path, created)
    authority = _authority(
        tmp_path,
        plan,
        [
            (existing, GrantClass.EXISTING_MUTABLE),
            (created, GrantClass.CREATION_AUTHORIZED),
        ],
    )

    result = _validate(
        tmp_path,
        plan,
        {
            "added_files": [created],
            "modified_files": [existing],
            "deleted_files": [],
        },
        authority,
    )

    assert result.status in {"accepted", "warning"}
    assert result.details["authorized_scope"] == [existing, created]
    assert result.details["observed_scope"] == [created, existing]
    assert result.details["verification_scope"] == [existing, created]


def test_deleted_observation_without_deletion_grant_is_rejected(
    tmp_path: Path,
) -> None:
    deleted = "app/deleted.py"
    plan = _plan("app/allowed.py")
    _write(tmp_path, "app/allowed.py")
    authority = _authority(
        tmp_path, plan, [("app/allowed.py", GrantClass.EXISTING_MUTABLE)]
    )

    result = _validate(
        tmp_path,
        plan,
        {"added_files": [], "modified_files": [], "deleted_files": [deleted]},
        authority,
    )

    assert result.status == "rejected"
    assert result.repairable is False
    assert result.details["observed_scope"] == [deleted]
    assert deleted not in result.details["authorized_scope"]


@pytest.mark.parametrize(
    ("label", "observed", "verification", "expected_source"),
    [
        (
            "python",
            ("app/app.py",),
            ("app/app.py",),
            "deterministic_existing_regression_tests",
        ),
        (
            "tsx",
            ("src/App.tsx",),
            ("src/App.tsx",),
            "no_trustworthy_focused_tests",
        ),
        (
            "mixed",
            ("app/app.py", "src/App.tsx"),
            ("app/app.py", "src/App.tsx"),
            "deterministic_existing_regression_tests",
        ),
        (
            "test-only",
            ("tests/test_app.py",),
            ("tests/test_app.py",),
            "candidate_changed_python_tests",
        ),
        (
            "documentation",
            ("README.md",),
            ("README.md",),
            "no_trustworthy_focused_tests",
        ),
        (
            "config",
            ("config/settings.toml",),
            ("config/settings.toml",),
            "no_trustworthy_focused_tests",
        ),
    ],
)
def test_verification_selection_is_explicit_and_language_aware(
    tmp_path: Path,
    label: str,
    observed: tuple[str, ...],
    verification: tuple[str, ...],
    expected_source: str,
) -> None:
    del label
    for path in observed:
        _write(
            tmp_path,
            path,
            (
                "def test_app():\n    assert True\n"
                if path.endswith(".py")
                else "content\n"
            ),
        )
    if "app/app.py" in observed or "app/app.py" in verification:
        _write(tmp_path, "tests/test_app.py", "def test_app():\n    assert True\n")

    selection = select_candidate_verification(
        project_dir=tmp_path,
        change_set={"added_files": [], "modified_files": [], "deleted_files": []},
        plan=[],
        task_prompt="Verify the candidate",
        observed_scope=observed,
        verification_scope=verification,
    )

    assert selection.source == expected_source
    if expected_source == "deterministic_existing_regression_tests":
        assert selection.paths == ("tests/test_app.py",)
    elif expected_source == "candidate_changed_python_tests":
        assert selection.paths == ("tests/test_app.py",)
    else:
        assert selection.paths == ()


def test_authority_missing_is_a_fail_closed_validator_rejection(tmp_path: Path) -> None:
    observed = "app/allowed.py"
    plan = _plan(observed)
    _write(tmp_path, observed)
    result = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=plan,
        task_prompt="Implement the requested files",
        execution_profile="full_lifecycle",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "candidate_delta_required": True,
            "change_set": {
                "added_files": [],
                "modified_files": [observed],
                "deleted_files": [],
            },
        },
        require_accepted_path_authority=True,
        accepted_path_authority_error={"code": "authority_record_missing"},
    )

    assert result.status == "rejected"
    assert result.repairable is False
    assert result.details["candidate_authority_invariant_failed"] is True
    assert any(
        finding.rule_id == "candidate_accepted_path_authority_missing"
        for finding in result.findings
    )
