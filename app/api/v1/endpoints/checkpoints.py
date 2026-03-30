"""Checkpoint API Endpoints for OpenClaw Session State Management

Provides RESTful endpoints for managing session checkpoints:
- Save checkpoints manually
- List available checkpoints
- Load/restore from checkpoints
- Delete old checkpoints
- Auto-checkpoint on pause/resume
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import json
import logging

from app.database import get_db
from app.models import LogEntry
from app.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


class CheckpointRequest:
    """Pydantic model for checkpoint save request"""

    @staticmethod
    def create(
        session_id: int,
        checkpoint_name: str = "manual",
        context_data: dict = None,
        orchestration_state: dict = None,
        current_step_index: int = 0,
        step_results: list = None,
    ) -> dict:
        """Create checkpoint save request"""
        return {
            "session_id": session_id,
            "checkpoint_name": checkpoint_name,
            "context_data": context_data or {},
            "orchestration_state": orchestration_state or {},
            "current_step_index": current_step_index,
            "step_results": step_results or [],
        }


@router.post("/sessions/{session_id}/checkpoints", response_model=dict)
async def save_checkpoint(
    session_id: int,
    checkpoint_name: str = "manual",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Save current session state to a checkpoint

    This allows you to manually create checkpoints during execution.

    Args:
        session_id: Session ID to checkpoint
        checkpoint_name: Custom name for this checkpoint (e.g., 'before_debug', 'after_planning')

    Returns:
        Checkpoint metadata including path and timestamp
    """
    try:
        from app.services.checkpoint_service import CheckpointService, CheckpointError

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        # Get current context
        from app.services.openclaw_service import OpenClawSessionService

        openclaw_service = OpenClawSessionService(db, session_id)
        context_data = await openclaw_service.get_session_context()

        # Save checkpoint with minimal orchestration state (will be populated later)
        checkpoint_result = checkpoint_service.save_checkpoint(
            session_id=session_id,
            checkpoint_name=checkpoint_name,
            context_data=context_data,
            orchestration_state={},  # TODO: Track actual orchestration state
            current_step_index=0,  # TODO: Track actual step index
            step_results=[],  # TODO: Save completed steps
        )

        return {
            "success": True,
            "checkpoint_name": checkpoint_result["checkpoint_name"],
            "path": checkpoint_result["path"],
            "created_at": checkpoint_result["created_at"],
        }

    except CheckpointError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/sessions/{session_id}/checkpoints", response_model=List[dict])
async def list_checkpoints(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    List all available checkpoints for a session

    Returns checkpoint metadata including creation time and step information.

    Returns:
        List of checkpoint metadata (oldest first)
    """
    try:
        from app.services.checkpoint_service import CheckpointService

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        checkpoints = checkpoint_service.list_checkpoints(session_id)

        return checkpoints

    except Exception as e:
        logger.error(f"Failed to list checkpoints for session {session_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list checkpoints: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoints/load", response_model=dict)
async def load_checkpoint(
    session_id: int,
    checkpoint_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Load a specific checkpoint (without resuming execution)

    This is useful to inspect what state was saved at a particular point.

    Args:
        session_id: Session ID
        checkpoint_name: Specific checkpoint name (optional - loads latest if not specified)

    Returns:
        Full checkpoint data including context and orchestration state
    """
    try:
        from app.services.checkpoint_service import CheckpointService, CheckpointError

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        checkpoint_data = checkpoint_service.load_checkpoint(
            session_id=session_id, checkpoint_name=checkpoint_name
        )

        return {
            "success": True,
            "checkpoint_name": checkpoint_data.get("checkpoint_name"),
            "context": checkpoint_data.get("context", {}),
            "orchestration_state": checkpoint_data.get("orchestration_state", {}),
            "current_step_index": checkpoint_data.get("current_step_index"),
            "step_results_count": len(checkpoint_data.get("step_results", [])),
            "created_at": checkpoint_data.get("created_at"),
        }

    except CheckpointError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/sessions/{session_id}/checkpoints/delete", response_model=dict)
async def delete_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete a specific checkpoint

    Args:
        session_id: Session ID
        checkpoint_name: Checkpoint name to delete

    Returns:
        Deletion confirmation
    """
    try:
        from app.services.checkpoint_service import CheckpointService

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        deleted = checkpoint_service.delete_checkpoint(
            session_id=session_id, checkpoint_name=checkpoint_name
        )

        if not deleted:
            raise HTTPException(status_code=404, detail="Checkpoint not found")

        return {
            "success": True,
            "message": f"Checkpoint '{checkpoint_name}' deleted successfully",
        }

    except Exception as e:
        logger.error(f"Failed to delete checkpoint: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete checkpoint: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoints/cleanup", response_model=dict)
async def cleanup_old_checkpoints(
    session_id: int,
    keep_latest: int = 3,
    max_age_hours: int = 24,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Clean up old checkpoints, keeping only the most recent N

    Args:
        session_id: Session ID
        keep_latest: Number of most recent checkpoints to keep (default: 3)
        max_age_hours: Delete checkpoints older than this (hours) (default: 24)

    Returns:
        Cleanup statistics
    """
    try:
        from app.services.checkpoint_service import CheckpointService

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        result = checkpoint_service.cleanup_old_checkpoints(
            session_id=session_id, keep_latest=keep_latest, max_age_hours=max_age_hours
        )

        return {
            "success": True,
            "deleted_count": result.get("deleted", 0),
            "kept_count": result.get("kept", 0),
        }

    except Exception as e:
        logger.error(f"Failed to cleanup checkpoints: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to cleanup checkpoints: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoints/auto-save", response_model=dict)
async def auto_save_checkpoint(
    session_id: int,
    event_type: str = "pause",  # pause, error_recovery, manual
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Automatically save checkpoint based on an event

    This is used by the backend when:
    - User clicks Pause → saves 'paused' checkpoint
    - Error detected → saves 'error_recovery' checkpoint
    - Task completes → saves 'completed' checkpoint

    Args:
        session_id: Session ID
        event_type: Type of event triggering auto-save (pause, error_recovery, manual)

    Returns:
        Checkpoint metadata
    """
    try:
        from app.services.checkpoint_service import CheckpointService
        from app.services.openclaw_service import OpenClawSessionService

        # Verify session exists
        from app.models import Session as SessionModel

        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        checkpoint_service = CheckpointService(db)

        # Generate checkpoint name based on event type and timestamp
        from datetime import datetime

        checkpoint_name = f"{event_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        # Get current context
        openclaw_service = OpenClawSessionService(db, session_id)
        context_data = await openclaw_service.get_session_context()

        # Save checkpoint
        checkpoint_result = checkpoint_service.save_checkpoint(
            session_id=session_id,
            checkpoint_name=checkpoint_name,
            context_data=context_data,
            orchestration_state={},  # TODO: Track actual orchestration state
            current_step_index=0,  # TODO: Track actual step index
            step_results=[],  # TODO: Save completed steps
        )

        return {
            "success": True,
            "checkpoint_name": checkpoint_result["checkpoint_name"],
            "event_type": event_type,
            "path": checkpoint_result["path"],
            "created_at": checkpoint_result["created_at"],
        }

    except Exception as e:
        logger.error(f"Failed to auto-save checkpoint: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to save checkpoint: {str(e)}"
        )
