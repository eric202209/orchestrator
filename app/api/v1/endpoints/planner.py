"""Planner API endpoints."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import Plan, Task, TaskStatus
from app.schemas import PlanResponse, PlannerTaskCandidate, TaskResponse
from app.services.authz import get_project_for_user
from app.services.name_formatter import humanize_display_name
from app.services.planning.plan_commit_service import PlanCommitService
from app.services.planning.planner_service import PlannerService

router = APIRouter()
MAX_PLANNER_MARKDOWN_CHARS = 100_000


class PlannerGenerateRequest(BaseModel):
    project_id: int
    requirement: str = Field(min_length=3)
    source_brain: str = "local"


class PlannerGenerateResponse(BaseModel):
    plan: PlanResponse
    tasks_preview: List[PlannerTaskCandidate]


class PlannerParseRequest(BaseModel):
    markdown: str


class PlannerParseResponse(BaseModel):
    tasks: List[PlannerTaskCandidate]


class BatchTaskCreateRequest(BaseModel):
    markdown: Optional[str] = None
    plan_id: Optional[int] = None
    plan_title: Optional[str] = None
    requirement: Optional[str] = None
    source_brain: str = "local"
    tasks: List[PlannerTaskCandidate]


class BatchTaskCreateResponse(BaseModel):
    plan: Optional[PlanResponse] = None
    tasks: List[TaskResponse]


class PlanUpdateRequest(BaseModel):
    title: Optional[str] = None
    requirement: Optional[str] = None
    markdown: Optional[str] = None
    source_brain: Optional[str] = None
    status: Optional[str] = None


@router.post("/planner/generate", response_model=PlannerGenerateResponse)
def generate_plan(
    payload: PlannerGenerateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Legacy manual planner flow retained for backward compatibility."""
    project = get_project_for_user(db, payload.project_id, current_user)

    markdown = PlannerService.generate_markdown(
        requirement=payload.requirement,
        project_name=project.name,
        source_brain=payload.source_brain,
        project_description=project.description,
    )
    parsed_tasks = [
        PlannerTaskCandidate(
            title=item.title,
            description=item.description,
            execution_profile=item.execution_profile,
            priority=item.priority,
            plan_position=item.plan_position,
            estimated_effort=item.estimated_effort,
        )
        for item in PlannerService.parse_markdown(markdown)
    ]

    plan = Plan(
        project_id=project.id,
        title=payload.requirement[:255],
        source_brain=payload.source_brain,
        requirement=payload.requirement,
        markdown=markdown,
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    return PlannerGenerateResponse(
        plan=PlanResponse.model_validate(plan),
        tasks_preview=parsed_tasks,
    )


@router.post("/planner/parse", response_model=PlannerParseResponse)
def parse_markdown(
    payload: PlannerParseRequest,
    _current_user=Depends(get_current_active_user),
):
    if len(payload.markdown or "") > MAX_PLANNER_MARKDOWN_CHARS:
        raise HTTPException(status_code=400, detail="Markdown too large")
    parsed_tasks = [
        PlannerTaskCandidate(
            title=item.title,
            description=item.description,
            execution_profile=item.execution_profile,
            priority=item.priority,
            plan_position=item.plan_position,
            estimated_effort=item.estimated_effort,
        )
        for item in PlannerService.parse_markdown(payload.markdown)
    ]
    return PlannerParseResponse(tasks=parsed_tasks)


@router.get("/projects/{project_id}/plans", response_model=List[PlanResponse])
def list_project_plans(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    get_project_for_user(db, project_id, current_user)

    plans = (
        db.query(Plan)
        .filter(Plan.project_id == project_id)
        .order_by(Plan.created_at.desc(), Plan.id.desc())
        .all()
    )
    return plans


@router.delete(
    "/projects/{project_id}/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_project_plan(
    project_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    get_project_for_user(db, project_id, current_user)

    plan = (
        db.query(Plan).filter(Plan.id == plan_id, Plan.project_id == project_id).first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Keep already-created project tasks intact; just detach their source plan link.
    db.query(Task).filter(Task.plan_id == plan.id).update(
        {"plan_id": None}, synchronize_session=False
    )
    db.delete(plan)
    db.commit()
    return None


@router.put("/projects/{project_id}/plans/{plan_id}", response_model=PlanResponse)
def update_project_plan(
    project_id: int,
    plan_id: int,
    payload: PlanUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    get_project_for_user(db, project_id, current_user)
    if (
        payload.markdown is not None
        and len(payload.markdown) > MAX_PLANNER_MARKDOWN_CHARS
    ):
        raise HTTPException(status_code=400, detail="Markdown too large")

    plan = (
        db.query(Plan).filter(Plan.id == plan_id, Plan.project_id == project_id).first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return plan


@router.post(
    "/projects/{project_id}/batch-tasks",
    response_model=BatchTaskCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_batch_tasks(
    project_id: int,
    payload: BatchTaskCreateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    project = get_project_for_user(db, project_id, current_user)
    if (
        payload.markdown is not None
        and len(payload.markdown) > MAX_PLANNER_MARKDOWN_CHARS
    ):
        raise HTTPException(status_code=400, detail="Markdown too large")
    if not payload.tasks:
        raise HTTPException(status_code=400, detail="At least one task is required")

    plan: Optional[Plan] = None
    if payload.plan_id is not None:
        plan = (
            db.query(Plan)
            .filter(Plan.id == payload.plan_id, Plan.project_id == project_id)
            .first()
        )
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
    plan, created_tasks = PlanCommitService(db).create_plan_tasks(
        project,
        payload.tasks,
        plan=plan,
        markdown=payload.markdown,
        plan_title=payload.plan_title,
        requirement=payload.requirement,
        source_brain=payload.source_brain,
    )

    return BatchTaskCreateResponse(
        plan=PlanResponse.model_validate(plan) if plan else None,
        tasks=created_tasks,
    )
