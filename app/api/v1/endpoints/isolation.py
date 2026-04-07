"""Project Isolation API endpoints

Provides endpoints for path validation and safety checks.
"""

import json
import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Project
from app.services.project_isolation_service import (
    ProjectIsolationService,
    ProjectIsolationError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/projects/{project_id}/isolation/status")
async def get_project_isolation_status(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get project isolation status and workspace information

    Args:
        project_id: Project ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Project isolation status
    """
    try:
        project = db.query(Project).filter(Project.id == project_id).first()

        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        isolation_service = ProjectIsolationService(db)

        try:
            project_root = isolation_service.get_project_root(project_id)
            status = "active"
            message = f"Isolation boundary: {project_root}"
        except ProjectIsolationError as e:
            status = "warning"
            message = str(e)

        return {
            "project_id": project_id,
            "project_name": project.name,
            "workspace_path": project.workspace_path or "not set",
            "project_root": str(project_root) if "project_root" in locals() else None,
            "status": status,
            "message": message,
            "isolation_enabled": True,
        }

    except Exception as e:
        logger.error(f"Failed to get isolation status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/isolation/validate")
async def validate_project_path(
    project_id: int,
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Validate a path is within project boundaries

    Args:
        project_id: Project ID
        request: Path to validate
        db: Database session
        current_user: Current authenticated user

    Returns:
        Validation result
    """
    try:
        path = request.get("path", "")

        if not path:
            raise HTTPException(status_code=422, detail="Path is required")

        isolation_service = ProjectIsolationService(db)

        result = isolation_service.validate_path(project_id, path)

        return {
            "valid": result["valid"],
            "requested_path": result["requested_path"],
            "resolved_path": result["resolved_path"],
            "project_root": result["project_root"],
            "is_within_bounds": result["is_within_bounds"],
            "message": result["message"],
        }

    except ProjectIsolationError as e:
        # Return 400 for validation errors (not 500)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Path validation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/isolation/safety-prompt")
async def get_safety_prompt(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get the safety prompt for a project

    Args:
        project_id: Project ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Safety prompt string
    """
    try:
        isolation_service = ProjectIsolationService(db)
        prompt = isolation_service.get_safety_prompt(project_id)

        return {
            "project_id": project_id,
            "safety_prompt": prompt,
            "prompt_length": len(prompt),
        }

    except Exception as e:
        logger.error(f"Failed to get safety prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/isolation/update-workspace")
async def update_project_workspace(
    project_id: int,
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Update the workspace path for a project

    Args:
        project_id: Project ID
        request: New workspace path
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated project info
    """
    try:
        workspace_path = request.get("workspace_path")

        if not workspace_path:
            raise HTTPException(status_code=422, detail="Workspace path is required")

        # Validate the path exists
        from pathlib import Path

        base_path = Path("/root/.openclaw/workspace/vault")
        full_path = (base_path / workspace_path).resolve()

        if not full_path.exists():
            raise HTTPException(
                status_code=400, detail=f"Workspace path does not exist: {full_path}"
            )

        if not full_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"Workspace path is not a directory: {full_path}",
            )

        # Update the project
        project = db.query(Project).filter(Project.id == project_id).first()

        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        project.workspace_path = workspace_path
        db.commit()
        db.refresh(project)

        return {
            "success": True,
            "project_id": project_id,
            "workspace_path": project.workspace_path,
            "full_path": str(full_path),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update workspace path: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/isolation/test-path")
async def test_path_safely(
    project_id: int,
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Safely test path validation without executing code

    This endpoint allows AI agents to verify isolation is working
    by testing paths without generating potentially dangerous code.

    Args:
        project_id: Project ID
        request: Path to test
        db: Database session
        current_user: Current authenticated user

    Returns:
        Test result showing if path would be blocked
    """
    try:
        path = request.get("path", "")

        if not path:
            raise HTTPException(status_code=422, detail="Path is required")

        isolation_service = ProjectIsolationService(db)
        result = isolation_service.safe_test_path(project_id, path)

        return result

    except Exception as e:
        logger.error(f"Path test failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
