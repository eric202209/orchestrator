"""P43-PRODUCT-25A-R1: Plan admission matches the C2 verification route.

The U3 live window accepted a Plan whose step 1 and step 2 verification was
``python -m py_compile``. Plan admission and the Planning capability contract
used only the local executor predicates, which admit ``py_compile``. In C2
mode every verification goes only through the Sandbox Gateway, which runs the
literal ``sys.exit`` script and admitted pytest commands. So step 1's
verification was refused with ``unsupported_operation`` before any Sandbox
request. That sent Debug Repair after a failure no source edit could fix.

The accepted Plan below is the retained ``tasks.steps`` value, vendored
because the evidence under ``docs/`` is gitignored.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.diagnostics.debug_feedback import (
    bounded_debug_repair_ops_authority_rejection,
)
from app.services.orchestration.execution import execution_flow
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.rules.core_execution_capability import (
    describe_unsupported_step_commands,
    local_executor_command_examples,
    plan_steps_with_unsupported_commands,
    plan_steps_with_unsupported_verification,
)
from app.services.orchestration.validation.validator import ValidatorService

STRUCTURED = ExecutionTopology.STRUCTURED_ORCHESTRATOR

# Retained U3 ``tasks.steps`` (P43-PRODUCT-25A live window, Task 1).
U3_ACCEPTED_STEPS = (
    '[{"step_number": 1, "description": "Implement page_window in pagekit/paging.py and export it", "commands": [], '
    '"verification": "python -m py_compile pagekit/paging.py", "rollback": null, "expected_files": '
    '["pagekit/paging.py", "pagekit/__init__.py"], "ops": [{"op": "append_file", "path": "pagekit/paging.py", '
    '"content": "\\n\\ndef page_window(page: Page, radius: int) -> List[int]:\\n    \\"\\"\\"Return a windowed list '
    'of page numbers around the current page.\\"\\"\\"\\n    if radius < 0:\\n        raise ValueError(\\"radius must '
    'be non-negative\\")\\n    total = page.total_pages\\n    current = page.number\\n    start = max(1, current - '
    "radius)\\n    end = min(total, current + radius)\\n    # Adjust start if we hit the end boundary\\n    "
    "window_size = 2 * radius + 1\\n    if end - start + 1 < window_size:\\n        start = max(1, end - "
    'window_size + 1)\\n    return list(range(start, end + 1))\\n"}, {"op": "replace_in_file", "path": '
    '"pagekit/__init__.py", "old": "from .links import page_numbers\\nfrom .paging import Page, paginate\\n\\n'
    '__all__ = [\\"Page\\", \\"page_numbers\\", \\"paginate\\"]", "new": "from .links import page_numbers\\nfrom '
    '.paging import Page, paginate, page_window\\n\\n__all__ = [\\"Page\\", \\"page_numbers\\", \\"paginate\\", '
    '\\"page_window\\"]"}]}, {"step_number": 2, "description": "Verify __init__.py syntax", "commands": ["python -m '
    'py_compile pagekit/__init__.py"], "verification": "python -m py_compile pagekit/__init__.py", "rollback": null, '
    '"expected_files": ["pagekit/__init__.py"], "ops": []}, {"step_number": 3, "description": "Run acceptance tests '
    'to verify implementation", "commands": ["python -m pytest tests/test_page_window_acceptance.py -q"], '
    '"verification": "python -m pytest tests/test_page_window_acceptance.py -q", "rollback": null, "expected_files": '
    '[], "ops": []}]'
)
U3_ACCEPTED_STEPS_SHA256 = (
    "ddb044773d8f18dfc07e19339ba5df13b857b93d3762cb939adc767af55aba94"
)

INIT = (
    '"""Small offset-pagination helpers."""\n\nfrom .links import page_numbers\n'
    'from .paging import Page, paginate\n\n__all__ = ["Page", "page_numbers", "paginate"]\n'
)
PAGING = (
    '"""Offset pagination over an in-memory sequence."""\n\n'
    "from dataclasses import dataclass, field\nfrom typing import Any, List, Sequence\n\n\n"
    "@dataclass(frozen=True)\nclass Page:\n    number: int\n    per_page: int\n"
    "    total_items: int\n    items: List[Any] = field(default_factory=list)\n"
)
TASK = (
    "Add `page_window(page, radius)` next to `page_numbers`, export it from the "
    "`pagekit` package, and add focused tests. Independent acceptance contract "
    "(required): - `python -m pytest tests/test_page_window_acceptance.py -q`"
)


@pytest.fixture
def c2_mode(monkeypatch):
    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "c2b-sandbox")


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.delenv("WORKER_ISOLATION_MODE", raising=False)
    monkeypatch.delenv("WORKER_BRIDGE_MODE", raising=False)


@pytest.fixture
def pagekit(tmp_path: Path) -> Path:
    (tmp_path / "pagekit").mkdir()
    (tmp_path / "pagekit" / "__init__.py").write_text(INIT, encoding="utf-8")
    (tmp_path / "pagekit" / "paging.py").write_text(PAGING, encoding="utf-8")
    (tmp_path / "pagekit" / "links.py").write_text(
        "def page_numbers(page):\n    return []\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_page_window_acceptance.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _u3_plan() -> list[dict]:
    assert (
        hashlib.sha256(U3_ACCEPTED_STEPS.encode("utf-8")).hexdigest()
        == U3_ACCEPTED_STEPS_SHA256
    )
    return json.loads(U3_ACCEPTED_STEPS)


def _validate(plan, project_dir):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="implementation",
        project_dir=project_dir,
        title="U3 pagekit task",
        description=TASK,
        is_first_ordered_task=True,
        execution_topology=STRUCTURED,
    )


def _with_verification(plan, command):
    for step in plan[:2]:
        step["verification"] = command
    plan[1]["commands"] = [command]
    return plan


# --- The U3 defect -----------------------------------------------------------


def test_u3_accepted_plan_is_not_admitted_in_c2_mode(c2_mode, pagekit):
    verdict = _validate(_u3_plan(), pagekit)

    assert verdict.status != "accepted"
    assert any(
        "unsupported_verification_command" in reason
        and "python -m py_compile pagekit/paging.py" in reason
        for reason in verdict.reasons
    ), verdict.reasons
    assert plan_steps_with_unsupported_verification(
        _u3_plan(), project_dir=pagekit, execution_topology=STRUCTURED
    ) == {
        1: "python -m py_compile pagekit/paging.py",
        2: "python -m py_compile pagekit/__init__.py",
    }


def test_c2_step_command_py_compile_is_reported_with_a_truthful_reason(
    c2_mode, pagekit
):
    plan = [
        {
            "description": "Compile",
            "commands": ["python -m py_compile pagekit/__init__.py"],
            "verification": None,
            "expected_files": [],
        }
    ]

    findings = plan_steps_with_unsupported_commands(
        plan, project_dir=pagekit, execution_topology=STRUCTURED
    )

    assert findings == {1: ["python -m py_compile pagekit/__init__.py"]}
    reason = describe_unsupported_step_commands(1, findings[1], project_dir=pagekit)
    assert "rejected 'python -m py_compile pagekit/__init__.py'" in reason
    assert len(reason) <= 180


def test_c2_planning_contract_advertises_only_sandbox_verification(c2_mode, pagekit):
    _, verifications = local_executor_command_examples(pagekit)

    assert "python -m py_compile module.py" not in verifications
    assert verifications[:2] == ["pytest -q tests", "python -m pytest -q tests"]


def test_c2_verification_route_itself_is_unchanged(c2_mode, pagekit):
    result = execution_flow.execute_verification_command(
        project_dir=pagekit,
        command="python -m py_compile pagekit/paging.py",
        execution_topology=STRUCTURED,
        sandbox_context=None,
    )

    assert result["success"] is False
    assert result["sandbox_rejection"] == "unsupported_operation"


# --- Controls ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_page_window_acceptance.py -q",
        'python -c "import sys; sys.exit(0)"',
    ],
)
def test_c2_admits_sandbox_executable_verification(c2_mode, pagekit, command):
    plan = _with_verification(_u3_plan(), command)

    assert (
        plan_steps_with_unsupported_verification(
            plan, project_dir=pagekit, execution_topology=STRUCTURED
        )
        == {}
    )
    assert (
        plan_steps_with_unsupported_commands(
            plan, project_dir=pagekit, execution_topology=STRUCTURED
        )
        == {}
    )


def test_c2_admits_pytest_on_a_test_file_the_plan_creates(c2_mode, pagekit):
    plan = [
        {
            "description": "Run the new test",
            "commands": ["python -m pytest tests/test_page_window.py -q"],
            "verification": "python -m pytest tests/test_page_window.py -q",
            "expected_files": [],
        }
    ]

    assert (
        plan_steps_with_unsupported_verification(
            plan, project_dir=pagekit, execution_topology=STRUCTURED
        )
        == {}
    )


def test_local_mode_admission_and_contract_are_unchanged(local_mode, pagekit):
    verdict = _validate(_u3_plan(), pagekit)
    _, verifications = local_executor_command_examples(pagekit)

    assert verdict.status == "accepted", verdict.reasons
    assert "python -m py_compile module.py" in verifications


def test_c2_chained_verification_keeps_its_existing_rejection(c2_mode, pagekit):
    plan = _with_verification(
        _u3_plan(),
        "python -m py_compile pagekit/paging.py && python -m py_compile pagekit/__init__.py",
    )

    assert set(
        plan_steps_with_unsupported_verification(
            plan, project_dir=pagekit, execution_topology=STRUCTURED
        )
    ) == {1, 2}


# --- Debug Repair provenance stays as strict as it was --------------------------


@pytest.mark.parametrize(
    ("op", "complete", "expected"),
    [
        (
            {"op": "write_file", "path": "pagekit/paging.py", "content": "x = 1\n"},
            ["pagekit/__init__.py"],
            "missing_source_provenance",
        ),
        (
            {"op": "write_file", "path": "pagekit/__init__.py", "content": INIT},
            ["pagekit/__init__.py"],
            None,
        ),
        (
            {"op": "write_file", "path": "pagekit/extra.py", "content": "x = 1\n"},
            ["pagekit/__init__.py"],
            "unauthorized_source_path",
        ),
    ],
)
def test_debug_repair_provenance_contract_is_unchanged(
    c2_mode, pagekit, op, complete, expected
):
    authority = accepted_path_authority_from_verdict(
        _validate(
            _with_verification(
                _u3_plan(), "python -m pytest tests/test_page_window_acceptance.py -q"
            ),
            pagekit,
        )
    )

    assert authority is not None
    assert (
        bounded_debug_repair_ops_authority_rejection(
            [op],
            authority=authority,
            project_dir=pagekit,
            complete_source_paths=complete,
            generated_test_paths=(),
        )
        == expected
    )
