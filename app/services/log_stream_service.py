"""Log Stream Service

Service for streaming and filtering logs.
Provides methods to fetch, filter, and stream logs from the database.
"""

import logging
import json
from typing import Optional, Generator, Dict, Any, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, and_
from app.models import LogEntry, Session as SessionModel, Task, Project

logger = logging.getLogger(__name__)


class LogStreamService:
    """Service for log streaming and filtering"""

    def __init__(self, db: Session):
        """
        Initialize log stream service

        Args:
            db: Database session
        """
        self.db = db

    def stream_logs(
        self,
        session_id: Optional[int] = None,
        session_instance_id: Optional[str] = None,
        project_id: Optional[int] = None,
        task_id: Optional[int] = None,
        limit: int = 100,
        follow: bool = False,
        since: Optional[datetime] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Stream logs from the database

        Args:
            session_id: Filter by session ID (optional)
            session_instance_id: Filter by session instance UUID (optional, prevents ID reuse)
            project_id: Filter by project ID (optional)
            task_id: Filter by task ID (optional)
            limit: Maximum number of logs to return
            follow: If True, continue reading (for WebSocket)
            since: Only return logs after this timestamp
            level: Optional log level filter
            search: Optional text search in log messages

        Yields:
            Log entries as dictionaries
        """
        # Build query
        query = self.db.query(LogEntry)

        # Apply filters
        if session_id:
            query = query.filter(LogEntry.session_id == session_id)

        # Critical: Filter by instance_id to prevent ID reuse issues
        if session_instance_id:
            query = query.filter(LogEntry.session_instance_id == session_instance_id)

        if task_id:
            query = query.filter(LogEntry.task_id == task_id)

        if project_id:
            # Filter logs by project through sessions (only NON-DELETED sessions)
            session_ids = (
                self.db.query(SessionModel.id)
                .filter(
                    SessionModel.project_id == project_id,
                    SessionModel.deleted_at.is_(None),
                )
                .all()
            )
            session_id_list = [s[0] for s in session_ids]
            query = query.filter(LogEntry.session_id.in_(session_id_list))

            # Also join to filter by session instance ID to prevent ID reuse issues
            query = query.join(SessionModel, LogEntry.session_id == SessionModel.id)
            query = query.filter(
                SessionModel.instance_id.isnot(None),
                LogEntry.session_instance_id == SessionModel.instance_id,
            )

        if since:
            query = query.filter(LogEntry.created_at > since)

        # Order by timestamp descending
        query = query.order_by(LogEntry.created_at.desc())

        # Limit results
        logs = query.limit(limit).all()

        # Convert to dict format
        for log in logs:
            log_dict = {
                "id": log.id,
                "session_id": log.session_id,
                "task_id": log.task_id,
                "message": log.message,
                "level": log.level,
                "timestamp": log.created_at.isoformat() if log.created_at else None,
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
                "session_instance_id": log.session_instance_id,
            }

            # Skip verbose tool schema logs (propertiesCount, schemaChars, etc.)
            if self._is_verbose_tool_log(log_dict):
                continue

            # Apply level filter
            if level and log_dict["level"] != level:
                continue

            # Apply search filter
            if search and search.lower() not in log_dict["message"].lower():
                continue

            yield log_dict

    def _is_verbose_tool_log(self, log_dict: Dict[str, Any]) -> bool:
        """Check if this is a verbose tool schema log that should be filtered out"""
        message = log_dict.get("message", "")

        # Skip logs with schema metadata patterns
        if "propertiesCount" in message or "schemaChars" in message:
            return True

        # Skip full JSON dumps of tool parameters
        try:
            metadata = log_dict.get("metadata", {})
            if isinstance(metadata, dict):
                # If metadata has many keys (schemafull dump), skip it
                if len(metadata) > 10 and "tool_name" not in metadata:
                    return True
        except (json.JSONDecodeError, TypeError):
            pass

        return False

    def get_project_logs_summary(self, project_id: int) -> Dict[str, Any]:
        """
        Get summary statistics for a project's logs

        Args:
            project_id: Project ID

        Returns:
            Summary statistics
        """
        # Get all NON-DELETED session IDs for this project (filter by deleted_at is NULL)
        session_ids = (
            self.db.query(SessionModel.id)
            .filter(
                SessionModel.project_id == project_id,
                SessionModel.deleted_at.is_(None),  # Only active sessions
            )
            .all()
        )
        session_id_list = [s[0] for s in session_ids]

        if not session_id_list:
            return {
                "total_logs": 0,
                "by_level": {},
                "recent_logs": [],
            }

        # Count logs by level (filtered by session_id AND session_instance_id)
        # This prevents showing logs from deleted/recreated sessions with reused IDs
        logs_by_level = (
            self.db.query(LogEntry.level, LogEntry.id)
            .join(SessionModel, LogEntry.session_id == SessionModel.id)
            .filter(
                LogEntry.session_id.in_(session_id_list),
                SessionModel.deleted_at.is_(None),  # Only active sessions
                SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
                LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
            )
            .all()
        )

        level_counts = {}
        for level, count in logs_by_level:
            level_counts[level] = count

        # Get recent logs (same filtering as above)
        recent_logs = (
            self.db.query(LogEntry)
            .join(SessionModel, LogEntry.session_id == SessionModel.id)
            .filter(
                LogEntry.session_id.in_(session_id_list),
                SessionModel.deleted_at.is_(None),  # Only active sessions
                SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
                LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
            )
            .order_by(LogEntry.created_at.desc())
            .limit(10)
            .all()
        )

        return {
            "total_logs": sum(level_counts.values()),
            "by_level": level_counts,
            "recent_logs": [
                {
                    "id": log.id,
                    "message": log.message[:100],
                    "level": log.level,
                    "timestamp": log.created_at.isoformat() if log.created_at else None,
                }
                for log in recent_logs
            ],
        }

    def get_recent_logs(
        self, session_id: int, instance_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get recent logs for a session (filtered by instance_id)

        Args:
            session_id: Session ID
            instance_id: Instance UUID to filter by (prevents ID reuse issues)
            limit: Maximum logs to return

        Returns:
            List of recent log entries
        """
        # Build query
        query = self.db.query(LogEntry).filter(LogEntry.session_id == session_id)

        # Filter by instance_id if provided (critical for preventing ID reuse)
        if instance_id:
            query = query.filter(LogEntry.session_instance_id == instance_id)

        # Order by timestamp descending
        logs = query.order_by(LogEntry.created_at.desc()).limit(limit).all()

        # Convert to dict format
        return [
            {
                "id": log.id,
                "session_id": log.session_id,
                "task_id": log.task_id,
                "message": log.message,
                "level": log.level,
                "timestamp": log.created_at.isoformat() if log.created_at else None,
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
                "session_instance_id": log.session_instance_id,
            }
            for log in logs
        ]


# Backward compatibility: keep the standalone functions
def get_db_session():
    """Get database session for log streaming"""
    engine = create_engine("sqlite:///./orchestrator.db")
    return Session(bind=engine)


def get_project_logs_summary_for_db(db: Session, project_id: int) -> Dict[str, Any]:
    """
    Get summary statistics for a project's logs (uses provided db session)

    Args:
        db: Database session
        project_id: Project ID

    Returns:
        Summary statistics
    """
    # Get all NON-DELETED session IDs for this project (filter by deleted_at is NULL)
    session_ids = (
        db.query(SessionModel.id)
        .filter(
            SessionModel.project_id == project_id,
            SessionModel.deleted_at.is_(None),  # Only active sessions
        )
        .all()
    )
    session_id_list = [s[0] for s in session_ids]

    if not session_id_list:
        return {
            "total_logs": 0,
            "by_level": {},
            "recent_logs": [],
        }

    # Count logs by level (filtered by session_id AND session_instance_id)
    # This prevents showing logs from deleted/recreated sessions with reused IDs
    logs_by_level = (
        db.query(LogEntry.level, LogEntry.id)
        .join(SessionModel, LogEntry.session_id == SessionModel.id)
        .filter(
            LogEntry.session_id.in_(session_id_list),
            SessionModel.deleted_at.is_(None),  # Only active sessions
            SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
            LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
        )
        .all()
    )

    level_counts = {}
    for level, count in logs_by_level:
        level_counts[level] = count

    # Get recent logs (same filtering as above)
    recent_logs = (
        db.query(LogEntry)
        .join(SessionModel, LogEntry.session_id == SessionModel.id)
        .filter(
            LogEntry.session_id.in_(session_id_list),
            SessionModel.deleted_at.is_(None),  # Only active sessions
            SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
            LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
        )
        .order_by(LogEntry.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "total_logs": sum(level_counts.values()),
        "by_level": level_counts,
        "recent_logs": [
            {
                "id": log.id,
                "message": log.message[:100],
                "level": log.level,
                "timestamp": log.created_at.isoformat() if log.created_at else None,
            }
            for log in recent_logs
        ],
    }


def stream_logs(
    session_id: Optional[int] = None,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    limit: int = 100,
    follow: bool = False,
    since: Optional[datetime] = None,
) -> Generator[Dict[str, Any], None, None]:
    """
    Stream logs from the database (backward compatibility)

    Args:
        session_id: Filter by session ID (optional)
        project_id: Filter by project ID (optional)
        task_id: Filter by task ID (optional)
        limit: Maximum number of logs to return
        follow: If True, continue reading (for WebSocket)
        since: Only return logs after this timestamp

    Yields:
        Log entries as dictionaries
    """
    db = get_db_session()

    try:
        # Build query
        query = db.query(LogEntry)

        # Apply filters
        if session_id:
            query = query.filter(LogEntry.session_id == session_id)

        if task_id:
            query = query.filter(LogEntry.task_id == task_id)

        if project_id:
            # Filter logs by project through sessions (only NON-DELETED sessions)
            session_ids = (
                db.query(SessionModel.id)
                .filter(
                    SessionModel.project_id == project_id,
                    SessionModel.deleted_at.is_(None),
                )
                .all()
            )
            session_id_list = [s[0] for s in session_ids]
            query = query.filter(LogEntry.session_id.in_(session_id_list))

            # Also join to filter by session instance ID to prevent ID reuse issues
            query = query.join(SessionModel, LogEntry.session_id == SessionModel.id)
            query = query.filter(
                SessionModel.instance_id.isnot(None),
                LogEntry.session_instance_id == SessionModel.instance_id,
            )

        if since:
            query = query.filter(LogEntry.created_at > since)

        # Order by timestamp descending
        query = query.order_by(LogEntry.created_at.desc())

        # Limit results
        logs = query.limit(limit).all()

        # Convert to dict format
        for log in logs:
            yield {
                "id": log.id,
                "session_id": log.session_id,
                "task_id": log.task_id,
                "message": log.message,
                "level": log.level,
                "timestamp": log.created_at.isoformat() if log.created_at else None,
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
            }

    finally:
        db.close()


def get_project_logs_summary(project_id: int) -> Dict[str, Any]:
    """
    Get summary statistics for a project's logs (backward compatibility)

    Args:
        project_id: Project ID

    Returns:
        Summary statistics
    """
    db = get_db_session()

    try:
        # Get all NON-DELETED session IDs for this project (filter by deleted_at is NULL)
        session_ids = (
            db.query(SessionModel.id)
            .filter(
                SessionModel.project_id == project_id,
                SessionModel.deleted_at.is_(None),  # Only active sessions
            )
            .all()
        )
        session_id_list = [s[0] for s in session_ids]

        if not session_id_list:
            return {
                "total_logs": 0,
                "by_level": {},
                "recent_logs": [],
            }

        # Count logs by level (filtered by session_id AND session_instance_id)
        # This prevents showing logs from deleted/recreated sessions with reused IDs
        logs_by_level = (
            db.query(LogEntry.level, LogEntry.id)
            .join(SessionModel, LogEntry.session_id == SessionModel.id)
            .filter(
                LogEntry.session_id.in_(session_id_list),
                SessionModel.deleted_at.is_(None),  # Only active sessions
                SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
                LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
            )
            .all()
        )

        level_counts = {}
        for level, count in logs_by_level:
            level_counts[level] = count

        # Get recent logs (same filtering as above)
        recent_logs = (
            db.query(LogEntry)
            .join(SessionModel, LogEntry.session_id == SessionModel.id)
            .filter(
                LogEntry.session_id.in_(session_id_list),
                SessionModel.deleted_at.is_(None),  # Only active sessions
                SessionModel.instance_id.isnot(None),  # Only sessions with instance tracking
                LogEntry.session_instance_id == SessionModel.instance_id,  # Match instance IDs
            )
            .order_by(LogEntry.created_at.desc())
            .limit(10)
            .all()
        )

        return {
            "total_logs": sum(level_counts.values()),
            "by_level": level_counts,
            "recent_logs": [
                {
                    "id": log.id,
                    "message": log.message[:100],
                    "level": log.level,
                    "timestamp": log.created_at.isoformat() if log.created_at else None,
                }
                for log in recent_logs
            ],
        }

    finally:
        db.close()
