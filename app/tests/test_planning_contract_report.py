from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "planning_contract_report.py"
)
SPEC = importlib.util.spec_from_file_location("planning_contract_report", SCRIPT_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _schema(conn: sqlite3.Connection) -> None:
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


def _insert_execution(
    conn: sqlite3.Connection,
    *,
    task_execution_id: int,
    project_name: str = "project",
    task_title: str = "Build a FastAPI backend",
    task_description: str = "Create a FastAPI notes API. Do not create frontend files.",
    task_status: str = "done",
    execution_status: str = "done",
) -> None:
    project_id = task_execution_id
    session_id = task_execution_id + 1000
    task_id = task_execution_id + 2000
    conn.execute(
        "insert into projects values (?, ?, ?)",
        (project_id, project_name, f"/tmp/{project_name}"),
    )
    conn.execute(
        "insert into sessions values (?, ?, 'stopped')", (session_id, project_id)
    )
    conn.execute(
        "insert into tasks values (?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            project_id,
            task_title,
            task_description,
            task_status,
            "full_lifecycle",
            "",
        ),
    )
    conn.execute(
        "insert into task_executions values (?, ?, ?, ?)",
        (task_execution_id, session_id, task_id, execution_status),
    )


def _insert_log(
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
            task_execution_id + 1000,
            task_execution_id + 2000,
            task_execution_id,
            "WARN",
            message,
            json.dumps(metadata),
        ),
    )


def test_planning_contract_report_summarizes_repair_recovery():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    _insert_execution(conn, task_execution_id=10)
    _insert_log(
        conn,
        log_id=1,
        task_execution_id=10,
        message="[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
        metadata={
            "phase": "planning",
            "reason": "plan_validation_failed",
            "contract_violation_type": (
                "plan_contains_brittle_heredoc_heavy_or_malformed_commands"
            ),
            "contract_violations": [
                "Plan contains brittle heredoc-heavy or malformed commands"
            ],
            "brittle_command_subcodes": ["brittle_inline_python"],
        },
    )
    _insert_log(
        conn,
        log_id=2,
        task_execution_id=10,
        message="[ORCHESTRATION] Planning repair attempt is now running",
        metadata={"phase": "planning", "attempt": "repair"},
    )
    _insert_log(
        conn,
        log_id=3,
        task_execution_id=10,
        message="[ORCHESTRATION] Planning repair completed in 2.00s",
        metadata={"phase": "planning", "attempt": "repair"},
    )
    _insert_log(
        conn,
        log_id=4,
        task_execution_id=10,
        message="[ORCHESTRATION] Generated 4 steps in plan",
        metadata={"phase": "planning", "steps": 4},
    )

    summary = module.summarize(conn, limit=10)
    record = summary["records"][0]

    assert summary["task_execution_count"] == 1
    assert summary["initial_contract_failed"] == 1
    assert summary["planning_repair_attempted"] == 1
    assert summary["planning_repair_recovered"] == 1
    assert record["workflow_profile"] == "backend_only"
    assert record["brittle_command_subcodes"] == ["brittle_inline_python"]


def test_planning_contract_report_threshold_requires_distinct_executions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    reason = "truncated_multi_step_plan_collapsed_into_a_single_step"
    for task_execution_id in (10, 11, 12):
        _insert_execution(conn, task_execution_id=task_execution_id)
        for offset in (0, 1):
            _insert_log(
                conn,
                log_id=task_execution_id * 10 + offset,
                task_execution_id=task_execution_id,
                message="[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
                metadata={
                    "phase": "planning",
                    "reason": "truncated_multistep_plan_detected",
                    "contract_violation_type": reason,
                },
            )

    summary = module.summarize(conn, limit=10, diagnostic_threshold=3)

    assert summary["contract_reason_counts"][reason] == 3
    assert summary["diagnostic_change_candidates"] == {reason: 3}


def test_planning_contract_report_summarizes_truncated_subcodes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    reason = "truncated_multi_step_plan_collapsed_into_a_single_step"
    _insert_execution(conn, task_execution_id=10, execution_status="failed")
    _insert_log(
        conn,
        log_id=1,
        task_execution_id=10,
        message="[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
        metadata={
            "phase": "planning",
            "reason": "truncated_multistep_plan_detected",
            "contract_violation_type": reason,
            "truncated_multistep_subcodes": [
                "original_steps_detected_3",
                "absorbed_into_step_1",
                "collapse_before_first_repair",
            ],
        },
    )

    summary = module.summarize(conn, limit=10)
    record = summary["records"][0]

    assert record["contract_reasons"] == [reason]
    assert record["truncated_multistep_subcodes"] == [
        "absorbed_into_step_1",
        "collapse_before_first_repair",
        "original_steps_detected_3",
    ]


def test_planning_contract_report_fallback_profile_handles_negated_frontend(
    monkeypatch,
):
    monkeypatch.setattr(module, "get_workflow_profile", None)
    context = {
        "execution_profile": "full_lifecycle",
        "task_title": "Build a FastAPI backend",
        "task_description": "Create API routes. Do not create frontend files.",
    }

    assert module._workflow_profile(context) == "backend_only"


def test_planning_contract_report_counts_saved_plan_reuse_as_completed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    _insert_execution(conn, task_execution_id=10)
    _insert_log(
        conn,
        log_id=1,
        task_execution_id=10,
        message="[ORCHESTRATION] Reusing saved plan with 4 steps",
        metadata={
            "phase": "planning",
            "source": "stored_task_plan",
            "task_execution_id": 10,
        },
    )

    summary = module.summarize(conn, limit=10)
    record = summary["records"][0]

    assert summary["planning_completed"] == 1
    assert record["initial_planning_seen"] is True
    assert record["saved_plan_reused"] is True
    assert record["planning_repair_count"] == 0


def test_planning_contract_report_groups_recovered_downstream_outcomes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    reason = "plan_contains_brittle_heredoc_heavy_or_malformed_commands"
    _insert_recovered_execution(
        conn,
        task_execution_id=10,
        reason=reason,
        task_status="done",
        execution_status="done",
    )
    _insert_recovered_execution(
        conn,
        task_execution_id=11,
        reason=reason,
        task_status="failed",
        execution_status="failed",
        terminal_reason="completion_validation_failed",
    )

    summary = module.summarize(conn, limit=10)

    assert summary["planning_repair_recovered"] == 2
    assert summary["recovered_outcomes"] == {
        "total": 2,
        "done": 1,
        "not_done": 1,
        "done_rate": 0.5,
        "terminal_reasons": {"completion_validation_failed": 1},
        "not_done_task_executions": [11],
    }
    assert summary["recovered_outcomes_by_contract_reason"][reason]["done"] == 1
    assert summary["recovered_outcomes_by_contract_reason"][reason]["not_done"] == 1
    assert (
        summary["recovered_outcomes_by_workflow_profile"]["backend_only"]["done_rate"]
        == 0.5
    )


def _insert_recovered_execution(
    conn: sqlite3.Connection,
    *,
    task_execution_id: int,
    reason: str,
    task_status: str,
    execution_status: str,
    terminal_reason: str = "",
) -> None:
    _insert_execution(
        conn,
        task_execution_id=task_execution_id,
        task_status=task_status,
        execution_status=execution_status,
    )
    _insert_log(
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
    _insert_log(
        conn,
        log_id=task_execution_id * 10 + 1,
        task_execution_id=task_execution_id,
        message="[ORCHESTRATION] Planning repair attempt is now running",
        metadata={"phase": "planning", "attempt": "repair"},
    )
    _insert_log(
        conn,
        log_id=task_execution_id * 10 + 2,
        task_execution_id=task_execution_id,
        message="[ORCHESTRATION] Generated 2 steps in plan",
        metadata={"phase": "planning"},
    )
    if terminal_reason:
        _insert_log(
            conn,
            log_id=task_execution_id * 10 + 3,
            task_execution_id=task_execution_id,
            message="[ORCHESTRATION] Task failed after recovered planning",
            metadata={"reason": terminal_reason},
        )
