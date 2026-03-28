"""Permission Approval Service

Provides permission approval system for sensitive operations.
Implements hard blocks with user approval workflow.
"""

import json
import logging
from datetime import timedelta
from enum import Enum
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)


class PermissionOperationType(str, Enum):
    """Types of operations that require permission"""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SHELL_COMMAND = "shell_command"
    EXTERNAL_API = "external_api"
    INSTALL_DEPENDENCIES = "install_dependencies"
    EXECUTE_SCRIPT = "execute_script"
    MODIFY_SYSTEM = "modify_system"
    DEPLOY = "deploy"


class PermissionStatus(str, Enum):
    """Permission request status"""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class PermissionApprovalService:
    """Service for managing permission approval workflow"""

    # Operations that always require approval
    ALWAYS_APPROVE = [
        PermissionOperationType.SHELL_COMMAND,
        PermissionOperationType.EXTERNAL_API,
        PermissionOperationType.INSTALL_DEPENDENCIES,
        PermissionOperationType.EXECUTE_SCRIPT,
        PermissionOperationType.MODIFY_SYSTEM,
        PermissionOperationType.DEPLOY,
    ]

    # Operations that need approval only first time per file type
    FIRST_TIME_APPROVE = {
        PermissionOperationType.FILE_WRITE: {"py", "js", "ts", "json", "yaml", "yml"},
        PermissionOperationType.FILE_DELETE: set(),  # All file types
    }

    def __init__(self, db: Session):
        self.db = db

    def create_permission_request(
        self,
        project_id: int,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        operation_type: str = "",
        target_path: Optional[str] = None,
        command: Optional[str] = None,
        description: Optional[str] = None,
        expires_in_minutes: int = 30,
    ):
        """
        Create a new permission request

        Args:
            project_id: Project ID
            session_id: Session ID (optional)
            task_id: Task ID (optional)
            operation_type: Type of operation
            target_path: Target file path
            command: Shell command (if applicable)
            description: User-friendly description
            expires_in_minutes: Request expiration time

        Returns:
            Created PermissionRequest
        """
        from datetime import datetime

        expires_at = datetime.utcnow() + timedelta(minutes=expires_in_minutes)

        from app.models import PermissionRequest

        request = PermissionRequest(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            operation_type=operation_type,
            target_path=target_path,
            command=command,
            description=description
            or self._generate_description(operation_type, target_path, command),
            status=PermissionStatus.PENDING.value,
            expires_at=expires_at,
        )

        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)

        logger.info(
            f"Permission request created: {request.id}, "
            f"type={operation_type}, path={target_path}"
        )

        return request

    def _generate_description(
        self, operation_type: str, target_path: Optional[str], command: Optional[str]
    ) -> str:
        """Generate user-friendly description for permission request"""
        if operation_type == PermissionOperationType.FILE_WRITE:
            return f"Write to file: {target_path}"
        elif operation_type == PermissionOperationType.FILE_DELETE:
            return f"Delete file: {target_path}"
        elif operation_type == PermissionOperationType.SHELL_COMMAND:
            return (
                f"Execute command: {command[:100]}..."
                if command
                else "Execute shell command"
            )
        elif operation_type == PermissionOperationType.EXTERNAL_API:
            return "Make external API call"
        elif operation_type == PermissionOperationType.INSTALL_DEPENDENCIES:
            return "Install project dependencies"
        elif operation_type == PermissionOperationType.EXECUTE_SCRIPT:
            return f"Execute script: {target_path}"
        elif operation_type == PermissionOperationType.MODIFY_SYSTEM:
            return "Modify system configuration"
        elif operation_type == PermissionOperationType.DEPLOY:
            return "Deploy application"
        else:
            return f"Perform operation: {operation_type}"

    def approve_permission(
        self, request_id: int, approved_by: str, auto_approve_same: bool = False
    ):
        """
        Approve a permission request

        Args:
            request_id: Permission request ID
            approved_by: User ID or name who approved
            auto_approve_same: If True, create auto-approve rule for same operation

        Returns:
            Updated PermissionRequest
        """
        from app.models import PermissionRequest
        from datetime import datetime

        request = (
            self.db.query(PermissionRequest)
            .filter(PermissionRequest.id == request_id)
            .first()
        )

        if not request:
            raise ValueError(f"Permission request {request_id} not found")

        if request.status != PermissionStatus.PENDING.value:
            raise ValueError(f"Request already {request.status}")

        request.status = PermissionStatus.APPROVED.value
        request.approved_by = approved_by
        request.approved_at = datetime.utcnow()

        if auto_approve_same:
            # Create auto-approve rule (could be stored in separate table)
            logger.info(f"Auto-approve rule created for {request.operation_type}")

        self.db.commit()
        self.db.refresh(request)

        logger.info(f"Permission approved: {request_id}, approved_by={approved_by}")

        return request

    def deny_permission(self, request_id: int, reason: Optional[str] = None):
        """
        Deny a permission request

        Args:
            request_id: Permission request ID
            reason: Reason for denial

        Returns:
            Updated PermissionRequest
        """
        from app.models import PermissionRequest

        request = (
            self.db.query(PermissionRequest)
            .filter(PermissionRequest.id == request_id)
            .first()
        )

        if not request:
            raise ValueError(f"Permission request {request_id} not found")

        if request.status != PermissionStatus.PENDING.value:
            raise ValueError(f"Request already {request.status}")

        request.status = PermissionStatus.DENIED.value
        request.denied_reason = reason

        self.db.commit()
        self.db.refresh(request)

        logger.warning(f"Permission denied: {request_id}, reason={reason}")

        return request

    def check_permission_required(
        self, operation_type: str, target_path: Optional[str] = None
    ) -> bool:
        """
        Check if an operation requires permission approval

        Args:
            operation_type: Operation type
            target_path: Target path (optional)

        Returns:
            True if permission is required
        """
        # Operations that always require approval
        if operation_type in [ot.value for ot in self.ALWAYS_APPROVE]:
            return True

        # File write - first time per file type
        if operation_type == PermissionOperationType.FILE_WRITE.value:
            if not target_path:
                return True
            ext = target_path.split(".")[-1].lower()
            if ext in self.FIRST_TIME_APPROVE.get(
                PermissionOperationType.FILE_WRITE, set()
            ):
                return True

        return False

    def is_permission_granted(
        self,
        project_id: int,
        operation_type: str,
        target_path: str,
        session_id: Optional[int] = None,
    ) -> bool:
        """
        Check if permission has been granted for this operation

        Args:
            project_id: Project ID
            operation_type: Operation type
            target_path: Target path
            session_id: Session ID (optional)

        Returns:
            True if permission is granted
        """
        from app.models import PermissionRequest
        from datetime import datetime, timedelta

        # Check for recent approved permissions
        recent_cutoff = datetime.utcnow() - timedelta(minutes=60)  # Check last hour

        query = self.db.query(PermissionRequest).filter(
            PermissionRequest.project_id == project_id,
            PermissionRequest.operation_type == operation_type,
            PermissionRequest.target_path == target_path,
            PermissionRequest.status == PermissionStatus.APPROVED.value,
            PermissionRequest.approved_at >= recent_cutoff,
        )

        if session_id:
            query = query.filter(PermissionRequest.session_id == session_id)

        return query.count() > 0

    def get_pending_permissions(
        self,
        project_id: Optional[int] = None,
        session_id: Optional[int] = None,
        limit: int = 50,
    ) -> List:
        """
        Get pending permission requests

        Args:
            project_id: Filter by project (optional)
            session_id: Filter by session (optional)
            limit: Maximum results

        Returns:
            List of pending PermissionRequest
        """
        from app.models import PermissionRequest

        query = self.db.query(PermissionRequest).filter(
            PermissionRequest.status == PermissionStatus.PENDING.value,
        )

        if project_id:
            query = query.filter(PermissionRequest.project_id == project_id)

        if session_id:
            query = query.filter(PermissionRequest.session_id == session_id)

        # Sort by created_at descending
        query = query.order_by(PermissionRequest.created_at.desc())

        return query.limit(limit).all()

    def cleanup_expired_permissions(self) -> int:
        """
        Clean up expired permission requests

        Returns:
            Number of expired requests removed
        """
        from app.models import PermissionRequest
        from datetime import datetime

        expired_count = (
            self.db.query(PermissionRequest)
            .filter(
                PermissionRequest.status == PermissionStatus.PENDING.value,
                PermissionRequest.expires_at < datetime.utcnow(),
            )
            .update(
                {"status": PermissionStatus.EXPIRED.value},
                synchronize_session=False,
            )
        )

        self.db.commit()
        logger.info(f"Cleaned up {expired_count} expired permission requests")

        return expired_count

    def get_permission_history(self, project_id: int, limit: int = 100) -> List:
        """
        Get permission history for a project

        Args:
            project_id: Project ID
            limit: Maximum results

        Returns:
            List of PermissionRequest (all statuses)
        """
        from app.models import PermissionRequest

        return (
            self.db.query(PermissionRequest)
            .filter(PermissionRequest.project_id == project_id)
            .order_by(PermissionRequest.created_at.desc())
            .limit(limit)
            .all()
        )
