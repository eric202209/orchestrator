#!/usr/bin/env python3
"""Capture a read-only evidence bundle for one TaskExecution."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.failure_taxonomy import failure_class, parse_log_metadata  # noqa: E402

DEFAULT_WORKSPACE_ROOT = REPO_ROOT.parent
EXPECTED_FILES = (
    "metadata.json",
    "failure_summary.json",
    "decision_timeline.json",
    "replay_report.semantic.json",
    "workspace_evidence_summary.json",
    "change_set_summary.json",
    "planning_contract_summary.json",
    "logs_summary.json",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(
    conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _one(
    conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _env_str(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return "unknown"


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _execution_context(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    task_id: int,
    task_execution_id: int,
) -> dict[str, Any]:
    row = _one(
        conn,
        """
        select te.id as task_execution_id,
               te.status as task_execution_status,
               te.attempt_number,
               te.started_at as task_execution_started_at,
               te.completed_at as task_execution_completed_at,
               te.created_at as task_execution_created_at,
               te.updated_at as task_execution_updated_at,
               te.session_id,
               te.task_id,
               t.title as task_title,
               t.description as task_description,
               t.status as task_status,
               t.execution_profile,
               t.error_message as task_error_message,
               t.current_step,
               s.name as session_name,
               s.status as session_status,
               s.is_active as session_is_active,
               p.id as project_id,
               p.name as project_name,
               p.workspace_path
        from task_executions te
        left join tasks t on t.id = te.task_id
        left join sessions s on s.id = te.session_id
        left join projects p on p.id = t.project_id
        where te.id = ?
          and te.session_id = ?
          and te.task_id = ?
        """,
        (task_execution_id, session_id, task_id),
    )
    if row is None:
        return {
            "available": False,
            "session_id": session_id,
            "task_id": task_id,
            "task_execution_id": task_execution_id,
            "reason": "task_execution_not_found",
        }
    row["available"] = True
    row["resolved_workspace_path"] = str(
        _resolve_workspace_path(row.get("workspace_path"))
        or row.get("workspace_path")
        or ""
    )
    return row


def _resolve_workspace_path(workspace_path: Any) -> Path | None:
    raw = str(workspace_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    workspace_root = Path(
        os.environ.get("OPENCLAW_WORKSPACE", str(DEFAULT_WORKSPACE_ROOT))
    )
    return workspace_root / raw


def _metadata_rows(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    task_id: int,
    task_execution_id: int,
) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        select id, level, message, log_metadata, created_at
        from log_entries
        where task_execution_id = ?
           or (session_id = ? and task_id = ?)
        order by id asc
        """,
        (task_execution_id, session_id, task_id),
    )


def _event_journal_path(context: dict[str, Any]) -> Path | None:
    workspace_path = _resolve_workspace_path(context.get("workspace_path"))
    if workspace_path is None:
        return None
    return (
        workspace_path
        / ".openclaw"
        / "events"
        / f"session_{context['session_id']}_task_{context['task_id']}.jsonl"
    )


def _read_event_journal(context: dict[str, Any]) -> dict[str, Any]:
    path = _event_journal_path(context)
    if path is None:
        return {"available": False, "reason": "workspace_path_missing", "events": []}
    if not path.exists():
        return {
            "available": False,
            "reason": "event_journal_missing",
            "path": str(path),
            "events": [],
        }
    events: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "available": False,
            "reason": "event_journal_unreadable",
            "path": str(path),
            "error": str(exc),
            "events": [],
        }
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
    return {
        "available": True,
        "path": str(path),
        "event_count": len(events),
        "malformed_line_count": malformed,
        "events": events,
    }


def _runtime_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    build_git_sha = _env_str("ORCHESTRATOR_GIT_SHA", "GIT_SHA", "COMMIT_SHA")
    try:
        from app.services.build_identity import _read_repo_git_sha

        repo_git_sha = _read_repo_git_sha() or "unknown"
    except Exception:
        repo_git_sha = "unknown"

    if build_git_sha != "unknown" and repo_git_sha != "unknown":
        stale_check = "ok" if build_git_sha == repo_git_sha else "stale"
    else:
        stale_check = "unknown"

    expected_migration_version = "unknown"
    try:
        from app.db_migrations import MIGRATIONS

        if MIGRATIONS:
            expected_migration_version = str(MIGRATIONS[-1].version)
    except Exception:
        pass

    migration_versions: list[str] = []
    latest_migration = "unknown"
    migration_status = "unavailable"
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        migration_versions = [str(row[0]) for row in rows]
        latest_migration = migration_versions[-1] if migration_versions else "unknown"
        migration_status = (
            "ok" if expected_migration_version in migration_versions else "pending"
        )
    except Exception:
        pass

    return {
        "source": "capture_time_fallback",
        "captured_at": datetime.now(UTC).isoformat(),
        "build": {
            "version": _env_str("VERSION"),
            "build_git_sha": build_git_sha,
            "repo_git_sha": repo_git_sha,
            "build_time": _env_str("ORCHESTRATOR_BUILD_TIME", "BUILD_TIME"),
            "image_tag": _env_str("ORCHESTRATOR_IMAGE_TAG", "IMAGE_TAG"),
            "image_id": _env_str("ORCHESTRATOR_IMAGE_ID", "IMAGE_ID"),
            "stale_container_check": stale_check,
        },
        "database": {
            "migration_version": latest_migration,
            "migration_count": len(migration_versions),
            "expected_migration_version": expected_migration_version,
            "migration_status": migration_status,
        },
        "lanes": {
            "planning": _env_str("PLANNING_BACKEND", "AGENT_BACKEND"),
            "execution": _env_str("EXECUTION_BACKEND", "AGENT_BACKEND"),
            "debug_repair": _env_str(
                "DEBUG_REPAIR_BACKEND", "REPAIR_BACKEND", "AGENT_BACKEND"
            ),
            "repair": _env_str("REPAIR_BACKEND", "AGENT_BACKEND"),
        },
        "models": {
            "planner": _env_str("PLANNER_MODEL", "AGENT_MODEL"),
            "execution": _env_str("EXECUTION_MODEL", "AGENT_MODEL"),
            "debug_repair": _env_str(
                "DEBUG_REPAIR_MODEL", "PLANNING_REPAIR_MODEL", "AGENT_MODEL"
            ),
            "planning_repair": _env_str("PLANNING_REPAIR_MODEL", "AGENT_MODEL"),
        },
        "config": {
            "config_source": _env_str("ORCHESTRATOR_CONFIG_SOURCE"),
            "capture_note": "captured at evidence-bundle time, not at run-start",
        },
    }


def _metadata_payload(
    context: dict[str, Any],
    journal: dict[str, Any],
    runtime_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "capture_tool": "scripts/capture_task_evidence_bundle.py",
        "context": context,
        "event_journal": {
            key: value for key, value in journal.items() if key not in {"events"}
        },
        "bundle_files": list(EXPECTED_FILES),
        "runtime_identity": runtime_identity,
    }


def _logs_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    levels = Counter(str(row.get("level") or "UNKNOWN") for row in rows)
    parsed = [parse_log_metadata(row.get("log_metadata")) for row in rows]
    event_types = Counter(
        str(metadata.get("event_type"))
        for metadata in parsed
        if metadata.get("event_type")
    )
    reasons = Counter(
        str(metadata.get("reason")) for metadata in parsed if metadata.get("reason")
    )
    warnings = [
        _log_excerpt(row)
        for row in rows
        if str(row.get("level") or "").upper() in {"WARN", "WARNING", "ERROR"}
    ][:50]
    return {
        "available": True,
        "log_count": len(rows),
        "levels": dict(levels),
        "event_types": dict(event_types),
        "reasons": dict(reasons),
        "warning_error_excerpts": warnings,
    }


def _log_excerpt(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "level": row.get("level"),
        "created_at": row.get("created_at"),
        "message": str(row.get("message") or "")[:500],
    }


def _failure_summary(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    task_execution_id: int,
    rows: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    if _table_exists(conn, "execution_failure_summaries"):
        columns = _table_columns(conn, "execution_failure_summaries")
        if "task_execution_id" in columns:
            row = _one(
                conn,
                """
                select id, session_id, task_execution_id, summary, operator_feedback,
                       generated_at, feedback_at, replan_planning_session_id
                from execution_failure_summaries
                where task_execution_id = ?
                order by generated_at desc, id desc
                limit 1
                """,
                (task_execution_id,),
            )
            if row is not None:
                row["available"] = True
                row["source"] = "execution_failure_summaries"
                row["scope"] = "task_execution"
                return row

        row = _one(
            conn,
            """
            select id, session_id, summary, operator_feedback, generated_at,
                   feedback_at, replan_planning_session_id
            from execution_failure_summaries
            where session_id = ?
            """,
            (session_id,),
        )
        if row is not None:
            fallback = _fallback_failure_summary(rows, context)
            return {
                "available": False,
                "source": "fallback_log_summary",
                "reason": "stored_failure_summary_not_task_execution_scoped",
                "summary": fallback,
                "ignored_stored_summary": {
                    "id": row.get("id"),
                    "session_id": row.get("session_id"),
                    "generated_at": row.get("generated_at"),
                    "source": "execution_failure_summaries",
                    "scope": "session",
                },
            }
    fallback = _fallback_failure_summary(rows, context)
    return {
        "available": False,
        "source": "fallback_log_summary",
        "reason": "stored_failure_summary_missing",
        "summary": fallback,
    }


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"pragma table_info({table_name})")}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _fallback_failure_summary(
    rows: list[dict[str, Any]], context: dict[str, Any]
) -> str:
    parts: list[str] = []
    task_error = str(context.get("task_error_message") or "").strip()
    if task_error:
        parts.append(f"Task error: {task_error[:500]}")
    for row in reversed(rows):
        level = str(row.get("level") or "").upper()
        if level not in {"ERROR", "WARN", "WARNING"}:
            continue
        message = str(row.get("message") or "").strip()
        if message:
            parts.append(f"[{level}] {message[:500]}")
        if len(parts) >= 8:
            break
    if not parts:
        return "No stored failure summary or warning/error log excerpts found."
    return "\n".join(parts)


def _decision_timeline(
    rows: list[dict[str, Any]], journal: dict[str, Any]
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for event in journal.get("events") or []:
        if not isinstance(event, dict):
            continue
        events.append(
            {
                "source": "event_journal",
                "event_type": event.get("event_type"),
                "phase": (event.get("details") or {}).get("phase"),
                "timestamp": event.get("timestamp"),
                "details": event.get("details") or {},
            }
        )
    for row in rows:
        metadata = parse_log_metadata(row.get("log_metadata"))
        event_type = metadata.get("event_type")
        reason = metadata.get("reason")
        if not event_type and not reason:
            continue
        events.append(
            {
                "source": "log_metadata",
                "log_id": row.get("id"),
                "event_type": event_type,
                "phase": metadata.get("phase"),
                "timestamp": row.get("created_at"),
                "reason": reason,
                "message": str(row.get("message") or "")[:300],
                "details": metadata,
            }
        )
    return {
        "available": bool(events),
        "source_event_count": len(events),
        "events": events[:300],
        "truncated": len(events) > 300,
    }


def _workspace_evidence_summary(
    rows: list[dict[str, Any]], journal: dict[str, Any]
) -> dict[str, Any]:
    evidence_rows: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []
    for row in rows:
        metadata = parse_log_metadata(row.get("log_metadata"))
        if metadata.get("event_type") == "workspace_evidence_collected":
            evidence_rows.append(metadata)
        if metadata.get("event_type") == "debug_feedback_captured" or metadata.get(
            "debug_feedback_captured"
        ):
            debug_rows.append(metadata)
    for event in journal.get("events") or []:
        if event.get("event_type") != "workspace_evidence_collected":
            continue
        details = event.get("details")
        if isinstance(details, dict):
            evidence_rows.append(details)
    if not evidence_rows and debug_rows:
        latest_debug = debug_rows[-1]
        if latest_debug.get("evidence_capsule_used") or int(
            latest_debug.get("evidence_chars_total") or 0
        ):
            evidence_rows.append(
                {
                    "failure_class": failure_class(latest_debug),
                    "evidence_chars_total": int(
                        latest_debug.get("evidence_chars_total") or 0
                    ),
                    "commands_run": latest_debug.get("commands_run") or [],
                    "evidence_files_inspected": latest_debug.get(
                        "evidence_files_inspected"
                    )
                    or [],
                    "source": "debug_feedback_metadata_fallback",
                }
            )
    commands: list[str] = []
    files: list[str] = []
    chars = 0
    for metadata in evidence_rows:
        chars += int(metadata.get("evidence_chars_total") or 0)
        commands.extend(str(item) for item in metadata.get("commands_run") or [])
        files.extend(
            str(item) for item in metadata.get("evidence_files_inspected") or []
        )
    latest_debug = debug_rows[-1] if debug_rows else {}
    return {
        "available": bool(evidence_rows or debug_rows),
        "workspace_evidence_collected": bool(evidence_rows),
        "workspace_evidence_empty": bool(debug_rows) and not bool(evidence_rows),
        "evidence_total_chars": chars,
        "evidence_command_count": len([cmd for cmd in commands if cmd.strip()]),
        "evidence_localization_count": len([path for path in files if path.strip()]),
        "failure_class": (
            failure_class(latest_debug or evidence_rows[-1])
            if (latest_debug or evidence_rows)
            else "unknown"
        ),
        "commands_run": commands,
        "evidence_files_inspected": files,
    }


def _json_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _json_object(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _change_set_summary(
    conn: sqlite3.Connection, task_execution_id: int
) -> dict[str, Any]:
    if not _table_exists(conn, "task_execution_change_sets"):
        return {"available": False, "reason": "change_set_table_missing"}

    row = _one(
        conn,
        """
        select id, project_id, task_id, session_id, task_execution_id,
               base_snapshot_key, head_snapshot_key, snapshot_path, target_path,
               snapshot_exists, added_files, modified_files, deleted_files,
               warning_flags, review_decision, review_reason, disposition,
               disposition_reason, disposition_at, disposition_metadata, status,
               captured_at, created_at, updated_at
        from task_execution_change_sets
        where task_execution_id = ?
        order by captured_at desc, id desc
        limit 1
        """,
        (task_execution_id,),
    )
    if row is None:
        return {"available": False, "reason": "change_set_missing"}

    added = [str(item) for item in _json_list(row.get("added_files"))]
    modified = [str(item) for item in _json_list(row.get("modified_files"))]
    deleted = [str(item) for item in _json_list(row.get("deleted_files"))]
    warning_flags = [str(item) for item in _json_list(row.get("warning_flags"))]
    return {
        "available": True,
        "change_set_id": row.get("id"),
        "project_id": row.get("project_id"),
        "task_id": row.get("task_id"),
        "session_id": row.get("session_id"),
        "task_execution_id": row.get("task_execution_id"),
        "base_snapshot_key": row.get("base_snapshot_key"),
        "head_snapshot_key": row.get("head_snapshot_key"),
        "snapshot_path": row.get("snapshot_path"),
        "target_path": row.get("target_path"),
        "snapshot_exists": bool(row.get("snapshot_exists")),
        "added_files": added,
        "modified_files": modified,
        "deleted_files": deleted,
        "added_count": len(added),
        "modified_count": len(modified),
        "deleted_count": len(deleted),
        "changed_count": len(added) + len(modified) + len(deleted),
        "warning_flags": warning_flags,
        "review_decision": _json_object(row.get("review_decision")),
        "review_reason": row.get("review_reason"),
        "disposition": row.get("disposition"),
        "disposition_reason": row.get("disposition_reason"),
        "disposition_at": row.get("disposition_at"),
        "disposition_metadata": _json_object(row.get("disposition_metadata")),
        "status": row.get("status"),
        "captured_at": row.get("captured_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _planning_contract_summary(
    conn: sqlite3.Connection, task_execution_id: int
) -> dict[str, Any]:
    try:
        import scripts.planning_contract_report as planning_report

        summary = planning_report.summarize(conn, limit=1000)
        for record in summary.get("records") or []:
            if int(record.get("task_execution_id") or -1) == task_execution_id:
                return {"available": True, "record": record}
        return {
            "available": False,
            "reason": "task_execution_not_in_planning_report",
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": "planning_contract_report_failed",
            "error": str(exc),
        }


def _replay_report_semantic(context: dict[str, Any]) -> dict[str, Any]:
    project_dir = _resolve_workspace_path(context.get("workspace_path"))
    if project_dir is None:
        return {"available": False, "reason": "workspace_path_missing"}
    if not project_dir.exists():
        return {
            "available": False,
            "reason": "workspace_missing",
            "project_dir": str(project_dir),
        }
    try:
        from app.services.orchestration.reporting.replay import (
            reconstruct_execution_state,
        )
        from app.tests.report_semantic_assertions import semantic_replay_report

        report = reconstruct_execution_state(
            project_dir=project_dir,
            session_id=int(context["session_id"]),
            task_id=int(context["task_id"]),
        )
        payload = semantic_replay_report(report)
        payload["available"] = True
        return payload
    except Exception as exc:
        return {
            "available": False,
            "reason": "replay_reconstruction_failed",
            "project_dir": str(project_dir),
            "error": str(exc),
        }


def capture_bundle(
    *,
    db_path: str,
    session_id: int,
    task_id: int,
    task_execution_id: int,
    output_dir: Path,
) -> Path:
    bundle_dir = (
        output_dir / f"session-{session_id}_task-{task_id}_te-{task_execution_id}"
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)

    conn = _connect(db_path)
    try:
        context = _execution_context(
            conn,
            session_id=session_id,
            task_id=task_id,
            task_execution_id=task_execution_id,
        )
        rows = _metadata_rows(
            conn,
            session_id=session_id,
            task_id=task_id,
            task_execution_id=task_execution_id,
        )
        journal = _read_event_journal(context)
        runtime_identity = _runtime_identity(conn)

        payloads = {
            "metadata.json": _metadata_payload(context, journal, runtime_identity),
            "logs_summary.json": _logs_summary(rows),
            "failure_summary.json": _failure_summary(
                conn,
                session_id=session_id,
                task_execution_id=task_execution_id,
                rows=rows,
                context=context,
            ),
            "decision_timeline.json": _decision_timeline(rows, journal),
            "replay_report.semantic.json": _replay_report_semantic(context),
            "workspace_evidence_summary.json": _workspace_evidence_summary(
                rows, journal
            ),
            "change_set_summary.json": _change_set_summary(conn, task_execution_id),
            "planning_contract_summary.json": _planning_contract_summary(
                conn, task_execution_id
            ),
        }
    finally:
        conn.close()

    for filename in EXPECTED_FILES:
        _write_json(bundle_dir / filename, payloads[filename])
    return bundle_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture a stable read-only evidence bundle for one TaskExecution."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--task-execution-id", type=int, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/roadmap/reports/evidence-bundles"),
    )
    args = parser.parse_args()

    bundle_dir = capture_bundle(
        db_path=args.db,
        session_id=args.session_id,
        task_id=args.task_id,
        task_execution_id=args.task_execution_id,
        output_dir=args.output_dir,
    )
    print(f"Wrote evidence bundle: {bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
