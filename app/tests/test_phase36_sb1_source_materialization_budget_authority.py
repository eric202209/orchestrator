"""PHASE36-SB1 provider-free source-materialization budget-authority tests.

PA1 established that the canonical Grounding handoff built a
``PlannerSourceMaterialization`` without propagating the cap fields governing
it, so they fell back to the unrelated planner-prompt defaults (2000 per file /
5000 total) while the evidence was sized by Grounding's own per-observation
bound (8192).  The post-Plan source fence then re-validated the stale total and
rejected *every* Plan containing any mutating operation -- including a fully
grounded, semantically correct repair of the cited implementation file.

These tests fence the reconciled contract: the producer declares the budget it
actually used, and the consumer verifies that declared budget.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from app.services.orchestration.phases.post_plan_source_grounding import (
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingRunConfig,
    GroundingTaskReference,
    build_grounding_planning_context,
)
from app.services.orchestration.planning.grounding.contracts import (
    MAX_OBSERVATION_BYTES,
)
from app.services.orchestration.planning.source_materialization import (
    MAX_RELEVANT_FILES,
    SOURCE_STATUS_EXISTING,
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    current_source_version_identity,
)


APPROVAL = "app/services/permissions/approval.py"


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)

    def decide(self, context):
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited repository evidence is sufficient.",
    }


def _result(root: Path, responses):
    config = GroundingRunConfig(
        grounding_run_id="sb1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-1",
        max_steps=5,
        max_exploration_provider_requests=4,
        operator_task="Find the implementation.",
    )
    return GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-1"),
        provider=_Provider(responses),
        config=config,
    ).run()


def _large_source(marker: str = "# approval", lines: int = 2500) -> str:
    return f"{marker}\n" + ("x = 1\n" * lines)


def _record(root: Path, relative_path: str, raw: bytes, included: int):
    """Build one materialized record the way the Grounding handoff does."""

    content = raw[:included].decode("utf-8", errors="replace")
    return MaterializedSourceFile(
        relative_path=relative_path,
        workspace_identity=str(root),
        content=content,
        content_hash=hashlib.sha256(raw).hexdigest(),
        version_identity=current_source_version_identity(root / relative_path),
        status=SOURCE_STATUS_EXISTING,
        truncated=len(content) < len(raw),
        source_length=len(raw),
        source_length_chars=len(raw.decode("utf-8", errors="replace")),
        included_prompt_length=len(content),
        expected=False,
        creation_authorized=False,
        priority="P0",
        selection_strategy="grounding_structural_region",
        full_source_bytes=len(raw),
        included_source_bytes=len(content),
    )


def _handoff(root: Path, records, *, per_file=None, total=None):
    """Mirror the reconciled declaration in consumer._materialize_evidence."""

    records = tuple(records)
    return PlannerSourceMaterialization(
        workspace_identity=str(root),
        files=records,
        maximum_files=MAX_RELEVANT_FILES,
        maximum_bytes_per_file=MAX_OBSERVATION_BYTES if per_file is None else per_file,
        maximum_total_source_bytes=(
            MAX_RELEVANT_FILES * MAX_OBSERVATION_BYTES if total is None else total
        ),
        materialized_source_bytes=sum(item.included_source_bytes for item in records),
    )


def _seed(root: Path, relative_path: str, marker: str = "# approval"):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _large_source(marker)
    path.write_text(body, encoding="utf-8")
    return body.encode("utf-8")


def _fence(plan, root: Path, materialization):
    return ground_post_plan_source_materialization(
        plan,
        project_dir=root,
        source_materialization=materialization,
        workspace_identity=str(root),
    )


def _repair_plan(path: str = APPROVAL):
    return [
        {
            "step_number": 1,
            "description": "Correct the permission policy",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": path,
                    "old": "# approval",
                    "new": "# approval fixed",
                }
            ],
        }
    ]


def _write_plan(path: str):
    return [
        {
            "step_number": 1,
            "description": "Create a new file",
            "commands": [],
            "ops": [{"op": "write_file", "path": path, "content": "x = 1\n"}],
        }
    ]


# --------------------------------------------------------------------------
# Producer: the canonical handoff declares the budget it actually used.
# --------------------------------------------------------------------------


def test_canonical_handoff_declares_grounding_owned_caps(tmp_path):
    """The handoff must not inherit the unrelated planner-prompt defaults."""

    root = _repo(tmp_path, {"app/sample.py": "def target():\n    return 'needle'\n"})
    result = _result(
        root,
        [
            {
                "action": "resolve_structure",
                "relation": "symbol_definition",
                "locator": {"path": "app/sample.py", "name": "target"},
            },
            _sufficient,
        ],
    )
    materialization = build_grounding_planning_context(
        result, project_dir=root
    ).source_materialization

    assert materialization.maximum_bytes_per_file == MAX_OBSERVATION_BYTES
    assert materialization.maximum_files == MAX_RELEVANT_FILES
    assert (
        materialization.maximum_total_source_bytes
        == MAX_RELEVANT_FILES * MAX_OBSERVATION_BYTES
    )
    # The stale planner defaults that caused the A2D rejection must be gone.
    assert materialization.maximum_bytes_per_file != 2000
    assert materialization.maximum_total_source_bytes != 5000


def test_canonical_handoff_is_internally_self_consistent(tmp_path):
    """A valid Grounding state can never declare a budget it has already broken."""

    root = _repo(tmp_path, {"app/sample.py": "def target():\n    return 'needle'\n"})
    result = _result(
        root,
        [
            {
                "action": "resolve_structure",
                "relation": "symbol_definition",
                "locator": {"path": "app/sample.py", "name": "target"},
            },
            _sufficient,
        ],
    )
    materialization = build_grounding_planning_context(
        result, project_dir=root
    ).source_materialization

    assert materialization.materialized_source_bytes == sum(
        item.included_source_bytes for item in materialization.files
    )
    assert (
        materialization.materialized_source_bytes
        <= materialization.maximum_total_source_bytes
    )
    assert len(materialization.files) <= materialization.maximum_files
    for item in materialization.files:
        assert item.included_source_bytes <= materialization.maximum_bytes_per_file


def test_structural_region_at_the_observation_bound_stays_within_declared_caps(
    tmp_path,
):
    """The 8192-byte structural region is the exact mechanism PA1 exposed."""

    root = _repo(tmp_path, {"app/sample.py": _large_source("def target(): pass")})
    result = _result(
        root,
        [
            {
                "action": "resolve_structure",
                "relation": "symbol_definition",
                "locator": {"path": "app/sample.py", "name": "target"},
            },
            _sufficient,
        ],
    )
    materialization = build_grounding_planning_context(
        result, project_dir=root
    ).source_materialization

    for item in materialization.files:
        assert item.included_source_bytes <= MAX_OBSERVATION_BYTES
        assert item.included_source_bytes <= materialization.maximum_bytes_per_file
    assert (
        materialization.materialized_source_bytes
        <= materialization.maximum_total_source_bytes
    )


# --------------------------------------------------------------------------
# Consumer: the fence verifies the declared budget (PA1 closure criteria).
# --------------------------------------------------------------------------


def test_a2d_counterfactual_grounded_source_repair_is_accepted(tmp_path):
    """PA1's direct closure criterion: the correct repair must pass the fence."""

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(root, [_record(root, APPROVAL, raw, 8192)])

    result = _fence(_repair_plan(), root, materialization)

    assert materialization.materialized_source_bytes == 8192
    assert result.failure_code is None
    assert result.ok


def test_a2d_original_test_only_plan_is_no_longer_budget_rejected(tmp_path):
    """SB1 removes only the categorical budget blocker.

    The A2D Plan remains semantically inadequate -- it mutates no implementation
    path -- but that is PC1's concern, not the source fence's.  What must not
    happen any more is a byte-budget rejection.
    """

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    plan = [
        {
            "step_number": 1,
            "description": "Inspect",
            "commands": [],
            "expected_files": [APPROVAL],
            "ops": [],
        },
        {
            "step_number": 2,
            "description": "Update the permission policy and add regression tests",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/tests/test_permission_policy_regression.py",
                    "content": "import pytest\n",
                }
            ],
        },
    ]

    result = _fence(plan, root, _handoff(root, [_record(root, APPROVAL, raw, 8192)]))

    assert result.failure_code is None


def test_multi_file_materialization_accounting_and_acceptance(tmp_path):
    """Grounding may cite several paths; the accounting must stay exact."""

    root = tmp_path.resolve()
    first = _seed(root, APPROVAL)
    second = _seed(root, "app/services/workspace/permissions.py", marker="# workspace")
    third = _seed(root, "app/services/permissions/policy.py", marker="# policy")
    records = [
        _record(root, APPROVAL, first, 8192),
        _record(root, "app/services/workspace/permissions.py", second, 8192),
        _record(root, "app/services/permissions/policy.py", third, 4096),
    ]
    materialization = _handoff(root, records)

    assert materialization.materialized_source_bytes == 8192 + 8192 + 4096
    assert materialization.materialized_source_bytes == sum(
        item.included_source_bytes for item in materialization.files
    )
    assert (
        materialization.materialized_source_bytes
        <= materialization.maximum_total_source_bytes
    )
    assert _fence(_repair_plan(), root, materialization).failure_code is None


def test_exact_total_cap_boundary_is_accepted(tmp_path):
    """The fence uses ``>``; equality is within budget and stays accepted."""

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(
        root, [_record(root, APPROVAL, raw, 5000)], per_file=5000, total=5000
    )

    assert materialization.materialized_source_bytes == 5000
    assert _fence(_repair_plan(), root, materialization).failure_code is None


# --------------------------------------------------------------------------
# The budget fence must still fail closed on a genuinely over-budget input.
# --------------------------------------------------------------------------


def test_true_total_overflow_still_fails_closed(tmp_path):
    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(
        root, [_record(root, APPROVAL, raw, 5001)], per_file=8192, total=5000
    )

    result = _fence(_repair_plan(), root, materialization)

    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert result.failure_detail == (
        "grounding would exceed the existing total source-byte bound"
    )


def test_true_per_file_overflow_still_fails_closed(tmp_path):
    """A record over its own declared per-file bound is rejected by path.

    Before SB1 the per-file cap was only propagated into re-materialization, so
    an inbound record could exceed its declared bound while the total still fit.
    """

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(
        root, [_record(root, APPROVAL, raw, 3000)], per_file=2000, total=8192
    )

    result = _fence(_repair_plan(), root, materialization)

    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert result.failure_path == APPROVAL
    assert result.failure_detail == (
        "materialized source exceeds the declared per-file byte bound"
    )


# --------------------------------------------------------------------------
# PA1's path-authority matrix must be unchanged by SB1.
# --------------------------------------------------------------------------


def test_new_test_file_creation_remains_accepted(tmp_path):
    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    result = _fence(
        _write_plan("app/tests/test_permission_policy_regression.py"),
        root,
        _handoff(root, [_record(root, APPROVAL, raw, 8192)]),
    )
    assert result.failure_code is None


def test_new_source_file_creation_remains_accepted(tmp_path):
    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    result = _fence(
        _write_plan("app/services/permissions/new_helper.py"),
        root,
        _handoff(root, [_record(root, APPROVAL, raw, 8192)]),
    )
    assert result.failure_code is None


def test_existing_ungrounded_source_is_self_grounded_and_accepted(tmp_path):
    """Authority is rebuilt from the Runtime Workspace, never from the Plan.

    The declared total must leave room for the records the fence itself adds
    while self-grounding a target Grounding never cited.
    """

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    other = root / "app/services/other.py"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("def f():\n    return 1\n", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Mutate an ungrounded existing file",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "app/services/other.py",
                    "old": "return 1",
                    "new": "return 2",
                }
            ],
        }
    ]
    result = _fence(plan, root, _handoff(root, [_record(root, APPROVAL, raw, 8192)]))

    assert result.failure_code is None
    assert "app/services/other.py" in result.grounded_paths


def test_absent_replace_and_delete_targets_remain_rejected(tmp_path):
    """Creation semantics stay graded: write_file may create, replace/delete not."""

    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(root, [_record(root, APPROVAL, raw, 8192)])

    for operation in ("replace_in_file", "delete_file"):
        plan = [
            {
                "step_number": 1,
                "description": "Mutate an absent path",
                "commands": [],
                "ops": [
                    {
                        "op": operation,
                        "path": "app/services/absent.py",
                        "old": "a",
                        "new": "b",
                    }
                ],
            }
        ]
        result = _fence(plan, root, materialization)
        assert result.failure_code == "POST_PLAN_GROUNDING_MISSING", operation


def test_productroot_escape_remains_rejected(tmp_path):
    root = tmp_path.resolve()
    raw = _seed(root, APPROVAL)
    materialization = _handoff(root, [_record(root, APPROVAL, raw, 8192)])

    result = _fence(_write_plan("../outside.py"), root, materialization)

    assert result.failure_code is not None
    assert result.ok is False
