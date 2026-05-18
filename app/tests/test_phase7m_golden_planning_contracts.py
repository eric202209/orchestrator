from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.validation.validator import ValidatorService

REPORT_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "planning_contract_report.py"
)
SPEC = importlib.util.spec_from_file_location("planning_contract_report", REPORT_SCRIPT)
assert SPEC and SPEC.loader
planning_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planning_report
SPEC.loader.exec_module(planning_report)


def _step(
    step_number: int,
    description: str,
    commands: list[str],
    *,
    verification: str = "echo ok",
    expected_files: list[str] | None = None,
) -> dict:
    return {
        "step_number": step_number,
        "description": description,
        "commands": commands,
        "verification": verification,
        "rollback": None,
        "expected_files": expected_files or [],
    }


@pytest.mark.parametrize(
    ("title", "description", "expected_profile"),
    [
        (
            "Golden Python CLI",
            "Build a Python CLI with pytest coverage.",
            "default",
        ),
        (
            "Golden FastAPI backend",
            "Build a FastAPI notes API with TestClient tests. Do not create frontend files.",
            "backend_only",
        ),
        (
            "Golden static site",
            "Build a static frontend site with index.html. Do not create a backend.",
            "frontend_only",
        ),
        (
            "Golden fullstack app",
            "Set up frontend React and backend FastAPI with clean architecture.",
            "fullstack_scaffold",
        ),
    ],
)
def test_phase7m_offline_golden_task_shape_profiles(
    title,
    description,
    expected_profile,
):
    assert (
        get_workflow_profile("full_lifecycle", title, description) == expected_profile
    )


def test_phase7m_offline_golden_missing_workspace_file_contract(tmp_path):
    plan = [
        _step(
            1,
            "Verify the expected FastAPI core file",
            ["python -m pytest -q"],
            verification="python -m pytest -q",
            expected_files=["app/main.py"],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Verify existing workspace files",
        execution_profile="test_only",
        project_dir=tmp_path,
        title="Golden file verifier",
        description="Verify app/main.py exists before running tests.",
        workflow_profile="backend_only",
    )

    assert verdict.repairable is True
    assert (
        "references source files that do not exist" in " ".join(verdict.reasons).lower()
    )
    assert verdict.details["missing_workspace_expected_files"] == ["app/main.py"]


def test_phase7m_offline_golden_pytest_assertion_plan_is_accepted(tmp_path):
    plan = [
        _step(
            1,
            "Implement calculator",
            ["printf 'def add(a, b):\\n    return a + b\\n' > calc.py"],
            verification="python -m pytest -q",
            expected_files=["calc.py"],
        ),
        _step(
            2,
            "Add assertion tests",
            [
                "printf 'from calc import add\\n\\ndef test_add():\\n    assert add(1, 2) == 3\\n' > test_calc.py"
            ],
            verification="python -m pytest -q",
            expected_files=["test_calc.py"],
        ),
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a Python CLI with pytest assertion tests",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Golden Python CLI",
        description="Build a Python CLI with pytest coverage.",
    )

    assert verdict.accepted is True


def test_phase7m_offline_golden_brittle_reason_includes_subcodes(tmp_path):
    heredoc1 = "cat > app.py <<'PY'\nprint('hello')\nPY"
    heredoc2 = "cat > test_app.py <<'PY'\ndef test_ok():\n    assert True\nPY"
    plan = [
        _step(1, "Write app", [heredoc1], expected_files=["app.py"]),
        _step(
            2,
            "Write tests",
            [heredoc2],
            verification="python -m pytest -q",
            expected_files=["test_app.py"],
        ),
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a Python CLI with pytest",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Golden Python CLI",
        description="Build a Python CLI with pytest coverage.",
    )

    assert verdict.repairable is True
    assert "Plan contains brittle heredoc-heavy or malformed commands" in " ".join(
        verdict.reasons
    )
    assert "multiple_heredoc_across_plan" in verdict.details["brittle_command_subcodes"]


def test_phase7m_offline_golden_report_threshold_is_not_sufficient():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _report_schema(conn)
    reason = "plan_contains_brittle_heredoc_heavy_or_malformed_commands"
    for task_execution_id in (1, 2, 3):
        _insert_report_execution(conn, task_execution_id)
        _insert_report_log(
            conn,
            log_id=task_execution_id * 10,
            task_execution_id=task_execution_id,
            message="[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
            metadata={
                "phase": "planning",
                "contract_violation_type": reason,
                "brittle_command_subcodes": ["too_many_lines"],
            },
        )
        _insert_report_log(
            conn,
            log_id=task_execution_id * 10 + 1,
            task_execution_id=task_execution_id,
            message="[ORCHESTRATION] Planning repair attempt is now running",
            metadata={"phase": "planning", "attempt": "repair"},
        )
        _insert_report_log(
            conn,
            log_id=task_execution_id * 10 + 2,
            task_execution_id=task_execution_id,
            message="[ORCHESTRATION] Generated 2 steps in plan",
            metadata={"phase": "planning"},
        )

    summary = planning_report.summarize(conn, limit=10, diagnostic_threshold=3)

    assert summary["diagnostic_change_candidates"] == {reason: 3}
    assert summary["planning_repair_recovered"] == 3
    assert all(
        record["brittle_command_subcodes"] == ["too_many_lines"]
        for record in summary["records"]
    )


def _report_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table projects (
            id integer primary key,
            name text,
            workspace_path text
        );
        create table sessions (
            id integer primary key,
            project_id integer,
            status text
        );
        create table tasks (
            id integer primary key,
            project_id integer,
            title text,
            description text,
            status text,
            execution_profile text,
            error_message text
        );
        create table task_executions (
            id integer primary key,
            session_id integer,
            task_id integer,
            status text
        );
        create table log_entries (
            id integer primary key,
            session_id integer,
            task_id integer,
            task_execution_id integer,
            level text,
            message text,
            log_metadata text
        );
        """
    )


def _insert_report_execution(conn: sqlite3.Connection, task_execution_id: int) -> None:
    conn.execute(
        "insert into projects values (?, ?, ?)",
        (task_execution_id, f"golden-{task_execution_id}", "/tmp/golden"),
    )
    conn.execute(
        "insert into sessions values (?, ?, 'stopped')",
        (task_execution_id + 100, task_execution_id),
    )
    conn.execute(
        "insert into tasks values (?, ?, ?, ?, ?, ?, ?)",
        (
            task_execution_id + 200,
            task_execution_id,
            "Golden FastAPI backend",
            "Build a FastAPI API. Do not create frontend files.",
            "done",
            "full_lifecycle",
            "",
        ),
    )
    conn.execute(
        "insert into task_executions values (?, ?, ?, 'done')",
        (task_execution_id, task_execution_id + 100, task_execution_id + 200),
    )


def _insert_report_log(
    conn: sqlite3.Connection,
    *,
    log_id: int,
    task_execution_id: int,
    message: str,
    metadata: dict,
) -> None:
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?)",
        (
            log_id,
            task_execution_id + 100,
            task_execution_id + 200,
            task_execution_id,
            "WARN",
            message,
            json.dumps(metadata),
        ),
    )
