"""Resume Session Service

Provides full resume capability for sessions with state persistence.
Handles pausing, resuming, and checkpoint management.
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from app.models import Session as SessionModel, SessionState, TaskCheckpoint
from app.services.openclaw_service import OpenClawSessionService

logger = logging.getLogger(__name__)


class ResumeError(Exception):
    """Custom exception for resume operations"""

    pass


class ResumeSessionService:
    """Service for managing session pause/resume functionality"""

    def __init__(self, db: Session, session_id: int):
        self.db = db
        self.session_id = session_id
        self.session_model = (
            db.query(SessionModel).filter(SessionModel.id == session_id).first()
        )

        if not self.session_model:
            raise ResumeError(f"Session {session_id} not found")

    def get_or_create_state(self) -> SessionState:
        """Get existing state or create new one"""
        state = (
            self.db.query(SessionState)
            .filter(SessionState.session_id == self.session_id)
            .first()
        )

        if not state:
            state = SessionState(
                session_id=self.session_id,
                project_id=self.session_model.project_id,
                current_step=0,
                total_steps=0,
                plan="[]",
                execution_results="[]",
                debug_attempts="[]",
                changed_files="[]",
            )
            self.db.add(state)
            self.db.commit()

        return state

    def save_state(
        self,
        current_step: int,
        total_steps: int,
        plan: List[Dict],
        execution_results: List[Dict],
        debug_attempts: List[Dict],
        changed_files: List[str],
    ) -> bool:
        """Save current session state to database"""
        try:
            state = self.get_or_create_state()

            state.current_step = current_step
            state.total_steps = total_steps
            state.plan = json.dumps(plan) if plan else "[]"
            state.execution_results = json.dumps(execution_results)
            state.debug_attempts = json.dumps(debug_attempts)
            state.changed_files = json.dumps(changed_files)

            self.db.commit()
            logger.info(
                f"State saved for session {self.session_id}: step {current_step}/{total_steps}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to save state: {str(e)}")
            self.db.rollback()
            raise ResumeError(f"Failed to save state: {str(e)}")

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load saved session state from database"""
        try:
            state = (
                self.db.query(SessionState)
                .filter(SessionState.session_id == self.session_id)
                .first()
            )

            if not state:
                logger.info(f"No saved state found for session {self.session_id}")
                return None

            # Parse JSON fields
            plan = json.loads(state.plan) if state.plan else []
            execution_results = (
                json.loads(state.execution_results) if state.execution_results else []
            )
            debug_attempts = (
                json.loads(state.debug_attempts) if state.debug_attempts else []
            )
            changed_files = (
                json.loads(state.changed_files) if state.changed_files else []
            )

            result = {
                "current_step": state.current_step,
                "total_steps": state.total_steps,
                "plan": plan,
                "execution_results": execution_results,
                "debug_attempts": debug_attempts,
                "changed_files": changed_files,
                "is_resumable": state.total_steps > 0
                and state.current_step < state.total_steps,
            }

            logger.info(
                f"State loaded for session {self.session_id}: step {state.current_step}/{state.total_steps}"
            )
            return result

        except Exception as e:
            logger.error(f"Failed to load state: {str(e)}")
            raise ResumeError(f"Failed to load state: {str(e)}")

    def pause_session(self, reason: Optional[str] = None) -> Dict[str, Any]:
        """Pause current session and save state"""
        try:
            # Update session status
            self.session_model.status = "paused"
            self.session_model.paused_at = datetime.utcnow()

            # Save checkpoint before pausing
            if self.session_model.is_active:
                # Try to get current state from OpenClawService if running
                # This would be called by the service during execution
                pass

            self.db.commit()

            logger.info(
                f"Session {self.session_id} paused. Reason: {reason or 'User request'}"
            )

            return {
                "success": True,
                "session_id": self.session_id,
                "status": "paused",
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Failed to pause session: {str(e)}")
            self.db.rollback()
            raise ResumeError(f"Failed to pause session: {str(e)}")

    def resume_session(self, start_from_step: Optional[int] = None) -> Dict[str, Any]:
        """Resume session from saved state"""
        try:
            # Load saved state
            state_data = self.load_state()

            if not state_data or not state_data["is_resumable"]:
                raise ResumeError("No valid state to resume from. Starting fresh.")

            # Determine start step
            if (
                start_from_step is not None
                and 0 <= start_from_step < state_data["total_steps"]
            ):
                current_step = start_from_step
                logger.info(f"Resuming from specified step: {current_step}")
            else:
                current_step = state_data["current_step"]
                logger.info(f"Resuming from last checkpoint: {current_step}")

            # Update session status
            self.session_model.status = "running"
            self.session_model.is_active = True
            self.session_model.resumed_at = datetime.utcnow()

            # Clear paused timestamp if set
            if self.session_model.paused_at:
                self.session_model.paused_at = None

            self.db.commit()

            result = {
                "success": True,
                "session_id": self.session_id,
                "status": "running",
                "resumed_from_step": current_step,
                "total_steps": state_data["total_steps"],
                "plan_length": len(state_data["plan"]),
                "previous_errors": len(state_data.get("debug_attempts", [])),
                "changed_files_count": len(state_data.get("changed_files", [])),
            }

            logger.info(f"Session {self.session_id} resumed from step {current_step}")

            return result

        except ResumeError:
            raise
        except Exception as e:
            logger.error(f"Failed to resume session: {str(e)}")
            self.db.rollback()
            raise ResumeError(f"Failed to resume session: {str(e)}")

    def create_checkpoint(
        self,
        checkpoint_type: str = "after",
        step_number: Optional[int] = None,
        description: Optional[str] = None,
        state_snapshot: Optional[Dict] = None,
    ) -> TaskCheckpoint:
        """Create a checkpoint for task resumption"""
        try:
            checkpoint = TaskCheckpoint(
                session_id=self.session_id,
                checkpoint_type=checkpoint_type,
                step_number=step_number,
                description=description or f"Checkpoint: {checkpoint_type}",
                state_snapshot=json.dumps(state_snapshot) if state_snapshot else None,
                logs_snapshot="[]",  # Will be populated separately
                error_info=None,
            )

            self.db.add(checkpoint)
            self.db.commit()

            logger.info(
                f"Checkpoint created: type={checkpoint_type}, step={step_number}"
            )

            return checkpoint

        except Exception as e:
            logger.error(f"Failed to create checkpoint: {str(e)}")
            self.db.rollback()
            raise ResumeError(f"Failed to create checkpoint: {str(e)}")

    def get_checkpoints(self, session_id: Optional[int] = None) -> List[TaskCheckpoint]:
        """Get all checkpoints for a session"""
        try:
            query = self.db.query(TaskCheckpoint)

            if session_id:
                query = query.filter(TaskCheckpoint.session_id == session_id)
            else:
                query = query.filter(TaskCheckpoint.session_id == self.session_id)

            checkpoints = query.order_by(TaskCheckpoint.created_at.desc()).all()

            logger.info(f"Retrieved {len(checkpoints)} checkpoints")

            return checkpoints

        except Exception as e:
            logger.error(f"Failed to get checkpoints: {str(e)}")
            raise ResumeError(f"Failed to get checkpoints: {str(e)}")

    def retry_failed_step(self, step_number: int) -> Dict[str, Any]:
        """Retry a specific failed step"""
        try:
            # Verify step exists and is within bounds
            state_data = self.load_state()

            if not state_data or step_number >= state_data["total_steps"]:
                raise ResumeError(f"Invalid step number: {step_number}")

            # Update session to indicate retry mode
            self.session_model.status = "running"
            self.db.commit()

            result = {
                "success": True,
                "session_id": self.session_id,
                "retry_step": step_number,
                "total_steps": state_data["total_steps"],
                "message": f"Ready to retry step {step_number + 1}",
            }

            logger.info(f"Retry initiated for step {step_number}")

            return result

        except ResumeError:
            raise
        except Exception as e:
            logger.error(f"Failed to prepare retry: {str(e)}")
            self.db.rollback()
            raise ResumeError(f"Failed to prepare retry: {str(e)}")

    def get_resume_summary(self) -> Dict[str, Any]:
        """Get summary of what can be resumed"""
        try:
            state_data = self.load_state()

            if not state_data or not state_data["is_resumable"]:
                return {"can_resume": False, "reason": "No valid state to resume from"}

            # Count completed and failed steps
            execution_results = state_data.get("execution_results", [])
            debug_attempts = state_data.get("debug_attempts", [])

            completed_steps = len(
                [r for r in execution_results if r.get("status") == "success"]
            )
            failed_steps = len(debug_attempts)

            result = {
                "can_resume": True,
                "current_step": state_data["current_step"],
                "total_steps": state_data["total_steps"],
                "completed_steps": completed_steps,
                "failed_steps": failed_steps,
                "remaining_steps": state_data["total_steps"]
                - state_data["current_step"],
                "changed_files_count": len(state_data.get("changed_files", [])),
                "debug_history_length": len(debug_attempts),
                "estimated_time_remaining": (
                    state_data["total_steps"] - state_data["current_step"]
                )
                * 180,  # ~3min per step
            }

            return result

        except Exception as e:
            logger.error(f"Failed to get resume summary: {str(e)}")
            return {"can_resume": False, "reason": f"Error loading state: {str(e)}"}
