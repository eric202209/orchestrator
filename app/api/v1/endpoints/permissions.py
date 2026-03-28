"""Permission API endpoints

Provides endpoints for permission approval workflow.
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Project
from app.services.permission_service import (
    PermissionApprovalService,
    PermissionStatus,
)
from app.services.project_isolation_service import ProjectIsolationService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/permissions/request")
async def create_permission_request(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Create a new permission request

    Args:
        request: Permission request data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created permission request
    """
    try:
        data = request

        permission_service = PermissionApprovalService(db)

        permission = permission_service.create_permission_request(
            project_id=data["project_id"],
            session_id=data.get("session_id"),
            task_id=data.get("task_id"),
            operation_type=data["operation_type"],
            target_path=data.get("target_path"),
            command=data.get("command"),
            description=data.get("description"),
            expires_in_minutes=data.get("expires_in_minutes", 30),
        )

        return {
            "id": permission.id,
            "project_id": permission.project_id,
            "session_id": permission.session_id,
            "task_id": permission.task_id,
            "operation_type": permission.operation_type,
            "target_path": permission.target_path,
            "command": permission.command,
            "description": permission.description,
            "status": permission.status,
            "created_at": permission.created_at.isoformat(),
            "expires_at": (
                permission.expires_at.isoformat() if permission.expires_at else None
            ),
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.error(f"Failed to create permission request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/permissions/{request_id}/approve")
async def approve_permission(
    request_id: int,
    request_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Approve a permission request

    Args:
        request_id: Permission request ID
        request_data: Approval data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated permission request
    """
    try:
        permission_service = PermissionApprovalService(db)

        auto_approve = request_data.get("auto_approve_same", False)

        permission = permission_service.approve_permission(
            request_id=request_id,
            approved_by=(
                current_user.email if hasattr(current_user, "email") else "user"
            ),
            auto_approve_same=auto_approve,
        )

        return {
            "success": True,
            "request_id": permission.id,
            "status": permission.status,
            "approved_by": permission.approved_by,
            "approved_at": (
                permission.approved_at.isoformat() if permission.approved_at else None
            ),
            "auto_approve_same": auto_approve,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve permission: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/permissions/{request_id}/deny")
async def deny_permission(
    request_id: int,
    request_data: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Deny a permission request

    Args:
        request_id: Permission request ID
        request_data: Denial data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated permission request
    """
    try:
        permission_service = PermissionApprovalService(db)

        reason = request_data.get("reason", "User denied")

        permission = permission_service.deny_permission(
            request_id=request_id,
            reason=reason,
        )

        return {
            "success": True,
            "request_id": permission.id,
            "status": permission.status,
            "denied_reason": permission.denied_reason,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to deny permission: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/permissions/pending")
async def get_pending_permissions(
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get pending permission requests

    Args:
        project_id: Filter by project (optional)
        session_id: Filter by session (optional)
        limit: Maximum results
        db: Database session
        current_user: Current authenticated user

    Returns:
        List of pending permissions
    """
    try:
        permission_service = PermissionApprovalService(db)

        permissions = permission_service.get_pending_permissions(
            project_id=project_id,
            session_id=session_id,
            limit=limit,
        )

        return {
            "count": len(permissions),
            "permissions": [
                {
                    "id": p.id,
                    "project_id": p.project_id,
                    "session_id": p.session_id,
                    "task_id": p.task_id,
                    "operation_type": p.operation_type,
                    "target_path": p.target_path,
                    "command": p.command,
                    "description": p.description,
                    "status": p.status,
                    "created_at": p.created_at.isoformat(),
                    "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                }
                for p in permissions
            ],
        }

    except Exception as e:
        logger.error(f"Failed to get pending permissions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/permissions/history/{project_id}")
async def get_permission_history(
    project_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get permission history for a project

    Args:
        project_id: Project ID
        limit: Maximum results
        db: Database session
        current_user: Current authenticated user

    Returns:
        Permission history
    """
    try:
        permission_service = PermissionApprovalService(db)

        permissions = permission_service.get_permission_history(
            project_id=project_id,
            limit=limit,
        )

        return {
            "count": len(permissions),
            "permissions": [
                {
                    "id": p.id,
                    "operation_type": p.operation_type,
                    "target_path": p.target_path,
                    "command": p.command,
                    "description": p.description,
                    "status": p.status,
                    "approved_by": p.approved_by,
                    "denied_reason": p.denied_reason,
                    "created_at": p.created_at.isoformat(),
                    "approved_at": p.approved_at.isoformat() if p.approved_at else None,
                }
                for p in permissions
            ],
        }

    except Exception as e:
        logger.error(f"Failed to get permission history: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/permissions/cleanup")
async def cleanup_expired_permissions(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Clean up expired permission requests

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Cleanup result
    """
    try:
        permission_service = PermissionApprovalService(db)

        count = permission_service.cleanup_expired_permissions()

        return {
            "success": True,
            "cleaned_count": count,
            "message": f"Cleaned up {count} expired permission requests",
        }

    except Exception as e:
        logger.error(f"Failed to cleanup expired permissions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/permissions/check")
async def check_permission_required(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Check if an operation requires permission approval

    Args:
        request: Operation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Permission requirement check
    """
    try:
        permission_service = PermissionApprovalService(db)

        operation_type = request.get("operation_type")
        target_path = request.get("target_path")
        project_id = request.get("project_id")
        session_id = request.get("session_id")

        if not operation_type:
            raise HTTPException(status_code=422, detail="operation_type is required")

        requires_permission = permission_service.check_permission_required(
            operation_type, target_path
        )

        is_granted = False
        if requires_permission and project_id and session_id:
            is_granted = permission_service.is_permission_granted(
                project_id, operation_type, target_path or "", session_id
            )

        return {
            "requires_permission": requires_permission,
            "is_granted": is_granted,
            "operation_type": operation_type,
            "target_path": target_path,
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.error(f"Failed to check permission: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
