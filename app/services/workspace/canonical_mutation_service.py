"""Central wrapper for mutations against a project's canonical root."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.models import Project
from app.services.workspace.project_mutation_lock import project_mutation_lock

T = TypeVar("T")


class CanonicalMutationService:
    """Run canonical project-root mutations under the project mutation lock."""

    def run_locked(
        self,
        project: Project,
        *,
        project_root: Path,
        operation: str,
        owner: str,
        fn: Callable[[], T],
    ) -> T:
        with project_mutation_lock(
            project_id=project.id,
            project_root=project_root,
            operation=operation,
            owner=owner,
        ):
            return fn()
