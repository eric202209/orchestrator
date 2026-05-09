from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "phase6b_evidence_report.py"
)
SPEC = importlib.util.spec_from_file_location("phase6b_evidence_report", SCRIPT_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table sessions (
            id integer primary key,
            project_id integer,
            name text,
            status text,
            is_active integer,
            created_at text,
            started_at text,
            stopped_at text,
            deleted_at text
        );
        create table tasks (
            id integer primary key,
            project_id integer,
            title text,
            status text
        );
        create table task_executions (
            id integer primary key,
            session_id integer,
            task_id integer,
            attempt_number integer,
            status text,
            started_at text,
            completed_at text
        );
        create table log_entries (
            id integer primary key,
            session_id integer,
            task_id integer,
            task_execution_id integer,
            level text,
            message text,
            created_at text
        );
        """
    )


def test_phase6b_evidence_report_passes_for_coherent_runtime_state(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        "insert into sessions values (1, 10, 'workflow', 'running', 1, null, null, null, null)"
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'RUNNING')")
    conn.executemany(
        "insert into task_executions values (?, ?, ?, ?, ?, null, null)",
        [(1, 1, 5, 1, "FAILED"), (2, 1, 5, 2, "RUNNING")],
    )
    conn.execute(
        "insert into log_entries values (20, 1, 5, 2, 'INFO', '[OPENCLAW] ok', null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=False,
    )

    assert report["pass"] is True
    assert report["checks"]["failed_task_rerun_stays_in_same_workflow_session"] is True
    assert report["checks"]["runtime_logs_have_task_execution_id"] is True


def test_phase6b_evidence_report_allows_other_isolated_attempts_for_same_task():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.executemany(
        "insert into sessions values (?, 10, ?, 'running', 1, null, null, null, null)",
        [(1, "workflow"), (2, "isolated")],
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'FAILED')")
    conn.executemany(
        "insert into task_executions values (?, ?, ?, ?, ?, null, null)",
        [
            (1, 1, 5, 1, "FAILED"),
            (2, 1, 5, 2, "FAILED"),
            (3, 2, 5, 1, "CANCELLED"),
        ],
    )
    conn.execute(
        "insert into log_entries values (20, 1, 5, 2, 'INFO', '[OPENCLAW] ok', null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1, 2},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=False,
    )

    assert report["pass"] is True
    assert report["attempt_report"]["all_attempt_session_ids"] == [1, 2]
    assert report["checks"]["failed_task_rerun_stays_in_same_workflow_session"] is True


def test_phase6b_evidence_report_flags_identity_regressions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.executemany(
        "insert into sessions values (?, 10, ?, ?, ?, null, null, null, null)",
        [(1, "workflow", "stopped", 0), (2, "unexpected", "running", 1)],
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'RUNNING')")
    conn.execute(
        "insert into task_executions values (1, 1, 5, 1, 'RUNNING', null, null)"
    )
    conn.execute(
        "insert into log_entries values (20, 1, 5, null, 'INFO', '[PERFORMANCE] ok', null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=False,
    )

    assert report["pass"] is False
    assert report["unexpected_sessions"] == [2]
    assert report["checks"]["stopped_sessions_have_no_running_executions"] is False
    assert report["checks"]["runtime_logs_have_task_execution_id"] is False


def test_phase6b_evidence_report_does_not_require_rerun_on_first_attempt():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        "insert into sessions values (1, 10, 'workflow', 'running', 1, null, null, null, null)"
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'RUNNING')")
    conn.execute(
        "insert into task_executions values (1, 1, 5, 1, 'RUNNING', null, null)"
    )
    conn.execute(
        "insert into log_entries values (20, 1, 5, 1, 'INFO', '[OPENCLAW] ok', null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=False,
    )

    assert report["pass"] is True
    assert "failed_task_rerun_stays_in_same_workflow_session" not in report["checks"]


def test_phase6b_evidence_report_can_require_failed_rerun_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        "insert into sessions values (1, 10, 'workflow', 'running', 1, null, null, null, null)"
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'RUNNING')")
    conn.execute(
        "insert into task_executions values (1, 1, 5, 1, 'RUNNING', null, null)"
    )
    conn.execute(
        "insert into log_entries values (20, 1, 5, 1, 'INFO', '[OPENCLAW] ok', null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=True,
    )

    assert report["pass"] is False
    assert report["checks"]["failed_task_rerun_stays_in_same_workflow_session"] is False


def test_phase6b_evidence_report_requires_runtime_log_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        "insert into sessions values (1, 10, 'workflow', 'running', 1, null, null, null, null)"
    )
    conn.execute("insert into tasks values (5, 10, 'task', 'RUNNING')")
    conn.execute(
        "insert into task_executions values (1, 1, 5, 1, 'RUNNING', null, null)"
    )

    report = module.build_report(
        conn,
        project_id=10,
        session_id=1,
        task_id=5,
        expected_session_ids={1},
        max_session_id_before=None,
        since_log_id=0,
        project_dir=None,
        api_base=None,
        api_token=None,
        require_failed_rerun=False,
    )

    assert report["pass"] is False
    assert report["checks"]["runtime_logs_have_task_execution_id"] is False


def test_phase6b_evidence_report_writes_machine_readable_json(tmp_path):
    report = {
        "project_id": 10,
        "session_id": 1,
        "checks": {"runtime_logs_have_task_execution_id": True},
        "pass": True,
    }
    output_path = tmp_path / "reports" / "phase6b" / "session_1_evidence.json"

    module.write_json_report(report, output_path)

    assert json.loads(output_path.read_text(encoding="utf-8")) == report
