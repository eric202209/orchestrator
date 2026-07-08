from __future__ import annotations

import csv
import sqlite3

from scripts.evals.dogfood_metrics_report import (
    compute_config_drift,
    compute_failure_table_metrics,
    compute_fallback_metrics,
    compute_fix_location,
    compute_intervention_metrics,
    compute_knowledge_metrics,
    compute_logging_completeness,
    compute_project_record_metrics,
    compute_recovery_metrics,
    compute_rule_hit_rates,
    compute_time_to_outcome,
    confidence_tier,
    parse_frozen_config,
)


def test_confidence_tier_boundaries():
    assert confidence_tier(0) == "T0-exploratory"
    assert confidence_tier(29) == "T0-exploratory"
    assert confidence_tier(30) == "T1-preliminary"
    assert confidence_tier(69) == "T1-preliminary"
    assert confidence_tier(70) == "T2-moderate"
    assert confidence_tier(99) == "T2-moderate"
    assert confidence_tier(100) == "T3-decision-grade"
    assert confidence_tier(500) == "T3-decision-grade"


def _memory_conn(script: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(script)
    return conn


def test_compute_knowledge_metrics_zero_state():
    conn = _memory_conn(
        """
        create table sessions (id integer primary key, deleted_at text);
        create table knowledge_usage_logs (
            session_id integer, was_effective integer
        );
        """
    )
    metrics = compute_knowledge_metrics(conn)
    assert metrics["retrieval_events"] == 0
    assert metrics["retrieval_rate"] == 0.0
    assert metrics["confidence_tier"] == "T0-exploratory"


def test_compute_knowledge_metrics_with_rows():
    conn = _memory_conn(
        """
        create table sessions (id integer primary key, deleted_at text);
        create table knowledge_usage_logs (
            session_id integer, was_effective integer
        );
        insert into sessions (id, deleted_at) values (1, null), (2, null);
        insert into knowledge_usage_logs (session_id, was_effective) values
            (1, 1), (1, 0), (2, null);
        """
    )
    metrics = compute_knowledge_metrics(conn)
    assert metrics["retrieval_events"] == 3
    assert metrics["sessions_with_retrieval"] == 2
    assert metrics["retrieval_rate"] == 1.0
    assert metrics["effectiveness_distribution"] == {
        "effective": 1,
        "not_effective": 1,
        "unjudged": 1,
    }


def test_compute_intervention_metrics_reuses_prompt_and_reply():
    conn = _memory_conn(
        """
        create table sessions (id integer primary key, deleted_at text);
        create table intervention_requests (
            session_id integer, intervention_type text, prompt text,
            operator_reply text, status text
        );
        insert into sessions (id, deleted_at) values (1, null);
        insert into intervention_requests
            (session_id, intervention_type, prompt, operator_reply, status)
        values
            (1, 'guidance', 'halted after retries', 'continue with X', 'replied');
        """
    )
    metrics = compute_intervention_metrics(conn)
    assert metrics["interventions_total"] == 1
    assert metrics["sessions_with_intervention"] == 1
    assert metrics["by_type"] == {"guidance": 1}
    assert metrics["replied_count"] == 1
    assert "already captured automatically" in metrics["note"]


def test_compute_rule_hit_rates_no_projects_is_not_an_error(tmp_path):
    conn = _memory_conn(
        """
        create table projects (id integer primary key, workspace_path text, deleted_at text);
        """
    )
    result = compute_rule_hit_rates(conn, workspace_root=tmp_path)
    assert result["sessions_with_validation_events"] == 0
    assert result["rule_hit_rates"] == []
    assert result["zero_fire_note"] is not None


def test_compute_rule_hit_rates_reads_real_journal(tmp_path):
    workspace = tmp_path / "proj1"
    events_dir = workspace / ".agent" / "events"
    events_dir.mkdir(parents=True)
    journal = events_dir / "session_7_task_3.jsonl"
    journal.write_text(
        '{"event_type": "plan_candidate_validated", '
        '"details": {"validator_rule_ids": ["rule_a", "rule_b"]}}\n'
        '{"event_type": "plan_candidate_validated", '
        '"details": {"validator_rule_ids": ["rule_a"]}}\n'
    )
    conn = _memory_conn(
        """
        create table projects (id integer primary key, workspace_path text, deleted_at text);
        """
    )
    conn.execute(
        "insert into projects (id, workspace_path, deleted_at) values (1, ?, null)",
        (str(workspace),),
    )

    result = compute_rule_hit_rates(conn, workspace_root=tmp_path)

    assert result["sessions_with_validation_events"] == 1
    fires = {row["rule_id"]: row["fires"] for row in result["rule_hit_rates"]}
    assert fires == {"rule_a": 2, "rule_b": 1}


def _write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_compute_fallback_metrics_absent_file_is_zero_not_error(tmp_path):
    result = compute_fallback_metrics(tmp_path / "missing.csv")
    assert result["fallback_count"] == 0
    assert result["note"] == "No fallback log provided/found."


def test_compute_fallback_metrics_reason_distribution(tmp_path):
    path = tmp_path / "fallback_log.csv"
    _write_csv(
        path,
        ["date", "task", "reason", "severity"],
        [
            {
                "date": "2026-07-08",
                "task": "t1",
                "reason": "interactive-shape",
                "severity": "",
            },
            {
                "date": "2026-07-08",
                "task": "t2",
                "reason": "interactive-shape",
                "severity": "",
            },
            {"date": "2026-07-08", "task": "t3", "reason": "timeout", "severity": "S3"},
        ],
    )
    result = compute_fallback_metrics(path)
    assert result["fallback_count"] == 3
    assert result["reason_distribution"] == {"interactive-shape": 2, "timeout": 1}


def test_compute_project_record_metrics_completion_rate_excludes_withdrawn(tmp_path):
    path = tmp_path / "project_records.csv"
    _write_csv(
        path,
        [
            "intake_date",
            "project_category",
            "intake_difficulty",
            "baseline_alternative",
            "baseline_expected_effort",
            "workspace_git_remote_verified",
            "session_ids",
            "outcome_class",
            "actual_difficulty",
            "observed_effort",
            "faster_similar_slower",
            "knowledge_usefulness",
            "failure_row_refs",
            "close_date",
        ],
        [
            {"outcome_class": "PLATFORM_COMPLETE", "faster_similar_slower": "faster"},
            {"outcome_class": "PLATFORM_COMPLETE", "faster_similar_slower": "similar"},
            {"outcome_class": "FALLBACK", "faster_similar_slower": ""},
            {"outcome_class": "WITHDRAWN", "faster_similar_slower": ""},
        ],
    )
    result = compute_project_record_metrics(path)
    assert result["projects_total"] == 4
    # 2 complete / 3 eligible (WITHDRAWN excluded)
    assert result["completion_rate"] == round(2 / 3, 4)
    assert result["baseline_comparison_distribution"] == {"faster": 1, "similar": 1}


def test_compute_failure_table_metrics_classification_rate(tmp_path):
    path = tmp_path / "failure_table.csv"
    _write_csv(
        path,
        [
            "date",
            "session_id",
            "task_execution_id",
            "stage",
            "symptom",
            "severity",
            "classifier_label",
            "repro_pointer",
            "disposition",
        ],
        [
            {"severity": "S2", "classifier_label": "planning_timeout"},
            {"severity": "S3", "classifier_label": "UNCLASSIFIED"},
        ],
    )
    result = compute_failure_table_metrics(path)
    assert result["failures_total"] == 2
    assert result["auto_classification_rate"] == 0.5
    assert result["by_severity"] == {"S2": 1, "S3": 1}


def test_compute_fix_location_no_since_returns_note():
    result = compute_fix_location(since=None)
    assert result["monolith_touch_counts"] == {}
    assert "No --since date" in result["note"]


def test_compute_fix_location_with_since_queries_git_log():
    # Use a since date far in the future relative to repo history so the
    # result is deterministic (zero touches) without depending on
    # when this test happens to run.
    result = compute_fix_location(since="2099-01-01")
    assert result["since"] == "2099-01-01"
    assert result["monolith_touch_counts"] == {}


# --- Phase 21F: M7 recovery-metrics integration -----------------------------


def test_compute_recovery_metrics_no_projects_is_not_an_error(tmp_path):
    conn = _memory_conn(
        """
        create table projects (id integer primary key, workspace_path text, deleted_at text);
        """
    )
    result = compute_recovery_metrics(conn, workspace_root=tmp_path)
    assert result["sessions_with_recovery_events"] == 0
    assert result["recovery_attempted_count"] == 0
    assert result["zero_fire_note"] is not None


def test_compute_recovery_metrics_reads_real_journal(tmp_path):
    workspace = tmp_path / "proj1"
    events_dir = workspace / ".agent" / "events"
    events_dir.mkdir(parents=True)
    journal = events_dir / "session_7_task_3.jsonl"
    journal.write_text(
        '{"event_type": "execution_recovery_attempted", '
        '"details": {"scope": "step", "failure_class": "timeout"}}\n'
        '{"event_type": "execution_recovery_succeeded", "details": {}}\n'
        '{"event_type": "plan_candidate_validated", '
        '"details": {"validator_rule_ids": ["rule_a"]}}\n'
    )
    conn = _memory_conn(
        """
        create table projects (id integer primary key, workspace_path text, deleted_at text);
        """
    )
    conn.execute(
        "insert into projects (id, workspace_path, deleted_at) values (1, ?, null)",
        (str(workspace),),
    )

    result = compute_recovery_metrics(conn, workspace_root=tmp_path)

    assert result["sessions_with_recovery_events"] == 1
    assert result["recovery_attempted_count"] == 1
    assert result["recovery_succeeded_count"] == 1
    assert result["recovery_by_scope"] == {"step": 1}
    assert result["recovery_by_failure_class"] == {"timeout": 1}
    assert result["recovered_success_rate"] == 1.0
    assert result["zero_fire_note"] is None


# --- Phase 21F: M12 wall-clock duration -------------------------------------


def test_compute_time_to_outcome_zero_state():
    conn = _memory_conn(
        """
        create table sessions (
            id integer primary key, started_at text, stopped_at text,
            deleted_at text
        );
        """
    )
    result = compute_time_to_outcome(conn, limit=100)
    assert result["sessions_with_duration"] == 0
    assert result["mean_duration_seconds"] is None
    assert result["median_duration_seconds"] is None
    assert result["confidence_tier"] == "T0-exploratory"


def test_compute_time_to_outcome_computes_duration():
    conn = _memory_conn(
        """
        create table sessions (
            id integer primary key, started_at text, stopped_at text,
            deleted_at text
        );
        insert into sessions (id, started_at, stopped_at, deleted_at) values
            (1, '2026-07-08 10:00:00', '2026-07-08 10:01:40', null),
            (2, '2026-07-08 11:00:00', '2026-07-08 11:05:00', null),
            (3, '2026-07-08 12:00:00', null, null);
        """
    )
    result = compute_time_to_outcome(conn, limit=100)
    assert result["sessions_considered"] == 3
    assert result["sessions_with_duration"] == 2
    # durations: 100s and 300s -> mean 200.0, median 200.0
    assert result["mean_duration_seconds"] == 200.0
    assert result["median_duration_seconds"] == 200.0


# --- Phase 21F: M17 S2 diagnosis-latency column -----------------------------


def test_compute_failure_table_metrics_diagnosis_latency(tmp_path):
    path = tmp_path / "failure_table.csv"
    _write_csv(
        path,
        [
            "date",
            "session_id",
            "task_execution_id",
            "stage",
            "symptom",
            "severity",
            "classifier_label",
            "repro_pointer",
            "disposition",
            "diagnosis_minutes",
        ],
        [
            {
                "severity": "S2",
                "classifier_label": "planning_timeout",
                "diagnosis_minutes": "10",
            },
            {
                "severity": "S2",
                "classifier_label": "planning_timeout",
                "diagnosis_minutes": "20",
            },
            {
                "severity": "S2",
                "classifier_label": "UNCLASSIFIED",
                "diagnosis_minutes": "",
            },
            {
                "severity": "S3",
                "classifier_label": "wrong_tool",
                "diagnosis_minutes": "5",
            },
        ],
    )
    result = compute_failure_table_metrics(path)
    assert result["s2_count"] == 3
    assert result["s2_rows_with_latency_recorded"] == 2
    assert result["s2_diagnosis_latency_minutes_avg"] == 15.0


def test_compute_failure_table_metrics_diagnosis_latency_absent_column(tmp_path):
    # Older-style file without the diagnosis_minutes column must still work.
    path = tmp_path / "failure_table.csv"
    _write_csv(
        path,
        [
            "date",
            "session_id",
            "task_execution_id",
            "stage",
            "symptom",
            "severity",
            "classifier_label",
            "repro_pointer",
            "disposition",
        ],
        [{"severity": "S2", "classifier_label": "planning_timeout"}],
    )
    result = compute_failure_table_metrics(path)
    assert result["s2_count"] == 1
    assert result["s2_rows_with_latency_recorded"] == 0
    assert result["s2_diagnosis_latency_minutes_avg"] is None


# --- Phase 21H: R5 config-freeze assertion -----------------------------


def test_parse_frozen_config_ignores_comments_and_prose(tmp_path):
    path = tmp_path / "dogfood.env.example"
    path.write_text(
        "# a comment\n"
        "\n"
        "HUMAN_GUIDANCE_TABLE_ENABLED=True\n"
        "CANDIDATE_RECOVERY_ENABLED=False\n"
        "# Rationale for keeping as-is over turning off: ...\n"
    )
    flags = parse_frozen_config(path)
    assert flags == {
        "HUMAN_GUIDANCE_TABLE_ENABLED": True,
        "CANDIDATE_RECOVERY_ENABLED": False,
    }


def test_compute_config_drift_no_file_is_not_an_error(tmp_path):
    result = compute_config_drift(tmp_path / "missing.env.example")
    assert result["drift_detected"] is False
    assert result["drift"] == {}
    assert result["note"] is not None


def test_compute_config_drift_matches_current_settings(tmp_path):
    path = tmp_path / "dogfood.env.example"
    path.write_text("CANDIDATE_RECOVERY_ENABLED=False\n")
    result = compute_config_drift(path)
    assert result["flags_checked"] == 1
    assert result["drift_detected"] is False
    assert result["drift"] == {}


def test_compute_config_drift_detects_flip(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CANDIDATE_RECOVERY_ENABLED", True)
    path = tmp_path / "dogfood.env.example"
    path.write_text("CANDIDATE_RECOVERY_ENABLED=False\n")
    result = compute_config_drift(path)
    assert result["drift_detected"] is True
    assert result["drift"] == {
        "CANDIDATE_RECOVERY_ENABLED": {"frozen": False, "current": True}
    }
    assert result["note"] is not None


# --- Phase 21H: R3 manual-logging completeness -------------------------


def test_compute_logging_completeness_zero_state():
    conn = _memory_conn(
        """
        create table sessions (
            id integer primary key, started_at text, deleted_at text
        );
        """
    )
    result = compute_logging_completeness(conn, project_records=None, fallback_log=None)
    assert result["sessions_total"] == 0
    assert result["project_records_total"] == 0
    assert result["records_to_sessions_ratio"] is None
    assert result["days_with_sessions_and_zero_fallback_entries"] == []


def test_compute_logging_completeness_flags_undercount_and_missing_fallback_days(
    tmp_path,
):
    conn = _memory_conn(
        """
        create table sessions (
            id integer primary key, started_at text, deleted_at text
        );
        insert into sessions (id, started_at, deleted_at) values
            (1, '2026-07-08 10:00:00', null),
            (2, '2026-07-09 10:00:00', null);
        """
    )
    project_records = tmp_path / "project_records.csv"
    _write_csv(
        project_records,
        ["project_id", "date"],
        [{"project_id": "p1", "date": "2026-07-08"}],
    )
    fallback_log = tmp_path / "fallback_log.csv"
    _write_csv(
        fallback_log,
        ["date", "task", "reason"],
        [{"date": "2026-07-08", "task": "t1", "reason": "interactive-shape"}],
    )

    result = compute_logging_completeness(
        conn, project_records=project_records, fallback_log=fallback_log
    )
    assert result["sessions_total"] == 2
    assert result["project_records_total"] == 1
    assert result["records_to_sessions_ratio"] == 0.5
    assert result["days_with_sessions_and_zero_fallback_entries"] == ["2026-07-09"]
