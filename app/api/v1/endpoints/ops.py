"""Production observability endpoints for operators."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any, Dict
from urllib.parse import urlparse

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.dependencies import get_current_admin_user, get_db
from sqlalchemy import func as sa_func

from app.models import (
    HumanGuidance,
    HumanGuidanceConflict,
    HumanGuidanceUsage,
    LogEntry,
    PermissionRequest,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.build_identity import build_identity_payload
from app.services.observability.metrics_collector import MetricsCollector
from app.services.project.state_summary import build_project_state_summary
from app.services.workspace.system_settings import diagnose_runtime_lane

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])


def _db_health(db: Session | None = None) -> Dict[str, Any]:
    try:
        from sqlalchemy import text

        if db is not None:
            db.execute(text("SELECT 1"))
        else:
            from app.database import engine

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


def _configured_backend_roles() -> Dict[str, list[str]]:
    role_settings = {
        "planning": settings.PLANNING_BACKEND or settings.AGENT_BACKEND,
        "execution": settings.EXECUTION_BACKEND or settings.AGENT_BACKEND,
        "repair": settings.REPAIR_BACKEND or settings.AGENT_BACKEND,
    }
    roles_by_backend: Dict[str, list[str]] = {}
    for role, backend_id in role_settings.items():
        normalized = str(backend_id or "").strip()
        if not normalized:
            continue
        roles_by_backend.setdefault(normalized, []).append(role)
    return roles_by_backend


def _last_failure_category_for_backend(db: Session, backend_id: str) -> str | None:
    latest = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.backend_id == backend_id,
            TaskExecution.failure_category.isnot(None),
        )
        .order_by(
            TaskExecution.completed_at.desc().nullslast(),
            TaskExecution.started_at.desc().nullslast(),
            TaskExecution.id.desc(),
        )
        .first()
    )
    return latest.failure_category if latest is not None else None


@router.get("/health")
def ops_health(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Runtime health: ok / degraded / unavailable per component."""
    components = {
        "database": _db_health(db),
        "redis": _redis_health(),
        "qdrant": _qdrant_health(),
        "celery": _celery_health(),
    }
    return {
        "status": _overall_status(components),
        "checked_at": datetime.now(UTC).isoformat(),
        "components": components,
    }


@router.get("/build-identity")
def ops_build_identity(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Read-only build/deployment identity for validation evidence."""
    return build_identity_payload(db)


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
            "task1_product_health": mc.task1_product_health(days=days),
            "ordered_project_health": mc.ordered_project_health(days=days),
            "model_lanes": mc.model_lane_distribution(days=days),
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


@router.get("/project-state/{project_id}")
def ops_project_state_summary(
    project_id: int,
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Read-only ProjectStateSummary: completed tasks, files changed, constraints, next task."""
    return build_project_state_summary(project_id, db)


@router.get("/planning-config")
def ops_planning_config(
    current_user=Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Effective planning/repair timeout values, their config source, and any low-timeout warning."""
    from app.config import (
        LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS,
        LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS,
    )

    planning_backend = (
        settings.PLANNING_BACKEND or settings.AGENT_BACKEND or ""
    ).strip()
    direct_timeout = settings.PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS
    repair_timeout = settings.PLANNING_REPAIR_TIMEOUT_SECONDS

    direct_source = (
        "env"
        if os.environ.get("PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS")
        else "default"
    )
    repair_source = (
        "env" if os.environ.get("PLANNING_REPAIR_TIMEOUT_SECONDS") else "default"
    )

    low_timeout_warning: str | None = None
    if (
        planning_backend == "local_openclaw"
        and direct_timeout < LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS
    ):
        low_timeout_warning = (
            f"PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS={direct_timeout} is below the "
            f"safe threshold of {LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS}s. "
            f"Validated value: {LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS}s."
        )

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "planning_backend": planning_backend,
        "planning_direct_local_openclaw_timeout_seconds": {
            "value": direct_timeout,
            "source": direct_source,
            "validated_value": LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS,
        },
        "planning_repair_timeout_seconds": {
            "value": repair_timeout,
            "source": repair_source,
        },
        "thinking_disabled": settings.PLANNING_REPAIR_DISABLE_THINKING,
        "local_openclaw_timeout_warning": low_timeout_warning,
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


@router.get("/backends")
def ops_backends(
    current_user=Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """List all registered backend descriptors with capabilities and config."""
    from app.services.agents.agent_backends import list_supported_backends

    backends = list_supported_backends()
    roles_by_backend = _configured_backend_roles()
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "count": len(backends),
        "backends": [
            {
                **b.to_dict(),
                "roles": roles_by_backend.get(b.name, []),
                "configured_for_roles": roles_by_backend.get(b.name, []),
                "max_parallel_sessions": b.capabilities.max_parallel_sessions,
            }
            for b in backends
        ],
    }


@router.get("/runtime-lane")
def ops_runtime_lane(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Runtime lane health: container/host identity, workspace root, writability, DB conflicts."""
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        **diagnose_runtime_lane(db),
    }


@router.get("/backends/health")
def ops_backends_health(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Health status for each registered backend, including runtime lane verdict."""
    from app.services.agents.agent_backends import list_supported_backends

    backends = list_supported_backends()
    lane = diagnose_runtime_lane(db)
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "runtime_lane": {
            "verdict": lane.get("verdict"),
            "runtime": lane.get("runtime"),
            "container_path_on_host": lane.get("container_path_on_host"),
            "reasons": lane.get("reasons"),
        },
        "backends": [
            {
                "name": b.name,
                "available": b.health.available,
                "ready": b.health.ready,
                "status": b.health.status,
                "errors": b.health.errors,
                "warnings": b.health.warnings,
            }
            for b in backends
        ],
    }


@router.get("/backends/concurrency")
def ops_backends_concurrency(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Live Redis slot usage per backend."""
    from app.services.agents.agent_backends import list_supported_backends
    from app.services.agents.backend_concurrency import (
        get_concurrency_snapshot,
        make_redis_client,
    )

    backends = list_supported_backends()
    roles_by_backend = _configured_backend_roles()
    try:
        redis_client = make_redis_client()
        redis_client.ping()
        redis_ok = True
    except Exception as exc:
        return {
            "computed_at": datetime.now(UTC).isoformat(),
            "redis_available": False,
            "error": str(exc),
            "backends": [],
        }

    snapshots = []
    for b in backends:
        max_slots = b.capabilities.max_parallel_sessions
        snapshot = get_concurrency_snapshot(redis_client, b.name)
        snapshot["max_slots"] = max_slots
        snapshot["max_parallel_sessions"] = max_slots
        snapshot["roles"] = roles_by_backend.get(b.name, [])
        snapshot["role"] = (
            roles_by_backend.get(b.name, [None])[0]
            if roles_by_backend.get(b.name)
            else None
        )
        snapshot["capacity_available"] = (
            True if max_slots is None else snapshot["active_count"] < max_slots
        )
        snapshot["last_failure_category"] = _last_failure_category_for_backend(
            db, b.name
        )
        snapshots.append(snapshot)

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "redis_available": redis_ok,
        "backends": snapshots,
    }


@router.get("/queue-latency")
def ops_queue_latency(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    days: int = 7,
) -> Dict[str, Any]:
    """Aggregate queue latency stats (avg, max, p95, count) over the last N days."""
    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        db.query(
            sa_func.count(TaskExecution.id).label("count"),
            sa_func.avg(TaskExecution.queue_latency_seconds).label("avg_seconds"),
            sa_func.max(TaskExecution.queue_latency_seconds).label("max_seconds"),
        )
        .filter(
            TaskExecution.queue_latency_seconds.isnot(None),
            TaskExecution.created_at >= since,
        )
        .one()
    )

    # p95 — Python sort (SQLite percentile_cont is unavailable without extensions)
    # Suppress for small samples where percentile is not meaningful.
    _P95_MIN_SAMPLES = 20
    p95: Optional[float] = None
    if row.count >= _P95_MIN_SAMPLES:
        values = [
            float(v)
            for (v,) in db.query(TaskExecution.queue_latency_seconds)
            .filter(
                TaskExecution.queue_latency_seconds.isnot(None),
                TaskExecution.created_at >= since,
            )
            .all()
        ]
        if values:
            values.sort()
            p95 = round(values[int(0.95 * len(values))], 3)

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "window_days": days,
        "executions_with_latency": row.count,
        "avg_queue_latency_seconds": (
            round(float(row.avg_seconds), 3) if row.avg_seconds is not None else None
        ),
        "max_queue_latency_seconds": (
            round(float(row.max_seconds), 3) if row.max_seconds is not None else None
        ),
        "p95_queue_latency_seconds": p95,
    }


def _parse_audit_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string; return None on any parse failure."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_event_type(message: str) -> str:
    """Extract bracket label from a structured message, e.g. '[FOO] bar' → 'FOO'."""
    if message.startswith("["):
        end = message.find("]")
        if end > 1:
            return message[1:end]
    return message


def _safe_metadata(raw: Optional[str]) -> Optional[object]:
    """Parse log_metadata JSON; return None on failure (never 500)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


@router.get("/audit-events")
def ops_audit_events(
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    event_type: Optional[str] = Query(
        None, description="e.g. PERMISSION_APPROVED (brackets optional)"
    ),
    session_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    level: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="ISO 8601 datetime"),
    until: Optional[str] = Query(None, description="ISO 8601 datetime"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> Dict[str, Any]:
    """Read-only structured audit event log for operators."""
    query = db.query(LogEntry)

    # Structured event filter
    if event_type:
        normalized = event_type.strip().strip("[]")
        query = query.filter(LogEntry.message.like(f"[{normalized}]%"))
    else:
        query = query.filter(
            or_(
                LogEntry.message.like("[%]%"),
                LogEntry.log_metadata.isnot(None),
            )
        )

    if session_id is not None:
        query = query.filter(LogEntry.session_id == session_id)

    if task_id is not None:
        query = query.filter(LogEntry.task_id == task_id)

    if project_id is not None:
        session_ids = (
            db.query(SessionModel.id)
            .filter(
                SessionModel.project_id == project_id,
                SessionModel.deleted_at.is_(None),
            )
            .all()
        )
        query = query.filter(LogEntry.session_id.in_([s[0] for s in session_ids]))

    if level is not None:
        query = query.filter(LogEntry.level == level.upper())

    if since is not None:
        since_dt = _parse_audit_datetime(since)
        if since_dt is None:
            raise HTTPException(
                status_code=422,
                detail="Invalid 'since' datetime format. Use ISO 8601, e.g. '2026-01-01T00:00:00Z'.",
            )
        query = query.filter(LogEntry.created_at >= since_dt)

    if until is not None:
        until_dt = _parse_audit_datetime(until)
        if until_dt is None:
            raise HTTPException(
                status_code=422,
                detail="Invalid 'until' datetime format. Use ISO 8601, e.g. '2026-01-01T00:00:00Z'.",
            )
        query = query.filter(LogEntry.created_at <= until_dt)

    total = query.count()

    order_col = (
        LogEntry.created_at.asc() if order == "asc" else LogEntry.created_at.desc()
    )
    rows = query.order_by(order_col).offset(offset).limit(limit).all()

    items = [
        {
            "id": r.id,
            "event_type": _extract_event_type(r.message),
            "message": r.message,
            "level": r.level,
            "session_id": r.session_id,
            "task_id": r.task_id,
            "session_instance_id": r.session_instance_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "metadata": _safe_metadata(r.log_metadata),
        }
        for r in rows
    ]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/workflow-templates")
def ops_workflow_templates(
    current_user=Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """List all available workflow templates."""
    from app.services.orchestration.workflow_templates import list_templates

    templates = list_templates()
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "count": len(templates),
        "templates": [
            {
                "id": t.id,
                "display_name": t.display_name,
                "workflow_profile": t.workflow_profile,
                "verification": t.verification,
                "auto_promote_eligible": t.auto_promote_eligible,
                "allowed_ops": t.allowed_ops,
                "risk_flags": t.risk_flags,
                "review_policy": t.review_policy,
            }
            for t in templates
        ],
    }


# ── Pilot Evidence Dashboard endpoints ────────────────────────────────────────


def _project_session_ids(db: Session, project_id: int) -> list[int]:
    """Return all non-deleted session IDs for a project."""
    rows = (
        db.query(SessionModel.id)
        .filter(
            SessionModel.project_id == project_id,
            SessionModel.deleted_at.is_(None),
        )
        .all()
    )
    return [r[0] for r in rows]


@router.get("/pilot-summary")
def ops_pilot_summary(
    project_id: int = Query(..., description="Project ID"),
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Task execution counts, success/rejection/timeout rates, and symbol verification for a project."""
    session_ids = _project_session_ids(db, project_id)

    if not session_ids:
        return {
            "computed_at": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "task_executions": {
                "total": 0,
                "done": 0,
                "failed": 0,
                "pending": 0,
                "running": 0,
                "cancelled": 0,
            },
            "rates": {
                "success_rate": None,
                "rejection_rate": None,
                "timeout_rate": None,
            },
            "symbol_verification": {"applicable_tasks": 0, "passed": None, "failed": 0},
        }

    executions = (
        db.query(TaskExecution).filter(TaskExecution.session_id.in_(session_ids)).all()
    )
    total = len(executions)
    counts: Dict[str, int] = {
        "done": 0,
        "failed": 0,
        "pending": 0,
        "running": 0,
        "cancelled": 0,
    }
    rejection_count = 0
    timeout_count = 0
    for ex in executions:
        status_val = ex.status.value if hasattr(ex.status, "value") else str(ex.status)
        key = status_val.lower()
        if key in counts:
            counts[key] += 1
        else:
            counts["pending"] += 1
        if ex.failure_category:
            fc = ex.failure_category.lower()
            if "reject" in fc:
                rejection_count += 1
            if "timeout" in fc:
                timeout_count += 1

    def _rate(n: int) -> Optional[float]:
        return round(n / total, 4) if total > 0 else None

    sym_failed = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id.in_(session_ids),
            LogEntry.message.like("[COMPLETION_SYMBOL_VERIFICATION_FAILED]%"),
        )
        .count()
    )

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "task_executions": {"total": total, **counts},
        "rates": {
            "success_rate": _rate(counts["done"]),
            "rejection_rate": _rate(rejection_count),
            "timeout_rate": _rate(timeout_count),
        },
        "symbol_verification": {
            "applicable_tasks": total,
            "passed": None,
            "failed": sym_failed,
        },
    }


@router.get("/pilot-guidance-stats")
def ops_pilot_guidance_stats(
    project_id: int = Query(..., description="Project ID"),
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """HumanGuidanceUsage injection stats and conflict summary for a project."""
    usage_rows = (
        db.query(HumanGuidanceUsage)
        .filter(
            HumanGuidanceUsage.project_id == project_id,
            HumanGuidanceUsage.selected.is_(True),
        )
        .all()
    )
    total_injections = len(usage_rows)
    total_rendered = sum(1 for r in usage_rows if r.rendered)
    task_ids_with_guidance: set[int] = {
        r.task_id for r in usage_rows if r.task_id is not None
    }
    tasks_with_guidance = len(task_ids_with_guidance)

    from collections import Counter

    injection_by_guidance: Counter = Counter(
        r.guidance_id for r in usage_rows if r.guidance_id is not None
    )
    top_guidance_ids = [gid for gid, _ in injection_by_guidance.most_common(5)]
    guidance_map: Dict[int, str] = {}
    if top_guidance_ids:
        rows = (
            db.query(HumanGuidance.id, HumanGuidance.message)
            .filter(HumanGuidance.id.in_(top_guidance_ids))
            .all()
        )
        guidance_map = {r.id: (r.message or "")[:60] for r in rows}

    top_entries = [
        {
            "guidance_id": gid,
            "message_preview": guidance_map.get(gid, ""),
            "injection_count": cnt,
        }
        for gid, cnt in injection_by_guidance.most_common(5)
    ]

    conflicts = (
        db.query(HumanGuidanceConflict)
        .filter(HumanGuidanceConflict.project_id == project_id)
        .all()
    )
    conflict_total = len(conflicts)
    conflict_open = sum(1 for c in conflicts if c.status == "open")
    conflict_resolved = sum(1 for c in conflicts if c.status == "resolved")
    conflict_rate = (
        round(conflict_total / tasks_with_guidance, 4)
        if tasks_with_guidance > 0
        else None
    )

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "usage": {
            "total_injections": total_injections,
            "total_rendered": total_rendered,
            "tasks_with_guidance": tasks_with_guidance,
            "top_entries": top_entries,
        },
        "conflicts": {
            "total": conflict_total,
            "open": conflict_open,
            "resolved": conflict_resolved,
            "conflict_rate": conflict_rate,
        },
    }


@router.get("/pilot-token-stats")
def ops_pilot_token_stats(
    project_id: int = Query(..., description="Project ID"),
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Token usage aggregates and top consumers for a project."""
    session_ids = _project_session_ids(db, project_id)
    if not session_ids:
        return {
            "computed_at": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "tasks_with_tokens": 0,
            "token_availability_rate": None,
            "avg_tokens_in": None,
            "avg_tokens_out": None,
            "total_tokens_in": None,
            "total_tokens_out": None,
            "top_consumers": [],
        }

    executions = (
        db.query(TaskExecution).filter(TaskExecution.session_id.in_(session_ids)).all()
    )
    total = len(executions)
    with_tokens = [
        ex for ex in executions if ex.tokens_in is not None or ex.tokens_out is not None
    ]
    tasks_with_tokens = len(with_tokens)

    token_availability_rate = round(tasks_with_tokens / total, 4) if total > 0 else None

    tokens_in_vals = [ex.tokens_in for ex in with_tokens if ex.tokens_in is not None]
    tokens_out_vals = [ex.tokens_out for ex in with_tokens if ex.tokens_out is not None]

    avg_in = (
        round(sum(tokens_in_vals) / len(tokens_in_vals), 1) if tokens_in_vals else None
    )
    avg_out = (
        round(sum(tokens_out_vals) / len(tokens_out_vals), 1)
        if tokens_out_vals
        else None
    )
    total_in = sum(tokens_in_vals) if tokens_in_vals else None
    total_out = sum(tokens_out_vals) if tokens_out_vals else None

    top_raw = sorted(
        with_tokens,
        key=lambda ex: (ex.tokens_in or 0) + (ex.tokens_out or 0),
        reverse=True,
    )[:5]
    task_ids_needed = [ex.task_id for ex in top_raw if ex.task_id is not None]
    task_title_map: Dict[int, str] = {}
    if task_ids_needed:
        title_rows = (
            db.query(Task.id, Task.title).filter(Task.id.in_(task_ids_needed)).all()
        )
        task_title_map = {r.id: (r.title or "") for r in title_rows}

    top_consumers = [
        {
            "task_id": ex.task_id,
            "task_title": task_title_map.get(ex.task_id, ""),
            "tokens_in": ex.tokens_in,
            "tokens_out": ex.tokens_out,
        }
        for ex in top_raw
    ]

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "tasks_with_tokens": tasks_with_tokens,
        "token_availability_rate": token_availability_rate,
        "avg_tokens_in": avg_in,
        "avg_tokens_out": avg_out,
        "total_tokens_in": total_in,
        "total_tokens_out": total_out,
        "top_consumers": top_consumers,
    }


@router.get("/pilot-permission-stats")
def ops_pilot_permission_stats(
    project_id: int = Query(..., description="Project ID"),
    current_user=Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Permission request counts and response time stats for a project."""
    session_ids = _project_session_ids(db, project_id)

    # Also include permission requests linked directly to project (no session)
    perm_rows = (
        db.query(PermissionRequest)
        .filter(
            or_(
                PermissionRequest.session_id.in_(session_ids),
                PermissionRequest.project_id == project_id,
            )
        )
        .all()
    )

    approvals = sum(1 for p in perm_rows if p.status == "approved")
    denials = sum(1 for p in perm_rows if p.status == "denied")
    pending = sum(1 for p in perm_rows if p.status == "pending")

    response_seconds: list[float] = []
    for p in perm_rows:
        if p.status == "approved" and p.approved_at and p.created_at:
            delta = (p.approved_at - p.created_at).total_seconds()
            if delta >= 0:
                response_seconds.append(delta)
        elif p.status == "denied" and p.updated_at and p.created_at:
            delta = (p.updated_at - p.created_at).total_seconds()
            if delta >= 0:
                response_seconds.append(delta)

    avg_response = (
        round(sum(response_seconds) / len(response_seconds), 1)
        if response_seconds
        else None
    )
    max_response = round(max(response_seconds), 1) if response_seconds else None

    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "approvals": approvals,
        "denials": denials,
        "pending": pending,
        "avg_response_seconds": avg_response,
        "max_response_seconds": max_response,
    }
