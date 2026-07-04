#!/usr/bin/env python3
"""Collect Phase 18E real-session validator evidence.

This script is read-only. It inspects persisted Orchestrator sessions from the
local SQLite database, reads matching workspace event journals, and aggregates
Phase 18B/18C validator rule telemetry fields when real
``plan_candidate_validated`` events exist.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable


PLAN_CANDIDATE_VALIDATED = "plan_candidate_validated"
PLAN_CANDIDATE_SELECTED = "plan_candidate_selected"
RECOVERY_STARTED = "recovery_started"
RECOVERY_COMPLETED = "recovery_completed"
RECOVERY_FAILED = "recovery_failed"
SUCCESS_STATUSES = {"accepted", "warning"}
RULE_ID_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SessionRow:
    session_id: int
    project_id: int
    session_name: str
    session_status: str
    project_name: str
    workspace_path: Path


@dataclass(frozen=True)
class ValidationRecord:
    session_id: int
    task_id: int | None
    source_file: str
    candidate_id: str
    selected_candidate_id: str
    rule_id: str
    validator_status: str
    failure_signature: str
    machine_profile: str
    runtime_profile: str
    timestamp: str
    recovery_triggered: bool
    recovery_rescued: bool
    selected_candidate_still_had_rule: bool
    recovery_latency_ms: int | None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument(
        "--workspace-root",
        default="/root/.openclaw/workspace/vault/projects",
        help="Root used for relative project workspace paths.",
    )
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    workspace_root = Path(args.workspace_root)
    sessions = load_sessions(db_path=db_path, workspace_root=workspace_root)
    event_files = list(iter_event_files(sessions))
    events_by_session = load_events_by_session(sessions, event_files)
    records = collect_records(events_by_session)
    print(render_markdown(sessions, event_files, records))


def load_sessions(*, db_path: Path, workspace_root: Path) -> tuple[SessionRow, ...]:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            """
            select
                s.id,
                s.project_id,
                s.name,
                s.status,
                p.name,
                p.workspace_path
            from sessions s
            join projects p on p.id = s.project_id
            order by s.id
            """
        ).fetchall()
    finally:
        con.close()

    sessions: list[SessionRow] = []
    for row in rows:
        workspace_path = Path(str(row[5] or "").strip())
        if not workspace_path.is_absolute():
            workspace_path = workspace_root / workspace_path
        sessions.append(
            SessionRow(
                session_id=int(row[0]),
                project_id=int(row[1]),
                session_name=str(row[2] or ""),
                session_status=str(row[3] or ""),
                project_name=str(row[4] or ""),
                workspace_path=workspace_path,
            )
        )
    return tuple(sessions)


def iter_event_files(sessions: Iterable[SessionRow]) -> Iterable[tuple[SessionRow, Path]]:
    for session in sessions:
        events_dir = session.workspace_path / ".agent" / "events"
        if not events_dir.exists():
            continue
        pattern = f"session_{session.session_id}_task_*.jsonl"
        for path in sorted(events_dir.glob(pattern)):
            if path.name.endswith("_state_snapshots.jsonl"):
                continue
            yield session, path


def load_events_by_session(
    sessions: tuple[SessionRow, ...],
    event_files: list[tuple[SessionRow, Path]],
) -> dict[int, list[dict[str, Any]]]:
    events_by_session: dict[int, list[dict[str, Any]]] = {
        session.session_id: [] for session in sessions
    }
    for session, path in event_files:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    event["_source_file"] = str(path)
                    event["_task_id"] = task_id_from_event_path(path)
                    events_by_session[session.session_id].append(event)
        except (OSError, json.JSONDecodeError) as exc:
            events_by_session[session.session_id].append(
                {
                    "event_type": "phase18e_collection_error",
                    "details": {"path": str(path), "error": str(exc)},
                    "_source_file": str(path),
                }
            )
    return events_by_session


def collect_records(
    events_by_session: dict[int, list[dict[str, Any]]],
) -> tuple[ValidationRecord, ...]:
    records: list[ValidationRecord] = []
    for session_id, events in events_by_session.items():
        selected_candidate_id = selected_candidate(events)
        selected_rules = rules_by_candidate(events).get(selected_candidate_id, set())
        recovery_triggered = any(
            event.get("event_type") == RECOVERY_STARTED for event in events
        )
        recovery_rescued = recovery_was_rescued(events)
        latency_ms = recovery_latency_ms(events)
        signature = failure_signature(events)
        machine_profile = machine_profile_from_events(events)
        runtime_profile = runtime_profile_from_events(events)

        for event in events:
            if event.get("event_type") != PLAN_CANDIDATE_VALIDATED:
                continue
            details = event_details(event)
            candidate_id = str(details.get("candidate_id") or "")
            for rule_id in rule_ids(details):
                records.append(
                    ValidationRecord(
                        session_id=session_id,
                        task_id=event.get("_task_id"),
                        source_file=str(event.get("_source_file") or ""),
                        candidate_id=candidate_id,
                        selected_candidate_id=selected_candidate_id,
                        rule_id=rule_id,
                        validator_status=str(details.get("validator_status") or ""),
                        failure_signature=signature,
                        machine_profile=machine_profile,
                        runtime_profile=runtime_profile,
                        timestamp=str(event.get("timestamp") or ""),
                        recovery_triggered=recovery_triggered,
                        recovery_rescued=recovery_rescued,
                        selected_candidate_still_had_rule=rule_id in selected_rules,
                        recovery_latency_ms=latency_ms,
                    )
                )
    return tuple(records)


def render_markdown(
    sessions: tuple[SessionRow, ...],
    event_files: list[tuple[SessionRow, Path]],
    records: tuple[ValidationRecord, ...],
) -> str:
    rule_frequency = Counter(record.rule_id for record in records)
    status_by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    signatures = Counter(record.failure_signature for record in records)
    machine_profiles = Counter(record.machine_profile for record in records)
    runtime_profiles = Counter(record.runtime_profile for record in records)
    latency_values = [
        record.recovery_latency_ms
        for record in records
        if record.recovery_triggered and record.recovery_latency_ms is not None
    ]

    lines = [
        "# Phase 18E Real-Session Validator Evidence Collection",
        "",
        f"Real sessions inspected: {len(sessions)}",
        f"Workspace event journal files inspected: {len(event_files)}",
        f"Total planning validation events: {len(records)}",
        "",
        "## Source Sessions",
    ]
    for session in sessions:
        lines.append(
            "- "
            f"session_id={session.session_id}, project_id={session.project_id}, "
            f"status={session.session_status}, project={session.project_name!r}, "
            f"workspace={session.workspace_path}"
        )

    lines.extend(["", "## Rule Frequency"])
    append_counter(lines, rule_frequency)

    lines.extend(["", "## Validator Status Distribution By Rule"])
    if status_by_rule:
        for record in records:
            status_by_rule[record.rule_id][record.validator_status] += 1
        for rule_id in sorted(status_by_rule):
            lines.append(f"- `{rule_id}`: {dict(status_by_rule[rule_id])}")
    else:
        lines.append("- none observed")

    lines.extend(["", "## Candidate Recovery Correlation By Rule"])
    if rule_frequency:
        for rule_id in sorted(rule_frequency):
            rule_records = [record for record in records if record.rule_id == rule_id]
            trigger_count = sum(1 for record in rule_records if record.recovery_triggered)
            rescue_count = sum(1 for record in rule_records if record.recovery_rescued)
            still_count = sum(
                1 for record in rule_records if record.selected_candidate_still_had_rule
            )
            trigger_rate = trigger_count / len(rule_records)
            rescue_rate = rescue_count / trigger_count if trigger_count else 0.0
            lines.append(
                "- "
                f"`{rule_id}`: trigger_count={trigger_count}, "
                f"trigger_rate={trigger_rate:.3f}, rescue_count={rescue_count}, "
                f"rescue_rate={rescue_rate:.3f}, "
                f"selected_candidate_still_had_rule={still_count}"
            )
    else:
        lines.append("- none observed")

    lines.extend(["", "## Failure Signatures"])
    append_counter(lines, signatures)

    lines.extend(["", "## Machine And Runtime Profiles"])
    lines.append(f"- machine_profiles: {dict(machine_profiles) if machine_profiles else {}}")
    lines.append(f"- runtime_profiles: {dict(runtime_profiles) if runtime_profiles else {}}")

    lines.extend(["", "## Recovery Latency"])
    if latency_values:
        lines.append(f"- count: {len(latency_values)}")
        lines.append(f"- min_ms: {min(latency_values)}")
        lines.append(f"- median_ms: {median(latency_values)}")
        lines.append(f"- max_ms: {max(latency_values)}")
    else:
        lines.append("- none observed")

    lines.extend(
        [
            "",
            "## Questionable Rescue Notes",
            "- none observable from the collected real-session dataset",
        ]
    )
    return "\n".join(lines) + "\n"


def append_counter(lines: list[str], counter: Counter[str]) -> None:
    if not counter:
        lines.append("- none observed")
        return
    for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{key}`: {count}")


def event_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") or {}
    return details if isinstance(details, dict) else {}


def rule_ids(details: dict[str, Any]) -> tuple[str, ...]:
    explicit = details.get("validator_rule_ids") or details.get("rule_ids")
    source = explicit if explicit else details.get("validator_reasons") or ()
    if isinstance(source, str):
        source = [source]
    return tuple(rule_id for rule_id in (normalize_rule_id(value) for value in source) if rule_id)


def normalize_rule_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return RULE_ID_RE.sub("_", raw).strip("_")


def rules_by_candidate(events: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.get("event_type") != PLAN_CANDIDATE_VALIDATED:
            continue
        details = event_details(event)
        candidate_id = str(details.get("candidate_id") or "")
        result[candidate_id].update(rule_ids(details))
    return result


def selected_candidate(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("event_type") != PLAN_CANDIDATE_SELECTED:
            continue
        details = event_details(event)
        return str(details.get("candidate_id") or details.get("selected_candidate_id") or "")
    return ""


def recovery_was_rescued(events: list[dict[str, Any]]) -> bool:
    if not any(event.get("event_type") == RECOVERY_STARTED for event in events):
        return False
    for event in reversed(events):
        if event.get("event_type") != RECOVERY_COMPLETED:
            continue
        details = event_details(event)
        status = str(details.get("validator_status") or details.get("status") or "")
        if status in SUCCESS_STATUSES or details.get("succeeded") is True:
            return True
    return False


def recovery_latency_ms(events: list[dict[str, Any]]) -> int | None:
    for event in reversed(events):
        if event.get("event_type") not in {RECOVERY_COMPLETED, RECOVERY_FAILED}:
            continue
        details = event_details(event)
        value = details.get("duration_ms") or details.get("recovery_latency_ms")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def failure_signature(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        details = event_details(event)
        for key in ("failure_signature", "signature", "signature_hash"):
            if details.get(key):
                return str(details[key])
    return "unknown"


def machine_profile_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        details = event_details(event)
        value = details.get("machine_profile")
        if value:
            return str(value)
    return "unknown"


def runtime_profile_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        details = event_details(event)
        value = details.get("runtime_profile")
        if value:
            return str(value)
    return "unknown"


def task_id_from_event_path(path: Path) -> int | None:
    match = re.search(r"_task_(\d+)\.jsonl$", path.name)
    if not match:
        return None
    return int(match.group(1))


if __name__ == "__main__":
    main()
