"""
Additional Task API Endpoints - Sorted Logs
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import json
from app.database import get_db
from app.models import Task, LogEntry
from app.services.log_utils import sort_logs, deduplicate_logs

router = APIRouter()


@router.get("/tasks/{task_id}/logs/sorted")
def get_sorted_task_logs(
    task_id: int,
    db: Session = Depends(get_db),
    order: str = "asc",  # "asc" for oldest first, "desc" for newest first
    deduplicate: bool = True,  # Remove duplicate entries
    level: Optional[str] = None,  # Optional filter by log level
    limit: Optional[int] = None,  # Optional limit on number of logs
    offset: int = 0,  # NEW: For pagination
):
    """
    Get sorted and optionally deduplicated logs for a task

    OPTIMIZED: Uses database-level sorting and pagination to avoid timeout issues
    
    Args:
        task_id: Task ID
        order: Sort order - "asc" (oldest first) or "desc" (newest first)
        deduplicate: Remove duplicate log entries (note: expensive for large datasets)
        level: Optional log level filter (INFO, WARNING, ERROR)
        limit: Optional limit on number of logs to return (default: 100)
        offset: Offset for pagination (default: 0)

    Returns:
        Sorted list of log entries
    """
    # Verify task exists
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Default limit to prevent timeouts
    default_limit = 100
    effective_limit = limit if limit else default_limit
    
    # Cap limit at 1000 to prevent abuse
    if effective_limit > 1000:
        effective_limit = 1000

    # OPTIMIZATION: Use database-level sorting instead of Python sorting
    logs_query = db.query(LogEntry).filter(LogEntry.task_id == task_id)

    # Apply level filter if specified
    if level:
        logs_query = logs_query.filter(LogEntry.level == level)

    # Get total count BEFORE pagination
    total_logs = logs_query.count()

    # Apply database-level sorting (ORDER BY) - this is fast!
    if order == "desc":
        logs_query = logs_query.order_by(LogEntry.created_at.desc())
    else:
        logs_query = logs_query.order_by(LogEntry.created_at.asc())

    # Apply pagination (LIMIT + OFFSET) - this is fast!
    logs_entries = logs_query.offset(offset).limit(effective_limit).all()

    # Convert to list of dicts
    logs = [
        {
            "id": log.id,
            "task_id": log.task_id,
            "session_id": log.session_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
        }
        for log in logs_entries
    ]

    # Only deduplicate if requested (this is expensive, so make it optional)
    if deduplicate:
        logs = deduplicate_logs(logs)

    return {
        "task_id": task_id,
        "total_logs": total_logs,
        "returned_logs": len(logs),
        "offset": offset,
        "limit": effective_limit,
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": logs,
        "has_more": (offset + len(logs)) < total_logs,
    }
