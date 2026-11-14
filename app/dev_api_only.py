"""Fail-closed HTTP surface for the existing development API, not a sandbox."""

from pathlib import Path
import re

from sqlalchemy.engine import make_url
from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.config import BASE_DIR, settings

DISABLED_DETAIL = {
    "code": "DEVELOPMENT_EXECUTION_DISABLED",
    "message": "Execution is disabled in development API-only mode.",
}

# Intentionally an allowlist: new routes and alternate execution APIs stay closed.
# Existing dependencies still authenticate and authorize every product request.
_READ_PATHS = re.compile(
    r"/api/v1/(?:auth/me|dashboard/attention|projects|tasks|sessions|"
    r"projects/[0-9]+(?:/(?:tasks|sessions))?|tasks/[0-9]+)"
)
_WRITE_PATHS = {
    ("POST", "/api/v1/auth/session/login"),
    ("POST", "/api/v1/auth/session/logout"),
    ("POST", "/api/v1/projects"),
    ("POST", "/api/v1/tasks"),
}


def validate_api_only_settings() -> None:
    """Reject obvious unsafe configuration; does not close same-root/SQLite F1."""
    if settings.ENVIRONMENT != "development":
        raise RuntimeError("DEV_API_ONLY requires ENVIRONMENT=development")
    url = make_url(settings.DATABASE_URL)
    if url.drivername != "sqlite" or not url.database:
        raise RuntimeError("API-only mode requires an existing development SQLite DB")
    db_path = Path(url.database).expanduser()
    runtime = Path(settings.RUNTIME_ROOT).expanduser()
    protected_names = {"orchestrator-production", "orchestrator-production-runtime"}
    for path in (db_path, runtime):
        if not path.is_absolute() or protected_names.intersection(path.parts):
            raise RuntimeError("API-only mode refuses non-absolute/production paths")
        resolved = path.resolve()
        if protected_names.intersection(resolved.parts) or resolved.is_relative_to(
            BASE_DIR
        ):
            raise RuntimeError("API-only state must be outside source/production")
    if (
        db_path.resolve()
        != (runtime.parent / "database" / "orchestrator-dev.sqlite3").resolve()
    ):
        raise RuntimeError("API-only DB must match the explicit development runtime")
    if not db_path.is_file():
        raise RuntimeError("API-only mode never initializes a database or account")


def validate_api_only_project_path(workspace_path: str, project_name: str, db) -> None:
    if not settings.DEV_API_ONLY:
        return
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )

    expected = (Path(settings.RUNTIME_ROOT).parent / "workspace").resolve()
    candidate = resolve_project_workspace_path(workspace_path, project_name, db=db)
    if not candidate.is_relative_to(expected) or candidate == expected:
        raise HTTPException(
            status_code=422,
            detail="API-only Project workspace must be inside its development workspace",
        )


class DevelopmentAPIOnlyMiddleware:
    """Keep the normal application, but deny all unreviewed request surfaces."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not settings.DEV_API_ONLY or scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        method, path = scope["method"], scope["path"].rstrip("/")
        if method == "GET" and path in {"", "/health", "/api/v1/health"}:
            response = JSONResponse(
                {
                    "status": "ok",
                    "mode": "development_api_only",
                    "execution_enabled": False,
                    "dependencies_checked": False,
                }
            )
            return await response(scope, receive, send)
        allowed = (method == "GET" and _READ_PATHS.fullmatch(path)) or (
            method,
            path,
        ) in _WRITE_PATHS
        if method == "OPTIONS":
            requested_method = (
                dict(scope["headers"])
                .get(b"access-control-request-method", b"")
                .decode("ascii", errors="replace")
            )
            allowed = (requested_method == "GET" and _READ_PATHS.fullmatch(path)) or (
                requested_method,
                path,
            ) in _WRITE_PATHS
        if not allowed:
            response = JSONResponse(
                status_code=503, content={"detail": DISABLED_DETAIL}
            )
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
