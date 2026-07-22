"""Interactive planning session endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import PlanningSession, Project, User
from app.schemas import (
    PlanResponse,
    PlanningSessionCommitRequest,
    PlanningSessionCreateRequest,
    PlanningSessionRespondRequest,
    PlanningStageAdvanceRequest,
    PlanningSessionResponse,
    PlanningSessionSummaryResponse,
    TaskResponse,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.auth.authorization import get_project_for_user, project_access_filter

router = APIRouter(prefix="/planning")


class PlanningCommitResponse(PlanningSessionResponse):
    plan: Optional[PlanResponse] = None
    tasks: List[TaskResponse] = Field(default_factory=list)


def _authorize_planning_session(
    db: Session, session_id: int, current_user: User
) -> None:
    service = PlanningSessionService(db)
    session = service.get_session(session_id)
    get_project_for_user(db, session.project_id, current_user)


@router.get("/sessions", response_model=List[PlanningSessionSummaryResponse])
def list_planning_sessions(
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if project_id is not None:
        get_project_for_user(db, project_id, current_user)
    query = db.query(PlanningSession).join(Project).filter(Project.deleted_at.is_(None))
    query = query.filter(project_access_filter(db, current_user))
    if project_id is not None:
        query = query.filter(PlanningSession.project_id == project_id)
    return query.order_by(
        PlanningSession.created_at.desc(), PlanningSession.id.desc()
    ).all()


@router.post(
    "/sessions",
    response_model=PlanningSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_planning_session(
    payload: PlanningSessionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    project = get_project_for_user(db, payload.project_id, current_user)

    service = PlanningSessionService(db)
    session = service.start_session(
        project,
        payload.prompt,
        source_brain=payload.source_brain,
        skip_clarification=payload.skip_clarification,
        protocol_version=payload.protocol_version,
        target_stage=payload.target_stage,
    )
    return service.build_session_payload(session)


@router.get("/sessions/{session_id}", response_model=PlanningSessionResponse)
def get_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session = service.get_session(session_id)
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/respond", response_model=PlanningSessionResponse)
def respond_to_planning_session(
    session_id: int,
    payload: PlanningSessionRespondRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session = service.respond(session_id, payload.response)
    return service.build_session_payload(session)


@router.post(
    "/sessions/{session_id}/advance-stage",
    response_model=PlanningSessionResponse,
)
def advance_planning_stage(
    session_id: int,
    payload: PlanningStageAdvanceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session = service.advance_to_stage(
        session_id,
        payload.target_stage,
        accepted_brief_checkpoint_id=payload.accepted_brief_checkpoint_id,
    )
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/retry", response_model=PlanningSessionResponse)
def retry_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session = service.retry(session_id)
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/cancel", response_model=PlanningSessionResponse)
def cancel_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session = service.cancel(session_id)
    return service.build_session_payload(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    service.delete_terminal_session(session_id)
    return None


@router.post("/sessions/{session_id}/commit", response_model=PlanningCommitResponse)
def commit_planning_session(
    session_id: int,
    payload: PlanningSessionCommitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _authorize_planning_session(db, session_id, current_user)
    service = PlanningSessionService(db)
    session, plan, tasks = service.commit(
        session_id,
        payload.selected_tasks,
        planner_markdown=payload.planner_markdown,
    )
    session_payload = service.build_session_payload(session)
    session_payload["plan"] = plan
    session_payload["tasks"] = tasks
    return session_payload
