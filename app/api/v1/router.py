"""API Router v1"""

from fastapi import APIRouter, Depends
from app.api.v1.endpoints import (
    tasks,
    github,
    sessions,
    projects,
    planner,
    planning,
    users,
    mobile,
    resume,
    settings,
    admin,
)
from app.api.v1.endpoints import isolation, permissions, context
from app.api.v1.endpoints.dashboard import router as dashboard_router
from app.api.v1.endpoints.knowledge import router as knowledge_router
from app.api.v1.endpoints.knowledge_lifecycle import (
    router as knowledge_lifecycle_router,
)
from app.api.v1.endpoints.project_logs import router as project_logs_router
from app.api.v1.endpoints.ops import router as ops_router
from app.api.v1.endpoints.guidance import router as guidance_router
from app.api.v1.endpoints.analytics import router as analytics_router
from app.dependencies import get_current_active_user
from app.services.health import api_root_payload, health_payload

# Import auth router separately
from app.api.v1.endpoints.auth import router as auth_router

api_router = APIRouter()


@api_router.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint for OpenClaw dashboard"""
    payload, status_code = health_payload()
    if status_code == 200:
        return payload
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=payload)


@api_router.get("/", tags=["root"])
async def root():
    """Root API endpoint"""
    return api_root_payload()


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
    dependencies=[Depends(get_current_active_user)],
)

api_router.include_router(
    settings.router,
    tags=["settings"],
    dependencies=[Depends(get_current_active_user)],
)

# Add other routers
api_router.include_router(
    projects.router,
    tags=["projects"],
    dependencies=[Depends(get_current_active_user)],
)

api_router.include_router(
    planner.router,
    tags=["planner"],
    dependencies=[Depends(get_current_active_user)],
)

api_router.include_router(
    planning.router,
    tags=["planning"],
    dependencies=[Depends(get_current_active_user)],
)

api_router.include_router(
    tasks.router,
    tags=["tasks"],
    dependencies=[Depends(get_current_active_user)],
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
    dependencies=[Depends(get_current_active_user)],
)

# Permission Approval
api_router.include_router(
    permissions.router,
    prefix="/permissions",
    tags=["permissions"],
    dependencies=[Depends(get_current_active_user)],
)

# Context Preservation
api_router.include_router(
    context.router,
    tags=["context-preservation"],
    dependencies=[Depends(get_current_active_user)],
)

# Project Logs (filter by project_id)
api_router.include_router(
    project_logs_router,
    prefix="/projects/{project_id}",
    tags=["project-logs"],
    dependencies=[Depends(get_current_active_user)],
)

# Mobile API — clawmobile integration via OpenClaw Gateway
api_router.include_router(mobile.router, tags=["mobile"])
api_router.include_router(mobile.admin_router, tags=["mobile-admin"])

# Resume Operations (pause, resume, retry steps)
api_router.include_router(
    resume.router,
    prefix="/sessions/{session_id}/resume",
    tags=["resume-operations"],
    dependencies=[Depends(get_current_active_user)],
)

# Admin diagnostics
api_router.include_router(
    admin.router,
    dependencies=[Depends(get_current_active_user)],
)

# Knowledge Layer
api_router.include_router(
    knowledge_router,
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(get_current_active_user)],
)

# Knowledge Lifecycle (Phase 16A-1)
api_router.include_router(
    knowledge_lifecycle_router,
    dependencies=[Depends(get_current_active_user)],
)

# Production observability (Phase 10B) — admin-only
api_router.include_router(ops_router)

# Human Guidance (HG-P1a)
api_router.include_router(
    guidance_router,
    tags=["human-guidance"],
    dependencies=[Depends(get_current_active_user)],
)

# Operational Analytics (Phase 15A-2) — admin-only, read-only
api_router.include_router(analytics_router)

# Dashboard — operator action center (Phase 15E-2)
api_router.include_router(
    dashboard_router,
    tags=["dashboard"],
    dependencies=[Depends(get_current_active_user)],
)
