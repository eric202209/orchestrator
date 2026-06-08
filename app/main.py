"""AI Dev Agent Orchestrator - FastAPI Application"""

from contextlib import asynccontextmanager
import logging
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import (
    settings,
    validate_runtime_secrets,
    warn_incremental_execution,
    warn_local_openclaw_timeout,
)
from app.api.v1.router import api_router
from app.api.v1.endpoints.auth import router as auth_router
from app.database import init_db, get_db_session
from app.services.observability import flush_langfuse
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.health import health_payload
from app.services.workspace.system_settings import emit_runtime_lane_warning

logger = logging.getLogger(__name__)


def _redact_broker_url(url: str) -> str:
    """Strip broker credentials before writing URLs to logs."""

    parts = urlsplit(url or "")
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle"""
    # Startup
    validate_runtime_secrets()
    init_db()
    cleanup_db = None
    try:
        cleanup_db = get_db_session()
        cleanup_result = CheckpointService(cleanup_db).cleanup_orphaned_checkpoints()
        cleanup_db.commit()
        if cleanup_result.get("orphaned_session_ids"):
            logger.info(
                "Checkpoint startup cleanup removed orphaned artifacts: sessions=%s files=%s dirs=%s",
                cleanup_result.get("orphaned_session_ids"),
                cleanup_result.get("deleted_files", 0),
                cleanup_result.get("deleted_dirs", 0),
            )
    except Exception as exc:
        logger.warning("Checkpoint startup cleanup skipped due to error: %s", exc)
        if cleanup_db is not None:
            cleanup_db.rollback()
    finally:
        if cleanup_db is not None:
            cleanup_db.close()

    planning_db = None
    try:
        planning_db = get_db_session()
        recovered = PlanningSessionService(planning_db).recover_active_sessions()
        if recovered:
            logger.info(
                "Requeued active planning sessions after startup: %s", recovered
            )
    except Exception as exc:
        logger.warning("Planning session recovery skipped due to error: %s", exc)
    finally:
        if planning_db is not None:
            planning_db.close()

    emit_runtime_lane_warning()
    warn_local_openclaw_timeout()
    warn_incremental_execution()

    logger.info("=" * 50)
    logger.info("Orchestrator API starting up")
    logger.info("Version: %s | Port: %s", settings.VERSION, settings.PORT)
    logger.info(
        "Backend: %s | Model family: %s",
        settings.AGENT_BACKEND,
        settings.AGENT_MODEL,
    )
    logger.info("Celery broker: %s", _redact_broker_url(settings.CELERY_BROKER_URL))
    logger.info("=" * 50)

    yield

    # Shutdown
    logger.info("=" * 50)
    logger.info("🛑 Orchestrator API shutting down...")
    logger.info("Cleaning up WebSocket connections and resources...")
    flush_langfuse()
    logger.info("=" * 50)


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="AI Development Agent Orchestrator API",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,  # Add lifespan handler for graceful shutdown
)

# Add standard FastAPI CORS middleware (handles all requests including POST)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Ensure CORS headers are included in error responses"""
    logger.error(f"HTTP Exception: {exc.status_code} - {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle uncaught exceptions with CORS headers"""
    logger.error(f"Uncaught Exception: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Include routers (API_V1_STR = "/api/v1")
app.include_router(api_router, prefix=settings.API_V1_STR, tags=["API"])
# Browser clients have previously cached API bases without `/api/v1`. Keep
# auth endpoints available at `/auth/*` so stale bundles can still log in and
# refresh user state after a backend/frontend restart.
app.include_router(auth_router, prefix="/auth", tags=["authentication-compat"])


@app.get("/")
async def root():
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "running",
    }


@app.get("/health")
async def health_check():
    payload, status_code = health_payload()
    if status_code == 200:
        return payload

    return JSONResponse(status_code=status_code, content=payload)
