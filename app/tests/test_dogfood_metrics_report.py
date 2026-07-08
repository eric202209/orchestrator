from __future__ import annotations

import csv
import sqlite3

from scripts.evals.dogfood_metrics_report import (
    compute_failure_table_metrics,
    compute_fallback_metrics,
    compute_fix_location,
    compute_intervention_metrics,
    compute_knowledge_metrics,
    compute_project_record_metrics,
    compute_rule_hit_rates,
    confidence_tier,
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
