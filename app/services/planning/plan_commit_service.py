"""Shared helpers for turning planner output into plan/task records."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.models import Plan, Project, Task, TaskStatus
from app.schemas import PlannerTaskCandidate
from app.services.project.name_formatter import humanize_display_name
from app.services.planning.task_position_integrity import (
    TaskPositionConflictError,
    acquire_sqlite_write_reservation,
    check_no_new_position_conflicts,
    resolve_effective_positions,
    snapshot_task_positions,
)


class PlanCommitPositionConflictError(TaskPositionConflictError):
    """Position conflict raised by PlanCommit, annotated for API status mapping."""

    def __init__(self, conflicts, *, candidate_only: bool):
        super().__init__(conflicts)
        self.candidate_only = candidate_only


class PlanCommitService:
    """Create or update persisted plan/task records from planner task candidates."""

    def __init__(self, db: Session):
        self.db = db

    def create_plan_tasks(
        self,
        project: Project,
        tasks: Sequence[PlannerTaskCandidate],
        *,
        plan: Optional[Plan] = None,
        markdown: Optional[str] = None,
        plan_title: Optional[str] = None,
        requirement: Optional[str] = None,
        source_brain: str = "local",
        commit: bool = True,
    ) -> tuple[Optional[Plan], list[Task]]:
        if not tasks:
            raise ValueError("At least one task is required")

        try:
            acquire_sqlite_write_reservation(self.db)
            before = snapshot_task_positions(
                self.db.query(Task).filter(Task.project_id == project.id).all()
            )
            if plan is not None and plan.project_id != project.id:
                raise ValueError("Plan does not belong to the supplied Project")

            if plan is None and markdown:
                plan = Plan(
                    project_id=project.id,
                    title=(plan_title or requirement or "Imported plan")[:255],
                    source_brain=source_brain,
                    requirement=requirement
                    or plan_title
                    or "Imported planner markdown",
                    markdown=markdown,
                    status="draft",
                )
                self.db.add(plan)
                self.db.flush()

            created_tasks: list[Task] = []
            resolved_positions = resolve_effective_positions(tasks)
            for item, position in zip(tasks, resolved_positions):
                task = Task(
                    project_id=project.id,
                    plan_id=plan.id if plan else None,
                    title=humanize_display_name(item.title),
                    description=item.description,
                    execution_profile=item.execution_profile,
                    workflow_stage=item.workflow_stage,
                    priority=item.priority,
                    plan_position=position,
                    estimated_effort=item.estimated_effort,
                    status=TaskStatus.PENDING,
                )
                self.db.add(task)
                created_tasks.append(task)

            if plan:
                plan.status = "committed"

            self.db.flush()
            after = snapshot_task_positions(
                self.db.query(Task).filter(Task.project_id == project.id).all()
            )
            try:
                check_no_new_position_conflicts(before, after)
            except TaskPositionConflictError as exc:
                created_task_ids = {id(task) for task in created_tasks}
                candidate_only = all(
                    id(getattr(conflict.left, "source_task", None)) in created_task_ids
                    and id(getattr(conflict.right, "source_task", None))
                    in created_task_ids
                    for conflict in exc.conflicts
                )
                raise PlanCommitPositionConflictError(
                    exc.conflicts, candidate_only=candidate_only
                ) from exc

            if commit:
                self.db.commit()
                for task in created_tasks:
                    self.db.refresh(task)
                if plan:
                    self.db.refresh(plan)

            return plan, created_tasks
        except Exception:
            if commit:
                self.db.rollback()
            raise
