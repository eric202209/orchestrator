"""PHASE36-MAINT-RPA2 — filename exemption authority.

The read-only verification guards exempted any file whose name started with
``verify`` or ``check``.  The exemption was introduced for creating a new
helper such as ``verify.js``, but it also matched existing Product source
(``checkout.py``, ``src/verifyToken.js``), so a verification-profile Plan
received ``existing_mutable`` authority over it.  It also let a verification
Plan create Product-like files (``checkout.js``).  Only a new, conventionally
named helper written by ``write_file``/``append_file`` is now exempt.
"""

from __future__ import annotations

import pytest

from app.services.orchestration.types import PlanAccepted
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.rules.contract_verification import (
    _is_new_verification_helper,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_rpa_read_only_profile_plan_admission import (
    VERIFY,
    _admit,
    _assert_repair_required,
    _grants,
    _replace,
    _seed,
)

BASE = "status = 'ready'\n"
PFX_PATHS = [
    "checkout.py",
    "checks.py",
    "checkout.js",
    "src/checker.ts",
    "src/verifyToken.js",
]
EXISTING_HELPER_PATHS = [
    "verify_status.js",
    "check_status.py",
    "verify_token.ts",
    "verify.js",
    "src/check-assets.sh",
]


def _op(op_name: str, path: str) -> dict:
    return {
        "replace_in_file": _replace(path),
        "write_file": {"op": "write_file", "path": path, "content": "x = 1\n"},
        "append_file": {"op": "append_file", "path": path, "content": "\n"},
    }[op_name]


# --- P36F2-PFX counterfactual: existing Product source ----------------------


@pytest.mark.parametrize("path", PFX_PATHS)
def test_pfx_replace_existing_product_source_requires_repair(tmp_path, path):
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[_replace(path)])
    _assert_repair_required(outcome, path)
    assert (tmp_path / path).read_text(encoding="utf-8") == BASE


@pytest.mark.parametrize("op_name", ["write_file", "append_file"])
@pytest.mark.parametrize("path", PFX_PATHS)
def test_pfx_write_and_append_existing_product_source_require_repair(
    tmp_path, path, op_name
):
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[_op(op_name, path)])
    assert not isinstance(outcome, PlanAccepted)
    assert outcome.verdict.details["verification_profile_mutated_source_assets"] == [
        path
    ]
    assert accepted_path_authority_from_verdict(outcome.verdict) is None


# --- Existing verify*/check* files are not distinguishable from Product source


@pytest.mark.parametrize("op_name", ["replace_in_file", "write_file"])
@pytest.mark.parametrize("path", EXISTING_HELPER_PATHS)
def test_existing_helper_named_file_gets_no_mutable_authority(tmp_path, path, op_name):
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[_op(op_name, path)])
    assert not isinstance(outcome, PlanAccepted)
    assert outcome.verdict.details["verification_profile_mutated_source_assets"] == [
        path
    ]
    assert accepted_path_authority_from_verdict(outcome.verdict) is None


@pytest.mark.parametrize("suffix", [".py", ".js", ".ts", ".sh"])
@pytest.mark.parametrize("parent", ["", "src/", "lib/store/"])
@pytest.mark.parametrize("stem", ["checkout", "checks", "checker", "verifyToken"])
def test_product_like_names_across_dirs_and_suffixes(tmp_path, stem, parent, suffix):
    path = f"{parent}{stem}{suffix}"
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[_replace(path)])
    _assert_repair_required(outcome, path)


# --- New verification helpers (historical 2cbc27e contract) -----------------


def _new_helper_plan(path: str) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Create content-aware verification script",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": path,
                    "content": (
                        "const fs=require('fs');"
                        "if(!fs.existsSync('index.html')) process.exit(1);"
                    ),
                }
            ],
            "verification": f"node {path}",
            "rollback": f"rm -f {path}",
            "expected_files": [path],
        }
    ]


def _validate_new_helper(tmp_path, path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">', encoding="utf-8"
    )
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    return ValidatorService.validate_plan(
        _new_helper_plan(path),
        output_text="[]",
        task_prompt="Improve static site verification commands",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )


@pytest.mark.parametrize(
    "path", ["verify.js", "verify_status.js", "check-assets.js", "check.sh"]
)
def test_new_conventional_helper_creation_is_preserved(tmp_path, path):
    outcome = _validate_new_helper(tmp_path, path)
    assert isinstance(outcome, PlanAccepted)
    assert outcome.verdict.profile == "verification"
    assert _grants(outcome) == [(path, "creation_authorized")]


@pytest.mark.parametrize(
    "path", ["checkout.js", "verifyToken.js", "checks.js", "checker.ts"]
)
def test_new_product_like_file_is_not_a_helper(tmp_path, path):
    outcome = _validate_new_helper(tmp_path, path)
    assert not isinstance(outcome, PlanAccepted)
    details = outcome.verdict.details
    assert details["verification_profile_mutated_source_assets"] == [path]
    assert details["verification_profile_created_source_assets"] == [path]


def test_new_helper_name_cannot_overwrite_existing_file(tmp_path):
    _seed(tmp_path, "verify.js", "module.exports = 1;\n")
    outcome = _validate_new_helper(tmp_path, "verify.js")
    assert not isinstance(outcome, PlanAccepted)
    assert outcome.verdict.details["verification_profile_mutated_source_assets"] == [
        "verify.js"
    ]


@pytest.mark.parametrize(
    ("op_name", "path", "exists", "expected"),
    [
        ("write_file", "verify.js", False, True),
        ("append_file", "src/check_status.py", False, True),
        ("write_file", "check-assets.sh", False, True),
        ("write_file", "verify.js", True, False),
        ("replace_in_file", "verify.js", False, False),
        ("write_file", "checkout.py", False, False),
        ("write_file", "verifyToken.js", False, False),
        ("write_file", "verification.js", False, False),
        ("write_file", "checks.py", False, False),
    ],
)
def test_new_helper_predicate(tmp_path, op_name, path, exists, expected):
    if exists:
        _seed(tmp_path, path, BASE)
    assert _is_new_verification_helper(op_name, path, tmp_path) is expected


# --- Established policies preserved -----------------------------------------


@pytest.mark.parametrize("path", ["tests/test_app.py", "test/app.js", "spec/app.js"])
def test_top_level_test_dir_policy_is_unchanged(tmp_path, path):
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[_replace(path)])
    assert isinstance(outcome, PlanAccepted)
    assert _grants(outcome) == [(path, "existing_mutable")]


@pytest.mark.parametrize("path", ["checkout.py", "verify_status.js"])
def test_delete_file_keeps_stronger_rejection(tmp_path, path):
    _seed(tmp_path, path, BASE)
    _, outcome = _admit(
        tmp_path, path, VERIFY, ops=[{"op": "delete_file", "path": path}]
    )
    assert not isinstance(outcome, PlanAccepted)
    assert outcome.verdict.status == "rejected"
    assert any("deletion_authorization_unavailable" in r for r in outcome.reasons)


def test_read_only_verification_of_checkout_is_admitted_read_only(tmp_path):
    _seed(tmp_path, "checkout.js", BASE)
    _, outcome = _admit(tmp_path, "checkout.js", VERIFY)
    assert isinstance(outcome, PlanAccepted)
    assert outcome.verdict.profile == "verification"
    assert _grants(outcome) == [("checkout.js", "existing_readonly")]


def test_implementation_checkout_mutation_is_still_admitted(tmp_path):
    _seed(tmp_path, "checkout.js", "export const status = 'ready';\n")
    check = (
        "node -e \"const s=require('fs').readFileSync('checkout.js','utf8');"
        " if(!s.includes('done')) process.exit(1)\""
    )
    op = {
        "op": "replace_in_file",
        "path": "checkout.js",
        "old": "'ready'",
        "new": "'done'",
    }
    _, outcome = _admit(
        tmp_path,
        "checkout.js",
        "Update the checkout export so it returns done.",
        ops=[op],
        verification=check,
    )
    assert isinstance(outcome, PlanAccepted)
    assert outcome.verdict.profile == "implementation"
    assert _grants(outcome) == [("checkout.js", "existing_mutable")]
