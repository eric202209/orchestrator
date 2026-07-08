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
from datetime import datetime
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
from app.services.orchestration.events.event_types import EventType  # noqa: E402
from app.services.orchestration.recovery.recovery_metrics import (  # noqa: E402
    _tally as tally_recovery_events,
)

# Phase 21F: the four recovery-lifecycle event types the existing
# `recovery_metrics` module already aggregates from event journals
# elsewhere in the codebase (ops recovery-inspection report). Phase 21E
# found this script never read them; walking the same per-project
# `.agent/events` journals `compute_rule_hit_rates` already reads and
# handing the matching events to the existing `_tally` aggregator closes
# that gap without adding any new telemetry.
RECOVERY_EVENT_TYPES = {
    EventType.EXECUTION_RECOVERY_ATTEMPTED,
    EventType.EXECUTION_RECOVERY_SUCCEEDED,
    EventType.EXECUTION_RECOVERY_FAILED,
    EventType.EXECUTION_RECOVERY_SKIPPED,
}

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


# --- M7 (recovery half): recovery-strategy invocation + outcome -----------


def compute_recovery_metrics(
    conn: sqlite3.Connection, *, workspace_root: Path
) -> dict[str, Any]:
    """Phase 21F fix for the Phase 21E M7 gap: walks the same registered-
    project `.agent/events` journals `compute_rule_hit_rates` reads,
    collects `EXECUTION_RECOVERY_*` events, and reuses the existing
    production aggregator (`recovery_metrics._tally`) rather than
    reimplementing its counting logic. Zero recovery events is an
    expected result while `CANDIDATE_RECOVERY_ENABLED` stays off in the
    frozen dogfood config (§8.1) and/or before real sessions exist — not
    an error."""
    projects = _rows(
        conn,
        "select id, workspace_path from projects where deleted_at is null "
        "and workspace_path is not null and workspace_path != ''",
    )
    recovery_events: list[dict[str, Any]] = []
    sessions_with_recovery_events: set[int] = set()

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
                if event.get("event_type") not in RECOVERY_EVENT_TYPES:
                    continue
                recovery_events.append(event)
                if session_id is not None:
                    sessions_with_recovery_events.add(session_id)

    tally = tally_recovery_events(recovery_events)
    n = len(sessions_with_recovery_events)
    return {
        **tally,
        "sessions_with_recovery_events": n,
        "confidence_tier": confidence_tier(n),
        "zero_fire_note": (
            "No projects/journals registered yet, or no recovery events "
            "fired — expected (zero real sessions yet, and/or "
            "CANDIDATE_RECOVERY_ENABLED is off in the frozen config), "
            "not an error."
            if not projects or tally["recovery_attempted_count"] == 0
            else None
        ),
    }


# --- M12 (wall-clock half): session duration from DB timestamps -----------


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def compute_time_to_outcome(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    """Phase 21F fix for the Phase 21E M12 gap: the quantitative
    wall-clock half of M12 (session duration), derived from the same
    `sessions.started_at`/`stopped_at` DB columns
    `session_outcome_report.py` already reads per-session but never
    reduces to a duration. This complements — does not replace — the
    qualitative faster/similar/slower judgment recorded in
    `project_records.csv` (M12/M19)."""
    rows = _rows(
        conn,
        """
        select id, started_at, stopped_at
        from sessions
        where deleted_at is null
        order by id desc
        limit ?
        """,
        (limit,),
    )
    durations: list[float] = []
    for row in rows:
        started = _parse_dt(row.get("started_at"))
        stopped = _parse_dt(row.get("stopped_at"))
        if started is None or stopped is None:
            continue
        delta = (stopped - started).total_seconds()
        if delta >= 0:
            durations.append(delta)

    n = len(durations)
    ordered = sorted(durations)
    if n:
        mean_seconds = round(sum(ordered) / n, 1)
        mid = n // 2
        median_seconds = (
            round(ordered[mid], 1)
            if n % 2 == 1
            else round((ordered[mid - 1] + ordered[mid]) / 2, 1)
        )
    else:
        mean_seconds = None
        median_seconds = None

    return {
        "sessions_considered": len(rows),
        "sessions_with_duration": n,
        "mean_duration_seconds": mean_seconds,
        "median_duration_seconds": median_seconds,
        "confidence_tier": confidence_tier(n),
        "note": (
            "Wall-clock component of M12 only (started_at -> stopped_at). "
            "Read alongside M12_baseline_comparison_distribution for the "
            "full metric (Phase 21A §5.2 pairs the coarse judgment with "
            "the duration)."
        ),
    }


# --- R5: config-freeze assertion (Phase 21A §10 risk mitigation) ----------


def parse_frozen_config(path: Path | None) -> dict[str, bool]:
    """Parses the `KEY=True`/`KEY=False` lines out of the frozen dogfood
    config record (Phase 21B/B3, `dogfood.env.example`). Comments and any
    non-boolean line are ignored — the file also carries prose."""
    if not path or not path.exists():
        return {}
    flags: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value in ("True", "False"):
            flags[key] = value == "True"
    return flags


def compute_config_drift(frozen_config: Path | None) -> dict[str, Any]:
    """Phase 21H fix for the Phase 21A R5 gap: the design's own risk table
    (§10) promises "report script asserts running config against frozen
    record" as R5's verification step; Phase 21G's audit found no such
    check existed. Compares every flag named in the frozen
    `dogfood.env.example` against the live `app.config.settings` value for
    the same name — a mismatch is a mid-window flag flip, which Phase 21A
    §10 R5 says forks the dogfood evidence and must be logged as a
    documented, dated event rather than discovered silently."""
    frozen = parse_frozen_config(frozen_config)
    if not frozen:
        return {
            "frozen_config_path": str(frozen_config) if frozen_config else None,
            "flags_checked": 0,
            "drift": {},
            "drift_detected": False,
            "note": (
                "No frozen config file provided/found — cannot assert against "
                "a baseline. Pass --frozen-config "
                "docs/roadmap/done/phase21/dogfood.env.example during Phase 22."
            ),
        }

    from app.config import settings  # local import: only needed for this check

    drift = {}
    for key, frozen_value in frozen.items():
        current_value = getattr(settings, key, None)
        if current_value is None:
            continue
        if bool(current_value) != frozen_value:
            drift[key] = {"frozen": frozen_value, "current": bool(current_value)}

    return {
        "frozen_config_path": str(frozen_config),
        "flags_checked": len(frozen),
        "drift": drift,
        "drift_detected": bool(drift),
        "note": (
            None
            if not drift
            else "CONFIG DRIFT DETECTED against the Phase 21B frozen record — "
            "one or more flags changed since the freeze. Per Phase 21A §10 R5 "
            "this forks the dogfood evidence; log it as a documented, dated "
            "config-change event before trusting further sessions as "
            "same-configuration corpus."
        ),
    }


# --- R3: manual-logging completeness (Phase 21A §10 risk mitigation) ------


def compute_logging_completeness(
    conn: sqlite3.Connection,
    *,
    project_records: Path | None,
    fallback_log: Path | None,
) -> dict[str, Any]:
    """Phase 21H fix for the Phase 21A R3 gap: the design's own risk table
    (§10) promises the daily/weekly report surfaces "sessions without
    project records" and "days with zero fallback-log entries" as
    anomalies; Phase 21G's audit found neither existed. Cross-checks the
    two manual logs against DB session activity, since under-logging
    otherwise *silently improves* M1/M2 (§21G §4.2) rather than showing up
    as a defect."""
    total_sessions_row = _rows(
        conn, "select count(*) as n from sessions where deleted_at is null"
    )
    total_sessions = int(total_sessions_row[0]["n"]) if total_sessions_row else 0

    project_rows = _read_csv_rows(project_records)
    records_count = len(project_rows)
    records_to_sessions_ratio = (
        round(records_count / total_sessions, 4) if total_sessions else None
    )

    session_day_rows = _rows(
        conn,
        "select distinct date(started_at) as day from sessions "
        "where deleted_at is null and started_at is not null",
    )
    session_days = {row["day"] for row in session_day_rows if row["day"]}

    fallback_rows = _read_csv_rows(fallback_log)
    fallback_days = {
        row.get("date", "").strip()[:10] for row in fallback_rows if row.get("date")
    }

    days_with_sessions_and_zero_fallback = sorted(session_days - fallback_days)

    return {
        "sessions_total": total_sessions,
        "project_records_total": records_count,
        "records_to_sessions_ratio": records_to_sessions_ratio,
        "days_with_sessions": sorted(session_days),
        "days_with_sessions_and_zero_fallback_entries": (
            days_with_sessions_and_zero_fallback
        ),
        "note": (
            "A low records_to_sessions_ratio or a non-empty "
            "days_with_sessions_and_zero_fallback_entries list is the Phase "
            "21A R3 (manual logging fatigue) early-warning signal — check "
            "this field at every weekly triage before treating M1/M2 as "
            "complete; a day with sessions and zero fallback entries is not "
            "proof nothing was skipped, only that nothing was logged."
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

    # Phase 21F fix for the Phase 21E M17 gap: `diagnosis_minutes` is an
    # optional, additive column (blank/absent is fine — csv.DictReader
    # tolerates rows without it and older files that predate it) an
    # operator fills at triage with the same coarse judgment already
    # named in the daily project record template's free-text M17 field
    # ("coarse time from failure to operator-understands-why", Phase 21A
    # §5 M17). It does not replace or reorder the canonical 9-column
    # failure record schema (§6.2).
    s2_latencies: list[float] = []
    for row in rows:
        if row.get("severity", "").strip() != "S2":
            continue
        raw = (row.get("diagnosis_minutes") or "").strip()
        if not raw:
            continue
        try:
            s2_latencies.append(float(raw))
        except ValueError:
            continue
    s2_count = sum(1 for row in rows if row.get("severity", "").strip() == "S2")
    s2_latency_avg = (
        round(sum(s2_latencies) / len(s2_latencies), 1) if s2_latencies else None
    )

    return {
        "failures_total": len(rows),
        "by_severity": dict(by_severity),
        "auto_classification_rate": round(classified / len(rows), 4) if rows else 0.0,
        "s2_count": s2_count,
        "s2_diagnosis_latency_minutes_avg": s2_latency_avg,
        "s2_rows_with_latency_recorded": len(s2_latencies),
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
    integrity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_kpis": self.product,
            "internal_kpis": self.internal,
            "integrity_checks": self.integrity,
        }


def build_dogfood_report(
    *,
    db_path: Path,
    workspace_root: Path,
    limit: int,
    fallback_log: Path | None,
    project_records: Path | None,
    failure_table: Path | None,
    since: str | None,
    frozen_config: Path | None = None,
) -> DogfoodReport:
    with _connect_ro(db_path) as conn:
        outcome = compute_outcome_rates(conn, limit=limit)
        rules = compute_rule_hit_rates(conn, workspace_root=workspace_root)
        knowledge = compute_knowledge_metrics(conn)
        interventions = compute_intervention_metrics(conn)
        resumes = compute_resume_metrics(conn)
        recovery = compute_recovery_metrics(conn, workspace_root=workspace_root)
        time_to_outcome = compute_time_to_outcome(conn, limit=limit)
        logging_completeness = compute_logging_completeness(
            conn, project_records=project_records, fallback_log=fallback_log
        )
    config_drift = compute_config_drift(frozen_config)

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
        "M12_wall_clock_duration": time_to_outcome,
        "M13_developer_waiting_time": "Not computed: requires approval-gate/intervention "
        "timestamp deltas; best-effort candidate for a future 21B patch if dogfood "
        "shows it's needed. Not blocking Phase 22 start (zeros/absent is acceptable).",
        "M17_s2_diagnosis_latency_minutes_avg": failure_metrics[
            "s2_diagnosis_latency_minutes_avg"
        ],
        "M17_s2_count": failure_metrics["s2_count"],
        "M17_s2_rows_with_latency_recorded": failure_metrics[
            "s2_rows_with_latency_recorded"
        ],
        "M17_note": "Derived from failure_table.csv's optional 'diagnosis_minutes' "
        "column on severity=S2 rows (Phase 21F). A blank/absent value on an S2 row "
        "means the operator has not yet recorded a latency estimate for it — not "
        "an error; the average excludes those rows.",
        "M18_resume": resumes,
        "M19_baseline_relative_effort": project_record_metrics[
            "baseline_comparison_distribution"
        ],
        "confidence_tiers": {
            "project_records": project_record_metrics["confidence_tier"],
            "interventions": interventions["confidence_tier"],
            "session_durations": time_to_outcome["confidence_tier"],
        },
        "notes": [n for n in (fallback["note"], project_record_metrics["note"]) if n],
    }
    report.internal = {
        "M4_planning_success": outcome["outcome_rates"],
        "M5_M6_validator": rules,
        "M7_repair_recovery_utilization": {
            "avg_repair_count": outcome["avg_repair_count"],
            "outcome_counts": outcome["outcome_counts"],
            "recovery_strategy_invocations": recovery,
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
            "recovery": recovery["confidence_tier"],
        },
        "notes": [n for n in (failure_metrics["note"],) if n],
    }
    report.integrity = {
        "R5_config_drift": config_drift,
        "R3_logging_completeness": logging_completeness,
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
    parser.add_argument(
        "--frozen-config",
        type=Path,
        default=None,
        help="Path to the frozen dogfood.env.example (Phase 21B/B3) to assert "
        "the running config against (Phase 21A §10 R5).",
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
        frozen_config=args.frozen_config,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print("=== Product KPIs (is this a good product?) ===")
        print(json.dumps(report.product, indent=2, default=str))
        print()
        print("=== Internal KPIs (what should engineers improve?) ===")
        print(json.dumps(report.internal, indent=2, default=str))
        print()
        print("=== Integrity Checks (R3 logging completeness / R5 config drift) ===")
        print(json.dumps(report.integrity, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
