"""Phase 32N-2 — characterization of the operation-level repair gap.

These tests pin what the *entering* architecture can and cannot express. They
are deliberately assertions of absence: no production merge authority accepts an
operation-level repair keyed by (step_number, operation_index), preserves the
accepted steps of the rejected plan, and revalidates the merged result.

They are expected to keep passing until an operation-level repair contract is
authorized. When one lands, the assertions marked GAP below must be inverted by
that phase, which is the point of retaining them.

Fixtures are the retained Attempt 9 shape (Phase 32K-1): four steps, one
rejected `replace_in_file` at step 2 / operation 1 with stale `old` text.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

import pytest

from app.services.orchestration.planning.slot_repair import (
    SlotRepairError,
    SlotRepairTaskContext,
    compile_slots_to_typed_plan,
    extract_plan_slots,
    merge_repair_slots,
)
from app.services.planning.slot_merge_operator import (
    SlotMergeInput,
    SlotMergeOperator,
    _step_numbers_from_reasons,
)


CONTEXT_SERVICE = "app/services/workspace/context_service.py"

# The exact finding string the production planning flow emits for this defect
# (app/services/orchestration/phases/planning_flow.py).
PRODUCTION_FINDING = "replace_in_file old text not found in workspace in steps [2]"

STALE_OLD = (
    "import json\nimport logging\nfrom datetime import datetime\n"
    "from typing import Optional, Dict, Any, List\n"
    "from sqlalchemy.orm import Session as DBSession\n"
    "from sqlalchemy import func\nfrom app.models import (\n"
    "    SessionState,\n    ConversationHistory,\n    TaskCheckpoint,\n)\n"
    "logger = logging.getLogger(__name__)"
)
CORRECT_OLD = (
    "import json\nimport logging\nfrom datetime import datetime\n"
    "from typing import Optional, Dict, Any, List"
)
CORRECT_NEW = (
    "import json\nimport logging\n"
    "from typing import Optional, Dict, Any, List\n\n"
    "from app.time_utils import utc_now"
)


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


@pytest.fixture
def attempt9_plan() -> list[dict]:
    """The retained Attempt 9 rejected plan shape."""

    return [
        {
            "step_number": 1,
            "description": "Create the time_utils.py file with the utc_now helper",
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/time_utils.py",
                    "content": (
                        "from datetime import datetime, timezone\n"
                        "def utc_now() -> datetime:\n"
                        "    return datetime.now(timezone.utc)\n"
                    ),
                }
            ],
            "commands": [],
            "verification": 'python -c "from app.time_utils import utc_now"',
            "rollback": "rm -f app/time_utils.py",
            "expected_files": ["app/time_utils.py"],
        },
        {
            "step_number": 2,
            "description": "Update context_service.py to use the new utc_now helper",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": CONTEXT_SERVICE,
                    "old": STALE_OLD,
                    "new": STALE_OLD.replace("from datetime import datetime\n", ""),
                }
            ],
            "commands": [],
            "verification": 'python -c "import app.services.workspace.context_service"',
            "rollback": None,
            "expected_files": [CONTEXT_SERVICE],
        },
        {
            "step_number": 3,
            "description": "Update exported_at to use utc_now().isoformat()",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": CONTEXT_SERVICE,
                    "old": '"exported_at": datetime.utcnow().isoformat(),',
                    "new": '"exported_at": utc_now().isoformat(),',
                }
            ],
            "commands": [],
            "verification": 'python -c "import app.services.workspace.context_service"',
            "rollback": None,
            "expected_files": [CONTEXT_SERVICE],
        },
        {
            "step_number": 4,
            "description": "Create the test file for the utc_now helper",
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/tests/test_utc_now_helper.py",
                    "content": "from app.time_utils import utc_now\n",
                }
            ],
            "commands": [],
            "verification": "python -m pytest app/tests/test_utc_now_helper.py -v",
            "rollback": "rm -f app/tests/test_utc_now_helper.py",
            "expected_files": ["app/tests/test_utc_now_helper.py"],
        },
    ]


@pytest.fixture
def operation_level_repair() -> dict:
    """The minimal operation-level repair response this phase specified."""

    return {
        "repairs": [
            {
                "step_number": 2,
                "operation_index": 1,
                "replacement_operation": {
                    "op": "replace_in_file",
                    "path": CONTEXT_SERVICE,
                    "old": CORRECT_OLD,
                    "new": CORRECT_NEW,
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1. A one-operation defect requires complete-plan regeneration today.
# ---------------------------------------------------------------------------


def test_slot_repair_cannot_represent_a_multi_operation_plan(attempt9_plan):
    """GAP: PlanSlots holds exactly one source op; Attempt 9 carries four."""

    task_context = SlotRepairTaskContext(
        allowed_target_files=(
            "app/time_utils.py",
            CONTEXT_SERVICE,
            "app/tests/test_utc_now_helper.py",
        ),
        allowed_verification_commands=("python3 -m pytest -q",),
        allow_test_changes=True,
    )
    slots = extract_plan_slots(attempt9_plan, task_context)

    total_ops = sum(len(step["ops"]) for step in attempt9_plan)
    assert total_ops == 4

    # Two ops target the same file, so extraction fails closed outright.
    assert slots.rejected is True
    assert any(
        "duplicate source materialization op" in reason
        for reason in slots.rejection_reasons
    )


def test_compile_slots_collapses_any_plan_to_two_fixed_steps(attempt9_plan):
    """GAP: compilation discards plan structure rather than preserving it."""

    task_context = SlotRepairTaskContext(
        allowed_target_files=(CONTEXT_SERVICE,),
        allowed_verification_commands=("python3 -m pytest -q",),
    )
    single_step = [attempt9_plan[2]]
    slots = extract_plan_slots(single_step, task_context)
    merged = dataclasses.replace(
        merge_repair_slots(slots, slots, PRODUCTION_FINDING),
        verification_command="python3 -m pytest -q",
    )

    compiled = compile_slots_to_typed_plan(merged)

    # A four-step plan can never survive: compilation always emits exactly two.
    assert len(compiled) == 2
    assert [step["step_number"] for step in compiled] == [1, 2]


def test_rejected_slots_cannot_be_compiled_at_all(attempt9_plan):
    task_context = SlotRepairTaskContext(
        allowed_target_files=(CONTEXT_SERVICE,),
        allowed_verification_commands=("python3 -m pytest -q",),
    )
    slots = extract_plan_slots(attempt9_plan, task_context)
    with pytest.raises(SlotRepairError):
        compile_slots_to_typed_plan(slots)


# ---------------------------------------------------------------------------
# 2. No authority accepts an operation-level repair document.
# ---------------------------------------------------------------------------


def test_no_merge_authority_accepts_operation_level_repairs(operation_level_repair):
    """GAP: SlotMergeOperator's only input is two COMPLETE plans."""

    fields = set(SlotMergeInput.__dataclass_fields__)
    assert fields == {
        "parent_a_plan",
        "parent_b_plan",
        "parent_a_reasons",
        "parent_b_reasons",
    }
    # There is no field that could carry a keyed replacement document.
    assert "repairs" not in fields
    assert not any("operation" in name for name in fields)
    assert "operation_index" in operation_level_repair["repairs"][0]


# ---------------------------------------------------------------------------
# 3. Identity is scraped from free text and does not match production findings.
# ---------------------------------------------------------------------------


def test_step_identity_regex_does_not_match_the_production_finding():
    """GAP: the production finding yields an EMPTY failed-step set."""

    assert _step_numbers_from_reasons((PRODUCTION_FINDING,)) == set()
    # It only matches a "step N" spelling the planning flow does not emit here.
    assert _step_numbers_from_reasons(("step 2 op 1 (x.py)",)) == {2}


def test_slot_merge_silently_returns_parent_a_for_the_production_finding(
    attempt9_plan,
):
    """GAP: unmatched identity degrades to a silent no-op, not a fail-closed."""

    repaired = copy.deepcopy(attempt9_plan)
    repaired[1]["ops"][0]["old"] = CORRECT_OLD
    repaired[1]["ops"][0]["new"] = CORRECT_NEW

    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=attempt9_plan,
            parent_b_plan=repaired,
            parent_a_reasons=(PRODUCTION_FINDING,),
            parent_b_reasons=(),
        )
    )

    # The correct repair is discarded and the stale plan is returned unchanged.
    assert _canonical(result.merged_plan) == _canonical(attempt9_plan)
    assert result.merged_plan[1]["ops"][0]["old"] == STALE_OLD


def test_slot_merge_keeps_the_stale_plan_when_both_parents_fail(attempt9_plan):
    """GAP: the real Attempt 9 case — both lineages fail — discards the repair."""

    repaired = copy.deepcopy(attempt9_plan)
    repaired[1]["ops"][0]["old"] = CORRECT_OLD

    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=attempt9_plan,
            parent_b_plan=repaired,
            parent_a_reasons=("step 2 stale",),
            parent_b_reasons=("step 2 stale",),
        )
    )
    assert result.merged_plan[1]["ops"][0]["old"] == STALE_OLD


# ---------------------------------------------------------------------------
# 4. Required safety invariants are absent from the existing merge.
# ---------------------------------------------------------------------------


def test_slot_merge_accepts_an_unauthorized_new_step_and_path(attempt9_plan):
    """GAP: a step present only in parent B is merged in unconditionally."""

    injected = copy.deepcopy(attempt9_plan)
    injected.append(
        {
            "step_number": 9,
            "description": "unauthorized injected step",
            "ops": [
                {"op": "write_file", "path": "app/unauthorized.py", "content": "x"}
            ],
            "commands": [],
            "verification": "true",
            "rollback": None,
            "expected_files": ["app/unauthorized.py"],
        }
    )

    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=attempt9_plan,
            parent_b_plan=injected,
            parent_a_reasons=("step 2 stale",),
            parent_b_reasons=(),
        )
    )

    paths = [
        op.get("path") for step in result.merged_plan for op in (step.get("ops") or [])
    ]
    assert len(result.merged_plan) == 5
    assert "app/unauthorized.py" in paths


def test_slot_merge_accepts_a_changed_path_for_a_replaced_step(attempt9_plan):
    """GAP: no path-equality invariant between rejected and replacement op."""

    diverted = copy.deepcopy(attempt9_plan)
    diverted[1]["ops"][0]["path"] = "app/somewhere_else.py"

    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=attempt9_plan,
            parent_b_plan=diverted,
            parent_a_reasons=("step 2 stale",),
            parent_b_reasons=(),
        )
    )
    assert result.merged_plan[1]["ops"][0]["path"] == "app/somewhere_else.py"


def test_slot_merge_does_not_validate_or_check_source_version(attempt9_plan):
    """GAP: merge has no validator, no stale-old-text and no version check."""

    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=attempt9_plan,
            parent_b_plan=attempt9_plan,
            parent_a_reasons=("step 2 stale",),
            parent_b_reasons=(),
        )
    )
    # A plan whose step 2 old text is known-stale is returned as "merged".
    assert result.merged_plan[1]["ops"][0]["old"] == STALE_OLD
    assert not hasattr(result, "validator_verdict")
    assert not hasattr(result, "source_version_identity")


# ---------------------------------------------------------------------------
# 5. A minimal operation-level response fits well under the retained bound.
# ---------------------------------------------------------------------------


def test_operation_level_response_fits_the_unchanged_output_bound(
    operation_level_repair,
):
    """The output-side case for operation-level repair, at the deployed limit."""

    max_output_tokens = 2048  # planner.py RuntimeInvocationOptions, unchanged
    pretty = json.dumps(operation_level_repair, indent=2)

    # 4 chars/token is the stack's own estimator (planner._estimate_prompt_tokens);
    # 3.89 is the ratio measured by the Phase 32N-1 provider probe.
    assert len(pretty) < 1_000
    assert round(len(pretty) / 3.89) < max_output_tokens / 4

    # Phase 32N-1 recorded completion_tokens == max_tokens == 2048 for the
    # complete-plan contract on this exact fixture.
    assert round(len(pretty) / 3.89) < 2048


def test_target_merged_plan_changes_only_the_rejected_operation(
    attempt9_plan, operation_level_repair
):
    """Defines the result no production path currently produces."""

    target = copy.deepcopy(attempt9_plan)
    target[1]["ops"][0] = operation_level_repair["repairs"][0]["replacement_operation"]

    assert [step["step_number"] for step in target] == [1, 2, 3, 4]
    for index in (0, 2, 3):
        assert _hash(target[index]) == _hash(attempt9_plan[index])
    assert _hash(target[1]) != _hash(attempt9_plan[1])
    assert target[1]["ops"][0]["path"] == attempt9_plan[1]["ops"][0]["path"]
    assert target[1]["ops"][0]["op"] == attempt9_plan[1]["ops"][0]["op"]
