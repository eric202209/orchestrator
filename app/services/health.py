"""Shared health and version payloads."""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import text

from app.config import settings
from app.database import engine


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
    }
    details = {
        "version": settings.VERSION,
        "openclaw_gateway_url": settings.OPENCLAW_GATEWAY_URL,
        "runtime_profile": settings.RUNTIME_PROFILE,
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

    payload = {
        "status": overall_status,
        "checks": checks,
        "details": details,
    }
    status_code = 200 if overall_status == "healthy" else 503
    return payload, status_code
