"""Tool Execution Tracking Service

Tracks all tool executions in OpenClaw sessions for audit trail,
debugging, and analytics.
"""

import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import LogEntry, Session as SessionModel
from app.config import settings

logger = logging.getLogger(__name__)


class ToolExecution:
    """Represents a tracked tool execution"""

    def __init__(
        self,
        tool_name: str,
        params: Dict[str, Any],
        result: Any,
        success: bool,
        execution_time_ms: float,
        session_id: int,
        task_id: Optional[int] = None,
        session_instance_id: Optional[str] = None,  # NEW: For proper log isolation
    ):
        self.tool_name = tool_name
        self.params = params
        self.result = result
        self.success = success
        self.execution_time_ms = execution_time_ms
        self.session_id = session_id
        self.task_id = task_id
        self.session_instance_id = session_instance_id  # NEW: For isolation
        self.timestamp = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "tool_name": self.tool_name,
            "params": self.params,
            "result": str(self.result)[:1000],  # Truncate long results
            "success": self.success,
            "execution_time_ms": self.execution_time_ms,
            "session_id": self.session_id,
            "session_instance_id": self.session_instance_id,  # NEW: For isolation
            "task_id": self.task_id,
            "timestamp": self.timestamp.isoformat(),
        }


class ToolTrackingService:
    """Service for tracking tool executions"""

    def __init__(self, db: Session):
        self.db = db
        self.active_executions: Dict[str, ToolExecution] = (
            {}
        )  # execution_id -> ToolExecution

    def start_execution(
        self,
        execution_id: str,
        tool_name: str,
        params: Dict[str, Any],
        session_id: int,
        task_id: Optional[int] = None,
        session_instance_id: Optional[str] = None,  # NEW: For isolation
    ) -> None:
        """
        Mark tool execution as started

        Args:
            execution_id: Unique execution identifier
            tool_name: Name of the tool
            params: Tool parameters
            session_id: Session ID
            task_id: Optional task ID
            session_instance_id: Instance UUID for log isolation (NEW)
        """
        self.active_executions[execution_id] = ToolExecution(
            tool_name=tool_name,
            params=params,
            result=None,
            success=False,
            execution_time_ms=0,
            session_id=session_id,
            task_id=task_id,
            session_instance_id=session_instance_id,  # NEW: For isolation
        )

        logger.info(f"Tool execution started: {tool_name} (id={execution_id})")

    def complete_execution(
        self,
        execution_id: str,
        result: Any,
        success: bool,
        error_message: Optional[str] = None,
    ) -> ToolExecution:
        """
        Mark tool execution as complete

        Args:
            execution_id: Execution identifier
            result: Tool execution result
            success: Whether execution was successful
            error_message: Error message if failed

        Returns:
            Completed ToolExecution object
        """
        if execution_id not in self.active_executions:
            logger.warning(f"Attempted to complete unknown execution: {execution_id}")
            return None

        execution = self.active_executions[execution_id]
        execution.result = result
        execution.success = success
        execution.execution_time_ms = (
            datetime.utcnow() - execution.timestamp
        ).total_seconds() * 1000

        # Log execution
        level = "INFO" if success else "ERROR"
        message = f"Tool '{execution.tool_name}' completed {'successfully' if success else 'failed'}"

        metadata = execution.to_dict()
        if error_message:
            metadata["error"] = error_message

        self._log_execution(level, message, metadata)

        # Clean up active executions
        del self.active_executions[execution_id]

        return execution

    def _log_execution(
        self, level: str, message: str, metadata: Dict[str, Any]
    ) -> None:
        """Create database log entry for tool execution with instance tracking

        Args:
            level: Log level
            message: Log message
            metadata: Dict with session_id, session_instance_id, task_id, etc.
        """
        log_entry = LogEntry(
            session_id=metadata.get("session_id"),
            session_instance_id=metadata.get(
                "session_instance_id"
            ),  # ✅ Critical for isolation
            task_id=metadata.get("task_id"),
            level=level,
            message=message,
            log_metadata=json.dumps(metadata),
        )
        self.db.add(log_entry)
        self.db.commit()

    def track(
        self,
        execution_id: str,
        tool_name: str,
        params: Dict[str, Any],
        result: Any,
        success: bool,
        session_id: int,
        task_id: Optional[int] = None,
        error_message: Optional[str] = None,
        session_instance_id: Optional[str] = None,  # NEW: For isolation
    ) -> ToolExecution:
        """
        Track a complete tool execution (start + complete in one call)

        Args:
            execution_id: Unique execution identifier
            tool_name: Name of the tool
            params: Tool parameters
            result: Tool execution result
            success: Whether execution was successful
            session_id: Session ID
            task_id: Optional task ID
            error_message: Error message if failed
            session_instance_id: Instance UUID for log isolation (NEW)

        Returns:
            Completed ToolExecution object
        """
        self.start_execution(
            execution_id, tool_name, params, session_id, task_id, session_instance_id
        )
        return self.complete_execution(execution_id, result, success, error_message)

    def get_execution_history(
        self,
        session_id: int,
        task_id: Optional[int] = None,
        limit: int = 50,
        tool_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get execution history for a session

        Args:
            session_id: Session ID
            task_id: Optional task filter
            limit: Maximum results to return
            tool_name: Optional tool name filter

        Returns:
            List of execution records
        """
        query = self.db.query(LogEntry).filter(
            LogEntry.session_id == session_id, LogEntry.level.in_(["INFO", "ERROR"])
        )

        if task_id:
            query = query.filter(LogEntry.task_id == task_id)

        if tool_name:
            # Filter logs that contain tool execution metadata
            query = query.filter(LogEntry.message.like(f"%Tool '{tool_name}'%"))

        logs = query.order_by(LogEntry.created_at.desc()).limit(limit).all()

        executions = []
        for log in logs:
            try:
                metadata = json.loads(log.metadata) if log.metadata else {}
                if "tool_name" in metadata:
                    executions.append(metadata)
            except json.JSONDecodeError:
                continue

        return executions

    def get_tool_statistics(self, session_id: int, days: int = 7) -> Dict[str, Any]:
        """
        Get statistics about tool usage

        Args:
            session_id: Session ID
            days: Number of days to analyze

        Returns:
            Statistics dictionary
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        query = self.db.query(LogEntry).filter(
            LogEntry.session_id == session_id,
            LogEntry.created_at >= cutoff_date,
            LogEntry.level.in_(["INFO", "ERROR"]),
        )

        logs = query.all()

        # Analyze tool usage
        tool_counts: Dict[str, int] = {}
        tool_success: Dict[str, int] = {}
        tool_failures: Dict[str, int] = {}
        execution_times: Dict[str, List[float]] = {}

        for log in logs:
            try:
                metadata = json.loads(log.metadata) if log.metadata else {}
                if "tool_name" not in metadata:
                    continue

                tool_name = metadata["tool_name"]
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

                if metadata.get("success"):
                    tool_success[tool_name] = tool_success.get(tool_name, 0) + 1
                else:
                    tool_failures[tool_name] = tool_failures.get(tool_name, 0) + 1

                if "execution_time_ms" in metadata:
                    if tool_name not in execution_times:
                        execution_times[tool_name] = []
                    execution_times[tool_name].append(metadata["execution_time_ms"])

            except (json.JSONDecodeError, KeyError):
                continue

        # Calculate averages
        avg_times = {}
        for tool, times in execution_times.items():
            if times:
                avg_times[tool] = sum(times) / len(times)

        return {
            "period_days": days,
            "total_executions": sum(tool_counts.values()),
            "total_success": sum(tool_success.values()),
            "total_failures": sum(tool_failures.values()),
            "tool_counts": tool_counts,
            "success_rates": {
                tool: (success / tool_counts.get(tool, 1)) * 100
                for tool, success in tool_success.items()
            },
            "avg_execution_times_ms": avg_times,
        }

    def get_active_executions(self, session_id: int) -> List[Dict[str, Any]]:
        """Get currently active (running) tool executions"""
        active = []
        for exec_id, execution in self.active_executions.items():
            if execution.session_id == session_id:
                active.append({"execution_id": exec_id, **execution.to_dict()})
        return active

    def cleanup_old_executions(self, max_age_hours: int = 24) -> int:
        """
        Clean up old execution records from active_executions

        Args:
            max_age_hours: Maximum age in hours

        Returns:
            Number of executions cleaned up
        """
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        cleaned = 0

        execution_ids = [
            exec_id
            for exec_id, exec_obj in self.active_executions.items()
            if exec_obj.timestamp < cutoff
        ]

        for exec_id in execution_ids:
            del self.active_executions[exec_id]
            cleaned += 1

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old executions")

        return cleaned
