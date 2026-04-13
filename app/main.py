"""AI Dev Agent Orchestrator - FastAPI Application"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from app.config import settings
from app.api.v1.router import api_router
from app.api.v1.endpoints import auth
from app.celery_app import celery_app
from app.database import engine, init_db
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle"""
    # Startup
    init_db()
    logger.info("=" * 50)
    logger.info("🚀 Orchestrator API starting up...")
    logger.info(f"Version: {settings.VERSION}")
    logger.info(f"Port: {settings.PORT}")
    logger.info("=" * 50)

    yield

    # Shutdown
    logger.info("=" * 50)
    logger.info("🛑 Orchestrator API shutting down...")
    logger.info("Cleaning up WebSocket connections and resources...")
    logger.info("=" * 50)


# Custom CORS middleware to handle OPTIONS before router
class CORSMiddlewareBeforeRouter:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            # Get the origin from the request
            origin = scope.get("headers", [])
            request_origin = None
            for name, value in origin:
                if name == b"origin":
                    request_origin = value.decode("utf-8")
                    break

            # Determine allowed origin
            # If origin is null or empty, allow * (for console/test requests)
            # Otherwise, validate against allowed origins
            allowed_origin = "*"
            if request_origin and request_origin not in ["null", ""]:
                # Check if origin is in allowed list
                allowed_origins = settings.CORS_ORIGINS
                if "*" in allowed_origins:
                    allowed_origin = "*"
                elif request_origin in allowed_origins:
                    allowed_origin = request_origin
                else:
                    allowed_origin = None  # Block this origin

            # Build response headers
            headers = {
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "86400",
                "Access-Control-Allow-Credentials": "true",  # Always include credentials
            }

            if allowed_origin:
                headers["Access-Control-Allow-Origin"] = allowed_origin

            response = Response(
                status_code=200,
                headers=headers,
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"access-control-allow-origin", allowed_origin.encode()),
                        (b"access-control-allow-methods", b"*"),
                        (b"access-control-allow-headers", b"*"),
                        (b"access-control-max-age", b"86400"),
                        (b"access-control-allow-credentials", b"true"),
                    ],
                }
            )

            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                }
            )
            return

        await self.app(scope, receive, send)


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
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle uncaught exceptions with CORS headers"""
    logger.error(f"Uncaught Exception: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        },
    )


# Include routers (API_V1_STR = "/api/v1")
app.include_router(api_router, prefix=settings.API_V1_STR, tags=["API"])


# Initialize Celery Beat periodic tasks
@app.on_event("startup")
async def startup_celery():
    """Setup Celery Beat when app starts"""
    logger.info("Celery task queue initialized")


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
