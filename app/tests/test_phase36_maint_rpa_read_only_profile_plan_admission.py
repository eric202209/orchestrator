"""PHASE36-MAINT-RPA — read-only profile Plan admission.

A ``verification``-profile Plan must not receive mutation authority for
Product source.  The guard used a private extension list that had drifted from
the canonical ``SOURCE_EXTENSIONS`` (it omitted ``.js`` and ``.sh``), so a
grounded ``replace_in_file`` on an existing ``app.js`` was accepted with an
``existing_mutable`` grant.  The guard now reuses ``SOURCE_EXTENSIONS``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.phases.post_plan_source_grounding import (
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.types import PlanAccepted, PlanRepairRequired
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.integrity import is_product_source_path
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_checks import SOURCE_EXTENSIONS

VERIFY = "Verify the status export."
INSPECT = "Inspect the status export."
JS_BASE = "export const status = 'ready';\n"
MUTATED_REASON = (
    "Verification/review plan mutates app source assets instead of only "
    "verifying the current workspace (files: ['{path}'])"
)


def _seed(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _admit(root: Path, path: str, task: str, *, ops=(), verification=None):
    verification = f"cat {path}" if verification is None else verification
    plan = [
        {
            "step_number": 1,
            "description": "Check the status export",
            "commands": [verification or "true"],
            "verification": verification,
            "rollback": None,
            "expected_files": [path],
            "ops": list(ops),
        }
    ]
    materialization = materialize_planner_source_context(
        root, expected_paths=[path], workspace_identity=str(root), source_cache={}
    )
    grounded = ground_post_plan_source_materialization(
        plan,
        project_dir=root,
        source_materialization=materialization,
        workspace_identity=str(root),
    )
    assert grounded.ok
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        title=task,
        source_materialization=grounded.materialization,
    )
    return plan, outcome


def _grants(outcome) -> list[tuple[str, str]]:
    if not isinstance(outcome, PlanAccepted):
        return []
    apa = accepted_path_authority_from_verdict(outcome.verdict)
    return [(g.path.value, g.grant_class.value) for g in (apa.grants if apa else ())]


def _replace(path: str) -> dict:
    return {"op": "replace_in_file", "path": path, "old": "'ready'", "new": "'brokn'"}


def _assert_repair_required(outcome, path: str) -> None:
    assert isinstance(outcome, PlanRepairRequired)
    assert outcome.verdict.profile == "verification"
    details = outcome.verdict.details
    assert details["verification_profile_mutated_source_assets"] == [path]
    assert "verification_mutates_source_assets" in details["semantic_violation_codes"]
    assert MUTATED_REASON.format(path=path) in outcome.reasons
    # R8: a repair-required Plan carries no Accepted Path Authority at all.
    assert accepted_path_authority_from_verdict(outcome.verdict) is None


# --- R1/R3/R13: exact P36F VPX shapes ---------------------------------------


@pytest.mark.parametrize(
    ("task", "verification"),
    [(VERIFY, "cat app.js"), (INSPECT, "")],
    ids=["VPX_js_verify_insufficient", "VPX_js_inspect_missing"],
)
def test_vpx_verification_replace_existing_js_requires_repair(
    tmp_path, task, verification
):
    _seed(tmp_path, "app.js", JS_BASE)
    _, outcome = _admit(
        tmp_path, "app.js", task, ops=[_replace("app.js")], verification=verification
    )
    _assert_repair_required(outcome, "app.js")
    assert (tmp_path / "app.js").read_text(encoding="utf-8") == JS_BASE


# --- R2: write_file .js ------------------------------------------------------


def test_verification_write_existing_js_requires_repair(tmp_path):
    _seed(tmp_path, "app.js", JS_BASE)
    op = {"op": "write_file", "path": "app.js", "content": "export const s = 2;\n"}
    _, outcome = _admit(tmp_path, "app.js", VERIFY, ops=[op])
    _assert_repair_required(outcome, "app.js")


def test_verification_create_new_js_is_reported_as_created_source(tmp_path):
    _seed(tmp_path, "keep.txt", "x\n")
    op = {"op": "write_file", "path": "app.js", "content": "export const s = 2;\n"}
    plan = [
        {
            "step_number": 1,
            "description": "Check the status export",
            "commands": ["ls"],
            "verification": "ls",
            "rollback": None,
            "expected_files": ["app.js"],
            "ops": [op],
        }
    ]
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=VERIFY,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title=VERIFY,
    )
    assert not isinstance(outcome, PlanAccepted)
    details = outcome.verdict.details
    assert details["verification_profile_created_source_assets"] == ["app.js"]
    assert details["verification_profile_mutated_source_assets"] == ["app.js"]


# --- R4: Python golden control -----------------------------------------------


def test_python_control_is_unchanged(tmp_path):
    _seed(tmp_path, "app.py", "status = 'ready'\n")
    _, outcome = _admit(
        tmp_path, "app.py", "Verify the status function.", ops=[_replace("app.py")]
    )
    _assert_repair_required(outcome, "app.py")


# --- R5/R6: read-only positive controls --------------------------------------


@pytest.mark.parametrize(
    "verification",
    [
        "cat app.js",
        "grep -n status app.js",
        "node -e \"const s=require('fs').readFileSync('app.js','utf8');"
        " if(!s.includes('ready')) process.exit(1)\"",
    ],
)
def test_read_only_verification_plan_is_admitted_without_mutation_grant(
    tmp_path, verification
):
    _seed(tmp_path, "app.js", JS_BASE)
    _, outcome = _admit(tmp_path, "app.js", VERIFY, verification=verification)
    assert isinstance(outcome, PlanAccepted)
    assert outcome.verdict.profile == "verification"
    assert _grants(outcome) == [("app.js", "existing_readonly")]


# --- R7: implementation profile .js policy unchanged -------------------------


def test_implementation_js_mutation_is_still_admitted(tmp_path):
    _seed(tmp_path, "app.js", JS_BASE)
    check = (
        "node -e \"const s=require('fs').readFileSync('app.js','utf8');"
        " if(!s.includes('done')) process.exit(1)\""
    )
    op = {"op": "replace_in_file", "path": "app.js", "old": "'ready'", "new": "'done'"}
    _, outcome = _admit(
        tmp_path,
        "app.js",
        "Update the status export so it returns done.",
        ops=[op],
        verification=check,
    )
    assert isinstance(outcome, PlanAccepted)
    assert outcome.verdict.profile == "implementation"
    assert _grants(outcome) == [("app.js", "existing_mutable")]


# --- R9/R10/R11: canonical source suffix matrix ------------------------------

# (path, canonical source, verification mutation admitted)
MATRIX = [
    ("app.py", True, False),
    ("app.js", True, False),
    ("app.jsx", True, False),
    ("app.ts", True, False),
    ("app.tsx", True, False),
    ("styles.css", True, False),
    ("page.html", True, False),
    ("styles.scss", True, False),
    ("icon.svg", True, False),
    ("script.sh", True, False),
    ("src/status.test.ts", True, False),
    # Established overrides of the guard, unchanged by RPA:
    # top-level test dirs and verify*/check* helpers stay writable.
    ("tests/test_app.py", True, True),
    ("verify_status.js", True, True),
    # Non-source controls.
    ("README.md", False, True),
    ("settings.json", False, True),
]


@pytest.mark.parametrize(("path", "canonical", "admitted"), MATRIX)
@pytest.mark.parametrize("op_name", ["replace_in_file", "write_file", "append_file"])
def test_canonical_source_matrix(tmp_path, op_name, path, canonical, admitted):
    assert (Path(path).suffix in SOURCE_EXTENSIONS) is canonical
    _seed(tmp_path, path, "status = 'ready'\n")
    op = {
        "replace_in_file": _replace(path),
        "write_file": {"op": "write_file", "path": path, "content": "x = 1\n"},
        "append_file": {"op": "append_file", "path": path, "content": "\n"},
    }[op_name]
    _, outcome = _admit(tmp_path, path, VERIFY, ops=[op])
    if admitted:
        assert isinstance(outcome, PlanAccepted)
        assert _grants(outcome) == [(path, "existing_mutable")]
    else:
        assert not isinstance(outcome, PlanAccepted)
        assert outcome.verdict.details[
            "verification_profile_mutated_source_assets"
        ] == [path]
        assert accepted_path_authority_from_verdict(outcome.verdict) is None


@pytest.mark.parametrize("path", ["app.js", "script.sh", "app.py", "README.md"])
def test_delete_file_keeps_stronger_rejection(tmp_path, path):
    _seed(tmp_path, path, "status = 'ready'\n")
    _, outcome = _admit(
        tmp_path, path, VERIFY, ops=[{"op": "delete_file", "path": path}]
    )
    assert not isinstance(outcome, PlanAccepted)
    assert outcome.verdict.status == "rejected"
    assert any("deletion_authorization_unavailable" in r for r in outcome.reasons)


# --- R12: shell mutation never yields mutation authority ---------------------


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("sed -i 's/ready/brokn/' app.js", "app.js"),
        ("echo x > app.js", "app.js"),
        ("sed -i 's/ready/brokn/' script.sh", "script.sh"),
    ],
)
def test_shell_mutation_gets_no_mutable_grant_and_is_rejected_at_completion(
    tmp_path, command, target
):
    _seed(tmp_path, "app.js", JS_BASE)
    _seed(tmp_path, "script.sh", "echo ready\n")
    plan, outcome = _admit(tmp_path, "app.js", VERIFY, verification="cat app.js")
    plan[0]["commands"] = [command]
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=VERIFY,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title=VERIFY,
        source_materialization=materialize_planner_source_context(
            tmp_path,
            expected_paths=["app.js"],
            workspace_identity=str(tmp_path),
            source_cache={},
        ),
    )
    assert _grants(outcome) == [("app.js", "existing_readonly")]
    apa = accepted_path_authority_from_verdict(outcome.verdict)
    (tmp_path / target).write_text("brokn\n", encoding="utf-8")
    verdict = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=plan,
        task_prompt=VERIFY,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=VERIFY,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": [target],
            "change_set": {
                "added_files": [],
                "modified_files": [target],
                "deleted_files": [],
            },
        },
        accepted_path_authority=apa,
        require_accepted_path_authority=True,
    )
    assert verdict.status == "rejected"
    assert any("outside the accepted mutation authority" in r for r in verdict.reasons)


# --- R14: VSA source classifier is not touched -------------------------------


def test_vsa_product_source_classifier_is_unchanged():
    expected = {
        "app.py": True,
        "app.js": True,
        "app.jsx": True,
        "app.ts": True,
        "app.tsx": True,
        "styles.css": False,
        "page.html": False,
        "styles.scss": False,
        "icon.svg": False,
        "script.sh": False,
        "README.md": False,
        "settings.json": False,
        "tests/test_app.py": False,
    }
    assert {path: is_product_source_path(path) for path in expected} == expected


# --- R15: deterministic replay -----------------------------------------------


def test_replay_is_result_stable(tmp_path):
    results = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        _seed(root, "app.js", JS_BASE)
        _, outcome = _admit(root, "app.js", VERIFY, ops=[_replace("app.js")])
        details = outcome.verdict.details
        results.append(
            (
                type(outcome).__name__,
                list(outcome.reasons),
                details["verification_profile_mutated_source_assets"],
                details["semantic_violation_codes"],
            )
        )
    assert results[0] == results[1]
