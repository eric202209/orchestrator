"""Context Preservation API Endpoints

Provides endpoints for session state, conversation history, and task checkpoints.
"""

import logging
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional

from app.database import get_db
from app.dependencies import get_current_user
from app.services.context_service import ContextPreservationService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/context/snapshot")
async def save_session_state(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Save current session state"""
    try:
        data = request
        context_service = ContextPreservationService(db)

        state = context_service.save_session_state(
            session_id=data["session_id"],
            project_id=data["project_id"],
            current_step=data.get("current_step", 0),
            total_steps=data.get("total_steps", 0),
            plan=data.get("plan"),
            execution_results=data.get("execution_results"),
            debug_attempts=data.get("debug_attempts"),
            changed_files=data.get("changed_files"),
        )

        return {
            "success": True,
            "state_id": state.id,
            "current_step": state.current_step,
            "total_steps": state.total_steps,
            "state_version": state.state_version,
            "saved_at": (
                state.last_snapshot_at.isoformat() if state.last_snapshot_at else None
            ),
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.error(f"Failed to save session state: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/state/{session_id}")
async def get_session_state(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Get session state"""
    try:
        context_service = ContextPreservationService(db)
        state = context_service.load_session_state(session_id)

        if not state:
            return {"exists": False}

        return {
            "exists": True,
            "session_id": state.session_id,
            "project_id": state.project_id,
            "current_step": state.current_step,
            "total_steps": state.total_steps,
            "completion_percent": (
                (state.current_step / state.total_steps * 100)
                if state.total_steps > 0
                else 0
            ),
            "state_version": state.state_version,
            "last_snapshot_at": (
                state.last_snapshot_at.isoformat() if state.last_snapshot_at else None
            ),
        }

    except Exception as e:
        logger.error(f"Failed to get session state: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/progress/{session_id}")
async def get_state_progress(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Get state progress summary"""
    try:
        context_service = ContextPreservationService(db)
        progress = context_service.get_state_progress(session_id)

        if not progress:
            raise HTTPException(status_code=404, detail="Session state not found")

        return progress

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get state progress: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/context/conversation")
async def add_conversation_message(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Add message to conversation history"""
    try:
        data = request
        context_service = ContextPreservationService(db)

        message = context_service.add_conversation_message(
            session_id=data["session_id"],
            role=data["role"],
            content=data["content"],
            metadata=data.get("metadata"),
        )

        return {
            "success": True,
            "message_id": message.id,
            "role": message.role,
            "created_at": message.created_at.isoformat(),
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.error(f"Failed to add conversation message: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/conversation/{session_id}")
async def get_conversation_history(
    session_id: int,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Get conversation history"""
    try:
        context_service = ContextPreservationService(db)
        messages = context_service.get_conversation_history(session_id, limit)

        return {
            "count": len(messages),
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "metadata": msg.metadata,
                    "created_at": msg.created_at.isoformat(),
                }
                for msg in messages
            ],
        }

    except Exception as e:
        logger.error(f"Failed to get conversation history: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/summary/{session_id}")
async def get_context_summary(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Get summarized context from conversation"""
    try:
        context_service = ContextPreservationService(db)
        summary = context_service.get_context_summary(session_id)

        return {
            "session_id": session_id,
            "context": summary,
            "length": len(summary),
        }

    except Exception as e:
        logger.error(f"Failed to get context summary: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/context/checkpoint")
async def create_checkpoint(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Create task checkpoint"""
    try:
        data = request
        context_service = ContextPreservationService(db)

        checkpoint = context_service.create_checkpoint(
            task_id=data["task_id"],
            session_id=data.get("session_id"),
            checkpoint_type=data["checkpoint_type"],
            step_number=data.get("step_number"),
            description=data.get("description"),
            state_snapshot=data.get("state_snapshot"),
            logs_snapshot=data.get("logs_snapshot"),
            error_info=data.get("error_info"),
        )

        return {
            "success": True,
            "checkpoint_id": checkpoint.id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "step_number": checkpoint.step_number,
            "created_at": checkpoint.created_at.isoformat(),
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except Exception as e:
        logger.error(f"Failed to create checkpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/context/checkpoints/{task_id}")
async def get_task_checkpoints(
    task_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Get checkpoints for a task"""
    try:
        context_service = ContextPreservationService(db)
        checkpoints = context_service.get_checkpoints(task_id)

        return {
            "count": len(checkpoints),
            "checkpoints": [
                {
                    "id": c.id,
                    "checkpoint_type": c.checkpoint_type,
                    "step_number": c.step_number,
                    "description": c.description,
                    "created_at": c.created_at.isoformat(),
                }
                for c in checkpoints
            ],
        }

    except Exception as e:
        logger.error(f"Failed to get task checkpoints: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/context/resume/{task_id}")
async def resume_from_checkpoint(
    task_id: int,
    checkpoint_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Resume from checkpoint"""
    try:
        context_service = ContextPreservationService(db)
        resume_data = context_service.resume_from_checkpoint(task_id, checkpoint_id)

        if not resume_data:
            raise HTTPException(status_code=404, detail="No checkpoints found for task")

        return {
            "success": True,
            "resume_data": resume_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resume from checkpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/context/export/{session_id}")
async def export_session_context(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Export full session context"""
    try:
        context_service = ContextPreservationService(db)
        context_data = context_service.export_session_context(session_id)

        if not context_data:
            raise HTTPException(status_code=404, detail="Session state not found")

        return {
            "success": True,
            "context": context_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export session context: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/context/import")
async def import_session_context(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Import session context"""
    try:
        data = request
        context_service = ContextPreservationService(db)

        success = context_service.import_session_context(
            session_id=data["session_id"],
            context_data=data["context_data"],
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to import context")

        return {
            "success": True,
            "session_id": data["session_id"],
        }

    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to import session context: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
