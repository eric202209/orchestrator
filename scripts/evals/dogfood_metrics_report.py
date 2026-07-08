#!/usr/bin/env python3
"""Phase 21B (B4): Dogfood metrics report.

Emits the Phase 22 dogfood metrics (M1-M19) from existing telemetry —
the orchestrator's SQLite database (read-only) and, for validator rule
hit-rates, the per-project `.agent/events` journals. It reuses the
existing report modules under `scripts/session_and_replay/` rather than
recomputing outcome/terminal classification logic, per the "verify-first,
add-only-if-missing" principle: this script adds no new production
instrumentation.

Two verified telemetry facts from the Phase 21B enablement pass (recorded
here so this script's assumptions are traceable):

- `intervention_requests.prompt` already captures what triggered an
  intervention, and `operator_reply` already captures the action taken —
  no schema change was needed for the automatic half of M11/M17's
  "trigger seen / action taken" data. Only the human sufficiency judgment
  ("did platform surfaces alone suffice?") is manual, and it is not a
  database field — it is recorded in the fallback/failure manual logs.
- `knowledge_usage_logs.was_effective` already provides a queryable
  effectiveness signal for M9/M10 — no schema change was needed there
  either.

Metrics that depend on manual logs (fallback log, project records,
failure table — Phase 21A design §8.2) are computed from those files when
given via CLI flags, and reported as zero/empty with an explicit note
when the files are absent or empty. Absent flags never causes drift: a
metric that cannot be produced from provided inputs prints as "0 (no
manual log provided)", not as a crash.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.session_and_replay.failure_taxonomy import (  # noqa: E402
    parse_log_metadata,
    status_key,
)
from scripts.session_and_replay.session_outcome_report import (  # noqa: E402
    build_report as build_session_outcome_report,
)

MONOLITH_FILES = (
    "app/services/orchestration/phases/execution_loop.py",
    "app/tasks/worker.py",
    "app/services/orchestration/phases/planning_flow.py",
)

PRODUCT_METRICS = {"M1", "M2", "M3", "M11", "M12", "M13", "M17", "M18", "M19"}
INTERNAL_METRICS = {"M4", "M5", "M6", "M7", "M8", "M9", "M10", "M14", "M15", "M16"}


def confidence_tier(n: int) -> str:
    if n < 30:
        return "T0-exploratory"
    if n < 70:
        return "T1-preliminary"
    if n < 100:
        return "T2-moderate"
    return "T3-decision-grade"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(
    conn: sqlite3.Connection, query: str, params: tuple = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


# --- M1: completion / outcome rates (reuses session_outcome_report) --------


def compute_outcome_rates(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    report = build_session_outcome_report(conn, limit=limit, check_timeline=False)
    summary = report["summary"]
    n = report["sessions_analyzed"]
    return {
        "sessions_analyzed": n,
        "confidence_tier": confidence_tier(n),
        "outcome_rates": summary.get("outcome_rates", {}),
        "outcome_counts": summary.get("outcome_counts", {}),
        "avg_repair_count": summary.get("avg_repair_count", 0.0),
    }


# --- M6: per-rule validator hit-rate table ---------------------------------


def compute_rule_hit_rates(
    conn: sqlite3.Connection, *, workspace_root: Path
) -> dict[str, Any]:
    """Best-effort: walks registered project workspaces' `.agent/events`
    journals for `plan_candidate_validated` events, exactly as
    `phase18e_collect_real_session_validator_evidence.py` does. Returns
    zero rows (not an error) if no projects/journals exist yet."""
    projects = _rows(
        conn,
        "select id, workspace_path from projects where deleted_at is null "
        "and workspace_path is not null and workspace_path != ''",
    )
    rule_fires: Counter[str] = Counter()
    rule_sessions: dict[str, set[int]] = {}
    sessions_with_validation = set()

    for project in projects:
        workspace = Path(str(project["workspace_path"]))
        if not workspace.is_absolute():
            workspace = workspace_root / workspace
        events_dir = workspace / ".agent" / "events"
        if not events_dir.is_dir():
            continue
        for path in sorted(events_dir.glob("session_*_task_*.jsonl")):
            match = re.match(r"session_(\d+)_task_\d+\.jsonl$", path.name)
            session_id = int(match.group(1)) if match else None
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event_type") != "plan_candidate_validated":
                    continue
                if session_id is not None:
                    sessions_with_validation.add(session_id)
                details = event.get("details") or {}
                rule_ids = (
                    details.get("validator_rule_ids") or details.get("rule_ids") or []
                )
                if isinstance(rule_ids, str):
                    rule_ids = [rule_ids]
                for rule_id in rule_ids:
                    rule_fires[str(rule_id)] += 1
                    if session_id is not None:
                        rule_sessions.setdefault(str(rule_id), set()).add(session_id)

    n = len(sessions_with_validation)
    return {
        "sessions_with_validation_events": n,
        "confidence_tier": confidence_tier(n),
        "rule_hit_rates": [
            {
                "rule_id": rule_id,
                "fires": count,
                "distinct_sessions": len(rule_sessions.get(rule_id, set())),
            }
            for rule_id, count in sorted(rule_fires.items(), key=lambda kv: -kv[1])
        ],
        "zero_fire_note": (
            "No projects/journals registered yet — zero rows expected, not an error."
            if not projects
            else None
        ),
    }


# --- M9/M10: knowledge retrieval + usefulness ------------------------------


def compute_knowledge_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    total_sessions = _rows(
        conn, "select count(*) as n from sessions where deleted_at is null"
    )
    total = int(total_sessions[0]["n"]) if total_sessions else 0

    usage_rows = _rows(
        conn,
        "select session_id, was_effective from knowledge_usage_logs",
    )
    sessions_with_retrieval = {row["session_id"] for row in usage_rows}
    # sqlite3 (unlike SQLAlchemy's Boolean type) returns raw ints for this
    # column, not Python bools, so compare against None first rather than
    # using `is True`/`is False` identity checks.
    effectiveness = Counter(
        (
            "unjudged"
            if row["was_effective"] is None
            else "effective" if row["was_effective"] else "not_effective"
        )
        for row in usage_rows
    )
    return {
        "total_sessions": total,
        "retrieval_events": len(usage_rows),
        "sessions_with_retrieval": len(sessions_with_retrieval),
        "retrieval_rate": (
            round(len(sessions_with_retrieval) / total, 4) if total else 0.0
        ),
        "effectiveness_distribution": dict(effectiveness),
        "confidence_tier": confidence_tier(len(usage_rows)),
    }


# --- M11: human intervention frequency -------------------------------------


def compute_intervention_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    total_sessions = _rows(
        conn, "select count(*) as n from sessions where deleted_at is null"
    )
    total = int(total_sessions[0]["n"]) if total_sessions else 0

    interventions = _rows(
        conn,
        "select session_id, intervention_type, prompt, operator_reply, status "
        "from intervention_requests",
    )
    sessions_with_intervention = {row["session_id"] for row in interventions}
    by_type = Counter(row["intervention_type"] for row in interventions)
    replied = sum(1 for row in interventions if row["operator_reply"])
    return {
        "total_sessions": total,
        "interventions_total": len(interventions),
        "sessions_with_intervention": len(sessions_with_intervention),
        "intervention_frequency": (
            round(len(sessions_with_intervention) / total, 4) if total else 0.0
        ),
        "by_type": dict(by_type),
        "replied_count": replied,
        "note": (
            "trigger (prompt) and action (operator_reply) are already captured "
            "automatically; the surfaces-sufficed judgment (yes/no) is manual "
            "and recorded in the fallback/failure logs, not the database."
        ),
        "confidence_tier": confidence_tier(len(interventions)),
    }


# --- M18: resume success (best-effort from checkpoint table) --------------


def compute_resume_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    checkpoints = _rows(conn, "select count(*) as n from task_checkpoints")
    n = int(checkpoints[0]["n"]) if checkpoints else 0
    return {
        "checkpoints_recorded": n,
        "note": (
            "Resume success/failure is not separately flagged in task_checkpoints; "
            "M18 numerator/denominator must be tallied manually against the "
            "project records log until a resume-outcome field exists. Checkpoint "
            "count is reported as a coverage proxy only."
        ),
    }


# --- M14: fix-location data (git log over the dogfood window) -------------


def compute_fix_location(*, since: str | None) -> dict[str, Any]:
    if not since:
        return {
            "since": None,
            "note": "No --since date provided; run with --since <dogfood-start-date> "
            "during Phase 22 to populate this metric.",
            "monolith_touch_counts": {},
        }
    try:
        output = subprocess.run(
            [
                "git",
                "log",
                f"--since={since}",
                "--name-only",
                "--pretty=format:--commit--",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return {"since": since, "error": str(exc), "monolith_touch_counts": {}}

    counts: Counter[str] = Counter()
    for line in output.splitlines():
        if line in ("--commit--", ""):
            continue
        for monolith in MONOLITH_FILES:
            if line == monolith:
                counts[monolith] += 1
    return {"since": since, "monolith_touch_counts": dict(counts)}


# --- Manual-log-derived metrics (M2, M3, M12, M17, M19) --------------------


def _read_csv_rows(path: Path | None) -> list[dict[str, str]]:
    if not path or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def compute_fallback_metrics(fallback_log: Path | None) -> dict[str, Any]:
    rows = _read_csv_rows(fallback_log)
    reasons = Counter(
        row.get("reason", "").strip() for row in rows if row.get("reason")
    )
    return {
        "fallback_count": len(rows),
        "reason_distribution": dict(reasons.most_common()),
        "note": (
            None
            if fallback_log and fallback_log.exists()
            else "No fallback log provided/found."
        ),
    }


def compute_project_record_metrics(project_records: Path | None) -> dict[str, Any]:
    rows = _read_csv_rows(project_records)
    outcome_counts = Counter(
        row.get("outcome_class", "").strip() for row in rows if row.get("outcome_class")
    )
    faster_similar_slower = Counter(
        row.get("faster_similar_slower", "").strip()
        for row in rows
        if row.get("faster_similar_slower")
    )
    eligible = sum(
        1 for row in rows if row.get("outcome_class", "").strip() != "WITHDRAWN"
    )
    completed = outcome_counts.get("PLATFORM_COMPLETE", 0)
    return {
        "projects_total": len(rows),
        "outcome_counts": dict(outcome_counts),
        "completion_rate": round(completed / eligible, 4) if eligible else 0.0,
        "baseline_comparison_distribution": dict(faster_similar_slower),
        "confidence_tier": confidence_tier(len(rows)),
        "note": (
            None
            if project_records and project_records.exists()
            else "No project records log provided/found."
        ),
    }


def compute_failure_table_metrics(failure_table: Path | None) -> dict[str, Any]:
    rows = _read_csv_rows(failure_table)
    by_severity = Counter(
        row.get("severity", "").strip() for row in rows if row.get("severity")
    )
    classified = sum(
        1
        for row in rows
        if row.get("classifier_label", "").strip().upper() != "UNCLASSIFIED"
    )
    return {
        "failures_total": len(rows),
        "by_severity": dict(by_severity),
        "auto_classification_rate": round(classified / len(rows), 4) if rows else 0.0,
        "note": (
            None
            if failure_table and failure_table.exists()
            else "No failure table provided/found."
        ),
    }


@dataclass
class DogfoodReport:
    product: dict[str, Any] = field(default_factory=dict)
    internal: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"product_kpis": self.product, "internal_kpis": self.internal}


def build_dogfood_report(
    *,
    db_path: Path,
    workspace_root: Path,
    limit: int,
    fallback_log: Path | None,
    project_records: Path | None,
    failure_table: Path | None,
    since: str | None,
) -> DogfoodReport:
    with _connect_ro(db_path) as conn:
        outcome = compute_outcome_rates(conn, limit=limit)
        rules = compute_rule_hit_rates(conn, workspace_root=workspace_root)
        knowledge = compute_knowledge_metrics(conn)
        interventions = compute_intervention_metrics(conn)
        resumes = compute_resume_metrics(conn)

    fallback = compute_fallback_metrics(fallback_log)
    project_record_metrics = compute_project_record_metrics(project_records)
    failure_metrics = compute_failure_table_metrics(failure_table)
    fix_location = compute_fix_location(since=since)

    report = DogfoodReport()
    report.product = {
        "M1_completion_rate": project_record_metrics["completion_rate"],
        "M1_note": "Derived from project records (PLATFORM_COMPLETE / eligible). "
        "Session-level outcome classes (first_pass/recovered/etc.) are in internal M7 "
        "for engineering triage.",
        "M2_fallback_rate": (
            round(
                fallback["fallback_count"] / project_record_metrics["projects_total"], 4
            )
            if project_record_metrics["projects_total"]
            else 0.0
        ),
        "M3_fallback_reason_distribution": fallback["reason_distribution"],
        "M11_intervention_frequency": interventions["intervention_frequency"],
        "M11_by_type": interventions["by_type"],
        "M12_baseline_comparison_distribution": project_record_metrics[
            "baseline_comparison_distribution"
        ],
        "M13_developer_waiting_time": "Not computed: requires approval-gate/intervention "
        "timestamp deltas; best-effort candidate for a future 21B patch if dogfood "
        "shows it's needed. Not blocking Phase 22 start (zeros/absent is acceptable).",
        "M17_s2_diagnosis_latency": "Derived from failure_table.csv 'severity'=S2 rows "
        "once populated; 0 rows currently.",
        "M18_resume": resumes,
        "M19_baseline_relative_effort": project_record_metrics[
            "baseline_comparison_distribution"
        ],
        "confidence_tiers": {
            "project_records": project_record_metrics["confidence_tier"],
            "interventions": interventions["confidence_tier"],
        },
        "notes": [n for n in (fallback["note"], project_record_metrics["note"]) if n],
    }
    report.internal = {
        "M4_planning_success": outcome["outcome_rates"],
        "M5_M6_validator": rules,
        "M7_repair_recovery_utilization": {
            "avg_repair_count": outcome["avg_repair_count"],
            "outcome_counts": outcome["outcome_counts"],
        },
        "M8_loop_wedge_frequency": {
            "stuck_or_manual_db_cleanup": outcome["outcome_counts"].get(
                "stuck_or_manual_db_cleanup", 0
            ),
        },
        "M9_M10_knowledge": knowledge,
        "M14_fix_location": fix_location,
        "M15_token_usage": "Not computed: requires per-session token accounting from "
        "planning telemetry; verify field availability during week 1 of Phase 22.",
        "M16_auto_classification_rate": failure_metrics["auto_classification_rate"],
        "M16_failures_total": failure_metrics["failures_total"],
        "M16_by_severity": failure_metrics["by_severity"],
        "confidence_tiers": {
            "sessions": outcome["confidence_tier"],
            "rules": rules["confidence_tier"],
            "knowledge": knowledge["confidence_tier"],
        },
        "notes": [n for n in (failure_metrics["note"],) if n],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit the Phase 22 dogfood metrics report (M1-M19)."
    )
    parser.add_argument("--db", default="orchestrator.db", type=Path)
    parser.add_argument(
        "--workspace-root",
        default=str(REPO_ROOT.parent),
        help="Root for resolving relative project workspace paths.",
    )
    parser.add_argument(
        "--limit", type=int, default=1000, help="Max sessions to analyze."
    )
    parser.add_argument("--fallback-log", type=Path, default=None)
    parser.add_argument("--project-records", type=Path, default=None)
    parser.add_argument("--failure-table", type=Path, default=None)
    parser.add_argument(
        "--since",
        default=None,
        help="Date (git log --since format) marking the dogfood window start, "
        "for M14 fix-location tallying.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_dogfood_report(
        db_path=args.db,
        workspace_root=Path(args.workspace_root),
        limit=args.limit,
        fallback_log=args.fallback_log,
        project_records=args.project_records,
        failure_table=args.failure_table,
        since=args.since,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print("=== Product KPIs (is this a good product?) ===")
        print(json.dumps(report.product, indent=2, default=str))
        print()
        print("=== Internal KPIs (what should engineers improve?) ===")
        print(json.dumps(report.internal, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
