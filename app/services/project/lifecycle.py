"""Project lifecycle eligibility, separate from destructive deletion."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.models import Project


def is_project_retired(project: Project) -> bool:
    return project.retired_at is not None


def assert_project_launch_eligible(project: Project) -> None:
    """Reject retired history-only Projects before any new runtime mutation."""

    if is_project_retired(project):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "category": "project_retired",
                "detail": (
                    f"Project {project.id} is retired and retained for historical inspection; "
                    "create or reactivate a different active Project for new work."
                ),
            },
        )
