"""AI Dev Agent Orchestrator - FastAPI Application"""

from contextlib import asynccontextmanager
import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings, validate_runtime_secrets
from app.api.v1.router import api_router
from app.database import engine, init_db, get_db_session
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.planning.planning_session_service import PlanningSessionService

logger = logging.getLogger(__name__)


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

    logger.info("=" * 50)
    logger.info("🚀 Orchestrator API starting up...")
    logger.info(f"Version: {settings.VERSION}")
    logger.info(f"Port: {settings.PORT}")
    logger.info("Celery task queue initialized")
    logger.info("=" * 50)

    yield

    # Shutdown
    logger.info("=" * 50)
    logger.info("🛑 Orchestrator API shutting down...")
    logger.info("Cleaning up WebSocket connections and resources...")
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


@app.get("/")
async def root():
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "running",
    }


@app.get("/health")
async def health_check():
    checks = {
        "api": "ok",
        "database": "unknown",
        "redis": "unknown",
    }
    details = {
        "version": settings.VERSION,
        "openclaw_gateway_url": settings.OPENCLAW_GATEWAY_URL,
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

    if overall_status == "healthy":
        return payload

    return JSONResponse(status_code=503, content=payload)
