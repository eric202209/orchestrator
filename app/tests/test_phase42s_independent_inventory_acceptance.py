"""P42-V17-S provider-free independent Inventory acceptance contract."""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.orchestration.state.persistence import _task_acceptance_evidence
from app.services.orchestration.types import ValidationVerdict
from app.services.orchestration.validation.candidate_checks import (
    task_acceptance_pytest_commands,
    task_prompt_requires_independent_acceptance,
    validate_candidate_delta,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_completion_verification_regressions import (
    _complete_task,
    _seed_legacy_finalize_ctx,
)
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    _accepted,
)


INDEPENDENT_PROMPT = """Implement Inventory Reservation Support.

Independent acceptance contract (required):
- Preserve `0 <= reserved_stock <= total_stock`.
- Preserve `available_stock == total_stock - reserved_stock`.
- `python -m pytest tests/test_inventory_acceptance.py -q`
"""

INDEPENDENT_MISSING_COMMAND_PROMPT = """Implement Inventory Reservation Support.

Independent acceptance contract (required):
- The operator-provided Inventory acceptance fixture must run.
"""

MODELS_SOURCE = """from dataclasses import dataclass


@dataclass
class InventoryItem:
    sku: str
    total_stock: int = 0
    reserved_stock: int = 0
"""

WRONG_SERVICE_SOURCE = """from .models import InventoryItem


class Inventory:
    def __init__(self):
        self._items = {}

    def add_stock(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        item = self._items.setdefault(sku, InventoryItem(sku=sku))
        item.total_stock += quantity
        return item.total_stock

    def remove_stock(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        item = self._items.setdefault(sku, InventoryItem(sku=sku))
        if quantity > item.total_stock:
            raise ValueError("insufficient stock")
        item.total_stock -= quantity
        return item.total_stock

    def available_stock(self, sku):
        item = self._items.get(sku, InventoryItem(sku=sku))
        return item.total_stock - item.reserved_stock

    def reserve_stock(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        item = self._items.setdefault(sku, InventoryItem(sku=sku))
        if quantity > item.total_stock - item.reserved_stock:
            raise ValueError("insufficient available stock")
        item.reserved_stock += quantity
        return item.reserved_stock

    def release_stock(self, sku, quantity):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        item = self._items.setdefault(sku, InventoryItem(sku=sku))
        if quantity > item.reserved_stock:
            raise ValueError("insufficient reserved stock")
        item.reserved_stock -= quantity
        return item.reserved_stock
"""

CORRECT_SERVICE_SOURCE = WRONG_SERVICE_SOURCE.replace(
    'if quantity > item.total_stock:\n            raise ValueError("insufficient stock")',
    "if quantity > item.total_stock - item.reserved_stock:\n"
    '            raise ValueError("insufficient unreserved stock")',
)

GENERATED_TEST_SOURCE = """from inventory.service import Inventory


def test_generated_baseline_operations_pass():
    inventory = Inventory()
    assert inventory.add_stock("SKU-42", 10) == 10
    assert inventory.reserve_stock("SKU-42", 6) == 6
    assert inventory.available_stock("SKU-42") == 4
"""

INDEPENDENT_TEST_SOURCE = """from inventory.service import Inventory


def test_physical_removal_preserves_outstanding_reservation():
    inventory = Inventory()
    inventory.add_stock("SKU-42", 10)
    inventory.reserve_stock("SKU-42", 6)
    before = (
        inventory._items["SKU-42"].total_stock,
        inventory._items["SKU-42"].reserved_stock,
        inventory.available_stock("SKU-42"),
    )
    try:
        inventory.remove_stock("SKU-42", 5)
    except ValueError:
        pass
    else:
        raise AssertionError("physical removal violated an outstanding reservation")
    assert (
        inventory._items["SKU-42"].total_stock,
        inventory._items["SKU-42"].reserved_stock,
        inventory.available_stock("SKU-42"),
    ) == before
"""


def _write_inventory_project(root: Path, *, corrected: bool) -> Path:
    (root / "inventory").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "inventory/__init__.py").write_text("", encoding="utf-8")
    (root / "inventory/models.py").write_text(MODELS_SOURCE, encoding="utf-8")
    (root / "inventory/service.py").write_text(
        CORRECT_SERVICE_SOURCE if corrected else WRONG_SERVICE_SOURCE,
        encoding="utf-8",
    )
    (root / "tests/test_generated.py").write_text(
        GENERATED_TEST_SOURCE, encoding="utf-8"
    )
    (root / "tests/test_inventory_acceptance.py").write_text(
        INDEPENDENT_TEST_SOURCE, encoding="utf-8"
    )
    shutil.copytree(root, root.parent / f"{root.name}-acceptance-baseline")
    return root


def _load_inventory(root: Path):
    sys.path.insert(0, str(root))
    for name in ("inventory.service", "inventory.models", "inventory"):
        sys.modules.pop(name, None)
    try:
        return importlib.import_module("inventory.service").Inventory
    finally:
        sys.path.remove(str(root))
        for name in ("inventory.service", "inventory.models", "inventory"):
            sys.modules.pop(name, None)


def _assert_quantity_error_and_unchanged(inventory, method, sku, quantity, before):
    try:
        method(sku, quantity)
    except ValueError:
        pass
    else:
        raise AssertionError("inventory operation accepted an invalid quantity")
    item = inventory._items[sku]
    assert (
        item.total_stock,
        item.reserved_stock,
        inventory.available_stock(sku),
    ) == before


def _run_inventory_contract(root: Path) -> None:
    Inventory = _load_inventory(root)
    inventory = Inventory()

    assert inventory.add_stock("SKU-42", 10) == 10
    assert inventory.available_stock("SKU-42") == 10
    assert inventory.reserve_stock("SKU-42", 6) == 6
    assert inventory.available_stock("SKU-42") == 4

    before = (10, 6, 4)
    _assert_quantity_error_and_unchanged(
        inventory,
        inventory.reserve_stock,
        "SKU-42",
        5,
        before,
    )

    assert inventory.release_stock("SKU-42", 2) == 4
    assert inventory.available_stock("SKU-42") == 6
    before = (10, 4, 6)
    _assert_quantity_error_and_unchanged(
        inventory,
        inventory.release_stock,
        "SKU-42",
        5,
        before,
    )

    before = (10, 4, 6)
    _assert_quantity_error_and_unchanged(
        inventory,
        inventory.remove_stock,
        "SKU-42",
        7,
        before,
    )

    before = (10, 4, 6)
    try:
        inventory.remove_stock("SKU-42", 7)
    except ValueError:
        pass
    else:
        raise AssertionError("physical removal violated an outstanding reservation")
    item = inventory._items["SKU-42"]
    assert (
        item.total_stock,
        item.reserved_stock,
        inventory.available_stock("SKU-42"),
    ) == before

    for method in (
        inventory.add_stock,
        inventory.remove_stock,
        inventory.reserve_stock,
        inventory.release_stock,
    ):
        _assert_quantity_error_and_unchanged(
            inventory,
            method,
            "SKU-42",
            0,
            before,
        )
        _assert_quantity_error_and_unchanged(
            inventory,
            method,
            "SKU-42",
            -1,
            before,
        )

    compatible = Inventory()
    assert compatible.add_stock("SKU-existing", 4) == 4
    assert compatible.remove_stock("SKU-existing", 1) == 3
    assert compatible.available_stock("SKU-existing") == 3


def _candidate_check(
    root: Path,
    *,
    prompt: str,
    acceptance_task_prompt: str | None = None,
    acceptance_authority_status: str | None = None,
):
    snapshot_root = root.parent / f"{root.name}-acceptance-baseline"
    return validate_candidate_delta(
        project_dir=root,
        change_set={
            "target_path": str(root),
            "snapshot_path": str(snapshot_root),
            "modified_files": ["inventory/service.py"],
            "added_files": [],
            "deleted_files": [],
        },
        plan=[
            {
                "step_number": 1,
                "description": "Run generated baseline tests",
                "verification": "python -m pytest tests/test_generated.py -q",
                "expected_files": ["inventory/service.py"],
            }
        ],
        task_prompt=prompt,
        acceptance_task_prompt=acceptance_task_prompt,
        acceptance_authority_status=acceptance_authority_status,
        include_static_checks=False,
        task_acceptance_enabled=True,
        timeout_seconds=30,
    )


def test_negative_control_te396_equivalent_fails_contract(tmp_path):
    root = _write_inventory_project(tmp_path / "wrong", corrected=False)

    with pytest.raises(AssertionError):
        _run_inventory_contract(root)


def test_positive_control_correct_implementation_passes_contract(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)

    _run_inventory_contract(root)


def test_generated_tests_do_not_override_failing_independent_contract(tmp_path):
    root = _write_inventory_project(tmp_path / "wrong", corrected=False)

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    rule_ids = [finding.rule_id for finding in run.findings]
    assert "focused_pytest_failed" not in rule_ids
    assert "task_acceptance_pytest_failed" in rule_ids
    assert run.task_acceptance["required"] is True
    assert run.task_acceptance["contract_kind"] == "independent"
    assert run.task_acceptance["results"][0]["outcome"] == "failed"


def test_correct_candidate_passes_independent_contract_runner(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    assert run.findings == ()
    assert run.task_acceptance["required"] is True
    assert run.task_acceptance["results"][0]["outcome"] == "passed"
    assert run.task_acceptance["integrity"]["status"] == "verified"


def test_weakened_required_acceptance_replacement_is_not_authoritative(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)
    (root / "tests/test_inventory_acceptance.py").write_text(
        "def test_placeholder():\n    assert bool('placeholder')\n",
        encoding="utf-8",
    )

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    assert "independent_acceptance_integrity_failed" in [
        finding.rule_id for finding in run.findings
    ]
    assert run.task_acceptance["status"] == "blocked_integrity"
    assert run.task_acceptance["results"] == []
    assert run.task_acceptance["integrity"]["failures"] == [
        {
            "code": "acceptance_file_identity_mismatch",
            "path": "tests/test_inventory_acceptance.py",
        }
    ]


def test_required_acceptance_assertion_removal_is_not_authoritative(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)
    (root / "tests/test_inventory_acceptance.py").write_text(
        "def test_removed_assertion():\n    pass\n",
        encoding="utf-8",
    )

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    assert "independent_acceptance_integrity_failed" in [
        finding.rule_id for finding in run.findings
    ]
    assert run.task_acceptance["integrity"]["failures"] == [
        {
            "code": "acceptance_file_identity_mismatch",
            "path": "tests/test_inventory_acceptance.py",
        }
    ]


def test_required_acceptance_symlink_replacement_is_not_authoritative(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)
    acceptance = root / "tests/test_inventory_acceptance.py"
    acceptance.unlink()
    (root / "tests/test_other.py").write_text(
        INDEPENDENT_TEST_SOURCE,
        encoding="utf-8",
    )
    acceptance.symlink_to("test_other.py")

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    assert "independent_acceptance_integrity_failed" in [
        finding.rule_id for finding in run.findings
    ]
    assert run.task_acceptance["integrity"]["failures"] == [
        {
            "code": "acceptance_path_symlink",
            "path": "tests/test_inventory_acceptance.py",
        }
    ]


def test_required_acceptance_command_redirect_is_refused(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)
    redirected = INDEPENDENT_PROMPT.replace(
        "python -m pytest tests/test_inventory_acceptance.py -q",
        "python -m pytest tests/test_other.py -q > redirected.txt",
    )

    run = _candidate_check(root, prompt=redirected)

    assert "task_acceptance_command_refused" in [
        finding.rule_id for finding in run.findings
    ]


def test_required_acceptance_deletion_is_not_authoritative(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)
    (root / "tests/test_inventory_acceptance.py").unlink()

    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)

    assert "independent_acceptance_integrity_failed" in [
        finding.rule_id for finding in run.findings
    ]
    assert run.task_acceptance["integrity"]["failures"] == [
        {
            "code": "acceptance_file_not_regular",
            "path": "tests/test_inventory_acceptance.py",
        }
    ]


def test_retry_feedback_cannot_replace_persisted_independent_contract(tmp_path):
    root = _write_inventory_project(tmp_path / "wrong", corrected=False)

    run = _candidate_check(
        root,
        prompt="Operator request-changes feedback: rerun the candidate tests.",
        acceptance_task_prompt=INDEPENDENT_PROMPT,
        acceptance_authority_status="verified_request_changes",
    )

    assert run.task_acceptance["required"] is True
    assert "task_acceptance_pytest_failed" in [
        finding.rule_id for finding in run.findings
    ]


def test_missing_required_contract_command_is_a_blocking_finding(tmp_path):
    root = _write_inventory_project(tmp_path / "correct", corrected=True)

    run = _candidate_check(root, prompt=INDEPENDENT_MISSING_COMMAND_PROMPT)

    assert "independent_acceptance_command_missing" in [
        finding.rule_id for finding in run.findings
    ]


def test_independent_result_has_distinct_attempt_bound_evidence(tmp_path):
    root = _write_inventory_project(tmp_path / "wrong", corrected=False)
    run = _candidate_check(root, prompt=INDEPENDENT_PROMPT)
    payload = {
        "stage": "task_completion",
        "status": "rejected",
        "details": {"task_acceptance_verification": run.task_acceptance},
    }

    projection = _task_acceptance_evidence(
        payload,
        session_id=11,
        task_id=22,
        task_execution_id=33,
    )

    assert projection["schema"] == "task_acceptance_evidence/v1"
    assert projection["required"] is True
    assert projection["contract_kind"] == "independent"
    assert (
        projection["session_id"],
        projection["task_id"],
        projection["task_execution_id"],
    ) == (
        11,
        22,
        33,
    )
    assert projection["results"][0]["outcome"] == "failed"
    assert projection["integrity"]["status"] == "verified"


def test_required_marker_is_forced_before_review_publication_even_flag_off(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session,
        tmp_path,
        existing_mutable_paths=("README.md",),
    )
    ctx.task.description = INDEPENDENT_PROMPT
    ctx.prompt = INDEPENDENT_PROMPT
    ctx.task.template_id = "dependency-config-change"
    db_session.flush()

    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    ctx.task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=workspace_snapshot_key(ctx.task_id, execution.id),
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")

    captured = []

    def _validate(**kwargs):
        captured.append(kwargs)
        return _accepted()

    monkeypatch.setattr(
        "app.config.settings.TASK_ACCEPTANCE_VERIFICATION_ENABLED", False
    )
    monkeypatch.setattr(ValidatorService, "validate_task_completion", _validate)
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={},
        ),
    )

    result = _complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert captured
    assert all(
        call["completion_evidence"].get("run_task_acceptance_checks") is True
        for call in captured
    )
    assert not (project_root / "README.md").exists()


def test_existing_ordinary_acceptance_parser_remains_optional():
    ordinary = "Acceptance criteria:\n- `python -m pytest -q`\n"

    extraction = task_acceptance_pytest_commands(ordinary)

    assert extraction.required is False
    assert task_prompt_requires_independent_acceptance(ordinary) is False
