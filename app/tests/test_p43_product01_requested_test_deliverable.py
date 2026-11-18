"""P43-PRODUCT-01: an explicitly requested test deliverable must reach the Plan.

Phase 42 V17-W (Task 308, TE 398) asked to "add focused regression tests".
The model's Plan edited only source files. The Validator accepted it because
(1) the strict new-test-writing detector did not recognise an adjective or
article between the verb and "regression tests", and (2) the prompt-requested
test check existed only in the Task 1 bootstrap contract, which does not run
for existing-project mutation Plans.

The fixture is a generic copy of the V17-W baseline shape (an existing Python
package with existing tests). The task text and the accepted Plan's source
operations are vendored from the retained V17-W records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.orchestration.planning.task_bootstrap_contract import (
    _has_explicit_new_test_writing_intent,
)
from app.services.orchestration.validation.validator import ValidatorService

REQUESTED_TEST_CODE = "requested_test_deliverable_missing"

MODELS = (
    '"""Domain models for the baseline inventory library."""\n\n'
    "from dataclasses import dataclass\n\n\n"
    "@dataclass\n"
    "class InventoryItem:\n"
    "    sku: str\n"
    "    total_stock: int = 0\n"
)
SERVICE = (
    '"""Baseline stock operations."""\n\n'
    "from .models import InventoryItem\n\n\n"
    "class Inventory:\n"
    "    def __init__(self) -> None:\n"
    "        self._items: dict[str, InventoryItem] = {}\n\n"
    "    def available_stock(self, sku: str) -> int:\n"
    "        return self._items.get(sku, InventoryItem(sku=sku)).total_stock\n"
)
EXISTING_TEST = (
    "from inventory.service import Inventory\n\n\n"
    "def test_available_stock_defaults_to_zero() -> None:\n"
    '    assert Inventory().available_stock("SKU-1") == 0\n'
)

# Vendored verbatim from the retained V17-W Task 308 description (1062 chars;
# Discovery capture session_257_task_308_execution_398.json).
V17W_TASK = (
    "Implement inventory reservation support in the existing Inventory "
    "Reservation Service. Extend the current inventory domain model and service "
    "using the project's established Python interfaces. Allow reserving "
    "available stock, releasing reservations, and preventing over-reservation. "
    "Maintain consistent stock accounting and add focused regression tests. "
    "Preserve existing behavior unless a change is necessary for the "
    "reservation feature.\n\n"
    "Requirements:\n"
    "- Add `Inventory.reserve_stock(sku, quantity)` and "
    "`Inventory.release_stock(sku, quantity)`. Both return the SKU's total "
    "reserved quantity after the operation.\n"
    "- Keep `0 <= reserved_stock <= total_stock` and `available_stock == "
    "total_stock - reserved_stock` for every SKU.\n"
    "- Raise `ValueError` without changing state for over-reservation, "
    "over-release, non-positive quantities, and any `remove_stock` that would "
    "leave total stock below reserved stock.\n"
    "- Do not modify `tests/test_inventory_acceptance.py`.\n\n"
    "Independent acceptance contract (required):\n"
    "- `python -m pytest tests/test_inventory_acceptance.py -q`"
)
# Negative control: same functional change, no test deliverable requested.
FUNCTIONAL_ONLY_TASK = (
    "Implement inventory reservation support in the existing Inventory "
    "Reservation Service. Maintain consistent stock accounting and keep the "
    "existing tests passing.\n\n"
    "Independent acceptance contract (required):\n"
    "- `python -m pytest tests/test_inventory_acceptance.py -q`\n"
)
ACCEPTANCE_TEST = (
    "from inventory.service import Inventory\n\n\n"
    "def test_reserve_stock_exists() -> None:\n"
    '    assert hasattr(Inventory(), "reserve_stock")\n'
)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "inventory").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "inventory" / "__init__.py").write_text(
        "from .service import Inventory\n", encoding="utf-8"
    )
    (root / "inventory" / "models.py").write_text(MODELS, encoding="utf-8")
    (root / "inventory" / "service.py").write_text(SERVICE, encoding="utf-8")
    (root / "tests" / "test_inventory.py").write_text(EXISTING_TEST, encoding="utf-8")
    (root / "tests" / "test_inventory_acceptance.py").write_text(
        ACCEPTANCE_TEST, encoding="utf-8"
    )
    return root


def _source_only_plan() -> list[dict[str, Any]]:
    """Shape of the accepted V17-W Plan: source edits plus a pytest run."""

    return [
        {
            "step_number": 1,
            "description": "Add reserved_stock to the model",
            "commands": [],
            "rollback": None,
            "verification": "python -m py_compile inventory/models.py",
            "expected_files": ["inventory/models.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "inventory/models.py",
                    "old": "    total_stock: int = 0\n",
                    "new": "    total_stock: int = 0\n    reserved_stock: int = 0\n",
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Make available_stock reservation aware",
            "commands": [],
            "rollback": None,
            "verification": "python -m py_compile inventory/service.py",
            "expected_files": ["inventory/service.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "inventory/service.py",
                    "old": (
                        "        return self._items.get(sku, "
                        "InventoryItem(sku=sku)).total_stock\n"
                    ),
                    "new": (
                        "        item = self._items.get(sku, InventoryItem(sku=sku))\n"
                        "        return item.total_stock - item.reserved_stock\n"
                    ),
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Run the test suite",
            "commands": ["python -m pytest tests/test_inventory.py -q"],
            "rollback": None,
            "verification": "python -m pytest tests/test_inventory.py -q",
            "expected_files": [],
            "ops": [],
        },
    ]


def _plan_with_test_mutation() -> list[dict[str, Any]]:
    plan = _source_only_plan()
    plan.insert(
        2,
        {
            "step_number": 3,
            "description": "Add focused regression tests",
            "commands": [],
            "rollback": None,
            "verification": "python -m pytest tests/test_inventory.py -q",
            "expected_files": ["tests/test_inventory.py"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "tests/test_inventory.py",
                    "content": (
                        "\n\ndef test_available_stock_unknown_sku() -> None:\n"
                        '    assert Inventory().available_stock("SKU-2") == 0\n'
                    ),
                }
            ],
        },
    )
    for number, step in enumerate(plan, start=1):
        step["step_number"] = number
    return plan


def _validate(root: Path, plan: list[dict[str, Any]], description: str):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=description,
        execution_profile="full_lifecycle",
        project_dir=root,
        title="Implement Inventory Reservation Support",
        description=description,
    )


def _all_findings(verdict) -> str:
    return "\n".join(verdict.reasons)


# --- detector -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Maintain consistent stock accounting and add focused regression tests.",
        "Fix the parser and add a regression test.",
        "Write comprehensive unit tests for the new endpoint.",
        "Add new unit tests.",
        "add regression tests",
    ],
)
def test_detector_recognises_qualified_test_requests(text: str) -> None:
    assert _has_explicit_new_test_writing_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        FUNCTIONAL_ONLY_TASK,
        "Verify with the existing unit tests.",
        "Add stock without breaking existing regression tests.",
        "Do not add new regression tests.",
        "Update the service so the existing tests pass.",
        "Run python -m pytest -q as verification.",
    ],
)
def test_detector_ignores_non_requests(text: str) -> None:
    assert not _has_explicit_new_test_writing_intent(text)


# --- Plan validation boundary ----------------------------------------------


def test_v17w_shape_source_only_plan_is_not_accepted(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    verdict = _validate(root, _source_only_plan(), V17W_TASK)

    # V17-W validated this Task under the ``mutation`` profile.
    assert verdict.verdict.profile == "mutation"
    assert verdict.status != "accepted"
    assert "requested regression or new tests" in _all_findings(verdict)
    assert verdict.details["requested_test_deliverable"] == {
        "requested": True,
        "test_mutation_paths": [],
        "code": REQUESTED_TEST_CODE,
    }


def test_v17w_shape_plan_with_test_mutation_is_accepted(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    verdict = _validate(root, _plan_with_test_mutation(), V17W_TASK)

    assert verdict.status == "accepted", _all_findings(verdict)
    assert verdict.details["requested_test_deliverable"]["test_mutation_paths"] == [
        "tests/test_inventory.py"
    ]


def test_functional_only_task_needs_no_test_mutation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    verdict = _validate(root, _source_only_plan(), FUNCTIONAL_ONLY_TASK)

    assert verdict.status == "accepted", _all_findings(verdict)
    assert "requested regression or new tests" not in _all_findings(verdict)
    assert "requested_test_deliverable" not in verdict.details


def test_read_only_review_task_is_not_given_the_test_deliverable_rule(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    plan = _source_only_plan()
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Review the service and recommend whether to add regression tests.",
        execution_profile="review_only",
        project_dir=root,
        title="Review Inventory Service",
        description=(
            "Review the service and recommend whether to add regression tests."
        ),
    )

    assert verdict.verdict.profile == "verification"
    assert "requested regression or new tests" not in _all_findings(verdict)
    assert "requested_test_deliverable" not in verdict.details


def test_requirement_is_satisfiable_under_planning_materialization(
    tmp_path: Path,
) -> None:
    """With Planning-style materialization (existing tests not materialized),
    a new test file satisfies the rule while a source-only Plan does not."""

    from app.services.orchestration.planning.source_materialization import (
        materialize_planner_source_context,
    )

    root = _workspace(tmp_path)
    materialization = materialize_planner_source_context(
        root, task_description=V17W_TASK, expected_paths=["inventory/models.py"]
    )
    new_test_plan = _plan_with_test_mutation()
    new_test_plan[2]["expected_files"] = ["tests/test_inventory_reservation.py"]
    new_test_plan[2]["ops"] = [
        {
            "op": "write_file",
            "path": "tests/test_inventory_reservation.py",
            "content": (
                "from inventory.service import Inventory\n\n\n"
                "def test_unknown_sku_has_no_available_stock() -> None:\n"
                '    assert Inventory().available_stock("SKU-3") == 0\n'
            ),
        }
    ]

    def validate(plan: list[dict[str, Any]]):
        return ValidatorService.validate_plan(
            plan,
            output_text=json.dumps(plan),
            task_prompt=V17W_TASK,
            execution_profile="full_lifecycle",
            project_dir=root,
            title="Implement Inventory Reservation Support",
            description=V17W_TASK,
            source_materialization=materialization,
        )

    source_only = validate(_source_only_plan())
    assert source_only.repairable
    assert all(
        reason.startswith("Task explicitly requested regression or new tests")
        for reason in source_only.reasons
    ), source_only.reasons

    with_new_test = validate(new_test_plan)
    assert with_new_test.status == "accepted", _all_findings(with_new_test)
