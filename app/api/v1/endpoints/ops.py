"""Production observability endpoints for operators."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_current_admin_user, get_db
from app.models import Project
from app.services.observability.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])


def _db_health() -> Dict[str, Any]:
    try:
        from app.database import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}


def _redis_health() -> Dict[str, Any]:
    try:
        import redis

        url = urlparse(settings.CELERY_BROKER_URL)
        client = redis.Redis(
            host=url.hostname or "localhost",
            port=url.port or 6379,
            db=int((url.path or "/0").lstrip("/") or "0"),
            password=url.password,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}


def _qdrant_health() -> Dict[str, Any]:
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{settings.QDRANT_URL}/collections",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return {"status": "ok", "url": settings.QDRANT_URL}
            return {"status": "degraded", "http_status": resp.status}
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}


def _celery_health() -> Dict[str, Any]:
    try:
        from app.celery_app import celery_app

        inspect = celery_app.control.inspect(timeout=1.5)
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        worker_count = len(active)
        active_count = sum(len(t) for t in active.values())
        reserved_count = sum(len(t) for t in reserved.values())
        return {
            "status": "ok" if worker_count > 0 else "degraded",
            "worker_count": worker_count,
            "active_tasks": active_count,
            "reserved_tasks": reserved_count,
        }
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}


def _overall_status(components: Dict[str, Dict[str, Any]]) -> str:
    statuses = {c["status"] for c in components.values()}
    if "unavailable" in statuses:
        return "unavailable"
    if "degraded" in statuses:
        return "degraded"
    return "ok"


@router.get("/health")
def ops_health(
    current_user=Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Runtime health: ok / degraded / unavailable per component."""
    components = {
        "database": _db_health(),
        "redis": _redis_health(),
        "qdrant": _qdrant_health(),
        "celery": _celery_health(),
    }
    return {
        "status": _overall_status(components),
        "checked_at": datetime.now(UTC).isoformat(),
        "components": components,
    }


@router.get("/metrics/summary")
def ops_metrics_summary(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Aggregated operational metrics for last 24h and 7d."""
    mc = MetricsCollector(db)

    def _window(days: int) -> Dict[str, Any]:
        return {
            "phase_latency": mc.phase_latency(days=days),
            "repair": mc.repair_stats(days=days),
            "retry_distribution": mc.retry_distribution(days=days),
            "review_policy_outcomes": mc.review_policy_outcomes(days=days),
            "operator_decisions": mc.operator_decisions(days=days),
            "rollback_count": mc.rollback_count(days=days),
            "mutation_lock_conflicts": mc.mutation_lock_conflicts(days=days),
            "qdrant_fallback_count": mc.qdrant_fallback_count(days=days),
            "openclaw_timeout_count": mc.openclaw_timeout_count(days=days),
            "security_events": mc.security_events_count(days=days),
        }

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "last_24h": _window(1),
        "last_7d": _window(7),
    }


@router.get("/failure-classes")
def ops_failure_classes(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Terminal failure reason distribution (top 20, last 30 days)."""
    mc = MetricsCollector(db)
    distribution = mc.failure_class_distribution(days=30)
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "window_days": 30,
        "top_failure_reasons": distribution,
        "total_classified": sum(item["count"] for item in distribution),
    }


@router.get("/storage")
def ops_storage(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Snapshot and archive storage bytes per project."""
    projects = (
        db.query(Project)
        .filter(
            Project.deleted_at.is_(None),
            Project.workspace_path.isnot(None),
        )
        .all()
    )
    mc = MetricsCollector(db)
    stats = mc.storage_stats(projects)
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        **stats,
    }
