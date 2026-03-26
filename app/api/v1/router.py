"""API Router v1"""

from fastapi import APIRouter, Depends
from fastapi import HTTPException
from app.api.v1.endpoints import tasks, github, sessions, projects
from app.api.v1.endpoints.tasks_sorted_logs import router as tasks_sorted_logs_router

# Import auth router separately
from app.api.v1.endpoints.auth import router as auth_router

api_router = APIRouter()


@api_router.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint for OpenClaw dashboard"""
    return {"status": "healthy"}


@api_router.get("/", tags=["root"])
async def root():
    """Root API endpoint"""
    return {
        "name": "AI Dev Agent Orchestrator",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "openapi": "/openapi.json"
    }

# Authentication (must be included first for JWT to work)
api_router.include_router(
    auth_router,
    prefix="/auth",
    tags=["authentication"],
)

# Add other routers
api_router.include_router(
    projects.router,
    tags=["projects"],
)

api_router.include_router(
    tasks.router,
    tags=["tasks"],
)

api_router.include_router(
    tasks_sorted_logs_router,
    tags=["tasks"],
)

api_router.include_router(
    sessions.router,
    tags=["sessions"],
)

api_router.include_router(
    github.router,
    tags=["github"],
)
