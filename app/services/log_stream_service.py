"""Log Stream Service

Real-time log streaming from OpenClaw sessions back to the orchestrator.
Supports WebSocket connections and SSE (Server-Sent Events).
"""

import json
import logging
import asyncio
from typing import Optional, Dict, Any, Callable, AsyncGenerator
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import LogEntry, Session as SessionModel
from app.config import settings

logger = logging.getLogger(__name__)


class LogStreamService:
    """Service for streaming logs from OpenClaw sessions"""
    
    def __init__(self, db: Session):
        self.db = db
        self.active_streams: Dict[int, set] = {}  # session_id -> set of callbacks
    
    def register_stream(self, session_id: int, callback: Callable) -> None:
        """
        Register a callback to receive logs for a session
        
        Args:
            session_id: Session ID to stream logs from
            callback: Async function to receive log events
        """
        if session_id not in self.active_streams:
            self.active_streams[session_id] = set()
        self.active_streams[session_id].add(callback)
        logger.info(f"Registered log stream for session {session_id}")
    
    def unregister_stream(self, session_id: int, callback: Callable) -> None:
        """
        Unregister a log stream callback
        
        Args:
            session_id: Session ID
            callback: Callback to remove
        """
        if session_id in self.active_streams:
            self.active_streams[session_id].discard(callback)
            if not self.active_streams[session_id]:
                del self.active_streams[session_id]
    
    async def stream_logs(self, session_id: int) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream all logs for a session (async generator)
        
        Args:
            session_id: Session ID to stream from
            
        Yields:
            Log entries as dictionaries
        """
        # Get all existing logs
        logs = self.db.query(LogEntry) \
            .filter(LogEntry.session_id == session_id) \
            .order_by(LogEntry.created_at.asc()) \
            .all()
        
        for log in logs:
            yield {
                "type": "log",
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
                "session_id": session_id
            }
        
        # Register for real-time updates
        async def log_callback(level: str, message: str, metadata: Dict[str, Any]):
            yield {
                "type": "log",
                "level": level,
                "message": message,
                "timestamp": datetime.utcnow().isoformat(),
                "metadata": metadata,
                "session_id": session_id,
                "live": True
            }
        
        self.register_stream(session_id, log_callback)
        
        try:
            # Yield continuously (would be infinite in production)
            yield {
                "type": "connected",
                "session_id": session_id,
                "timestamp": datetime.utcnow().isoformat()
            }
        finally:
            self.unregister_stream(session_id, log_callback)
    
    async def emit_log(self, session_id: int, level: str, message: str, 
                      metadata: Optional[Dict[str, Any]] = None,
                      task_id: Optional[int] = None) -> None:
        """
        Emit a log entry to all registered streams
        
        Args:
            session_id: Session ID
            level: Log level (INFO, WARNING, ERROR)
            message: Log message
            metadata: Optional metadata dictionary
            task_id: Optional task ID
        """
        # Create database log entry
        log_entry = LogEntry(
            session_id=session_id,
            task_id=task_id,
            level=level,
            message=message,
            log_metadata=json.dumps(metadata) if metadata else None
        )
        self.db.add(log_entry)
        self.db.commit()
        
        # Emit to registered streams
        if session_id in self.active_streams:
            emit_data = {
                "level": level,
                "message": message,
                "metadata": metadata or {}
            }
            
            for callback in self.active_streams[session_id]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(**emit_data)
                    else:
                        callback(**emit_data)
                except Exception as e:
                    logger.error(f"Failed to emit log to callback: {str(e)}")
    
    async def stream_task_logs(self, task_id: int) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream logs for a specific task (includes session logs)
        
        Args:
            task_id: Task ID to stream logs from
            
        Yields:
            Log entries as dictionaries
        """
        # Get task to find associated sessions
        task = self.db.query(LogEntry).filter(LogEntry.task_id == task_id).first()
        
        if not task:
            yield {
                "type": "error",
                "message": f"Task {task_id} not found"
            }
            return
        
        # Stream logs for this task
        logs = self.db.query(LogEntry) \
            .filter(LogEntry.task_id == task_id) \
            .order_by(LogEntry.created_at.asc()) \
            .all()
        
        for log in logs:
            yield {
                "type": "log",
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
                "task_id": task_id
            }
        
        yield {
            "type": "connected",
            "task_id": task_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def get_session_log_count(self, session_id: int) -> int:
        """Get total number of log entries for a session"""
        return self.db.query(LogEntry).filter(LogEntry.session_id == session_id).count()
    
    def get_recent_logs(self, session_id: int, limit: int = 50) -> list:
        """Get recent logs for a session"""
        logs = self.db.query(LogEntry) \
            .filter(LogEntry.session_id == session_id) \
            .order_by(LogEntry.created_at.desc()) \
            .limit(limit) \
            .all()
        
        return [
            {
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {}
            }
            for log in logs
        ]
