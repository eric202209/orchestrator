"""Background tasks for interactive planning sessions."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.database import get_db_session
from app.services.planning.planning_session_service import PlanningSessionService

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def advance_planning_session(
    self,
    session_id: int,
    generation_id: str | None = None,
    owner_token: str | None = None,
) -> dict[str, object]:
    db = get_db_session()
    retrying = False
    try:
        if not generation_id or not owner_token:
            return {
                "status": "stale_owner",
                "session_id": session_id,
                "generation_id": generation_id or "",
                "reason": "legacy_task_arguments",
            }
        service = PlanningSessionService(db)
        session = service.process_session(
            session_id,
            generation_id,
            owner_token,
            processing_task_id=getattr(self.request, "id", None),
        )
        if isinstance(session, dict):
            return session
        if not session:
            return {"status": "skipped", "session_id": session_id}
        return {
            "status": session.status,
            "session_id": session.id,
            "project_id": session.project_id,
        }
    except Exception as exc:
        logger.exception("Planning background task failed for session %s", session_id)
        retrying = True
        raise self.retry(
            exc=exc,
            args=(session_id, generation_id, owner_token),
        )
    finally:
        if not retrying and generation_id and owner_token:
            PlanningSessionService(db).release_processing_task(
                session_id,
                generation_id,
                owner_token,
                getattr(self.request, "id", None),
            )
        db.close()
