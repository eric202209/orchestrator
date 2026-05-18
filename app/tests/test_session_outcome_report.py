from __future__ import annotations

import sqlite3

from scripts.session_outcome_report import _task_outcome_rates


def test_task_outcome_rates_are_task_centric_not_session_centric():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        create table projects (id integer primary key, deleted_at text);
        create table tasks (id integer primary key, project_id integer, status text);
        create table task_executions (
            id integer primary key,
            task_id integer,
            attempt_number integer,
            status text
        );

        insert into projects (id, deleted_at) values (1, null);
        insert into tasks (id, project_id, status) values
            (287, 1, 'done'),
            (288, 1, 'done'),
            (289, 1, 'done'),
            (290, 1, 'done');
        insert into task_executions (id, task_id, attempt_number, status) values
            (422, 287, 1, 'done'),
            (423, 288, 1, 'done'),
            (424, 289, 1, 'done'),
            (425, 290, 1, 'failed'),
            (426, 290, 2, 'done');
        """)

    metrics = _task_outcome_rates(conn, limit=50)

    assert metrics["total"] == 4
    assert metrics["counts"]["first_pass_success"] == 3
    assert metrics["counts"]["recovered_success"] == 1
    assert metrics["counts"]["final_done"] == 4
    assert metrics["rates"]["first_pass_success_rate"] == 0.75
    assert metrics["rates"]["recovered_success_rate"] == 0.25
    assert metrics["rates"]["final_success_rate"] == 1.0
