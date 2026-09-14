"""PHASE36-PC1 provider-free Planning behavioral-completeness regressions.

Phase36 observed the same Planning defect in A1B and A2D: a task asked for an
existing product behavior to be corrected, Grounding materialized the relevant
implementation source, and Planning emitted a structurally valid Plan whose only
executable mutation wrote a test.  In A2D the generated test asserted the
defective behavior was correct and should be preserved.

These tests fence the repaired contract and, just as importantly, fence it
against over-rejection: legitimate test-coverage, documentation, read-only, and
config/data behavioral-repair plans must all remain acceptable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

from app.services.orchestration.planning.behavioral_repair_contract import (
    BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE,
    BehavioralRepairContractVerdict,
    evaluate_behavioral_repair_contract,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    current_source_version_identity,
)
from app.services.orchestration.validation.validator import ValidatorService


APPROVAL = "app/services/permissions/approval.py"
APPROVAL_TEST = "app/tests/test_permission_policy_regression.py"
SETTINGS = "config/permissions.yaml"
DOCS = "docs/permissions.md"

# The A2D task meaning: an existing permission behavior is wrong and must be
# corrected.  Wording is faithful to the observed dogfood task, not copied
# verbatim, because the contract must not encode this specific task.
A2D_TASK = (
    "FILE_DELETE is auto-approved the first time it is requested, which is "
    "wrong. Fix the permission approval policy so a first-time FILE_DELETE "
    "request must not be approved without an explicit operator decision."
)
TEST_COVERAGE_TASK = (
    "Add regression test coverage for the permission approval service. The "
    "existing approval behavior is already correct; this task only adds tests "
    "that lock it in."
)
DOCS_TASK = (
    "Document the permission approval policy in the docs directory so "
    "operators can see which permission kinds require explicit approval."
)
ANALYSIS_TASK = (
    "Inspect the permission approval service and explain which permission "
    "kinds are approved on first use. Report the findings; do not change "
    "any files."
)

APPROVAL_SOURCE = (
    '"""Permission approval policy."""\n\n'
    "FIRST_TIME_APPROVE = {\n"
    '    "FILE_DELETE": set(),\n'
    "}\n"
)


def _file(
    path: str,
    *,
    status: str = SOURCE_STATUS_EXISTING,
    root: Path | None = None,
) -> MaterializedSourceFile:
    new_file = status == SOURCE_STATUS_NEW
    content = None if new_file else APPROVAL_SOURCE
    version_identity = None
    content_hash = None
    if root is not None and not new_file:
        content = (root / path).read_text(encoding="utf-8")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        version_identity = current_source_version_identity(root / path)
    return MaterializedSourceFile(
        relative_path=path,
        workspace_identity=str(root.resolve()) if root is not None else "pc1-workspace",
        content=content,
        content_hash=content_hash,
        version_identity=version_identity,
        status=status,
        truncated=False,
        source_length=None if content is None else len(content),
        source_length_chars=None if content is None else len(content),
        included_prompt_length=0 if content is None else len(content),
        expected=True,
        creation_authorized=new_file,
    )


def _materialization(
    *paths: str,
    new_paths: tuple[str, ...] = (),
    root: Path | None = None,
) -> PlannerSourceMaterialization:
    return PlannerSourceMaterialization(
        workspace_identity=str(root.resolve()) if root is not None else "pc1-workspace",
        files=tuple(_file(path, root=root) for path in paths)
        + tuple(_file(path, status=SOURCE_STATUS_NEW, root=root) for path in new_paths),
    )


def _write_step(
    path: str, *, description: str, expected_files: list[str] | None = None
):
    return {
        "step_number": 1,
        "description": description,
        "commands": [],
        "rollback": None,
        "verification": "python -m pytest -q",
        "expected_files": expected_files if expected_files is not None else [path],
        "ops": [{"op": "write_file", "path": path, "content": "x = 1\n"}],
    }


def _evaluate(plan, task_text, *paths):
    return evaluate_behavioral_repair_contract(
        plan=plan,
        task_text=task_text,
        source_materialization=_materialization(*(paths or (APPROVAL,))),
    )


# --------------------------------------------------------------------------
# Deterministic matrix P1-P9
# --------------------------------------------------------------------------


def test_p1_behavioral_repair_with_test_only_mutations_is_rejected():
    verdict = _evaluate(
        [_write_step(APPROVAL_TEST, description="Add regression test")], A2D_TASK
    )

    assert verdict.behavior_change_required is True
    assert verdict.behavior_change_satisfied is False
    assert verdict.failure_code == BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE
    assert verdict.test_only_mutation_paths == [APPROVAL_TEST]


def test_p2_behavioral_repair_with_implementation_and_tests_is_accepted():
    verdict = _evaluate(
        [
            _write_step(APPROVAL, description="Update the permission policy"),
            _write_step(APPROVAL_TEST, description="Add regression test"),
        ],
        A2D_TASK,
    )

    assert verdict.behavior_change_required is True
    assert verdict.passed
    assert verdict.behavior_capable_paths == [APPROVAL]


def test_p3_behavioral_repair_with_implementation_only_is_accepted():
    verdict = _evaluate(
        [_write_step(APPROVAL, description="Update the permission policy")], A2D_TASK
    )

    assert verdict.passed
    assert verdict.behavior_capable_paths == [APPROVAL]


def test_p4_legitimate_test_coverage_task_with_test_only_plan_is_accepted():
    verdict = _evaluate(
        [_write_step(APPROVAL_TEST, description="Add regression test")],
        TEST_COVERAGE_TASK,
    )

    assert verdict.behavior_change_required is False
    assert verdict.passed
    assert "already_correct" in verdict.suppression_signals


def test_p5_documentation_task_with_docs_only_plan_is_accepted():
    verdict = _evaluate(
        [_write_step(DOCS, description="Document the policy")], DOCS_TASK
    )

    assert verdict.behavior_change_required is False
    assert verdict.passed


def test_p6_read_only_analysis_plan_with_no_mutation_is_accepted():
    plan = [
        {
            "step_number": 1,
            "description": "Inspect the approval policy",
            "commands": ["cat " + APPROVAL],
            "rollback": None,
            "verification": "python -c \"print('ok')\"",
            "expected_files": [],
            "ops": [],
        }
    ]

    verdict = _evaluate(plan, ANALYSIS_TASK)

    assert verdict.mutating_paths == []
    assert verdict.passed


def test_p7_expected_files_implementation_does_not_satisfy_the_contract():
    """expected_files is a declaration, never a behavior mutation (R3)."""

    plan = [
        _write_step(
            APPROVAL_TEST,
            description="Add regression test",
            expected_files=[APPROVAL, APPROVAL_TEST],
        )
    ]

    verdict = _evaluate(plan, A2D_TASK)

    assert verdict.failure_code == BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE
    assert APPROVAL not in verdict.mutating_paths
    assert verdict.to_dict()["expected_files_counts_as_behavior_change"] is False


def test_p8_description_claiming_a_fix_does_not_satisfy_the_contract():
    """Prose is not execution authority (R2)."""

    plan = [
        _write_step(
            APPROVAL_TEST,
            description=(
                "Update the permission policy so FILE_DELETE requires approval"
            ),
            expected_files=[APPROVAL_TEST],
        )
    ]

    verdict = _evaluate(plan, A2D_TASK)

    assert verdict.failure_code == BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE
    assert verdict.to_dict()["description_counts_as_behavior_change"] is False


def test_p9_config_behavioral_repair_is_not_falsely_rejected():
    verdict = _evaluate(
        [
            _write_step(SETTINGS, description="Require approval for FILE_DELETE"),
            _write_step(APPROVAL_TEST, description="Add regression test"),
        ],
        A2D_TASK,
    )

    assert verdict.behavior_change_required is True
    assert verdict.passed
    assert verdict.behavior_capable_paths == [SETTINGS]


def test_p10_migration_behavioral_repair_is_not_falsely_rejected():
    verdict = _evaluate(
        [
            _write_step(
                "migrations/0007_require_delete_approval.sql",
                description="Backfill approval rows",
            ),
            _write_step(APPROVAL_TEST, description="Add regression test"),
        ],
        A2D_TASK,
    )

    assert verdict.passed
    assert verdict.behavior_capable_paths == [
        "migrations/0007_require_delete_approval.sql"
    ]


# --------------------------------------------------------------------------
# Fail-closed / fail-open boundaries
# --------------------------------------------------------------------------


def test_contract_stands_down_without_grounded_existing_implementation():
    """Ambiguous intent policy: no grounded implementation, no invariant."""

    verdict = evaluate_behavioral_repair_contract(
        plan=[_write_step(APPROVAL_TEST, description="Add regression test")],
        task_text=A2D_TASK,
        source_materialization=None,
    )

    assert verdict.behavior_change_required is False
    assert verdict.passed


def test_grounded_tests_alone_are_not_implementation_evidence():
    verdict = _evaluate(
        [_write_step(APPROVAL_TEST, description="Add regression test")],
        A2D_TASK,
        APPROVAL_TEST,
    )

    assert verdict.grounded_implementation_paths == []
    assert verdict.passed


def test_explicit_no_source_change_scope_suppresses_the_contract():
    verdict = _evaluate(
        [_write_step(APPROVAL_TEST, description="Add regression test")],
        "Fix the missing coverage for the approval policy without changing the "
        "implementation.",
    )

    assert verdict.suppression_signals == ["without_changing"]
    assert verdict.passed


def test_shell_written_implementation_satisfies_the_contract():
    """Shell-write targets resolved by the validator count as behavior change."""

    plan = [
        {
            "step_number": 1,
            "description": "Rewrite the policy file",
            "commands": [f"printf 'x = 1\\n' > {SETTINGS}"],
            "rollback": None,
            "verification": "python -m pytest -q",
            "expected_files": [SETTINGS],
            "ops": [],
        },
        _write_step(APPROVAL_TEST, description="Add regression test"),
    ]

    verdict = evaluate_behavioral_repair_contract(
        plan=plan,
        task_text=A2D_TASK,
        source_materialization=_materialization(APPROVAL),
        additional_mutation_paths=(SETTINGS,),
    )

    assert SETTINGS in verdict.behavior_capable_paths
    assert verdict.passed


# --------------------------------------------------------------------------
# A2D exact regression through ValidatorService.validate_plan
# --------------------------------------------------------------------------


def _seed(root: Path) -> None:
    """A2D workspace: the implementation exists; the test file does not yet."""

    target = root / APPROVAL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(APPROVAL_SOURCE, encoding="utf-8")
    (root / "app" / "tests").mkdir(parents=True, exist_ok=True)


def _validate(root: Path, plan: list[dict], prompt: str):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=prompt,
        execution_profile="implementation",
        project_dir=root,
        is_first_ordered_task=False,
        source_materialization=_materialization(
            APPROVAL, new_paths=(APPROVAL_TEST,), root=root
        ),
    )


def _a2d_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": (
                "Update the permission policy so FILE_DELETE is not approved on "
                "first use"
            ),
            "commands": [],
            "rollback": None,
            "verification": f"python -m pytest {APPROVAL_TEST} -q",
            # A2D declared the implementation file it never mutated.
            "expected_files": [APPROVAL, APPROVAL_TEST],
            "ops": [
                {
                    "op": "write_file",
                    "path": APPROVAL_TEST,
                    "content": (
                        "from app.services.permissions import approval\n\n\n"
                        "def test_file_delete_policy_preserved():\n"
                        '    assert approval.FIRST_TIME_APPROVE["FILE_DELETE"]'
                        " == set()\n"
                    ),
                }
            ],
        }
    ]


def test_a2d_test_only_plan_was_accepted_before_the_pc1_contract(tmp_path):
    """The recovered A2D Plan was structurally valid; only PC1 rejects it."""

    _seed(tmp_path)

    with mock.patch(
        "app.services.orchestration.validation.validator."
        "evaluate_behavioral_repair_contract",
        return_value=BehavioralRepairContractVerdict(),
    ):
        outcome = _validate(tmp_path, _a2d_plan(), A2D_TASK)

    assert outcome.accepted, outcome.reasons


def test_a1b_structurally_faithful_test_only_repair_is_rejected():
    """A1B's exact Plan is unrecoverable; this is a faithful equivalent."""

    plan = [
        {
            "step_number": 1,
            "description": "Fix the discount rounding defect",
            "commands": [],
            "rollback": None,
            "verification": "python -m pytest -q",
            "expected_files": ["src/pricing.py", "tests/test_pricing.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_pricing.py",
                    "content": "def test_rounding():\n    assert True\n",
                }
            ],
        }
    ]

    verdict = evaluate_behavioral_repair_contract(
        plan=plan,
        task_text=(
            "The discount rounding is wrong for half-cent totals. Fix the "
            "pricing calculation so it rounds half up."
        ),
        source_materialization=_materialization("src/pricing.py"),
    )

    assert verdict.failure_code == BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE
    assert verdict.grounded_implementation_paths == ["src/pricing.py"]


def test_a2d_test_only_plan_is_rejected_by_plan_validation(tmp_path):
    _seed(tmp_path)

    outcome = _validate(tmp_path, _a2d_plan(), A2D_TASK)

    assert not outcome.accepted
    assert BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE in (
        outcome.details.get("semantic_violation_codes") or []
    )
    contract = outcome.details["behavioral_repair_contract"]
    assert contract["failure_code"] == BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE
    assert contract["grounded_implementation_paths"] == [APPROVAL]
    assert contract["behavior_capable_paths"] == []
    # The rejection is repairable, so bounded Planning repair can regenerate.
    assert outcome.repairable


def test_a2d_repaired_plan_with_implementation_mutation_clears_the_contract(tmp_path):
    _seed(tmp_path)
    repaired = _a2d_plan()
    repaired[0]["ops"].insert(
        0,
        {
            "op": "replace_in_file",
            "path": APPROVAL,
            "old": '    "FILE_DELETE": set(),\n',
            "new": '    "FILE_DELETE": {"operator"},\n',
        },
    )

    outcome = _validate(tmp_path, repaired, A2D_TASK)

    assert BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE not in (
        outcome.details.get("semantic_violation_codes") or []
    )
    assert outcome.accepted, outcome.reasons
    contract = outcome.details["behavioral_repair_contract"]
    assert contract["behavior_change_required"] is True
    assert contract["behavior_change_satisfied"] is True


def test_legitimate_test_only_task_is_not_blocked_by_plan_validation(tmp_path):
    _seed(tmp_path)

    outcome = _validate(tmp_path, _a2d_plan(), TEST_COVERAGE_TASK)

    assert BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE not in (
        outcome.details.get("semantic_violation_codes") or []
    )
    assert outcome.details["behavioral_repair_contract"]["failure_code"] is None


def test_read_only_stage_does_not_evaluate_the_contract(tmp_path):
    _seed(tmp_path)

    outcome = ValidatorService.validate_plan(
        _a2d_plan(),
        output_text=json.dumps(_a2d_plan()),
        task_prompt=A2D_TASK,
        execution_profile="review_only",
        project_dir=tmp_path,
        is_first_ordered_task=False,
        source_materialization=_materialization(APPROVAL, root=tmp_path),
    )

    assert "behavioral_repair_contract" not in outcome.details
