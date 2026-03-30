"""Resume Session API Endpoints

Provides pause, resume, and checkpoint management endpoints.
"""

import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.database import get_db
from app.models import Session as SessionModel
from app.services.resume_service import ResumeSessionService, ResumeError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Resume Operations"])


# Request/Response Models
class PauseRequest(BaseModel):
    reason: Optional[str] = None


class ResumeRequest(BaseModel):
    start_from_step: Optional[int] = None  # If not provided, resume from last step


class RetryStepRequest(BaseModel):
    step_number: int  # 0-indexed step number to retry


class PauseResponse(BaseModel):
    success: bool
    session_id: int
    status: str
    reason: Optional[str] = None
    timestamp: str


class ResumeResponse(BaseModel):
    success: bool
    session_id: int
    status: str
    resumed_from_step: int
    total_steps: int
    plan_length: int
    previous_errors: int
    changed_files_count: int


class RetryStepResponse(BaseModel):
    success: bool
    session_id: int
    retry_step: int
    total_steps: int
    message: str


class ResumeSummaryResponse(BaseModel):
    can_resume: bool
    reason: Optional[str] = None
    current_step: Optional[int] = None
    total_steps: Optional[int] = None
    completed_steps: Optional[int] = None
    failed_steps: Optional[int] = None
    remaining_steps: Optional[int] = None
    changed_files_count: Optional[int] = None
    debug_history_length: Optional[int] = None
    estimated_time_remaining: Optional[int] = None


class Checkpoint(BaseModel):
    id: int
    checkpoint_type: str
    step_number: Optional[int] = None
    description: Optional[str] = None
    created_at: str


@router.post("/pause", response_model=PauseResponse)
async def pause_session(
    request: PauseRequest, session_id: int, db: Session = Depends(get_db)
):
    """Pause a running session and save current state"""
    try:
        service = ResumeSessionService(db, session_id)

        # Verify session exists and is running
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        if session.status not in ["running", "paused"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot pause session with status: {session.status}",
            )

        # Pause the session
        result = service.pause_session(reason=request.reason)

        return PauseResponse(**result)

    except ResumeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except Exception as e:
        logger.error(f"Pause session error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to pause session: {str(e)}",
        )


@router.post("/resume", response_model=ResumeResponse)
async def resume_session(
    request: ResumeRequest, session_id: int, db: Session = Depends(get_db)
):
    """Resume a paused session from saved state"""
    try:
        service = ResumeSessionService(db, session_id)

        # Verify session exists and is paused
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        if session.status != "paused":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resume session with status: {session.status}. Session must be paused.",
            )

        # Resume the session
        result = service.resume_session(start_from_step=request.start_from_step)

        return ResumeResponse(**result)

    except ResumeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except Exception as e:
        logger.error(f"Resume session error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume session: {str(e)}",
        )


@router.post("/retry-step", response_model=RetryStepResponse)
async def retry_failed_step(
    request: RetryStepRequest, session_id: int, db: Session = Depends(get_db)
):
    """Retry a specific failed step"""
    try:
        service = ResumeSessionService(db, session_id)

        # Verify session exists
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        # Prepare retry
        result = service.retry_failed_step(request.step_number)

        return RetryStepResponse(**result)

    except ResumeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Retry step error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to prepare retry: {str(e)}",
        )


@router.get("/summary", response_model=ResumeSummaryResponse)
async def get_resume_summary(session_id: int, db: Session = Depends(get_db)):
    """Get summary of what can be resumed"""
    try:
        service = ResumeSessionService(db, session_id)

        # Verify session exists
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        # Get resume summary
        result = service.get_resume_summary()

        return ResumeSummaryResponse(**result)

    except Exception as e:
        logger.error(f"Get resume summary error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get resume summary: {str(e)}",
        )


@router.get("/checkpoints", response_model=List[Checkpoint])
async def list_checkpoints(session_id: int, db: Session = Depends(get_db)):
    """Get all checkpoints for a session"""
    try:
        service = ResumeSessionService(db, session_id)

        # Verify session exists
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        # Get checkpoints
        checkpoints = service.get_checkpoints(session_id=session_id)

        return [
            Checkpoint(
                id=cp.id,
                checkpoint_type=cp.checkpoint_type,
                step_number=cp.step_number,
                description=cp.description,
                created_at=cp.created_at.isoformat() if cp.created_at else None,
            )
            for cp in checkpoints
        ]

    except Exception as e:
        logger.error(f"List checkpoints error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list checkpoints: {str(e)}",
        )
