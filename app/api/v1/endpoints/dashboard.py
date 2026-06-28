"""Dashboard endpoints — operator action center."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.services.query.attention_query_service import AttentionQueryService

router = APIRouter()


@router.get("/dashboard/attention")
def get_dashboard_attention(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Return operator attention state in a single lightweight call.

    Orchestrates AttentionQueryService only — no business logic lives here.

    Response shape:
        pending_interventions: list of pending intervention request dicts (with project_name)
        sessions_needing_attention: count of sessions in failed/awaiting_input/stopped
        tasks_pending_review: count of tasks with workspace_status == 'ready'
    """
    svc = AttentionQueryService(db)
    return {
        "pending_interventions": svc.get_pending_interventions(current_user),
        "sessions_needing_attention": svc.get_sessions_needing_attention_count(
            current_user
        ),
        "tasks_pending_review": svc.get_review_queue_count(current_user),
        "running_sessions": svc.get_running_sessions_count(current_user),
        "active_sessions": svc.get_total_active_sessions_count(current_user),
        "total_projects": svc.get_total_projects_count(current_user),
        "total_tasks": svc.get_total_tasks_count(current_user),
        "completed_tasks": svc.get_completed_tasks_count(current_user),
    }
