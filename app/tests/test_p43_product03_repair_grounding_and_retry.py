"""P43-PRODUCT-03: requested-test repair grounding and deterministic retry.

P43-PRODUCT-02 ran two live evaluations. Planning Repair never added the
requested regression tests (A: V17-W twin; B: pagekit off-by-one, first
ordered Task), and B's terminal ``repair_candidate_rejected_by_bootstrap_contract``
was auto-retried as transient. These cases cover:

* F: the deterministic retry defect, through the real ``handle_task_failure``
  and ``EnhancedErrorHandler`` (no stubbed retry policy);
* C/E: the feasibility boundary for a repair that creates a new test file:
  real materializer, Validator, Accepted Path Authority, ``execute_file_ops``,
  Change Set builder and publication scope check, in a disposable workspace;
* A/B/D: the repair context the model receives for the requested-test finding
  and the Task 1 bootstrap test violation, at both repair budgets.

No provider is called.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.orchestration.error_handler import EnhancedErrorHandler
from app.services.orchestration.phases.planning_support import (
    _build_repair_rejection_reasons,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    GrantClass,
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.path_authority import (
    declare,
    publication_scope_violations,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.session.execution_policy import (
    classify_failure,
    is_deterministic_planning_contract_failure,
)
from app.services.workspace.changeset_service import ChangesetService
from app.services.workspace.workspace_paths import AUTO_SNAPSHOT_ROOT
from app.tests.test_p43_product01_requested_test_deliverable import (
    FUNCTIONAL_ONLY_TASK,
    V17W_TASK,
    _source_only_plan,
    _workspace,
)
from app.tests.test_post33_d1r1_recovery_escalation_ceiling import (
    _FirstAttemptSelfTask,
    _make_ctx,
    _make_episode,
    _run_failure,
)

BOOTSTRAP_REJECTION = "repair_candidate_rejected_by_bootstrap_contract"
REQUESTED_TEST_FINDING = "Task explicitly requested regression or new tests"
BOOTSTRAP_TEST_FINDING = "bootstrap prompt asks for tests but no test files"
# The second (bootstrap-targeted) repair pass reason, as rendered in the
# retained Evaluation B attempt-2 prompt.
BOOTSTRAP_SECOND_PASS_REASON = (
    "task1_bootstrap_contract: Repair must satisfy the same "
    "TaskBootstrapContract payload that caused validation rejection; "
    "violation_codes=['task1_bootstrap_missing_expected_test_files']"
)
BLOCK = "## REQUESTED TEST DELIVERABLE"
CREATION_RULE = "listed in that step's expected_files"


def _is_test_deliverable_finding(reason: str) -> bool:
    return REQUESTED_TEST_FINDING in reason or BOOTSTRAP_TEST_FINDING in reason


# ── Pagekit fixture (Evaluation B baseline; acceptance file shortened) ───────

PAGEKIT_INIT = (
    '"""Small offset-pagination helpers."""\n\n'
    "from .links import page_numbers\n"
    "from .paging import Page, paginate\n\n"
    '__all__ = ["Page", "page_numbers", "paginate"]\n'
)
PAGEKIT_LINKS = (
    '"""Navigation helpers built on :class:`Page`."""\n\n'
    "from typing import List\n\n"
    "from .paging import Page\n\n\n"
    "def page_numbers(page: Page) -> List[int]:\n"
    '    """All page numbers a pager control should render, in order."""\n'
    "    return list(range(1, page.total_pages + 1))\n"
)
PAGEKIT_BUGGY_TOTAL = "        return self.total_items // self.per_page\n"
PAGEKIT_FIXED_TOTAL = "        return max(1, -(-self.total_items // self.per_page))\n"
PAGEKIT_PAGING = (
    '"""Offset pagination over an in-memory sequence."""\n\n'
    "from dataclasses import dataclass, field\n"
    "from typing import Any, List, Sequence\n\n\n"
    "@dataclass(frozen=True)\n"
    "class Page:\n"
    "    number: int\n"
    "    per_page: int\n"
    "    total_items: int\n"
    "    items: List[Any] = field(default_factory=list)\n\n"
    "    @property\n"
    "    def total_pages(self) -> int:\n"
    f"{PAGEKIT_BUGGY_TOTAL}\n"
    "    @property\n"
    "    def has_next(self) -> bool:\n"
    "        return self.number < self.total_pages\n\n"
    "    @property\n"
    "    def has_previous(self) -> bool:\n"
    "        return self.number > 1\n\n\n"
    "def paginate(items: Sequence[Any], number: int, per_page: int) -> Page:\n"
    '    """Return page ``number`` (1-based) of ``items``."""\n'
    "    if per_page < 1:\n"
    '        raise ValueError("per_page must be at least 1")\n'
    "    if number < 1:\n"
    '        raise ValueError("page number must be at least 1")\n'
    "    probe = Page(number=number, per_page=per_page, total_items=len(items))\n"
    "    if number > probe.total_pages:\n"
    '        raise ValueError("page number out of range")\n'
    "    start = (number - 1) * per_page\n"
    "    return Page(\n"
    "        number=number,\n"
    "        per_page=per_page,\n"
    "        total_items=len(items),\n"
    "        items=list(items[start : start + per_page]),\n"
    "    )\n"
)
PAGEKIT_EXISTING_TEST = (
    "from pagekit import page_numbers, paginate\n\n\n"
    "def test_last_full_page_has_no_next() -> None:\n"
    "    page = paginate(list(range(9)), 3, 3)\n"
    "    assert page.items == [6, 7, 8]\n"
    "    assert not page.has_next\n"
    "    assert page_numbers(page) == [1, 2, 3]\n"
)
PAGEKIT_ACCEPTANCE = (
    '"""Independent pagekit acceptance (operator-owned)."""\n\n'
    "from pagekit import paginate\n\n\n"
    "def test_final_partial_page_is_returned() -> None:\n"
    "    assert paginate(list(range(10)), 4, 3).items == [9]\n"
)
PAGEKIT_NEW_TEST = "tests/test_partial_page_regression.py"
PAGEKIT_NEW_TEST_CONTENT = (
    "from pagekit import page_numbers, paginate\n\n\n"
    "def test_final_partial_page_is_kept() -> None:\n"
    "    page = paginate(list(range(10)), 4, 3)\n"
    "    assert page.items == [9]\n"
    "    assert page_numbers(page) == [1, 2, 3, 4]\n\n\n"
    "def test_empty_sequence_has_one_page() -> None:\n"
    "    assert paginate([], 1, 3).total_pages == 1\n"
)
# Byte-identical to the Evaluation B Task 313 description (sha256 c5f73742…).
PAGEKIT_TASK = (
    "Fix a pagination defect in the pagekit library. When the number of items "
    "is not an exact multiple of the page size, the final partial page is lost: "
    '`paginate(list(range(10)), 4, 3)` raises `ValueError("page number out of '
    'range")` instead of returning a page containing `[9]`, and page 3 of the '
    "same data reports `has_next == False`. Paginating an empty sequence also "
    "fails for page 1. Fix the defect and add focused regression tests. "
    "Preserve existing behavior.\n\n"
    "Requirements:\n"
    "- `Page.total_pages` counts a final partial page, and an empty sequence "
    "has exactly one (empty) page.\n"
    "- `paginate` returns the final partial page and still raises "
    '`ValueError("page number out of range")` for pages beyond the last one.\n'
    "- Preserve all other existing behavior, including the existing "
    "`ValueError` messages for invalid `per_page` and page numbers.\n"
    "- The new regression tests must fail on the current code and pass after "
    "the fix.\n"
    "- Do not modify `tests/test_pagekit_acceptance.py`.\n\n"
    "Independent acceptance contract (required):\n"
    "- `python -m pytest tests/test_pagekit_acceptance.py -q`"
)
PAGEKIT_FUNCTIONAL_ONLY_TASK = (
    "Fix a pagination defect in the pagekit library: `Page.total_pages` drops "
    "the final partial page. Keep the existing tests passing."
)


def _pagekit_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "pagekit-workspace"
    (root / "pagekit").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pagekit" / "__init__.py").write_text(PAGEKIT_INIT, encoding="utf-8")
    (root / "pagekit" / "links.py").write_text(PAGEKIT_LINKS, encoding="utf-8")
    (root / "pagekit" / "paging.py").write_text(PAGEKIT_PAGING, encoding="utf-8")
    (root / "tests" / "test_paging.py").write_text(
        PAGEKIT_EXISTING_TEST, encoding="utf-8"
    )
    (root / "tests" / "test_pagekit_acceptance.py").write_text(
        PAGEKIT_ACCEPTANCE, encoding="utf-8"
    )
    return root


def _pagekit_source_only_plan() -> list[dict[str, Any]]:
    """Shape of the Evaluation B initial Plan: a source fix plus pytest."""

    return [
        {
            "step_number": 1,
            "description": "Count the final partial page",
            "commands": [],
            "rollback": None,
            "verification": "python3 -m py_compile pagekit/paging.py",
            "expected_files": ["pagekit/paging.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "pagekit/paging.py",
                    "old": PAGEKIT_BUGGY_TOTAL,
                    "new": PAGEKIT_FIXED_TOTAL,
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Run the acceptance tests",
            "commands": ["python3 -m pytest tests/test_pagekit_acceptance.py -q"],
            "rollback": None,
            "verification": "python3 -m pytest tests/test_pagekit_acceptance.py -q",
            "expected_files": [],
            "ops": [],
        },
    ]


def _with_new_test_step(
    plan: list[dict[str, Any]],
    path: str,
    content: str,
    *,
    expected_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    plan = [dict(step) for step in plan]
    plan.insert(
        len(plan) - 1,
        {
            "step_number": 0,
            "description": "Add focused regression tests",
            "commands": [],
            "rollback": None,
            "verification": f"python3 -m pytest {path} -q",
            "expected_files": [path] if expected_files is None else expected_files,
            "ops": [{"op": "write_file", "path": path, "content": content}],
        },
    )
    for number, step in enumerate(plan, start=1):
        step["step_number"] = number
    return plan


V17W_NEW_TEST = "tests/test_inventory_reservation.py"
V17W_NEW_TEST_CONTENT = (
    "from inventory.service import Inventory\n\n\n"
    "def test_unknown_sku_has_no_available_stock() -> None:\n"
    '    assert Inventory().available_stock("SKU-3") == 0\n'
)


def _validate(
    root: Path,
    plan: list[dict[str, Any]],
    task: str,
    *,
    first_ordered: bool,
):
    # Planning-time materialization: the Discovery-selected source files are
    # expected; existing tests other than the acceptance file are not
    # materialized (the live Evaluation B shape).
    materialization = materialize_planner_source_context(
        root, task_description=task, expected_paths=_source_paths(plan)
    )
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        title="Fix the defect",
        description=task,
        is_first_ordered_task=first_ordered,
        source_materialization=materialization,
    )


def _source_paths(plan: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            operation["path"]
            for step in plan
            for operation in step.get("ops") or []
            if operation.get("op") == "replace_in_file"
        }
    )


def _execute_and_publish(root: Path, plan, verdict) -> dict[str, Any]:
    """Run the accepted Plan's structured ops and check the publication scope."""

    authority = accepted_path_authority_from_verdict(verdict)
    assert authority is not None
    snapshot_key = "p43-product-03"
    shutil.copytree(
        root,
        root / AUTO_SNAPSHOT_ROOT / snapshot_key,
        ignore=shutil.ignore_patterns(".agent"),
    )
    for step in plan:
        result = ExecutorService.execute_file_ops(
            root, step.get("ops") or [], accepted_path_authority=authority
        )
        assert result["success"], result
    service = ChangesetService(MagicMock())
    service.get_project_root = lambda project: root
    project = MagicMock(id=1, tasks=[])
    change_set = service.build_task_execution_change_set(
        project,
        MagicMock(id=1),
        task_execution_id=1,
        snapshot_key=snapshot_key,
        target_dir=root,
    )
    return {
        "authority": authority,
        "change_set": change_set,
        "violations": publication_scope_violations(
            authority,
            added_paths=change_set["added_files"],
            modified_paths=change_set["modified_files"],
            deleted_paths=change_set["deleted_files"],
        ),
    }


# ── F: deterministic retry ───────────────────────────────────────────────────


def test_f_bootstrap_repair_rejection_is_classified_deterministic():
    """Pre-fix: False / planning_failure / should_retry True (Evaluation B)."""

    assert is_deterministic_planning_contract_failure(BOOTSTRAP_REJECTION) is True
    assert classify_failure(BOOTSTRAP_REJECTION, "", {"failure_phase": "planning"}) == (
        "planning_contract_violation"
    )
    assert EnhancedErrorHandler().should_retry(RuntimeError(BOOTSTRAP_REJECTION)) is (
        False
    )


def test_f_bootstrap_repair_rejection_schedules_no_celery_retry(db_session):
    """The live shape: the worker re-raises RuntimeError(reason) on attempt 1.

    Pre-fix this raised RetrySignal: TE 403 was retried after 60 s and spent
    two more provider calls before an operator stop.
    """

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=_FirstAttemptSelfTask(),
        reason=BOOTSTRAP_REJECTION,
        queued=queued,
    )

    from app.models import TaskExecution, TaskStatus

    execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )
    assert not isinstance(raised, _FirstAttemptSelfTask.RetrySignal)
    assert isinstance(raised, RuntimeError)
    assert str(raised) == BOOTSTRAP_REJECTION
    assert queued == []
    assert execution.failure_category == "planning_contract_violation"
    assert execution.status == TaskStatus.FAILED


# ── C: a corrected Plan that creates a new test file is admissible ───────────


@pytest.mark.parametrize("case", ["v17w-mutation", "pagekit-bootstrap"])
def test_c_new_test_file_plan_is_validated_executed_and_published(tmp_path, case):
    if case == "v17w-mutation":
        root = _workspace(tmp_path)
        task, first_ordered = V17W_TASK, False
        incomplete = _source_only_plan()
        new_test, content = V17W_NEW_TEST, V17W_NEW_TEST_CONTENT
        source_paths = ["inventory/models.py", "inventory/service.py"]
    else:
        root = _pagekit_workspace(tmp_path)
        task, first_ordered = PAGEKIT_TASK, True
        incomplete = _pagekit_source_only_plan()
        new_test, content = PAGEKIT_NEW_TEST, PAGEKIT_NEW_TEST_CONTENT
        source_paths = ["pagekit/paging.py"]

    # The incomplete Plan is not admitted (requested-test / bootstrap finding).
    rejected = _validate(root, incomplete, task, first_ordered=first_ordered)
    assert rejected.status != "accepted"
    assert rejected.repairable
    assert any(_is_test_deliverable_finding(reason) for reason in rejected.reasons)

    corrected = _with_new_test_step(incomplete, new_test, content)
    verdict = _validate(root, corrected, task, first_ordered=first_ordered)
    assert verdict.status == "accepted", verdict.reasons

    outcome = _execute_and_publish(root, corrected, verdict)
    grant = outcome["authority"].grant_for(declare(new_test))
    assert grant is not None
    assert grant.grant_class is GrantClass.CREATION_AUTHORIZED
    assert (root / new_test).read_text(encoding="utf-8") == content
    assert outcome["change_set"]["added_files"] == [new_test]
    assert outcome["change_set"]["modified_files"] == source_paths
    assert outcome["change_set"]["deleted_files"] == []
    assert outcome["violations"] == ()

    if case == "pagekit-bootstrap":
        # The created regression test passes on the candidate and fails on
        # the baseline source: the deliverable is effective, not cosmetic.
        def pytest_rc(cwd: Path) -> int:
            return subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
                + [new_test],
                cwd=cwd,
                capture_output=True,
                timeout=120,
            ).returncode

        assert pytest_rc(root) == 0
        baseline = root / AUTO_SNAPSHOT_ROOT / "p43-product-03"
        (baseline / new_test).write_text(content, encoding="utf-8")
        assert pytest_rc(baseline) == 1


# ── E: the boundary stays closed for unauthorized test writes ────────────────


def test_e_new_test_write_without_expected_files_is_not_admitted(tmp_path):
    root = _pagekit_workspace(tmp_path)
    plan = _with_new_test_step(
        _pagekit_source_only_plan(),
        PAGEKIT_NEW_TEST,
        PAGEKIT_NEW_TEST_CONTENT,
        expected_files=[],
    )
    verdict = _validate(root, plan, PAGEKIT_TASK, first_ordered=True)
    assert verdict.status != "accepted"
    assert "accepted_path_authority" not in verdict.details


@pytest.mark.parametrize(
    "path", ["../outside/test_escape.py", "/tmp/test_escape.py", ".agent/test_x.py"]
)
def test_e_test_write_outside_the_workspace_is_not_admitted(tmp_path, path):
    root = _pagekit_workspace(tmp_path)
    plan = _with_new_test_step(
        _pagekit_source_only_plan(), path, PAGEKIT_NEW_TEST_CONTENT
    )
    verdict = _validate(root, plan, PAGEKIT_TASK, first_ordered=True)
    assert verdict.status != "accepted"
    assert not (tmp_path / "outside").exists()


def test_e_append_to_unmaterialized_existing_test_is_not_admitted(tmp_path):
    """Existing tests not shown to Planning cannot be edited by repair."""

    root = _pagekit_workspace(tmp_path)
    plan = _pagekit_source_only_plan()
    plan[0] = dict(plan[0])
    plan[0]["ops"] = plan[0]["ops"] + [
        {
            "op": "append_file",
            "path": "tests/test_paging.py",
            "content": "\n\ndef test_partial() -> None:\n    assert True\n",
        }
    ]
    plan[0]["expected_files"] = ["pagekit/paging.py", "tests/test_paging.py"]
    verdict = _validate(root, plan, PAGEKIT_TASK, first_ordered=True)
    assert verdict.status != "accepted"


def test_e_execution_refuses_a_test_write_the_authority_did_not_grant(tmp_path):
    root = _pagekit_workspace(tmp_path)
    plan = _with_new_test_step(
        _pagekit_source_only_plan(), PAGEKIT_NEW_TEST, PAGEKIT_NEW_TEST_CONTENT
    )
    verdict = _validate(root, plan, PAGEKIT_TASK, first_ordered=True)
    assert verdict.status == "accepted", verdict.reasons
    authority = accepted_path_authority_from_verdict(verdict)

    result = ExecutorService.execute_file_ops(
        root,
        [{"op": "write_file", "path": "tests/test_other.py", "content": "x = 1\n"}],
        accepted_path_authority=authority,
    )
    assert result["success"] is False
    assert result["authority_error"]["code"] == "authority_missing"
    assert not (root / "tests" / "test_other.py").exists()


# ── A/B/D: the repair context for a missing requested test ───────────────────


@pytest.fixture(params=[None, 32768], ids=["pytest-budget", "runtime-budget"])
def repair_budget(request, monkeypatch):
    """Both repair-prompt shapes: the 8000-char floor and the runtime 32768."""

    monkeypatch.setattr(
        settings, "PLANNING_REPAIR_CONTEXT_TOKENS", request.param, raising=False
    )
    return request.param


def _repair_prompt(root: Path, task: str, plan, reasons: list[str]) -> str:
    return " ".join(
        PlannerService.build_planning_repair_prompt_with_metadata(
            task, json.dumps(plan), root, rejection_reasons=reasons
        ).prompt.split()
    )


def _funnel_reasons(root: Path, plan, task: str, *, first_ordered: bool):
    """The rejection reasons the live funnel hands to Planning Repair."""

    verdict = _validate(root, plan, task, first_ordered=first_ordered)
    assert verdict.status != "accepted"
    return _build_repair_rejection_reasons(verdict.reasons, verdict.details)


def _block(prompt: str) -> str:
    return prompt.split(BLOCK, 1)[1].split("Strict output schema", 1)[0]


def test_a_v17w_repair_context_names_requirement_and_test_target(
    tmp_path, repair_budget
):
    root = _workspace(tmp_path)
    plan = _source_only_plan()
    reasons = _funnel_reasons(root, plan, V17W_TASK, first_ordered=False)
    prompt = _repair_prompt(root, V17W_TASK, plan, reasons)

    if repair_budget is None:
        # The 8000-char floor renders the over-budget compact prompt; the
        # correction is scoped to the full prompt the runtime (32768) renders.
        assert BLOCK not in prompt
        return
    assert "Task objective (preserve): " in prompt
    block = _block(prompt)
    assert "add focused regression tests" in block
    assert "tests/test_inventory.py (existing; not editable" in block
    assert "tests/test_inventory_acceptance.py (protected acceptance" in block
    assert "tests/test_<topic>.py" in block
    assert CREATION_RULE in block
    # The block names a convention, never a fabricated concrete filename.
    assert V17W_NEW_TEST not in block


@pytest.mark.parametrize("attempt", ["first-repair", "bootstrap-second-pass"])
def test_b_bootstrap_repair_context_keeps_objective_and_test_target(
    tmp_path, repair_budget, attempt
):
    root = _pagekit_workspace(tmp_path)
    plan = _pagekit_source_only_plan()
    reasons = _funnel_reasons(root, plan, PAGEKIT_TASK, first_ordered=True)
    assert any(BOOTSTRAP_TEST_FINDING in reason for reason in reasons)
    assert not any(REQUESTED_TEST_FINDING in reason for reason in reasons)
    if attempt == "bootstrap-second-pass":
        reasons = [BOOTSTRAP_SECOND_PASS_REASON]
    prompt = _repair_prompt(root, PAGEKIT_TASK, plan, reasons)

    if repair_budget is None:
        assert BLOCK not in prompt
        return
    objective = prompt.split("Task objective (preserve): ", 1)[1]
    assert objective.startswith("Fix a pagination defect in the pagekit library.")
    block = _block(prompt)
    # The 420-char objective cuts the Task before "regression tests"; the
    # block carries the test requirement sentences verbatim.
    assert "Fix the defect and add focused regression tests." in block
    assert (
        "The new regression tests must fail on the current code and pass after "
        "the fix." in block
    )
    assert "tests/test_paging.py (existing; not editable" in block
    assert "tests/test_pagekit_acceptance.py (protected acceptance" in block
    assert "tests/test_<topic>.py" in block
    assert CREATION_RULE in block
    assert PAGEKIT_NEW_TEST not in block


def test_d_unrelated_rejection_gets_no_test_creation_context(tmp_path):
    root = _workspace(tmp_path)
    for task in (V17W_TASK, FUNCTIONAL_ONLY_TASK):
        prompt = _repair_prompt(
            root, task, _source_only_plan(), ["Step 3: verification command is weak"]
        )
        assert BLOCK not in prompt
        assert "Task objective (preserve)" not in prompt


def test_d_functional_only_tasks_raise_no_test_finding(tmp_path):
    inventory = _workspace(tmp_path / "inventory")
    verdict = _validate(
        inventory, _source_only_plan(), FUNCTIONAL_ONLY_TASK, first_ordered=False
    )
    assert verdict.status == "accepted", verdict.reasons

    pagekit = _pagekit_workspace(tmp_path / "pagekit")
    verdict = _validate(
        pagekit,
        _pagekit_source_only_plan(),
        PAGEKIT_FUNCTIONAL_ONLY_TASK,
        first_ordered=True,
    )
    assert not any(_is_test_deliverable_finding(reason) for reason in verdict.reasons)
