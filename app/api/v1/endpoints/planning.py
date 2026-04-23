"""Interactive planning session endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import Project, User
from app.schemas import (
    PlanResponse,
    PlanningSessionCommitRequest,
    PlanningSessionCreateRequest,
    PlanningSessionRespondRequest,
    PlanningSessionResponse,
    PlanningSessionSummaryResponse,
    TaskResponse,
)
from app.services.planning.planning_session_service import PlanningSessionService

router = APIRouter(prefix="/planning")


class PlanningCommitResponse(PlanningSessionResponse):
    plan: Optional[PlanResponse] = None
    tasks: List[TaskResponse] = Field(default_factory=list)


@router.get("/sessions", response_model=List[PlanningSessionSummaryResponse])
def list_planning_sessions(
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
    return PlanningSessionService(db).list_sessions(project_id=project_id)


@router.post(
    "/sessions",
    response_model=PlanningSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_planning_session(
    payload: PlanningSessionCreateRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
    project = (
        db.query(Project)
        .filter(Project.id == payload.project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    service = PlanningSessionService(db)
    session = service.start_session(
        project, payload.prompt, source_brain=payload.source_brain
    )
    return service.build_session_payload(session)


@router.get("/sessions/{session_id}", response_model=PlanningSessionResponse)
def get_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
    service = PlanningSessionService(db)
    session = service.get_session(session_id)
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/respond", response_model=PlanningSessionResponse)
def respond_to_planning_session(
    session_id: int,
    payload: PlanningSessionRespondRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
    service = PlanningSessionService(db)
    session = service.respond(session_id, payload.response)
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/cancel", response_model=PlanningSessionResponse)
def cancel_planning_session(
    session_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
    service = PlanningSessionService(db)
    session = service.cancel(session_id)
    return service.build_session_payload(session)


@router.post("/sessions/{session_id}/commit", response_model=PlanningCommitResponse)
def commit_planning_session(
    session_id: int,
    payload: PlanningSessionCommitRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
):
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
