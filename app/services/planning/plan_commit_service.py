"""Shared helpers for turning planner output into plan/task records."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.models import Plan, Project, Task, TaskStatus
from app.schemas import PlannerTaskCandidate
from app.services.name_formatter import humanize_display_name


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

        if plan is None and markdown:
            plan = Plan(
                project_id=project.id,
                title=(plan_title or requirement or "Imported plan")[:255],
                source_brain=source_brain,
                requirement=requirement or plan_title or "Imported planner markdown",
                markdown=markdown,
                status="draft",
            )
            self.db.add(plan)
            self.db.flush()

        created_tasks: list[Task] = []
        for index, item in enumerate(tasks, start=1):
            task = Task(
                project_id=project.id,
                plan_id=plan.id if plan else None,
                title=humanize_display_name(item.title),
                description=item.description,
                execution_profile=item.execution_profile,
                priority=item.priority,
                plan_position=item.plan_position or index,
                estimated_effort=item.estimated_effort,
                status=TaskStatus.PENDING,
            )
            self.db.add(task)
            created_tasks.append(task)

        if plan:
            plan.status = "committed"

        self.db.flush()

        if commit:
            self.db.commit()
            for task in created_tasks:
                self.db.refresh(task)
            if plan:
                self.db.refresh(plan)

        return plan, created_tasks
