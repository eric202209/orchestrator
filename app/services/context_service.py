"""Context Preservation Service.

Provides session state persistence, task resumption, and conversation history.
This service intentionally follows the ORM model shape and avoids referencing
columns that are not defined there.
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session as DBSession
from sqlalchemy import func

from app.models import (
    SessionState,
    ConversationHistory,
    TaskCheckpoint,
)

logger = logging.getLogger(__name__)


class ContextPreservationService:
    """Service for managing context preservation"""

    # Snapshot intervals (in seconds)
    AUTO_SNAPSHOT_INTERVAL = 300  # 5 minutes
    MAX_CONVERSATION_MESSAGES = 100
    MAX_CONTEXT_SIZE = 50000  # characters

    def __init__(self, db: DBSession):
        self.db = db

    # Session State Management
    def save_session_state(
        self,
        session_id: int,
        project_id: int,
        current_step: int = 0,
        total_steps: int = 0,
        plan: Optional[List[Dict]] = None,
        execution_results: Optional[List[Dict]] = None,
        debug_attempts: Optional[List[Dict]] = None,
        changed_files: Optional[List[str]] = None,
    ) -> SessionState:
        """
        Save current session state

        Args:
            session_id: Session ID
            project_id: Project ID
            current_step: Current step number
            total_steps: Total steps in plan
            plan: Orchestration plan
            execution_results: Results of executed steps
            debug_attempts: Debug history
            changed_files: List of changed files

        Returns:
            Saved SessionState
        """
        # Get or create session state
        state = (
            self.db.query(SessionState)
            .filter(SessionState.session_id == session_id)
            .first()
        )

        if not state:
            state = SessionState(
                session_id=session_id,
                project_id=project_id,
                current_step=0,
                total_steps=0,
            )
            self.db.add(state)

        # Update state
        state.current_step = current_step
        state.total_steps = total_steps
        state.plan = json.dumps(plan) if plan else None
        state.execution_results = (
            json.dumps(execution_results) if execution_results else None
        )
        state.debug_attempts = json.dumps(debug_attempts) if debug_attempts else None
        state.changed_files = json.dumps(changed_files) if changed_files else None

        self.db.commit()
        self.db.refresh(state)

        logger.info(
            "Session state saved: session=%s, step=%s/%s",
            session_id,
            current_step,
            total_steps,
        )

        return state

    def load_session_state(self, session_id: int) -> Optional[SessionState]:
        """
        Load session state

        Args:
            session_id: Session ID

        Returns:
            SessionState or None
        """
        return (
            self.db.query(SessionState)
            .filter(SessionState.session_id == session_id)
            .first()
        )

    def get_state_progress(self, session_id: int) -> Optional[Dict[str, Any]]:
        """
        Get state progress summary

        Args:
            session_id: Session ID

        Returns:
            Progress summary dict
        """
        state = self.load_session_state(session_id)
        if not state:
            return None

        return {
            "current_step": state.current_step,
            "total_steps": state.total_steps,
            "completion_percent": (
                (state.current_step / state.total_steps * 100)
                if state.total_steps > 0
                else 0
            ),
            "has_debug_attempts": bool(state.debug_attempts),
            "has_changed_files": bool(state.changed_files),
        }

    # Conversation History Management
    def add_conversation_message(
        self,
        session_id: int,
        role: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> ConversationHistory:
        """
        Add message to conversation history

        Args:
            session_id: Session ID
            role: Message role (user/assistant/system)
            content: Message content
            metadata: Additional metadata

        Returns:
            Created ConversationHistory
        """
        history_count = (
            self.db.query(func.count(ConversationHistory.id))
            .filter(ConversationHistory.session_id == session_id)
            .scalar()
        ) or 0
        if history_count >= self.MAX_CONVERSATION_MESSAGES:
            oldest = (
                self.db.query(ConversationHistory)
                .filter(ConversationHistory.session_id == session_id)
                .order_by(ConversationHistory.id.asc())
                .first()
            )
        else:
            oldest = None
        if oldest:
            self.db.delete(oldest)
            self.db.commit()

        message = ConversationHistory(
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata,
        )

        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)

        logger.debug(f"Conversation message added: session={session_id}, role={role}")

        return message

    def get_conversation_history(
        self, session_id: int, limit: int = 50
    ) -> List[ConversationHistory]:
        """
        Get conversation history

        Args:
            session_id: Session ID
            limit: Maximum messages

        Returns:
            List of ConversationHistory
        """
        return (
            self.db.query(ConversationHistory)
            .filter(ConversationHistory.session_id == session_id)
            .order_by(ConversationHistory.id.desc())
            .limit(limit)
            .all()
        )

    def get_context_summary(self, session_id: int) -> str:
        """
        Get summarized context from conversation history

        Args:
            session_id: Session ID

        Returns:
            Summarized context string
        """
        messages = self.get_conversation_history(session_id, limit=20)

        # Build context string
        context_parts = []
        for msg in reversed(messages):
            if len(context_parts) > 0:
                context_parts.append("\n")
            context_parts.append(f"[{msg.role.upper()}]: {msg.content[:500]}")

        context = "\n".join(context_parts)

        # Truncate if too long
        if len(context) > self.MAX_CONTEXT_SIZE:
            context = context[: self.MAX_CONTEXT_SIZE] + "\n... (truncated)"

        return context

    # Task Checkpoint Management
    def create_checkpoint(
        self,
        task_id: int,
        session_id: Optional[int] = None,
        checkpoint_type: str = "before",
        step_number: Optional[int] = None,
        description: Optional[str] = None,
        state_snapshot: Optional[Dict] = None,
        logs_snapshot: Optional[List[Dict]] = None,
        error_info: Optional[Dict] = None,
    ) -> TaskCheckpoint:
        """
        Create task checkpoint

        Args:
            task_id: Task ID
            session_id: Session ID (optional)
            checkpoint_type: Type of checkpoint
            step_number: Step number
            description: Description
            state_snapshot: State snapshot
            logs_snapshot: Recent logs
            error_info: Error details

        Returns:
            Created TaskCheckpoint
        """
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            checkpoint_type=checkpoint_type,
            step_number=step_number,
            description=description,
            state_snapshot=json.dumps(state_snapshot) if state_snapshot else None,
            logs_snapshot=json.dumps(logs_snapshot) if logs_snapshot else None,
            error_info=json.dumps(error_info) if error_info else None,
        )

        self.db.add(checkpoint)
        self.db.commit()
        self.db.refresh(checkpoint)

        logger.info(f"Checkpoint created: task={task_id}, type={checkpoint_type}")

        return checkpoint

    def get_checkpoints(self, task_id: int) -> List[TaskCheckpoint]:
        """
        Get checkpoints for a task

        Args:
            task_id: Task ID

        Returns:
            List of TaskCheckpoints
        """
        return (
            self.db.query(TaskCheckpoint)
            .filter(TaskCheckpoint.task_id == task_id)
            .order_by(TaskCheckpoint.created_at.asc())
            .all()
        )

    def resume_from_checkpoint(
        self, task_id: int, checkpoint_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Resume from checkpoint

        Args:
            task_id: Task ID
            checkpoint_id: Specific checkpoint ID (optional)

        Returns:
            Resume data dict or None
        """
        checkpoints = self.get_checkpoints(task_id)

        if not checkpoints:
            return None

        # Use latest checkpoint if none specified
        if not checkpoint_id:
            checkpoint = checkpoints[-1]
        else:
            checkpoint = next((c for c in checkpoints if c.id == checkpoint_id), None)

        if not checkpoint:
            return None

        # Parse checkpoint data
        state_snapshot = (
            json.loads(checkpoint.state_snapshot) if checkpoint.state_snapshot else None
        )
        logs_snapshot = (
            json.loads(checkpoint.logs_snapshot) if checkpoint.logs_snapshot else None
        )
        error_info = (
            json.loads(checkpoint.error_info) if checkpoint.error_info else None
        )

        return {
            "checkpoint_id": checkpoint.id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "step_number": checkpoint.step_number,
            "description": checkpoint.description,
            "state_snapshot": state_snapshot,
            "logs_snapshot": logs_snapshot,
            "error_info": error_info,
        }

    # Auto-Snapshot Management
    def should_auto_snapshot(self, session_id: int) -> bool:
        """
        Check if auto-snapshot should be triggered

        Args:
            session_id: Session ID

        Returns:
            True if snapshot should be taken
        """
        state = self.load_session_state(session_id)
        return state is not None

    def auto_snapshot_session(self, session_id: int) -> Optional[SessionState]:
        """
        Trigger auto-snapshot for session

        Args:
            session_id: Session ID

        Returns:
            Saved state or None
        """
        # This would be called from orchestration workflow
        # For now, return placeholder
        logger.info(f"Auto-snapshot triggered for session {session_id}")
        return None

    # Full Context Export/Import
    def export_session_context(self, session_id: int) -> Dict[str, Any]:
        """
        Export full session context

        Args:
            session_id: Session ID

        Returns:
            Complete context export dict
        """
        state = self.load_session_state(session_id)
        if not state:
            return {}

        return {
            "session_id": session_id,
            "project_id": state.project_id,
            "current_step": state.current_step,
            "total_steps": state.total_steps,
            "plan": json.loads(state.plan) if state.plan else [],
            "execution_results": (
                json.loads(state.execution_results) if state.execution_results else []
            ),
            "debug_attempts": (
                json.loads(state.debug_attempts) if state.debug_attempts else []
            ),
            "changed_files": (
                json.loads(state.changed_files) if state.changed_files else []
            ),
            "conversation_history": [
                {
                    "role": msg.role,
                    "content": msg.content,
                    "metadata": msg.metadata_json,
                    "created_at": None,
                }
                for msg in self.get_conversation_history(session_id)
            ],
            "exported_at": datetime.utcnow().isoformat(),
        }

    def import_session_context(
        self, session_id: int, context_data: Dict[str, Any]
    ) -> bool:
        """
        Import session context

        Args:
            session_id: Session ID
            context_data: Context data to import

        Returns:
            True if import successful
        """
        try:
            # Restore session state
            plan = context_data.get("plan", [])
            execution_results = context_data.get("execution_results", [])
            debug_attempts = context_data.get("debug_attempts", [])
            changed_files = context_data.get("changed_files", [])

            self.save_session_state(
                session_id=session_id,
                project_id=context_data.get("project_id", 0),
                current_step=context_data.get("current_step", 0),
                total_steps=context_data.get("total_steps", 0),
                plan=plan,
                execution_results=execution_results,
                debug_attempts=debug_attempts,
                changed_files=changed_files,
            )

            # Restore conversation history
            for msg_data in context_data.get("conversation_history", []):
                self.add_conversation_message(
                    session_id=session_id,
                    role=msg_data["role"],
                    content=msg_data["content"],
                    metadata=msg_data.get("metadata"),
                )

            logger.info(f"Session context imported: session={session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to import session context: {str(e)}")
            return False
