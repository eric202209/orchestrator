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
):
    """
    Get sorted and optionally deduplicated logs for a task
    
    Args:
        task_id: Task ID
        order: Sort order - "asc" (oldest first) or "desc" (newest first)
        deduplicate: Remove duplicate log entries
        level: Optional log level filter (INFO, WARNING, ERROR)
        limit: Optional limit on number of logs to return
        
    Returns:
        Sorted list of log entries
    """
    # Verify task exists
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Get all logs for the task
    logs_entries = db.query(LogEntry).filter(LogEntry.task_id == task_id).all()
    
    # Apply level filter if specified
    if level:
        logs_entries = [log for log in logs_entries if log.level == level]
    
    # Convert to list of dicts
    logs = [
        {
            "id": log.id,
            "task_id": log.task_id,
            "session_id": log.session_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {}
        }
        for log in logs_entries
    ]
    
    # Sort and deduplicate
    sorted_logs = sort_logs(logs, order=order, deduplicate=deduplicate)
    
    # Apply limit if specified
    if limit:
        sorted_logs = sorted_logs[:limit]
    
    return {
        "task_id": task_id,
        "total_logs": len(logs),
        "returned_logs": len(sorted_logs),
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": sorted_logs
    }
