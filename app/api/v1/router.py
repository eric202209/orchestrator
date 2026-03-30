"""API Router v1"""

from fastapi import APIRouter, Depends
from fastapi import HTTPException
from app.api.v1.endpoints import (
    tasks,
    github,
    sessions,
    projects,
    users,
    mobile,
    resume,
)
from app.api.v1.endpoints.tasks_sorted_logs import router as tasks_sorted_logs_router
from app.api.v1.endpoints import isolation, permissions, context
from app.api.v1.endpoints.project_logs import router as project_logs_router

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
        "openapi": "/openapi.json",
    }


# Authentication (must be included first for JWT to work)
api_router.include_router(
    auth_router,
    prefix="/auth",
    tags=["authentication"],
)

# Users (management endpoints)
api_router.include_router(
    users.router,
    tags=["users"],
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

# Project Isolation
api_router.include_router(
    isolation.router,
    # No prefix needed - endpoints already have /projects/{project_id}/isolation/...
    tags=["project-isolation"],
)

# Permission Approval
api_router.include_router(
    permissions.router,
    prefix="/permissions",
    tags=["permissions"],
)

# Context Preservation
api_router.include_router(
    context.router,
    prefix="/context",
    tags=["context-preservation"],
)

# Project Logs (filter by project_id)
api_router.include_router(
    project_logs_router,
    prefix="/projects/{project_id}",
    tags=["project-logs"],
)

# Mobile API — clawmobile integration via OpenClaw Gateway
api_router.include_router(mobile.router, tags=["mobile"])

# Resume Operations (pause, resume, retry steps)
api_router.include_router(
    resume.router,
    prefix="/sessions/{session_id}/resume",
    tags=["resume-operations"],
)
