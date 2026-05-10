from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "capture_task_evidence_bundle.py"
)
SPEC = importlib.util.spec_from_file_location(
    "capture_task_evidence_bundle", SCRIPT_PATH
)
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
            name text,
            status text,
            is_active boolean,
            deleted_at text
        );
        create table tasks (
            id integer primary key,
            project_id integer,
            title text,
            description text,
            status text,
            execution_profile text,
            error_message text,
            current_step integer
        );
        create table task_executions (
            id integer primary key,
            session_id integer,
            task_id integer,
            attempt_number integer,
            status text,
            started_at text,
            completed_at text,
            created_at text,
            updated_at text
        );
        create table log_entries (
            id integer primary key,
            session_id integer,
            task_id integer,
            task_execution_id integer,
            level text,
            message text,
            log_metadata text,
            created_at text
        );
        create table execution_failure_summaries (
            id integer primary key,
            session_id integer,
            summary text,
            operator_feedback text,
            generated_at text,
            feedback_at text,
            replan_planning_session_id integer
        );
        """
    )


def _seed(conn: sqlite3.Connection, workspace_path: str) -> None:
    conn.execute(
        "insert into projects values (1, 'bundle-project', ?)", (workspace_path,)
    )
    conn.execute(
        "insert into sessions values (10, 1, 'Bundle Session', 'stopped', 0, null)"
    )
    conn.execute(
        "insert into tasks values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            20,
            1,
            "Bundle Task",
            "Build a FastAPI backend. Do not create frontend files.",
            "failed",
            "full_lifecycle",
            "completion_validation_failed",
            2,
        ),
    )
    conn.execute(
        "insert into task_executions values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (30, 10, 20, 1, "failed", None, None, "now", "now"),
    )
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            100,
            10,
            20,
            30,
            "WARN",
            "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
            json.dumps(
                {
                    "phase": "planning",
                    "contract_violation_type": (
                        "plan_contains_brittle_heredoc_heavy_or_malformed_commands"
                    ),
                    "brittle_command_subcodes": ["too_many_lines"],
                }
            ),
            "now",
        ),
    )
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            101,
            10,
            20,
            30,
            "INFO",
            "[ORCHESTRATION] Planning repair attempt is now running",
            json.dumps({"phase": "planning", "attempt": "repair"}),
            "now",
        ),
    )
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            102,
            10,
            20,
            30,
            "INFO",
            "[ORCHESTRATION] Generated 2 steps in plan",
            json.dumps({"phase": "planning"}),
            "now",
        ),
    )
    conn.execute(
        "insert into log_entries values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            103,
            10,
            20,
            30,
            "WARN",
            "[ORCHESTRATION] Debug feedback captured",
            json.dumps(
                {
                    "event_type": "debug_feedback_captured",
                    "debug_failure_class": "completion_validation_failed",
                    "evidence_capsule_used": False,
                    "evidence_chars_total": 0,
                    "debug_feedback_envelope": {
                        "failure_class": "completion_validation_failed",
                        "eligible_for_debug_repair": True,
                    },
                }
            ),
            "now",
        ),
    )
    conn.execute(
        "insert into execution_failure_summaries values (?, ?, ?, ?, ?, ?, ?)",
        (1, 10, "Stored failure summary", None, "now", None, None),
    )


def _load(bundle_dir: Path, filename: str) -> dict:
    return json.loads((bundle_dir / filename).read_text(encoding="utf-8"))


def test_capture_task_evidence_bundle_writes_expected_files(tmp_path):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    workspace = tmp_path / "workspace"
    journal_dir = workspace / ".openclaw" / "events"
    journal_dir.mkdir(parents=True)
    (journal_dir / "session_10_task_20.jsonl").write_text(
        json.dumps(
            {
                "event_type": "workspace_evidence_collected",
                "timestamp": "now",
                "details": {
                    "failure_class": "completion_validation_failed",
                    "evidence_chars_total": 12,
                    "commands_run": ["find . -maxdepth 2 -type f"],
                    "evidence_files_inspected": ["index.html"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _seed(conn, str(workspace))
    conn.commit()
    conn.close()

    bundle_dir = module.capture_bundle(
        db_path=str(db_path),
        session_id=10,
        task_id=20,
        task_execution_id=30,
        output_dir=tmp_path / "bundles",
    )

    assert sorted(path.name for path in bundle_dir.iterdir()) == sorted(
        module.EXPECTED_FILES
    )
    assert _load(bundle_dir, "metadata.json")["context"]["task_execution_id"] == 30
    assert _load(bundle_dir, "failure_summary.json")["summary"] == (
        "Stored failure summary"
    )
    evidence = _load(bundle_dir, "workspace_evidence_summary.json")
    assert evidence["workspace_evidence_collected"] is True
    assert evidence["evidence_total_chars"] == 12
    planning = _load(bundle_dir, "planning_contract_summary.json")
    assert planning["available"] is True
    assert planning["record"]["planning_repair_recovered"] is True


def test_capture_task_evidence_bundle_degrades_when_workspace_missing(tmp_path):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    _seed(conn, str(tmp_path / "missing-workspace"))
    conn.commit()
    conn.close()

    bundle_dir = module.capture_bundle(
        db_path=str(db_path),
        session_id=10,
        task_id=20,
        task_execution_id=30,
        output_dir=tmp_path / "bundles",
    )

    metadata = _load(bundle_dir, "metadata.json")
    replay = _load(bundle_dir, "replay_report.semantic.json")
    timeline = _load(bundle_dir, "decision_timeline.json")

    assert metadata["event_journal"]["available"] is False
    assert metadata["event_journal"]["reason"] == "event_journal_missing"
    assert replay["available"] is False
    assert replay["reason"] == "workspace_missing"
    assert timeline["available"] is True
