"""Shared health and version payloads."""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import text

from app.config import settings
from app.database import engine, get_pool_status
from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
)


def api_root_payload() -> dict:
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "running",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


def health_payload() -> tuple[dict, int]:
    checks = {
        "api": "ok",
        "database": "unknown",
        "redis": "unknown",
        "backend": "unknown",
    }
    details = {
        "version": settings.VERSION,
        "runtime_profile": settings.RUNTIME_PROFILE,
        "agent_backend": settings.AGENT_BACKEND,
    }
    overall_status = "healthy"

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = "error"
        details["database_error"] = str(exc)
        overall_status = "degraded"

    try:
        # Reflects only this process's own pool -- API and each Celery worker
        # process each hold a separate engine/pool onto the same sqlite file.
        details["database_pool"] = get_pool_status()
    except Exception as exc:
        details["database_pool_error"] = str(exc)

    try:
        import redis

        broker_url = urlparse(settings.CELERY_BROKER_URL)
        redis_client = redis.Redis(
            host=broker_url.hostname or "localhost",
            port=broker_url.port or 6379,
            db=int((broker_url.path or "/0").lstrip("/") or "0"),
            password=broker_url.password,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = "error"
        details["redis_error"] = str(exc)
        overall_status = "degraded"

    try:
        backend = get_backend_descriptor(settings.AGENT_BACKEND)
        checks["backend"] = "ok" if backend.health.ready else "degraded"
        details["backend"] = {
            "name": backend.name,
            "display_name": backend.display_name,
            "status": backend.health.status,
            "available": backend.health.available,
            "ready": backend.health.ready,
            "errors": backend.health.errors,
            "warnings": backend.health.warnings,
        }
    except UnsupportedAgentBackendError as exc:
        checks["backend"] = "degraded"
        details["backend_error"] = str(exc)

    payload = {
        "status": overall_status,
        "checks": checks,
        "details": details,
    }
    status_code = 200 if overall_status == "healthy" else 503
    return payload, status_code
