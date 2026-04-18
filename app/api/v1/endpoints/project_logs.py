"""Project Log API Endpoints

Log streaming with project-level filtering.
Supports showing logs from all sessions within a project.
"""

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional, AsyncGenerator
import json
from app.database import get_db
from app.models import Project
from app.services.log_stream_service import LogStreamService

router = APIRouter()


@router.get("/logs")
def get_project_logs(
    project_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    level: Optional[str] = None,
    search: Optional[str] = None,
):
    """
    Get logs for all sessions in a project

    Args:
        project_id: Project ID
        limit: Maximum logs to return
        level: Optional log level filter
        search: Optional text search in log messages

    Returns:
        List of log entries from all sessions in the project
    """
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Create log service instance
    log_service = LogStreamService(db)

    # Get summary
    summary = log_service.get_project_logs_summary(project_id)

    # Get logs using service
    logs = list(
        log_service.stream_logs(
            project_id=project_id,
            limit=limit,
            level=level,
            search=search,
        )
    )

    return {
        "project_id": project_id,
        "project_name": project.name,
        "total_logs": summary["total_logs"],
        "returned_logs": len(logs),
        "by_level": summary["by_level"],
        "logs": logs,
    }


@router.get("/logs/stream")
async def stream_project_logs(
    project_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    follow: bool = Query(False, description="Continue streaming logs"),
    since: Optional[str] = None,
):
    """
    Stream logs for a project (for SSE)

    Args:
        project_id: Project ID
        limit: Maximum logs to return
        follow: Continue streaming new logs
        since: Only return logs after this timestamp (ISO format)

    Returns:
        Streaming response of log entries
    """
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Parse since timestamp
    from datetime import datetime

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid since timestamp format"
            )

    async def log_generator() -> AsyncGenerator[str, None]:
        """Generate log entries as JSON lines"""
        log_service = LogStreamService(db)

        try:
            for log in log_service.stream_logs(
                project_id=project_id,
                limit=limit,
                follow=follow,
                since=since_dt,
            ):
                yield json.dumps(log) + "\n"
        finally:
            db.close()

    return StreamingResponse(
        log_generator(),
        media_type="application/x-ndjson",
    )


@router.get("/logs/summary")
def get_project_logs_summary(project_id: int, db: Session = Depends(get_db)):
    """
    Get summary statistics for a project's logs

    Args:
        project_id: Project ID

    Returns:
        Summary statistics
    """
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    log_service = LogStreamService(db)
    summary = log_service.get_project_logs_summary(project_id)
    summary["project_id"] = project_id
    summary["project_name"] = project.name

    return summary


@router.websocket("/logs/ws")
async def websocket_project_logs(
    websocket: WebSocket,
    project_id: int,
    db: Session = Depends(get_db),
):
    """
    WebSocket endpoint for real-time project logs

    Args:
        websocket: WebSocket connection
        project_id: Project ID
    """
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        await websocket.close(code=4000, reason="Project not found")
        return

    # Accept connection
    await websocket.accept()

    try:
        log_service = LogStreamService(db)

        # Send initial logs
        initial_logs = list(
            log_service.stream_logs(
                project_id=project_id,
                limit=100,
            )
        )

        await websocket.send_json(
            {
                "type": "initial",
                "logs": initial_logs,
            }
        )

        # Stream new logs
        async for log in log_service.stream_logs(
            project_id=project_id,
            follow=True,
            limit=10,
        ):
            await websocket.send_json(
                {
                    "type": "new",
                    "log": log,
                }
            )

    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for project {project_id}")
    except Exception as e:
        logging.error(f"WebSocket error for project {project_id}: {e}")
        await websocket.close(code=4001, reason=str(e))
