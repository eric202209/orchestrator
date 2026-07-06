"""Reusable queries for operator attention state.

Single owner for all "what needs operator action" business rules.
Dashboard, Session pages, and future surfaces all call this service.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session as DbSession

from app.models import (
    InterventionRequest,
    Project,
    Session as SessionModel,
    Task,
)
from app.services.auth.authorization import project_access_filter

ATTENTION_STATUSES: frozenset[str] = frozenset({"failed", "awaiting_input", "stopped"})


class AttentionQueryService:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Pending interventions
    # ------------------------------------------------------------------

    def get_pending_interventions(self, current_user) -> List[Dict[str, Any]]:
        """All pending intervention requests for sessions accessible to user.

        Includes project_name so callers don't need a separate projectMap fetch.
        """
        rows = (
            self.db.query(InterventionRequest, Project)
            .join(SessionModel, SessionModel.id == InterventionRequest.session_id)
            .join(Project, Project.id == SessionModel.project_id)
            .filter(
                InterventionRequest.status == "pending",
                SessionModel.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                project_access_filter(self.db, current_user),
            )
            .order_by(InterventionRequest.created_at.asc())
            .all()
        )
        return [
            {
                "id": req.id,
                "session_id": req.session_id,
                "task_id": req.task_id,
                "project_id": req.project_id,
                "project_name": project.name,
                "intervention_type": req.intervention_type,
                "initiated_by": req.initiated_by,
                "prompt": req.prompt,
                "status": req.status,
                "created_at": req.created_at.isoformat() if req.created_at else None,
                "expires_at": req.expires_at.isoformat() if req.expires_at else None,
            }
            for req, project in rows
        ]

    # ------------------------------------------------------------------
    # Sessions needing attention
    # ------------------------------------------------------------------

    def get_sessions_needing_attention_count(self, current_user) -> int:
        """Count of non-deleted sessions in a status that requires operator action."""
        return (
            self.db.query(SessionModel)
            .join(Project, Project.id == SessionModel.project_id)
            .filter(
                SessionModel.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                SessionModel.status.in_(ATTENTION_STATUSES),
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_attention_sessions(
        self, current_user, *, limit: int = 25
    ) -> List[SessionModel]:
        """Sessions requiring operator action, newest first."""
        return (
            self.db.query(SessionModel)
            .join(Project, Project.id == SessionModel.project_id)
            .filter(
                SessionModel.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                SessionModel.status.in_(ATTENTION_STATUSES),
                project_access_filter(self.db, current_user),
            )
            .order_by(SessionModel.created_at.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    def get_review_queue_count(self, current_user) -> int:
        """Count of tasks with workspace_status == 'ready'."""
        return (
            self.db.query(Task)
            .join(Project, Project.id == Task.project_id)
            .filter(
                Project.deleted_at.is_(None),
                Task.workspace_status == "ready",
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_review_queue_tasks(self, current_user, *, limit: int = 25) -> List[Task]:
        """Tasks with workspace_status == 'ready', newest first."""
        return (
            self.db.query(Task)
            .join(Project, Project.id == Task.project_id)
            .filter(
                Project.deleted_at.is_(None),
                Task.workspace_status == "ready",
                project_access_filter(self.db, current_user),
            )
            .order_by(Task.created_at.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # System overview counts
    # ------------------------------------------------------------------

    def get_running_sessions_count(self, current_user) -> int:
        """Count of non-deleted sessions currently in running or awaiting_input status.

        Deliberately excludes "paused": a paused session is not running (it is
        either genuinely suspended or, for a planning-repair-exhaustion
        failure, a terminal failure mislabeled "paused" -- see
        docs/roadmap/done/phase18/phase18l-r-runtime-verification-report.md,
        "Planning-Failure Lifecycle Asymmetry"). Counting it here previously
        made the dashboard's running-session count disagree with the Sessions
        page, which does not treat "paused" as running.
        """
        return (
            self.db.query(SessionModel)
            .join(Project, Project.id == SessionModel.project_id)
            .filter(
                SessionModel.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                SessionModel.status.in_(("running", "awaiting_input")),
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_total_active_sessions_count(self, current_user) -> int:
        """Count of non-deleted sessions that are active (is_active=True)."""
        return (
            self.db.query(SessionModel)
            .join(Project, Project.id == SessionModel.project_id)
            .filter(
                SessionModel.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                SessionModel.is_active.is_(True),
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_total_projects_count(self, current_user) -> int:
        """Count of non-deleted projects accessible to user."""
        return (
            self.db.query(Project)
            .filter(
                Project.deleted_at.is_(None),
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_total_tasks_count(self, current_user) -> int:
        """Count of all tasks in accessible projects."""
        return (
            self.db.query(Task)
            .join(Project, Project.id == Task.project_id)
            .filter(
                Project.deleted_at.is_(None),
                project_access_filter(self.db, current_user),
            )
            .count()
        )

    def get_completed_tasks_count(self, current_user) -> int:
        """Count of tasks with status='done'."""
        return (
            self.db.query(Task)
            .join(Project, Project.id == Task.project_id)
            .filter(
                Project.deleted_at.is_(None),
                Task.status == "done",
                project_access_filter(self.db, current_user),
            )
            .count()
        )
