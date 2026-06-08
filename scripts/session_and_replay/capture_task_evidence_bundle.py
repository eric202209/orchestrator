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

from scripts.session_and_replay.failure_taxonomy import (  # noqa: E402
    failure_class,
    parse_log_metadata,
    terminal_reason_from_rows,
)

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
    "run_replay_bundle.json",
    "run_replay_bundle.txt",
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


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


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
        mapped = _map_container_workspace_path(path)
        if mapped is not None:
            return mapped
        return path
    return _host_workspace_root() / raw


def _host_workspace_root() -> Path:
    for key in ("OPENCLAW_WORKSPACE", "WORKSPACE_ROOT"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    default_projects = DEFAULT_WORKSPACE_ROOT / "projects"
    if default_projects.exists():
        return default_projects
    return DEFAULT_WORKSPACE_ROOT


def _map_container_workspace_path(path: Path) -> Path | None:
    container_root = Path("/app/projects")
    try:
        relative = path.relative_to(container_root)
    except ValueError:
        return None
    if not relative.parts:
        return _host_workspace_root()
    return _host_workspace_root() / relative


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
        / ".agent"
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


def _run_start_runtime_identity(journal: dict[str, Any]) -> dict[str, Any] | None:
    for event in journal.get("events") or []:
        if event.get("event_type") != "task_started":
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        identity = details.get("run_start_runtime_identity")
        if isinstance(identity, dict):
            return identity
    return None


def _effective_runtime_identity(
    conn: sqlite3.Connection,
    journal: dict[str, Any],
) -> dict[str, Any]:
    run_start = _run_start_runtime_identity(journal)
    if run_start is not None:
        return run_start
    return _runtime_identity(conn)


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
        "config_snapshot": runtime_identity.get("config") or {},
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
    return {
        str(row["name"]) for row in conn.execute(f"pragma table_info({table_name})")
    }


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


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


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
        import scripts.maintenance.planning_contract_report as planning_report

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


def _extract_checkpoint_refs(timeline: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    checkpoint_event_types = {
        "checkpoint_saved",
        "checkpoint_loaded",
        "checkpoint_redirected",
    }
    for event in timeline.get("events") or []:
        if event.get("event_type") in checkpoint_event_types:
            name = str((event.get("details") or {}).get("checkpoint_name") or "")
            if name and name not in seen:
                refs.append(name)
                seen.add(name)
    return refs


def _extract_contract_verdicts(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    for event in timeline.get("events") or []:
        if event.get("event_type") == "validation_result":
            details = event.get("details") or {}
            verdicts.append(
                {
                    "stage": details.get("stage"),
                    "status": details.get("status"),
                    "reason": details.get("reason"),
                    "timestamp": event.get("timestamp"),
                }
            )
    return verdicts


def _extract_planning_prompt_ref(timeline: dict[str, Any]) -> dict[str, Any] | None:
    for event in timeline.get("events") or []:
        details = event.get("details") or {}
        if not isinstance(details, dict):
            continue
        prompt_ref = details.get("planning_prompt_ref") or details.get("prompt_ref")
        if isinstance(prompt_ref, dict):
            return prompt_ref
        if isinstance(prompt_ref, str) and prompt_ref.strip():
            return {"ref": prompt_ref.strip(), "source": event.get("source")}
    return None


def _append_verification_record(
    records: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    *,
    command: Any,
    channel: str,
    source: str,
    timestamp: Any = None,
    status: Any = None,
    exit_code: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    text = str(command or "").strip()
    if not text:
        return
    key = (channel, source, text)
    if key in seen:
        return
    seen.add(key)
    records.append(
        {
            "channel": channel,
            "source": source,
            "command": text,
            "timestamp": timestamp,
            "status": status,
            "exit_code": exit_code,
            "details": details or {},
        }
    )


def _extract_verification_records(
    decision_timeline: dict[str, Any],
    workspace_evidence_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for command in workspace_evidence_summary.get("commands_run") or []:
        _append_verification_record(
            records,
            seen,
            command=command,
            channel="workspace_evidence",
            source="workspace_evidence_summary",
        )

    for event in decision_timeline.get("events") or []:
        details = event.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        event_type = str(event.get("event_type") or "")
        timestamp = event.get("timestamp")
        source = str(event.get("source") or "unknown")

        if event_type == "validation_result":
            channel = str(details.get("stage") or "validation")
            commands = (
                _string_list(details.get("commands"))
                or _string_list(details.get("verification_commands"))
                or _string_list(details.get("command"))
                or _string_list(details.get("verification_command"))
            )
            for command in commands:
                _append_verification_record(
                    records,
                    seen,
                    command=command,
                    channel=channel,
                    source=source,
                    timestamp=timestamp,
                    status=details.get("status"),
                    exit_code=details.get("exit_code"),
                    details={
                        key: details.get(key)
                        for key in ("reason", "stage", "status")
                        if details.get(key) is not None
                    },
                )
            continue

        for key in (
            "verification_command",
            "completion_verification_command",
            "failed_command",
        ):
            if details.get(key):
                _append_verification_record(
                    records,
                    seen,
                    command=details.get(key),
                    channel=str(details.get("phase") or event_type or "verification"),
                    source=source,
                    timestamp=timestamp,
                    status=details.get("status"),
                    exit_code=details.get("exit_code"),
                    details={
                        name: details.get(name)
                        for name in ("reason", "phase", "status")
                        if details.get(name) is not None
                    },
                )
    return records


def _extract_scorer_result_from_timeline(
    decision_timeline: dict[str, Any],
) -> dict[str, Any] | None:
    for event in reversed(decision_timeline.get("events") or []):
        details = event.get("details") or {}
        if not isinstance(details, dict):
            continue
        for key in ("scorer_result", "scorer", "eval_scorer_result"):
            value = details.get(key)
            if isinstance(value, dict):
                return {
                    "available": True,
                    "source": f"decision_timeline.{key}",
                    "timestamp": event.get("timestamp"),
                    "result": value,
                }
        if "scorer_passed" in details or "verifier_passed" in details:
            return {
                "available": True,
                "source": "decision_timeline.details",
                "timestamp": event.get("timestamp"),
                "result": {
                    key: details.get(key)
                    for key in (
                        "scorer_passed",
                        "verifier_passed",
                        "lifecycle_agrees_with_scorer",
                        "verification_command",
                    )
                    if key in details
                },
            }
    return None


def _extract_scorer_result_from_reports(context: dict[str, Any]) -> dict[str, Any] | None:
    reports_dir = Path(
        os.environ.get(
            "RUN_REPLAY_SCORER_REPORT_DIR",
            str(REPO_ROOT / "docs" / "roadmap" / "reports" / "evals"),
        )
    )
    if not reports_dir.is_dir():
        return None

    session_id = context.get("session_id")
    task_id = context.get("task_id")
    workspace_path = str(
        context.get("resolved_workspace_path") or context.get("workspace_path") or ""
    )
    matches: list[tuple[float, Path, dict[str, Any]]] = []
    for path in reports_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        input_block = payload.get("input") or {}
        if not isinstance(input_block, dict):
            continue
        if session_id is not None and input_block.get("session_id") != session_id:
            continue
        if task_id is not None and input_block.get("task_id") != task_id:
            continue
        if workspace_path and str(input_block.get("project_dir") or "") != workspace_path:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        matches.append((mtime, path, payload))

    if not matches:
        return None
    _, path, payload = sorted(matches, key=lambda item: item[0])[-1]
    return {
        "available": True,
        "source": "eval_report",
        "path": str(path),
        "result": payload.get("result") or {},
        "verifier": payload.get("verifier") or {},
        "path_observability": payload.get("path_observability") or {},
    }


def _scorer_result(
    context: dict[str, Any],
    decision_timeline: dict[str, Any],
) -> dict[str, Any]:
    from_timeline = _extract_scorer_result_from_timeline(decision_timeline)
    if from_timeline is not None:
        return from_timeline
    from_report = _extract_scorer_result_from_reports(context)
    if from_report is not None:
        return from_report
    return {
        "available": False,
        "reason": "scorer_result_not_found",
    }


def _extract_repair_event_counts(timeline: dict[str, Any]) -> dict[str, int]:
    _REPAIR_TYPES = {
        "repair_generated",
        "repair_applied",
        "repair_rejected",
        "debug_repair_attempted",
        "debug_feedback_captured",
    }
    counts: Counter[str] = Counter()
    for event in timeline.get("events") or []:
        etype = str(event.get("event_type") or "")
        if etype in _REPAIR_TYPES:
            counts[etype] += 1
    return dict(counts)


def _extract_repair_attempts(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    repair_event_types = {
        "repair_generated",
        "repair_applied",
        "repair_rejected",
        "debug_repair_attempted",
        "debug_feedback_captured",
        "planning_repair_arbitration",
    }
    attempts: list[dict[str, Any]] = []
    for event in timeline.get("events") or []:
        event_type = str(event.get("event_type") or "")
        if event_type not in repair_event_types:
            continue
        details = event.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        attempts.append(
            {
                "event_type": event_type,
                "timestamp": event.get("timestamp"),
                "source": event.get("source"),
                "attempt": details.get("attempt")
                or details.get("repair_attempt")
                or details.get("debug_attempt")
                or details.get("retry_count"),
                "backend": details.get("backend")
                or details.get("backend_id")
                or details.get("debug_repair_backend")
                or details.get("planning_repair_backend"),
                "model": details.get("model")
                or details.get("model_name")
                or details.get("debug_repair_model")
                or details.get("planning_repair_model"),
                "status": details.get("status") or details.get("result"),
                "reason": details.get("reason")
                or details.get("rejection_reason")
                or details.get("failure_type"),
                "input_ref": details.get("input_ref")
                or details.get("failure_signature")
                or details.get("failure_class"),
                "output_ref": details.get("output_ref")
                or details.get("candidate_ref")
                or details.get("event_id"),
                "details": {
                    key: details.get(key)
                    for key in (
                        "phase",
                        "repair_type",
                        "compliance_retry_attempted",
                        "compliance_retry_succeeded",
                        "debug_repair_attempted",
                        "cross_stage_convergence_class",
                    )
                    if details.get(key) is not None
                },
            }
        )
    return attempts


def _run_replay_bundle_manifest(
    *,
    context: dict[str, Any],
    runtime_identity: dict[str, Any],
    journal: dict[str, Any],
    failure_summary: dict[str, Any],
    replay_report: dict[str, Any],
    decision_timeline: dict[str, Any],
    change_set_summary: dict[str, Any],
    workspace_evidence_summary: dict[str, Any],
    terminal_reason: str,
    scorer_result: dict[str, Any],
) -> dict[str, Any]:
    ri = runtime_identity
    workspace_path = context.get("resolved_workspace_path") or context.get(
        "workspace_path"
    )

    state_snapshot_path = None
    if workspace_path and context.get("session_id") and context.get("task_id"):
        state_snapshot_path = str(
            Path(workspace_path)
            / ".agent"
            / "events"
            / f"session_{context['session_id']}_task_{context['task_id']}_state_snapshots.jsonl"
        )

    integrity: dict[str, Any] = (
        (replay_report.get("integrity") or {})
        if isinstance(replay_report, dict)
        else {}
    )
    artifact_state: dict[str, Any] = (
        (replay_report.get("artifact_state") or {})
        if isinstance(replay_report, dict)
        else {}
    )
    cs = change_set_summary
    verification_records = _extract_verification_records(
        decision_timeline,
        workspace_evidence_summary,
    )

    return {
        "schema_version": 1,
        "captured_at": ri.get("captured_at"),
        "capture_tool": "scripts/capture_task_evidence_bundle.py",
        "bundle_files": list(EXPECTED_FILES),
        "ids": {
            "project_id": context.get("project_id"),
            "project_name": context.get("project_name"),
            "session_id": context.get("session_id"),
            "task_id": context.get("task_id"),
            "task_execution_id": context.get("task_execution_id"),
            "attempt_number": context.get("attempt_number"),
        },
        "prompt": {
            "task_title": context.get("task_title"),
            "task_description": context.get("task_description"),
            "planning_prompt_ref": _extract_planning_prompt_ref(decision_timeline),
        },
        "runtime_identity": {
            "source": ri.get("source"),
            "build_identity": ri.get("build"),
            "backend_lanes": ri.get("lanes"),
            "model_names": ri.get("models"),
            "config_snapshot": ri.get("config") or {},
            "config_source": (ri.get("config") or {}).get("config_source")
            or (ri.get("config") or {}).get("source"),
            "capture_note": (ri.get("config") or {}).get("capture_note"),
            "run_start_snapshot_available": ri.get("source") == "task_started_event",
        },
        "workspace": {
            "path": workspace_path,
            "event_journal_path": journal.get("path"),
            "event_journal_available": journal.get("available", False),
            "state_snapshot_path": state_snapshot_path,
            "checkpoint_refs": _extract_checkpoint_refs(decision_timeline),
            "workspace_hashes": artifact_state.get("workspace_hashes") or [],
            "change_set": {
                "available": cs.get("available", False),
                "changed_count": cs.get("changed_count"),
                "added_count": cs.get("added_count"),
                "modified_count": cs.get("modified_count"),
                "deleted_count": cs.get("deleted_count"),
            },
        },
        "verification": {
            "commands": verification_records,
            "results": [
                record
                for record in verification_records
                if record.get("status") is not None or record.get("exit_code") is not None
            ],
            "contract_verdicts": _extract_contract_verdicts(decision_timeline),
            "scorer_result_ref": (
                scorer_result.get("path")
                if scorer_result.get("source") == "eval_report"
                else scorer_result.get("source")
                if scorer_result.get("available")
                else None
            ),
        },
        "scorer": scorer_result,
        "repair": {
            "repair_event_counts": _extract_repair_event_counts(decision_timeline),
            "attempts": _extract_repair_attempts(decision_timeline),
        },
        "terminal": {
            "status": context.get("task_execution_status"),
            "failure_category": context.get("task_error_message"),
            "terminal_reason": terminal_reason,
            "failure_summary_available": failure_summary.get("available", False),
            "failure_summary": failure_summary.get("summary"),
        },
        "integrity": {
            "confidence": integrity.get("confidence"),
            "event_count_applied": integrity.get("event_count_applied"),
            "findings": integrity.get("findings") or [],
        },
    }


def _run_replay_bundle_text(
    *,
    context: dict[str, Any],
    runtime_identity: dict[str, Any],
    failure_summary: dict[str, Any],
    replay_report: dict[str, Any],
    decision_timeline: dict[str, Any],
    terminal_reason: str,
    scorer_result: dict[str, Any],
) -> str:
    lines: list[str] = [
        "RunReplayBundle v1",
        "=" * 50,
        "",
    ]

    session_id = context.get("session_id", "?")
    task_id = context.get("task_id", "?")
    te_id = context.get("task_execution_id", "?")
    attempt = context.get("attempt_number", "?")
    lines.append(
        f"Session {session_id} / Task {task_id} / Execution {te_id} (Attempt {attempt})"
    )
    title = context.get("task_title") or ""
    if title:
        lines.append(f"Task:   {title}")
    lines.append(f"Status: {context.get('task_execution_status') or 'unknown'}")
    fc = context.get("task_error_message") or ""
    if fc:
        lines.append(f"Failure category: {fc}")
    if terminal_reason:
        lines.append(f"Terminal reason:  {terminal_reason}")
    lines.append(f"Captured: {runtime_identity.get('captured_at', 'unknown')}")
    lines.append("")

    lines.append("Runtime Identity")
    lines.append("-" * 30)
    build = runtime_identity.get("build") or {}
    lines.append(f"  Build SHA:    {build.get('build_git_sha', 'unknown')}")
    lines.append(f"  Repo SHA:     {build.get('repo_git_sha', 'unknown')}")
    lines.append(f"  Stale check:  {build.get('stale_container_check', 'unknown')}")
    lanes = runtime_identity.get("lanes") or {}
    lines.append(f"  Planning:     {lanes.get('planning', 'unknown')}")
    lines.append(f"  Execution:    {lanes.get('execution', 'unknown')}")
    lines.append(f"  Debug repair: {lanes.get('debug_repair', 'unknown')}")
    lines.append("")

    lines.append("Failure Summary")
    lines.append("-" * 30)
    summary_text = failure_summary.get("summary") or ""
    if summary_text:
        for line in str(summary_text)[:600].splitlines():
            lines.append(f"  {line}")
    else:
        lines.append("  (no stored failure summary)")
    lines.append("")

    lines.append("Repair Events")
    lines.append("-" * 30)
    repair_counts = _extract_repair_event_counts(decision_timeline)
    if repair_counts:
        for etype, count in sorted(repair_counts.items()):
            lines.append(f"  {etype}: {count}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Scorer")
    lines.append("-" * 30)
    if scorer_result.get("available"):
        lines.append(f"  Source: {scorer_result.get('source', 'unknown')}")
        result = scorer_result.get("result") or {}
        if isinstance(result, dict):
            for key in ("clean_success", "verifier_passed", "scorer_passed"):
                if key in result:
                    lines.append(f"  {key}: {result[key]}")
    else:
        lines.append(f"  {scorer_result.get('reason', 'not available')}")
    lines.append("")

    lines.append("Replay Integrity")
    lines.append("-" * 30)
    integrity: dict[str, Any] = (
        (replay_report.get("integrity") or {})
        if isinstance(replay_report, dict)
        else {}
    )
    lines.append(f"  Confidence:     {integrity.get('confidence', 'unavailable')}")
    applied = integrity.get("event_count_applied")
    lines.append(f"  Events applied: {applied if applied is not None else 'N/A'}")
    lines.append("")

    lines.append("Bundle Files")
    lines.append("-" * 30)
    for fname in EXPECTED_FILES:
        lines.append(f"  {fname}")
    lines.append("")

    return "\n".join(lines)


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
        runtime_identity = _effective_runtime_identity(conn, journal)

        payloads: dict[str, Any] = {
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
        terminal_reason = terminal_reason_from_rows(
            rows,
            {
                **context,
                "error_message": context.get("task_error_message"),
            },
        )
        scorer_result = _scorer_result(
            context=context,
            decision_timeline=payloads["decision_timeline.json"],
        )
        payloads["run_replay_bundle.json"] = _run_replay_bundle_manifest(
            context=context,
            runtime_identity=runtime_identity,
            journal=journal,
            failure_summary=payloads["failure_summary.json"],
            replay_report=payloads["replay_report.semantic.json"],
            decision_timeline=payloads["decision_timeline.json"],
            change_set_summary=payloads["change_set_summary.json"],
            workspace_evidence_summary=payloads["workspace_evidence_summary.json"],
            terminal_reason=terminal_reason,
            scorer_result=scorer_result,
        )
        payloads["run_replay_bundle.txt"] = _run_replay_bundle_text(
            context=context,
            runtime_identity=runtime_identity,
            failure_summary=payloads["failure_summary.json"],
            replay_report=payloads["replay_report.semantic.json"],
            decision_timeline=payloads["decision_timeline.json"],
            terminal_reason=terminal_reason,
            scorer_result=scorer_result,
        )
    finally:
        conn.close()

    for filename in EXPECTED_FILES:
        payload = payloads[filename]
        if filename.endswith(".txt"):
            _write_text(bundle_dir / filename, payload)
        else:
            _write_json(bundle_dir / filename, payload)
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
