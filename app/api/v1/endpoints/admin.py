"""Admin diagnostics endpoints for operator visibility into platform health."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_current_active_user, get_db
from app.models import LogEntry, Session as SessionModel
from app.services.agents.agent_backends import list_supported_backends
from app.services.knowledge.readiness import knowledge_readiness_snapshot
from app.services.streaming_health import get_streaming_health_snapshot
from app.celery_app import celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _get_queue_health() -> Dict[str, Any]:
    """Return a best-effort snapshot of Celery queue depth and worker state."""
    try:
        inspect = celery_app.control.inspect(timeout=1.5)
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        scheduled = inspect.scheduled() or {}

        active_count = sum(len(tasks) for tasks in active.values())
        reserved_count = sum(len(tasks) for tasks in reserved.values())
        scheduled_count = sum(len(tasks) for tasks in scheduled.values())
        worker_count = len(active)

        return {
            "status": "healthy" if worker_count > 0 else "no_workers",
            "worker_count": worker_count,
            "active_tasks": active_count,
            "reserved_tasks": reserved_count,
            "scheduled_tasks": scheduled_count,
        }
    except Exception as exc:
        return {
            "status": "unreachable",
            "error": str(exc),
            "worker_count": 0,
            "active_tasks": 0,
            "reserved_tasks": 0,
            "scheduled_tasks": 0,
        }


def _get_session_stats(db: Session) -> Dict[str, Any]:
    """Return session counts grouped by status and recent failure detail."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    rows = (
        db.query(SessionModel.status, func.count(SessionModel.id))
        .filter(SessionModel.deleted_at.is_(None))
        .group_by(SessionModel.status)
        .all()
    )
    by_status = {status: count for status, count in rows}

    recent_failures: List[Dict[str, Any]] = []
    failed_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.status == "stopped",
            SessionModel.last_alert_level == "ERROR",
            SessionModel.last_alert_at >= cutoff,
        )
        .order_by(SessionModel.last_alert_at.desc())
        .limit(10)
        .all()
    )
    for s in failed_sessions:
        recent_failures.append(
            {
                "session_id": s.id,
                "session_name": s.name,
                "project_id": s.project_id,
                "last_alert": s.last_alert_message,
                "stopped_at": s.stopped_at.isoformat() if s.stopped_at else None,
            }
        )

    return {
        "by_status": by_status,
        "failed_last_24h": len(recent_failures),
        "recent_failures": recent_failures,
    }


def _get_recent_audit_events(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent structured audit log entries."""
    import json

    rows = (
        db.query(LogEntry)
        .filter(LogEntry.log_metadata.isnot(None))
        .order_by(LogEntry.id.desc())
        .limit(limit)
        .all()
    )
    events: List[Dict[str, Any]] = []
    for row in rows:
        try:
            meta = json.loads(row.log_metadata or "{}")
        except Exception:
            continue
        event_type = meta.get("event_type")
        if not event_type:
            continue
        events.append(
            {
                "id": row.id,
                "event_type": event_type,
                "level": row.level,
                "message": row.message,
                "session_id": row.session_id,
                "metadata": meta,
            }
        )
    return events


@router.get("/outcome-rates", tags=["admin"])
def get_outcome_rates(
    limit: int = 50,
    current_user=Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Compute the four Phase 9N production-readiness outcome rates from DB state.

    Returns outcome_rates and outcome_counts for the most recent `limit` sessions.
    `stuck_or_manual_db_cleanup_rate = 0` is the production-readiness gate.
    """
    import sys

    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[5]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        from scripts.session_outcome_report import (
            _outcome_rates,
            _operator_review_count,
            _rows,
            _session_report,
            _task_outcome_rates,
        )
    except ImportError as exc:
        return {"error": f"scripts not importable: {exc}"}

    db_url = str(settings.DATABASE_URL)
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        sessions = _rows(
            conn,
            """
            select s.id, s.project_id, s.name, s.status, s.is_active,
                   s.created_at, s.started_at, s.stopped_at,
                   p.name as project_name, p.workspace_path
            from sessions s
            left join projects p on p.id = s.project_id
            where s.deleted_at is null
            order by s.id desc
            limit ?
            """,
            (limit,),
        )
        session_reports = [
            _session_report(conn, session=s, check_timeline=False) for s in sessions
        ]
        rates, oc_counts = _outcome_rates(session_reports)
        task_outcomes = _task_outcome_rates(conn, limit=limit)
        op_review = _operator_review_count(conn)
        conn.close()
    except Exception as exc:
        return {"error": str(exc), "outcome_rates": {}, "outcome_counts": {}}

    stuck_count = oc_counts.get("stuck_or_manual_db_cleanup", 0)
    gate_pass = stuck_count == 0
    stuck_sessions = [
        {
            "session_id": r["session_id"],
            "status": r["status"],
            "terminal_class": r["terminal_class"],
        }
        for r in session_reports
        if r.get("outcome_class") == "stuck_or_manual_db_cleanup"
    ]

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "sessions_analyzed": len(session_reports),
        "outcome_rates": rates,
        "outcome_counts": oc_counts,
        "classified_sessions": sum(oc_counts.values()),
        "task_outcomes": task_outcomes,
        "operator_review_count": op_review,
        "gate_pass": gate_pass,
        "stuck_sessions": stuck_sessions,
    }


@router.get("/diagnostics", tags=["admin"])
def get_diagnostics(
    current_user=Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Operator diagnostics snapshot.

    Returns:
    - backend health for each registered provider
    - Celery queue depth and worker count
    - websocket/session streaming health
    - session counts by status and recent failure detail
    - recent structured audit log events
    """
    backends = [b.to_dict() for b in list_supported_backends()]
    queue = _get_queue_health()
    streaming = get_streaming_health_snapshot()
    sessions = _get_session_stats(db)
    audit = _get_recent_audit_events(db)
    knowledge = knowledge_readiness_snapshot(db, probe_embedding=False)

    overall = "healthy"
    if queue["status"] in ("no_workers", "unreachable"):
        overall = "degraded"
    elif knowledge["status"] in ("warning", "unavailable"):
        overall = "warning"
    elif streaming["status"] == "warning":
        overall = "warning"
    elif sessions["failed_last_24h"] > 0:
        overall = "warning"

    return {
        "overall_status": overall,
        "checked_at": datetime.now(UTC).isoformat(),
        "backends": backends,
        "queue": queue,
        "streaming": streaming,
        "knowledge": knowledge,
        "sessions": sessions,
        "recent_audit_events": audit,
    }


@router.get("/knowledge-readiness", tags=["admin"])
def get_knowledge_readiness(
    probe_embedding: bool = True,
    current_user=Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return active runtime knowledge DB/Qdrant/embedding readiness."""

    return knowledge_readiness_snapshot(db, probe_embedding=probe_embedding)
