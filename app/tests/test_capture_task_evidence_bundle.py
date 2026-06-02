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
        create table task_execution_change_sets (
            id integer primary key,
            project_id integer,
            task_id integer,
            session_id integer,
            task_execution_id integer,
            base_snapshot_key text,
            head_snapshot_key text,
            snapshot_path text,
            target_path text,
            snapshot_exists boolean,
            added_files text,
            modified_files text,
            deleted_files text,
            warning_flags text,
            review_decision text,
            review_reason text,
            disposition text,
            disposition_reason text,
            disposition_at text,
            disposition_metadata text,
            status text,
            captured_at text,
            created_at text,
            updated_at text
        );
        """
    )


def _seed(
    conn: sqlite3.Connection,
    workspace_path: str,
    *,
    include_failure_summary: bool = True,
) -> None:
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
    if include_failure_summary:
        conn.execute(
            "insert into execution_failure_summaries values (?, ?, ?, ?, ?, ?, ?)",
            (1, 10, "Stored failure summary", None, "now", None, None),
        )


def _seed_change_set(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        insert into task_execution_change_sets values (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            40,
            1,
            20,
            10,
            30,
            "autosave_before_task_30",
            "autosave_after_task_30",
            "/tmp/snapshot",
            "/tmp/workspace",
            1,
            json.dumps(["src/app.py"]),
            json.dumps(["tests/test_app.py"]),
            json.dumps(["old.txt"]),
            json.dumps(["deleted_files", "config_files_changed"]),
            json.dumps({"outcome": "hold_for_review"}),
            "warning_flags_present",
            "captured",
            None,
            None,
            json.dumps({"source": "test"}),
            "failed",
            "now",
            "now",
            "now",
        ),
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
    _seed_change_set(conn)
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
    failure_summary = _load(bundle_dir, "failure_summary.json")
    assert failure_summary["available"] is False
    assert (
        failure_summary["reason"] == "stored_failure_summary_not_task_execution_scoped"
    )
    assert "Task error: completion_validation_failed" in failure_summary["summary"]
    assert failure_summary["ignored_stored_summary"]["scope"] == "session"
    evidence = _load(bundle_dir, "workspace_evidence_summary.json")
    assert evidence["workspace_evidence_collected"] is True
    assert evidence["evidence_total_chars"] == 12
    replay = _load(bundle_dir, "replay_report.semantic.json")
    assert replay["available"] is True
    assert replay["integrity"]["event_count_applied"] == 1
    change_set = _load(bundle_dir, "change_set_summary.json")
    assert change_set["available"] is True
    assert change_set["task_execution_id"] == 30
    assert change_set["changed_count"] == 3
    assert change_set["added_files"] == ["src/app.py"]
    assert change_set["modified_files"] == ["tests/test_app.py"]
    assert change_set["deleted_files"] == ["old.txt"]
    assert change_set["review_decision"]["outcome"] == "hold_for_review"
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


def test_capture_task_evidence_bundle_degrades_when_failure_summary_missing(
    tmp_path,
):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    _seed(
        conn,
        str(tmp_path / "missing-workspace"),
        include_failure_summary=False,
    )
    conn.commit()
    conn.close()

    bundle_dir = module.capture_bundle(
        db_path=str(db_path),
        session_id=10,
        task_id=20,
        task_execution_id=30,
        output_dir=tmp_path / "bundles",
    )

    failure_summary = _load(bundle_dir, "failure_summary.json")

    assert failure_summary["available"] is False
    assert failure_summary["reason"] == "stored_failure_summary_missing"
    assert "Debug feedback captured" in failure_summary["summary"]


def test_capture_task_evidence_bundle_prefers_task_execution_scoped_summary(
    tmp_path,
):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    _seed(conn, str(tmp_path / "workspace"), include_failure_summary=False)
    conn.execute(
        "alter table execution_failure_summaries add column task_execution_id integer"
    )
    conn.execute(
        """
        insert into execution_failure_summaries (
            id, session_id, summary, operator_feedback, generated_at, feedback_at,
            replan_planning_session_id, task_execution_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, 10, "Session-level stale summary", None, "older", None, None, 999),
    )
    conn.execute(
        """
        insert into execution_failure_summaries (
            id, session_id, summary, operator_feedback, generated_at, feedback_at,
            replan_planning_session_id, task_execution_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (2, 10, "TE30 scoped summary", None, "now", None, None, 30),
    )
    conn.commit()
    conn.close()

    bundle_dir = module.capture_bundle(
        db_path=str(db_path),
        session_id=10,
        task_id=20,
        task_execution_id=30,
        output_dir=tmp_path / "bundles",
    )

    failure_summary = _load(bundle_dir, "failure_summary.json")

    assert failure_summary["available"] is True
    assert failure_summary["scope"] == "task_execution"
    assert failure_summary["task_execution_id"] == 30
    assert failure_summary["summary"] == "TE30 scoped summary"


def test_capture_task_evidence_bundle_degrades_for_missing_task_execution(
    tmp_path,
):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    conn.commit()
    conn.close()

    bundle_dir = module.capture_bundle(
        db_path=str(db_path),
        session_id=133,
        task_id=19,
        task_execution_id=141,
        output_dir=tmp_path / "bundles",
    )

    assert sorted(path.name for path in bundle_dir.iterdir()) == sorted(
        module.EXPECTED_FILES
    )
    metadata = _load(bundle_dir, "metadata.json")
    replay = _load(bundle_dir, "replay_report.semantic.json")
    planning = _load(bundle_dir, "planning_contract_summary.json")

    assert metadata["context"]["available"] is False
    assert metadata["context"]["reason"] == "task_execution_not_found"
    assert metadata["event_journal"]["reason"] == "workspace_path_missing"
    assert replay["available"] is False
    assert replay["reason"] == "workspace_path_missing"
    assert planning["available"] is False
