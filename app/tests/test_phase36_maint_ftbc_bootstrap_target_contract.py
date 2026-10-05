"""Provider-free FTBC tests: Task-1 bootstrap evidence and repair target contract."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_prompts import (
    build_compact_planning_repair_prompt,
    build_planning_repair_prompt_with_metadata,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.planning.task_bootstrap_contract import (
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.validation.validator import ValidatorService

PERMISSIONS = "app/api/v1/endpoints/permissions.py"
ROUTER = "app/api/v1/router.py"
TASK = (
    "Make pending permissions collection available at its public route. "
    "Authenticated clients should be able to GET /api/v1/permissions/pending, "
    "and the doubled /api/v1/permissions/permissions/pending route should not "
    "be exposed."
)
SEMANTIC_SHAPE = "{op,path,target_id,new}"
UNAVAILABLE = "Semantic target mode is unavailable for this task."
HISTORICAL_TARGET_ID = "tgt_1c2010e9e4e77195896bf7f2"

# REENTRY-10 initial Alternative-B operation (new is 23 characters).
ALT_B = {
    "op": "replace_in_file",
    "path": PERMISSIONS,
    "old": '@router.get("/permissions/pending")',
    "new": '@router.get("/pending")',
}


def _seed_product(root: Path) -> None:
    endpoints = root / "app" / "api" / "v1" / "endpoints"
    endpoints.mkdir(parents=True)
    (endpoints / "permissions.py").write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        '@router.get("/permissions/pending")\n'
        "def list_pending():\n    return []\n\n\n"
        '@router.post("/permissions/check")\n'
        "def check():\n    return {}\n",
        encoding="utf-8",
    )
    (root / "app" / "api" / "v1" / "router.py").write_text(
        "from fastapi import APIRouter\n"
        "from app.api.v1.endpoints import permissions\n\n"
        "api_router = APIRouter()\n"
        "api_router.include_router(\n"
        "    permissions.router,\n"
        '    prefix="/permissions",\n'
        '    tags=["permissions"],\n'
        ")\n",
        encoding="utf-8",
    )
    (root / "app" / "tests").mkdir()
    (root / "app" / "tests" / "test_permissions.py").write_text(
        "def test_placeholder_route():\n    assert True\n", encoding="utf-8"
    )


def _plan(operation: dict[str, Any], path: str = PERMISSIONS) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Fix the doubled permission route",
            "commands": [],
            "verification": f"test -f {path}",
            "rollback": None,
            "expected_files": [path],
            "ops": [operation],
        },
        {
            "step_number": 2,
            "description": "Run the permission tests",
            "commands": ["python -m pytest app/tests/ -k permission -v"],
            "verification": "python -m pytest app/tests/ -k permission -v",
            "rollback": None,
            "expected_files": [],
        },
    ]


def _existing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _bootstrap(root: Path, plan: list[dict], planner_contract=None):
    return validate_task1_bootstrap_contract(
        plan=plan,
        task_prompt=TASK,
        existing_files=_existing(root),
        planner_contract=planner_contract,
        require_registered_contract=True,
    )


def _registered(**overrides: Any) -> dict[str, Any]:
    contract = {
        "contract_id": "ST23-PLANNER-001",
        "contract_version": "v1",
        "scenario_id": "S1-3",
        "source_expectation": "SOURCE_PRESENT",
        "test_expectation": "EXPECTED_TEST_NOT_REQUIRED",
        "structural_evidence": [
            "CONTRACT_REGISTERED",
            "SCENARIO_ID_MATCH",
            "SOURCE_EXPECTATION_DECLARED",
            "TEST_EXPECTATION_DECLARED",
        ],
    }
    contract.update(overrides)
    return contract


# ---------------------------------------------------------------------------
# H1 — Task-1 minimum implementation evidence for exact existing-file edits
# ---------------------------------------------------------------------------


def test_reentry10_short_exact_edit_of_existing_source_is_implementation_evidence(
    tmp_path,
):
    _seed_product(tmp_path)

    verdict = _bootstrap(tmp_path, _plan(ALT_B))

    assert verdict.passed, verdict.violation_codes
    assert verdict.contract.minimum_implementation_evidence is True
    # Missing certification facts stay diagnostic; nothing is synthesized.
    evidence = verdict.contract.classification_evidence
    assert evidence["contract_status"] == "missing_registered_contract_facts"
    assert evidence["missing_facts"] == ["CONTRACT_REGISTERED", "SCENARIO_ID_MATCH"]
    assert "CONTRACT_REGISTERED" not in verdict.contract.structural_evidence_used


@pytest.mark.parametrize(
    "old,new",
    [
        ('prefix="/permissions",', 'prefix="",'),
        ('    prefix="/permissions",\n', ""),
    ],
    ids=["prefix_empty", "line_delete"],
)
def test_alternative_a_surgical_forms_are_not_less_admissible(tmp_path, old, new):
    _seed_product(tmp_path)
    operation = {"op": "replace_in_file", "path": ROUTER, "old": old, "new": new}

    verdict = _bootstrap(tmp_path, _plan(operation, ROUTER))

    assert verdict.passed, verdict.violation_codes


@pytest.mark.parametrize(
    "operation",
    [
        # placeholder replacement
        {**ALT_B, "new": "pass  # TODO"},
        # short no-op edit
        {**ALT_B, "old": "return []", "new": "return []"},
        # no exact old anchor
        {"op": "replace_in_file", "path": PERMISSIONS, "new": "x = 1"},
    ],
    ids=["placeholder", "noop", "no_old"],
)
def test_short_edits_without_real_change_still_fail_closed(tmp_path, operation):
    _seed_product(tmp_path)

    verdict = _bootstrap(tmp_path, _plan(operation))

    assert not verdict.passed
    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        in verdict.violation_codes
    )


def test_files_created_by_the_plan_keep_the_stub_length_floor(tmp_path):
    plan = _plan({"op": "write_file", "path": "src/app.py", "content": "x = 1\n"})
    plan[0]["ops"].append(
        {"op": "replace_in_file", "path": "src/app.py", "old": "x = 1", "new": "x = 2"}
    )

    verdict = _bootstrap(tmp_path, plan)

    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        in verdict.violation_codes
    )


# Bootstrap matrix B1–B5, B8 (B6/B7 are traced in the FTBC report).


def test_b1_registered_contract_with_matching_scenario_passes(tmp_path):
    _seed_product(tmp_path)

    verdict = _bootstrap(tmp_path, _plan(ALT_B), _registered())

    assert verdict.passed, verdict.violation_codes
    assert verdict.contract.planner_contract_status == "registered"
    assert verdict.contract.scenario_id == "S1-3"


@pytest.mark.parametrize(
    "overrides,missing",
    [
        ({"contract_id": None}, None),
        ({"scenario_id": "S9-9"}, "SCENARIO_ID_MATCH"),
        ({"contract_id": None, "scenario_id": None}, None),
    ],
    ids=["b2_missing_registration", "b3_scenario_mismatch", "b4_both_missing"],
)
def test_b2_b4_registered_contract_gaps_fail_closed(tmp_path, overrides, missing):
    _seed_product(tmp_path)

    verdict = _bootstrap(tmp_path, _plan(ALT_B), _registered(**overrides))

    assert not verdict.passed
    assert "task1_bootstrap_missing_registered_contract_facts" in (
        verdict.violation_codes
    )
    if missing:
        assert missing in verdict.contract.classification_evidence["missing_facts"]


def test_b5_later_task_does_not_apply_task1_bootstrap(tmp_path):
    _seed_product(tmp_path)
    plan = _plan(ALT_B)

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        is_first_ordered_task=False,
    )

    assert "task1_bootstrap_contract" not in outcome.details


def test_b8_contract_identity_is_stable_across_initial_and_repaired_plans(tmp_path):
    _seed_product(tmp_path)
    contract = _registered()
    repaired = _plan({**ALT_B, "new": '@router.get("/pending")  '})

    initial = _bootstrap(tmp_path, _plan(ALT_B), contract)
    after_repair = _bootstrap(tmp_path, repaired, contract)

    assert initial.contract.scenario_id == after_repair.contract.scenario_id == "S1-3"
    assert initial.contract.contract_id == after_repair.contract.contract_id
    assert contract == _registered()


# ---------------------------------------------------------------------------
# H2 — generic repair must not advertise an unissued target-ID namespace
# ---------------------------------------------------------------------------


def _repair_prompt(root: Path, guidance_block: str = "") -> str:
    return build_planning_repair_prompt_with_metadata(
        task_description=TASK,
        malformed_output=json.dumps(_plan(ALT_B)),
        project_dir=root,
        rejection_reasons=[
            "Task 1 bootstrap planning contract failed: "
            "Task 1 bootstrap lacks minimum implementation evidence"
        ],
        guidance_block=guidance_block,
    ).prompt


def _zero_handle_ctx_block(root: Path) -> str:
    materialization = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[PERMISSIONS]
    )
    assert build_semantic_target_inventory(materialization).handles == ()
    return materialization.to_prompt_block(provider_safe=True)


def _handle_materialization(root: Path):
    (root / "target.txt").write_text("needle()\n", encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description="Replace the exact snippet `needle()` in target.txt.",
        expected_paths=["target.txt"],
    )


def test_t8_zero_handle_generic_repair_does_not_advertise_target_ids(tmp_path):
    _seed_product(tmp_path)

    prompt = _repair_prompt(tmp_path, _zero_handle_ctx_block(tmp_path))

    assert SEMANTIC_SHAPE not in prompt
    assert "use a listed Orchestrator `target_id`" not in prompt
    assert UNAVAILABLE in prompt
    assert "{op,path,old,new}" in prompt


def test_t8_zero_handle_compact_repair_does_not_advertise_target_ids(tmp_path):
    _seed_product(tmp_path)

    prompt = build_compact_planning_repair_prompt(
        json.dumps(_plan(ALT_B)),
        rejection_reasons=["malformed"],
        guidance_block=_zero_handle_ctx_block(tmp_path),
    )

    assert SEMANTIC_SHAPE not in prompt
    assert UNAVAILABLE in prompt


def test_t1_listed_handle_keeps_semantic_repair_contract_and_resolves(tmp_path):
    materialization = _handle_materialization(tmp_path)
    inventory = build_semantic_target_inventory(materialization)
    (handle,) = inventory.handles
    block = materialization.to_prompt_block(provider_safe=True)

    prompt = build_compact_planning_repair_prompt(
        "[]", rejection_reasons=["malformed"], guidance_block=block
    )
    normalized = normalize_provider_semantic_intents(
        [{"ops": [{**_semantic(handle.target_id), "path": "target.txt"}]}],
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )

    assert SEMANTIC_SHAPE in prompt
    assert f"target_id: {handle.target_id}" in prompt
    assert normalized[0]["ops"][0]["selector"]


def _semantic(target_id: str, path: str = "target.txt", op: str = "replace_in_file"):
    return {"op": op, "path": path, "target_id": target_id, "new": "other()\n"}


def _normalize(root: Path, materialization, operation):
    return normalize_provider_semantic_intents(
        [{"ops": [operation]}],
        inventory=build_semantic_target_inventory(materialization),
        project_dir=root,
        source_materialization=materialization,
    )


def _rejection_code(root: Path, materialization, operation) -> str:
    with pytest.raises(SemanticTargetContractError) as error:
        _normalize(root, materialization, operation)
    return error.value.code


def test_t2_historical_and_unknown_ids_are_rejected(tmp_path):
    materialization = _handle_materialization(tmp_path)

    assert (
        _rejection_code(tmp_path, materialization, _semantic(HISTORICAL_TARGET_ID))
        == "unknown_target_id"
    )


def test_t3_t6_regenerated_inventory_accepts_only_newly_issued_ids(tmp_path):
    before = _handle_materialization(tmp_path)
    (stale,) = build_semantic_target_inventory(before).handles
    (tmp_path / "target.txt").write_text("# v2\nneedle()\n", encoding="utf-8")
    after = materialize_planner_source_context(
        tmp_path,
        task_description="Replace the exact snippet `needle()` in target.txt.",
        expected_paths=["target.txt"],
    )
    (current,) = build_semantic_target_inventory(after).handles

    assert current.target_id != stale.target_id
    assert (
        _rejection_code(tmp_path, after, _semantic(stale.target_id))
        == "unknown_target_id"
    )
    assert _normalize(tmp_path, after, _semantic(current.target_id))


def test_t4_t5_path_and_operation_bindings_are_enforced(tmp_path):
    materialization = _handle_materialization(tmp_path)
    (tmp_path / "other.txt").write_text("needle()\n", encoding="utf-8")
    (handle,) = build_semantic_target_inventory(materialization).handles

    assert (
        _rejection_code(
            tmp_path, materialization, _semantic(handle.target_id, path="other.txt")
        )
        == "target_id_path_mismatch"
    )
    assert (
        _rejection_code(
            tmp_path, materialization, _semantic(handle.target_id, op="write_file")
        )
        == "target_id_operation_forbidden"
    )


def test_t7_preserved_inventory_reissues_the_same_id(tmp_path):
    first = _handle_materialization(tmp_path)
    second = materialize_planner_source_context(
        tmp_path,
        task_description="Replace the exact snippet `needle()` in target.txt.",
        expected_paths=["target.txt"],
    )

    assert [h.target_id for h in build_semantic_target_inventory(first).handles] == [
        h.target_id for h in build_semantic_target_inventory(second).handles
    ]


def test_t9_path_based_operation_is_not_converted_to_target_namespace(tmp_path):
    _seed_product(tmp_path)
    materialization = materialize_planner_source_context(
        tmp_path, task_description=TASK, expected_paths=[PERMISSIONS]
    )

    normalized = _normalize(tmp_path, materialization, dict(ALT_B))

    assert normalized[0]["ops"][0] == ALT_B


def test_t10_target_id_from_another_generation_is_rejected(tmp_path):
    first_root = tmp_path / "generation-1"
    second_root = tmp_path / "generation-2"
    first_root.mkdir()
    first = _handle_materialization(first_root)
    shutil.copytree(first_root, second_root)
    second = materialize_planner_source_context(
        second_root,
        task_description="Replace the exact snippet `needle()` in target.txt.",
        expected_paths=["target.txt"],
    )
    (old_handle,) = build_semantic_target_inventory(first).handles

    assert (
        _rejection_code(second_root, second, _semantic(old_handle.target_id))
        == "unknown_target_id"
    )


def test_reentry10_reconstruction_moves_boundary_past_bootstrap(tmp_path):
    _seed_product(tmp_path)
    plan = PlannerService.sanitize_common_plan_issues(_plan(ALT_B), task_prompt=TASK)

    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = outcome.details["task1_bootstrap_contract"]
    assert contract["passed"], contract["violation_codes"]
    assert not any("bootstrap" in reason.lower() for reason in outcome.reasons)
