"""P42-V17-V provider-free Planning source-target selection regressions."""

from __future__ import annotations

from pathlib import Path

from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryObservation,
    materialize_observation_source_context,
)
from app.services.orchestration.planning.source_materialization import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    MAX_SOURCE_CONTENT_TOTAL_CHARS,
    materialize_planner_source_context,
    render_repair_source_materialization,
)
from app.services.orchestration.validation.candidate_checks import (
    independent_acceptance_pytest_commands,
)


TASK = """Implement Inventory Reservation Support.

Requirements:
- Add reserve_stock and release_stock to the Inventory API.
- Preserve the existing implementation and do not modify tests/test_inventory_acceptance.py.

Independent acceptance contract (required):
- `python -m pytest tests/test_inventory_acceptance.py -q`
"""

TASK_WITH_IMPLEMENTATION_TARGET = TASK.replace(
    "Add reserve_stock and release_stock to the Inventory API.",
    "Modify inventory/service.py to add reserve_stock and release_stock.",
)

TASK_WITH_TEST_TARGET = TASK.replace(
    "Preserve the existing implementation and do not modify tests/test_inventory_acceptance.py.",
    "Modify tests/test_inventory_acceptance.py to add the reservation cases.",
)

MODELS_SOURCE = "class InventoryItem:\n    pass\n"
SERVICE_SOURCE = """from .models import InventoryItem


class Inventory:
    def __init__(self):
        self._items = {}

    def add_stock(self, sku, quantity):
        item = self._items.setdefault(sku, InventoryItem())
        item.total_stock += quantity
        return item.total_stock

    def remove_stock(self, sku, quantity):
        item = self._items.setdefault(sku, InventoryItem())
        item.total_stock -= quantity
        return item.total_stock

    def reserve_stock(self, sku, quantity):
        return quantity

    def release_stock(self, sku, quantity):
        return quantity
"""
ACCEPTANCE_SOURCE = """from inventory.service import Inventory


def test_physical_removal_cannot_violate_outstanding_reservations():
    Inventory().remove_stock("SKU-42", 1)
"""


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _workspace(root: Path) -> Path:
    _write(root, "inventory/__init__.py", "")
    _write(root, "inventory/models.py", MODELS_SOURCE)
    _write(root, "inventory/service.py", SERVICE_SOURCE)
    _write(root, "tests/test_inventory.py", "from inventory.service import Inventory\n")
    _write(root, "tests/test_inventory_acceptance.py", ACCEPTANCE_SOURCE)
    return root


def _record(materialization, relative: str):
    return next(
        item for item in materialization.files if item.relative_path == relative
    )


def _post_discovery_materialization(root: Path):
    observation = DiscoveryObservation(
        action="read_file",
        status="completed",
        paths=("inventory/models.py",),
        content=(root / "inventory/models.py").read_text(encoding="utf-8"),
    )
    return materialize_observation_source_context(
        project_dir=root,
        prompt=TASK,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )


def test_acceptance_oracle_does_not_displace_implementation_source(tmp_path):
    root = _workspace(tmp_path)

    materialization = _post_discovery_materialization(root)
    acceptance = _record(materialization, "tests/test_inventory_acceptance.py")
    service = _record(materialization, "inventory/service.py")

    assert acceptance.expected is False
    assert service.expected is False
    assert service.status == "existing_file_with_materialized_source"
    assert service.truncated is False
    assert service.content == SERVICE_SOURCE
    assert materialization.materialized_source_bytes <= MAX_SOURCE_CONTENT_TOTAL_CHARS


def test_explicit_implementation_file_remains_expected(tmp_path):
    root = _workspace(tmp_path)

    materialization = materialize_planner_source_context(
        root,
        task_description=TASK_WITH_IMPLEMENTATION_TARGET,
        supporting_paths=(),
    )

    service = _record(materialization, "inventory/service.py")
    assert service.expected is True
    assert service.priority == "P0"


def test_explicit_test_modification_remains_expected(tmp_path):
    root = _workspace(tmp_path)

    materialization = materialize_planner_source_context(
        root,
        task_description=TASK_WITH_TEST_TARGET,
        supporting_paths=(),
    )

    acceptance = _record(materialization, "tests/test_inventory_acceptance.py")
    assert acceptance.expected is True
    assert acceptance.priority == "P1"


def test_independent_acceptance_contract_remains_authoritative():
    extraction = independent_acceptance_pytest_commands(TASK)

    assert extraction.section_found is True
    assert extraction.required is True
    assert [command.text for command in extraction.admitted] == [
        "python -m pytest tests/test_inventory_acceptance.py -q"
    ]


def test_corrected_source_is_available_to_planning_repair(tmp_path):
    root = _workspace(tmp_path)
    materialization = _post_discovery_materialization(root)

    repair_context = render_repair_source_materialization(materialization)

    assert "### inventory/service.py" in repair_context
    assert SERVICE_SOURCE in repair_context


def test_source_budget_contract_is_unchanged(tmp_path):
    root = _workspace(tmp_path)
    materialization = _post_discovery_materialization(root)

    assert materialization.maximum_bytes_per_file == MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert materialization.maximum_total_source_bytes == MAX_SOURCE_CONTENT_TOTAL_CHARS
