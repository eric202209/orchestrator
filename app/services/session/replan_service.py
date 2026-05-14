"""Replan flow: failure summary generation, operator feedback, and replan trigger.

Lifecycle:
  1. Session fails → operator opens failure-summary endpoint.
  2. Backend generates a compact summary (~500 tokens) via LLM.
  3. Operator optionally adds feedback via POST /operator-feedback.
  4. Operator triggers POST /replan → new PlanningSession seeded with summary + feedback.
"""

from __future__ import annotations

import logging
import re as _re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from app.models import (
    ExecutionFailureSummary,
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    SessionTask,
    TaskStatus,
)
from .session_lookup import get_session_or_404

logger = logging.getLogger(__name__)

_SUMMARY_CHAR_LIMIT = 2000  # ~500 tokens


_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_json_fragment(msg: str) -> bool:
    """True for log lines that are raw JSON fragments from OpenClaw stderr dump."""
    stripped = msg.strip()
    return (
        stripped.startswith('"')
        or stripped.startswith("{")
        or stripped.startswith("}")
        or stripped.startswith("]")
        or (stripped.endswith(",") and ":" in stripped and len(stripped) < 60)
    )


def _build_fallback_summary(db: DBSession, session_id: int) -> str:
    """Build a summary from DB log entries and task errors when LLM is unavailable."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

    error_logs = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.level.in_(["ERROR", "WARN", "WARNING"]),
        )
        .order_by(LogEntry.created_at.desc())
        .limit(50)
        .all()
    )
    meaningful_errors = [
        entry for entry in error_logs if not _is_json_fragment(entry.message)
    ][:10]

    failed_tasks = (
        db.query(Task)
        .join(SessionTask, SessionTask.task_id == Task.id)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.status == TaskStatus.FAILED,
        )
        .all()
    )

    pending_tasks = (
        db.query(Task)
        .join(SessionTask, SessionTask.task_id == Task.id)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.status == TaskStatus.PENDING,
        )
        .all()
    )

    parts: list[str] = ["## Execution Summary\n"]

    if session:
        parts.append(f"Session '{session.name}' ended with status: {session.status}")

    if failed_tasks:
        parts.append("\n### Failed Tasks")
        for task in failed_tasks[:5]:
            parts.append(
                f"- {task.title}: {(task.error_message or 'unknown error')[:300]}"
            )

    if pending_tasks:
        parts.append("\n### Incomplete Tasks (never ran)")
        for task in pending_tasks[:5]:
            parts.append(f"- {task.title}")

    if meaningful_errors:
        parts.append("\n### Notable Errors/Warnings")
        for log in meaningful_errors:
            clean = _strip_ansi(log.message)[:200]
            parts.append(f"- [{log.level}] {clean}")

    if not failed_tasks and not pending_tasks and not meaningful_errors:
        parts.append("No specific failure details found in logs.")

    return "\n".join(parts)[:_SUMMARY_CHAR_LIMIT]


def _latest_summary_task_execution(
    db: DBSession, session_id: int
) -> TaskExecution | None:
    failed_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status == TaskStatus.FAILED,
        )
        .order_by(
            TaskExecution.completed_at.desc().nullslast(), TaskExecution.id.desc()
        )
        .first()
    )
    if failed_execution:
        return failed_execution

    return (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id)
        .order_by(
            TaskExecution.completed_at.desc().nullslast(),
            TaskExecution.started_at.desc().nullslast(),
            TaskExecution.id.desc(),
        )
        .first()
    )


def _generate_summary_via_llm(db: DBSession, session_id: int) -> Optional[str]:
    """Call the LLM to produce a compact failure summary. Returns None on failure."""
    try:
        from app.services.agents.agent_runtime import invoke_runtime_prompt

        error_logs = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session_id,
                LogEntry.level.in_(["ERROR", "WARN", "WARNING"]),
            )
            .order_by(LogEntry.created_at.desc())
            .limit(30)
            .all()
        )

        failed_tasks = (
            db.query(Task)
            .join(SessionTask, SessionTask.task_id == Task.id)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.status == TaskStatus.FAILED,
            )
            .all()
        )

        log_block = (
            "\n".join(f"[{entry.level}] {entry.message[:300]}" for entry in error_logs)
            or "(no error logs)"
        )

        task_block = (
            "\n".join(
                f"- {t.title}: {(t.error_message or 'unknown')[:300]}"
                for t in failed_tasks
            )
            or "(no failed tasks)"
        )

        prompt = (
            "You are reviewing a failed execution session. "
            "Write a compact technical summary (max 350 words) of what failed and why, "
            "suitable for seeding a new planning session. "
            "Focus on root causes, not symptoms. "
            "Do NOT suggest fixes yet — just describe the failure.\n\n"
            f"## Failed Tasks\n{task_block}\n\n"
            f"## Error Logs\n{log_block}"
        )

        task_execution = _latest_summary_task_execution(db, session_id)
        result = invoke_runtime_prompt(
            db,
            prompt,
            session_id=session_id,
            task_id=task_execution.task_id if task_execution else None,
            task_execution_id=task_execution.id if task_execution else None,
            timeout_seconds=60,
            session_prefix="failure_summary",
        )

        output = result.get("output") or result.get("content") or ""
        output = output.strip()

        _error_signals = (
            "timed out",
            "request timed out",
            "please try again",
            "error occurred",
            "failed to generate",
            "no response",
        )
        if not output or len(output) < 20:
            return None
        if any(sig in output.lower() for sig in _error_signals):
            logger.warning(
                "LLM returned error/timeout response for session %s; using fallback",
                session_id,
            )
            return None

        return output[:_SUMMARY_CHAR_LIMIT]
    except Exception as exc:
        logger.warning(
            "LLM summary generation failed for session %s: %s", session_id, exc
        )
        return None


def get_or_generate_failure_summary(
    db: DBSession, session_id: int
) -> ExecutionFailureSummary:
    """Return existing summary or generate one if it doesn't exist yet."""
    session = get_session_or_404(db, session_id)

    existing = (
        db.query(ExecutionFailureSummary)
        .filter(ExecutionFailureSummary.session_id == session_id)
        .first()
    )
    if existing:
        return existing

    summary_text = _generate_summary_via_llm(db, session_id)
    if not summary_text:
        summary_text = _build_fallback_summary(db, session_id)

    record = ExecutionFailureSummary(
        session_id=session_id,
        summary=summary_text,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(
        "Failure summary generated for session %s (%d chars)",
        session_id,
        len(summary_text),
    )
    return record


def store_operator_feedback(
    db: DBSession, session_id: int, feedback: str
) -> ExecutionFailureSummary:
    """Store operator free-text feedback on the failure summary."""
    get_session_or_404(db, session_id)

    record = (
        db.query(ExecutionFailureSummary)
        .filter(ExecutionFailureSummary.session_id == session_id)
        .first()
    )
    if not record:
        record = get_or_generate_failure_summary(db, session_id)

    record.operator_feedback = feedback.strip()
    record.feedback_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record


def trigger_replan(db: DBSession, session_id: int) -> dict:
    """Combine failure summary + operator feedback and seed a new PlanningSession.

    Returns dict with planning_session_id and message.
    """
    session = get_session_or_404(db, session_id)

    project = (
        db.query(Project)
        .filter(Project.id == session.project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    record = get_or_generate_failure_summary(db, session_id)

    prompt_parts = [
        "## Failure Context\n\nThe following execution session failed and requires replanning.",
        f"Session: {session.name}",
        "",
        record.summary,
    ]
    if record.operator_feedback:
        prompt_parts += ["", "## Operator Guidance", record.operator_feedback]

    prompt_parts += [
        "",
        "Based on the failure context above, create a revised plan that addresses the root cause.",
    ]

    replan_prompt = "\n".join(prompt_parts)

    from app.services.planning.planning_session_service import PlanningSessionService

    svc = PlanningSessionService(db)
    planning_session = svc.start_session(
        project, replan_prompt, source_brain="local", skip_clarification=True
    )

    record.replan_planning_session_id = planning_session.id
    db.commit()

    logger.info(
        "Replan triggered for session %s → planning session %s",
        session_id,
        planning_session.id,
    )
    return {
        "planning_session_id": planning_session.id,
        "session_id": session_id,
        "message": "Replan started. Open Project Architect to review and commit the revised plan.",
    }
