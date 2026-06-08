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
    / "capture_task_evidence_bundle.py"
)
SPEC = importlib.util.spec_from_file_location(
    "capture_task_evidence_bundle", SCRIPT_PATH
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_resolve_workspace_path_maps_docker_project_path_to_host_root(
    tmp_path, monkeypatch
):
    host_root = tmp_path / "projects"
    host_root.mkdir()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(host_root))

    resolved = module._resolve_workspace_path("/app/projects/example-workspace")

    assert resolved == host_root / "example-workspace"


def test_resolve_workspace_path_maps_relative_slug_to_host_root(tmp_path, monkeypatch):
    host_root = tmp_path / "projects"
    host_root.mkdir()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(host_root))

    resolved = module._resolve_workspace_path("example-workspace")

    assert resolved == host_root / "example-workspace"


def test_resolve_workspace_path_leaves_other_absolute_paths_unchanged(tmp_path):
    resolved = module._resolve_workspace_path(tmp_path / "workspace")

    assert resolved == tmp_path / "workspace"


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
    journal_dir = workspace / ".agent" / "events"
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


def test_replay_bundle_field_coverage_characterization(tmp_path):
    """Characterize RunReplayBundle field coverage against the gap matrix.

    Each assertion is labelled AVAILABLE, PARTIAL, or ABSENT to document the
    current state.  This test should fail if a gap is accidentally closed or
    regressed.
    """
    workspace = tmp_path / "workspace"
    journal_dir = workspace / ".agent" / "events"
    journal_dir.mkdir(parents=True)

    known_workspace_hash = "abc123def456abc123"
    journal_events = [
        {
            "event_id": "evt-001",
            "event_type": "task_started",
            "timestamp": "2026-06-02T10:00:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"phase": "execution"},
        },
        {
            "event_id": "evt-002",
            "event_type": "phase_started",
            "timestamp": "2026-06-02T10:00:01Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"phase": "planning"},
        },
        {
            "event_id": "evt-003",
            "event_type": "checkpoint_saved",
            "timestamp": "2026-06-02T10:01:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "checkpoint_name": "step_2_pre_repair",
                "current_step_index": 2,
            },
        },
        {
            "event_id": "evt-004",
            "event_type": "validation_result",
            "timestamp": "2026-06-02T10:02:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "stage": "completion_validation",
                "status": "failed",
                "reason": "missing_test_file",
                "verification_command": "pytest tests/",
            },
        },
        {
            "event_id": "evt-005",
            "event_type": "repair_generated",
            "timestamp": "2026-06-02T10:02:30Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"attempt": 1},
        },
        {
            "event_id": "evt-006",
            "event_type": "repair_rejected",
            "timestamp": "2026-06-02T10:03:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"attempt": 1, "reason": "bootstrap_contract_violation"},
        },
        {
            "event_id": "evt-007",
            "event_type": "debug_repair_attempted",
            "timestamp": "2026-06-02T10:03:30Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"attempt": 1, "backend": "qwen-local"},
        },
        {
            "event_id": "evt-008",
            "event_type": "workspace_evidence_collected",
            "timestamp": "2026-06-02T10:04:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "failure_class": "completion_validation_failed",
                "evidence_chars_total": 512,
                "commands_run": ["pytest tests/", "find . -name '*.py'"],
                "evidence_files_inspected": ["tests/test_app.py"],
                "workspace_hash": known_workspace_hash,
            },
        },
        {
            "event_id": "evt-009",
            "event_type": "task_failed",
            "timestamp": "2026-06-02T10:05:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "failure_category": "completion_validation_failed",
                "terminal_reason": "max_repair_attempts_reached",
            },
        },
    ]
    journal_path = journal_dir / "session_10_task_20.jsonl"
    journal_path.write_text(
        "\n".join(json.dumps(e) for e in journal_events) + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    _seed(conn, str(workspace))
    _seed_change_set(conn)
    conn.execute(
        "insert into execution_failure_summaries values (?, ?, ?, ?, ?, ?, ?)",
        (
            2,
            10,
            "Completion validation failed: missing test file",
            None,
            "2026-06-02T10:05:01Z",
            None,
            None,
        ),
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

    metadata = _load(bundle_dir, "metadata.json")
    failure_doc = _load(bundle_dir, "failure_summary.json")
    timeline = _load(bundle_dir, "decision_timeline.json")
    replay = _load(bundle_dir, "replay_report.semantic.json")
    workspace_ev = _load(bundle_dir, "workspace_evidence_summary.json")
    manifest = _load(bundle_dir, "run_replay_bundle.json")

    # ── AVAILABLE ────────────────────────────────────────────────────────────

    # prompt: task title + description present in context
    assert metadata["context"]["task_title"] == "Bundle Task"
    assert "FastAPI" in (metadata["context"]["task_description"] or "")

    # event_journal: path resolved, all events present
    assert metadata["event_journal"]["available"] is True
    assert metadata["event_journal"]["event_count"] == len(journal_events)

    # workspace_path: both raw and resolved present
    assert metadata["context"]["workspace_path"] is not None
    assert metadata["context"]["resolved_workspace_path"] is not None

    # build_identity / backend_lanes / model_names: via runtime_identity capture-time
    ri = metadata["runtime_identity"]
    assert ri["source"] == "capture_time_fallback"
    assert set(ri["build"]) >= {
        "version",
        "build_git_sha",
        "repo_git_sha",
        "image_tag",
        "stale_container_check",
    }
    assert set(ri["lanes"]) >= {"planning", "execution", "debug_repair", "repair"}
    assert set(ri["models"]) >= {
        "planner",
        "execution",
        "debug_repair",
        "planning_repair",
    }

    # workspace_hash: captured in workspace_evidence_collected event details
    assert replay["available"] is True
    assert known_workspace_hash in replay["artifact_state"]["workspace_hashes"]

    # ── PARTIAL ──────────────────────────────────────────────────────────────

    # checkpoint_refs: latest_checkpoint_name in replay state; no consolidated
    # checkpoint_refs[] list across event/db/disk sources exists in the bundle
    assert replay["state"]["latest_checkpoint_name"] == "step_2_pre_repair"

    # verification_commands: canonical manifest consolidates workspace and
    # validation commands.
    assert workspace_ev["workspace_evidence_collected"] is True
    assert "pytest tests/" in workspace_ev["commands_run"]
    assert any(
        record["command"] == "pytest tests/"
        for record in manifest["verification"]["commands"]
    )

    # verification_results: validation command status is represented separately.
    assert "failed" in replay["state"]["validation_verdict_status_history"]
    assert any(
        record["status"] == "failed" for record in manifest["verification"]["results"]
    )

    # repair_attempts: repair_count in replay state and repair events in
    # decision timeline; no repair.attempts[] list with input/model/backend/result
    assert replay["state"]["repair_count"] > 0
    journal_event_types = [
        e["event_type"]
        for e in timeline["events"]
        if e.get("source") == "event_journal"
    ]
    assert "repair_generated" in journal_event_types
    assert "repair_rejected" in journal_event_types

    # terminal_reason: canonical manifest has a separate terminal_reason field.
    assert metadata["context"]["task_error_message"] == "completion_validation_failed"
    assert failure_doc["summary"] is not None
    assert "completion_validation_failed" in failure_doc["summary"]
    assert manifest["terminal"]["terminal_reason"] == "completion_validation_failed"

    # failure_summary: stored but session-scoped so bundle falls back to log
    # excerpt; gap closed when failure summary is scoped to task_execution
    assert failure_doc["available"] is False
    assert failure_doc["reason"] == "stored_failure_summary_not_task_execution_scoped"
    assert failure_doc["ignored_stored_summary"]["scope"] == "session"

    # ── ABSENT ───────────────────────────────────────────────────────────────

    # config_snapshot: metadata exposes sanitized runtime config provenance.
    assert "config_snapshot" in metadata
    assert "effective_config" not in metadata

    # scorer_result: non-eval runs record explicit absence in the manifest.
    assert manifest["scorer"]["available"] is False
    assert manifest["scorer"]["reason"] == "scorer_result_not_found"


def test_capture_task_evidence_bundle_runtime_identity_structure(tmp_path):
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
    ri = metadata["runtime_identity"]
    assert ri["source"] == "capture_time_fallback"
    assert "captured_at" in ri
    for section in ("build", "database", "lanes", "models", "config"):
        assert section in ri, f"runtime_identity missing section: {section}"
    assert set(ri["build"]) >= {
        "version",
        "build_git_sha",
        "repo_git_sha",
        "build_time",
        "image_tag",
        "image_id",
        "stale_container_check",
    }
    assert set(ri["lanes"]) >= {"planning", "execution", "debug_repair", "repair"}
    assert set(ri["models"]) >= {
        "planner",
        "execution",
        "debug_repair",
        "planning_repair",
    }
    assert ri["database"]["migration_status"] in {"ok", "pending", "unavailable"}
    assert "config_source" in ri["config"]


def test_run_replay_bundle_manifest_canonical_shape(tmp_path):
    """run_replay_bundle.json follows the canonical RunReplayBundle v1 shape."""
    workspace = tmp_path / "workspace"
    journal_dir = workspace / ".agent" / "events"
    journal_dir.mkdir(parents=True)

    journal_events = [
        {
            "event_type": "task_started",
            "timestamp": "2026-06-02T10:00:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "execution_profile": "full_lifecycle",
                "run_start_runtime_identity": {
                    "source": "task_started_event",
                    "captured_at": "2026-06-02T10:00:00Z",
                    "build": {
                        "version": "0.1.0",
                        "build_git_sha": "abc",
                        "repo_git_sha": "abc",
                        "stale_container_check": "ok",
                    },
                    "lanes": {
                        "planning": "openai_responses_api",
                        "execution": "local_openclaw",
                        "debug_repair": "openai_responses_api",
                        "repair": "local_openclaw",
                    },
                    "models": {
                        "planner": "gpt-5",
                        "execution": "local",
                        "debug_repair": "gpt-5",
                        "planning_repair": "qwen-local",
                    },
                    "config": {
                        "source": "task_started_event",
                        "values": {
                            "AGENT_BACKEND": "local_openclaw",
                            "PLANNING_BACKEND": "openai_responses_api",
                        },
                        "effective": {
                            "planning_backend": "openai_responses_api",
                        },
                        "secret_fields_omitted": ["OPENAI_API_KEY"],
                    },
                },
                "run_start_config_snapshot": {
                    "source": "task_started_event",
                },
            },
        },
        {
            "event_type": "checkpoint_saved",
            "timestamp": "2026-06-02T10:01:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"checkpoint_name": "pre_repair_step_2"},
        },
        {
            "event_type": "phase_started",
            "timestamp": "2026-06-02T10:01:30Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "phase": "planning",
                "planning_prompt_ref": {
                    "kind": "sha256",
                    "sha256": "prompt-sha",
                    "chars": 1200,
                    "estimated_tokens": 300,
                },
            },
        },
        {
            "event_type": "validation_result",
            "timestamp": "2026-06-02T10:02:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {
                "stage": "completion_validation",
                "status": "failed",
                "reason": "missing_test_file",
                "verification_command": "pytest tests/",
            },
        },
        {
            "event_type": "repair_generated",
            "timestamp": "2026-06-02T10:02:30Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"attempt": 1},
        },
        {
            "event_type": "repair_rejected",
            "timestamp": "2026-06-02T10:03:00Z",
            "session_id": 10,
            "task_id": 20,
            "details": {"attempt": 1, "reason": "bootstrap_contract_violation"},
        },
    ]
    (journal_dir / "session_10_task_20.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal_events) + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
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

    manifest = _load(bundle_dir, "run_replay_bundle.json")

    # top-level shape
    assert manifest["schema_version"] == 1
    assert "captured_at" in manifest
    assert manifest["capture_tool"] == "scripts/capture_task_evidence_bundle.py"
    assert "run_replay_bundle.json" in manifest["bundle_files"]
    assert "run_replay_bundle.txt" in manifest["bundle_files"]

    # ids
    ids = manifest["ids"]
    assert ids["session_id"] == 10
    assert ids["task_id"] == 20
    assert ids["task_execution_id"] == 30
    assert ids["attempt_number"] == 1

    # prompt
    assert manifest["prompt"]["task_title"] == "Bundle Task"
    assert "FastAPI" in (manifest["prompt"]["task_description"] or "")

    # runtime_identity sections present
    ri = manifest["runtime_identity"]
    assert ri["source"] == "task_started_event"
    assert "build_identity" in ri
    assert "backend_lanes" in ri
    assert "model_names" in ri
    assert ri["run_start_snapshot_available"] is True
    assert ri["config_snapshot"]["source"] == "task_started_event"
    assert ri["config_snapshot"]["values"]["PLANNING_BACKEND"] == "openai_responses_api"
    assert ri["build_identity"]["stale_container_check"] == "ok"

    # workspace
    ws = manifest["workspace"]
    assert ws["event_journal_available"] is True
    assert "session_10_task_20.jsonl" in (ws["event_journal_path"] or "")
    assert "session_10_task_20_state_snapshots.jsonl" in (
        ws["state_snapshot_path"] or ""
    )
    assert ws["checkpoint_refs"] == ["pre_repair_step_2"]
    assert ws["change_set"]["available"] is True
    assert ws["change_set"]["changed_count"] == 3

    assert manifest["prompt"]["planning_prompt_ref"]["sha256"] == "prompt-sha"

    # verification: contract verdicts extracted from timeline
    verdicts = manifest["verification"]["contract_verdicts"]
    assert len(verdicts) == 1
    assert verdicts[0]["stage"] == "completion_validation"
    assert verdicts[0]["status"] == "failed"
    commands = manifest["verification"]["commands"]
    assert commands[0]["command"] == "pytest tests/"
    assert commands[0]["channel"] == "completion_validation"
    assert manifest["verification"]["results"][0]["status"] == "failed"

    # repair counts
    counts = manifest["repair"]["repair_event_counts"]
    assert counts.get("repair_generated") == 1
    assert counts.get("repair_rejected") == 1
    attempt_types = [item["event_type"] for item in manifest["repair"]["attempts"]]
    assert "repair_generated" in attempt_types
    assert "repair_rejected" in attempt_types

    # terminal
    terminal = manifest["terminal"]
    assert terminal["status"] == "failed"
    assert terminal["failure_category"] == "completion_validation_failed"
    assert terminal["terminal_reason"] == "completion_validation_failed"
    assert manifest["scorer"]["available"] is False

    # integrity present (may be partial when workspace exists)
    assert "integrity" in manifest
    assert "confidence" in manifest["integrity"]


def test_run_replay_bundle_text_human_readable(tmp_path):
    """run_replay_bundle.txt is a human-readable summary with key sections."""
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

    text_path = bundle_dir / "run_replay_bundle.txt"
    assert text_path.exists()
    text = text_path.read_text(encoding="utf-8")

    assert "RunReplayBundle v1" in text
    assert "Session 10 / Task 20 / Execution 30" in text
    assert "Runtime Identity" in text
    assert "Failure Summary" in text
    assert "Repair Events" in text
    assert "Scorer" in text
    assert "Replay Integrity" in text
    assert "Bundle Files" in text
    assert "run_replay_bundle.json" in text


def test_run_replay_bundle_manifest_reads_eval_scorer_report(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    event_dir = workspace / ".agent" / "events"
    event_dir.mkdir(parents=True)
    (event_dir / "session_10_task_20.jsonl").write_text(
        json.dumps(
            {
                "event_type": "validation_result",
                "timestamp": "2026-06-02T10:02:00Z",
                "session_id": 10,
                "task_id": 20,
                "details": {
                    "stage": "scorer",
                    "status": "passed",
                    "verification_command": "python -m pytest -q",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    reports_dir = tmp_path / "evals"
    reports_dir.mkdir()
    scorer_report = reports_dir / "score.json"
    scorer_report.write_text(
        json.dumps(
            {
                "input": {
                    "project_dir": str(workspace),
                    "session_id": 10,
                    "task_id": 20,
                },
                "result": {
                    "clean_success": True,
                    "verifier_passed": True,
                },
                "verifier": {
                    "available": True,
                    "command": "python -m pytest -q",
                    "passed": True,
                },
                "path_observability": {
                    "execution_reached": True,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUN_REPLAY_SCORER_REPORT_DIR", str(reports_dir))

    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
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

    manifest = _load(bundle_dir, "run_replay_bundle.json")

    assert manifest["scorer"]["available"] is True
    assert manifest["scorer"]["source"] == "eval_report"
    assert manifest["scorer"]["path"] == str(scorer_report)
    assert manifest["scorer"]["result"]["clean_success"] is True
    assert manifest["verification"]["scorer_result_ref"] == str(scorer_report)


def test_capture_task_evidence_bundle_runtime_identity_with_migrations(tmp_path):
    db_path = tmp_path / "bundle.db"
    conn = sqlite3.connect(db_path)
    _schema(conn)
    conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES ('001')")
    conn.execute("INSERT INTO schema_migrations VALUES ('002')")
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
    ri = metadata["runtime_identity"]
    assert ri["database"]["migration_version"] == "002"
    assert ri["database"]["migration_count"] == 2
    assert ri["database"]["migration_status"] in {"ok", "pending"}
