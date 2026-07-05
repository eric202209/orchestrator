"""Helpers for analytics scans over orchestration event journals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session as DbSession

from app.models import Project, Session as SessionModel, Task
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)


@dataclass(frozen=True)
class EventJournalTarget:
    project_id: int
    session_id: int
    task_id: int
    project_dir: Path


@dataclass(frozen=True)
class _ProjectSnapshot:
    id: int
    name: str
    workspace_path: Optional[str]


@dataclass(frozen=True)
class _SessionSnapshot:
    id: int
    project_id: int


@dataclass(frozen=True)
class _TaskSnapshot:
    id: int
    project_id: int


def load_event_journal_targets(db: DbSession) -> List[EventJournalTarget]:
    """Snapshot DB metadata needed for event-journal analytics.

    Analytics endpoints can spend most of their time reading JSONL files. Keeping
    a checked-out DB connection during that filesystem walk can exhaust the app
    pool and make unrelated page loads time out. This function copies the small
    amount of metadata needed for the walk, then releases the read transaction
    before returning plain value objects.
    """

    try:
        project_rows = (
            db.query(Project.id, Project.name, Project.workspace_path)
            .filter(Project.deleted_at.is_(None))
            .all()
        )
        session_rows = (
            db.query(SessionModel.id, SessionModel.project_id)
            .filter(SessionModel.deleted_at.is_(None))
            .all()
        )
        task_rows = db.query(Task.id, Task.project_id).all()
    except Exception:
        return []
    finally:
        try:
            db.rollback()
        except Exception:
            pass

    projects: Dict[int, _ProjectSnapshot] = {
        row.id: _ProjectSnapshot(
            id=row.id,
            name=row.name,
            workspace_path=row.workspace_path,
        )
        for row in project_rows
    }
    sessions = [
        _SessionSnapshot(id=row.id, project_id=row.project_id) for row in session_rows
    ]
    tasks_by_project: Dict[int, List[_TaskSnapshot]] = {}
    for row in task_rows:
        tasks_by_project.setdefault(row.project_id, []).append(
            _TaskSnapshot(id=row.id, project_id=row.project_id)
        )

    targets: List[EventJournalTarget] = []
    for session in sessions:
        project = projects.get(session.project_id)
        if not project:
            continue
        project_dir = Path(
            resolve_project_workspace_path(project.workspace_path, project.name)
        )
        for task in tasks_by_project.get(session.project_id, []):
            targets.append(
                EventJournalTarget(
                    project_id=session.project_id,
                    session_id=session.id,
                    task_id=task.id,
                    project_dir=project_dir,
                )
            )
    return targets
