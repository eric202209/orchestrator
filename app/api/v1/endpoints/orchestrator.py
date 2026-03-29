"""
API endpoints for orchestrator service
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/orchestrator/health")
async def orchestrator_health():
    """Health check for orchestrator service"""
    return {"status": "healthy", "service": "orchestrator"}


@router.post("/orchestrator/task")
async def orchestrator_task():
    """Execute an orchestrated task"""
    return {"status": "ok", "message": "Task execution endpoint"}


@router.get("/orchestrator/status")
async def orchestrator_status():
    """Get orchestrator status"""
    return {
        "status": "running",
        "version": "1.0.0",
        "services": ["planning", "execution", "debugging"],
    }
