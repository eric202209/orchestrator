"""Background tasks for interactive planning sessions."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.database import get_db_session
from app.services.planning.planning_session_service import PlanningSessionService

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def advance_planning_session(self, session_id: int) -> dict[str, object]:
    db = get_db_session()
    try:
        service = PlanningSessionService(db)
        session = service.process_session(session_id)
        if not session:
            return {"status": "skipped", "session_id": session_id}
        return {
            "status": session.status,
            "session_id": session.id,
            "project_id": session.project_id,
        }
    except Exception as exc:
        logger.exception("Planning background task failed for session %s", session_id)
        raise self.retry(exc=exc)
    finally:
        db.close()
