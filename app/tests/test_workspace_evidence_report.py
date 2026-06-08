from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "session_and_replay"
    / "workspace_evidence_report.py"
)
SPEC = importlib.util.spec_from_file_location("workspace_evidence_report", SCRIPT_PATH)
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
            status text
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


def test_workspace_evidence_report_merges_log_and_journal_evidence(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    workspace = tmp_path / "project"
    journal_dir = workspace / ".agent" / "events"
    journal_dir.mkdir(parents=True)
    journal = journal_dir / "session_10_task_20.jsonl"
    journal.write_text(
        json.dumps(
            {
                "event_type": "workspace_evidence_collected",
                "details": {
                    "phase": "execution",
                    "failure_class": "pytest_failure",
                    "evidence_chars_total": 42,
                    "evidence_files_inspected": ["tests/test_demo.py"],
                    "commands_run": ["find . -maxdepth 4 -type f"],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "debug_repair_attempted",
                "details": {
                    "debug_prompt_mode": "phase7f_bounded_debug_repair",
                    "debug_prompt_mode_architecture": (
                        "bounded_execution_debug_repair"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    conn.execute("insert into projects values (1, 'project', ?)", (str(workspace),))
    conn.execute("insert into sessions values (10, 1, 'stopped')")
    conn.execute("insert into tasks values (20, 1, 'failed')")
    conn.execute("insert into task_executions values (30, 10, 20, 'failed')")
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?)",
        (
            100,
            10,
            20,
            30,
            "WARN",
            "Debug feedback captured",
            json.dumps(
                {
                    "event_type": "debug_feedback_captured",
                    "debug_failure_class": "pytest_failure",
                    "debug_feedback_envelope": {
                        "failure_class": "pytest_failure",
                        "eligible_for_debug_repair": True,
                    },
                    "evidence_capsule_used": True,
                    "evidence_chars_total": 42,
                }
            ),
        ),
    )

    summary = module.summarize(conn, limit=10)

    assert summary["task_execution_count"] == 1
    assert summary["workspace_evidence_collected"] == 1
    assert summary["average_evidence_chars"] == 42
    assert summary["by_failure_class"]["pytest_failure"]["evidence_collected"] == 1
    assert summary["top_evidence_files"] == [("tests/test_demo.py", 1)]
    assert summary["records"][0]["debug_prompt_mode"] == (
        "phase7f_bounded_debug_repair"
    )
    assert summary["records"][0]["debug_prompt_mode_architecture"] == (
        "bounded_execution_debug_repair"
    )


def test_workspace_evidence_report_uses_metadata_when_journal_is_gone(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    workspace = tmp_path / "removed-project"
    conn.execute(
        "insert into projects values (1, 'removed-project', ?)", (str(workspace),)
    )
    conn.execute("insert into sessions values (10, 1, 'stopped')")
    conn.execute("insert into tasks values (20, 1, 'failed')")
    conn.execute("insert into task_executions values (30, 10, 20, 'failed')")
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?)",
        (
            100,
            10,
            20,
            30,
            "WARN",
            "Debug feedback captured",
            json.dumps(
                {
                    "event_type": "debug_feedback_captured",
                    "debug_failure_class": "pytest_failure",
                    "debug_feedback_envelope": {
                        "failure_class": "pytest_failure",
                        "eligible_for_debug_repair": True,
                    },
                    "evidence_capsule_used": True,
                    "evidence_chars_total": 77,
                }
            ),
        ),
    )

    summary = module.summarize(conn, limit=10)

    assert summary["workspace_evidence_collected"] == 1
    assert summary["average_evidence_chars"] == 77
    assert summary["by_failure_class"]["pytest_failure"]["evidence_collected"] == 1
    assert summary["top_evidence_files"] == []
