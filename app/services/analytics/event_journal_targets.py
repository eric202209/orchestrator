"""Helpers for analytics scans over orchestration event journals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session as DbSession

from app.models import Project, Session as SessionModel, SessionTask
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


def load_event_journal_targets(db: DbSession) -> List[EventJournalTarget]:
    """Snapshot DB metadata needed for event-journal analytics.

    Analytics endpoints can spend most of their time reading JSONL files. Keeping
    a checked-out DB connection during that filesystem walk can exhaust the app
    pool and make unrelated page loads time out. This function copies the small
    amount of metadata needed for the walk, then releases the read transaction
    before returning plain value objects.

    Phase 19F: targets are the actual `session_tasks` link rows (one journal
    per session actually assigned a task), not the full cross product of
    every session in a project against every task in that project. The prior
    cross-product scan was O(sessions x tasks) per project and was the
    dominant cost in `/analytics/execution` (~6.4s) and `/analytics/failures`
    (~6.3s) under Phase 19E measurement — most generated pairs never
    co-occurred and every one still cost a filesystem read attempt.
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
        session_task_rows = db.query(SessionTask.session_id, SessionTask.task_id).all()
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
    sessions: Dict[int, _SessionSnapshot] = {
        row.id: _SessionSnapshot(id=row.id, project_id=row.project_id)
        for row in session_rows
    }

    project_dirs: Dict[int, Path] = {}
    targets: List[EventJournalTarget] = []
    for row in session_task_rows:
        session = sessions.get(row.session_id)
        if session is None:
            continue
        project = projects.get(session.project_id)
        if project is None:
            continue
        project_dir = project_dirs.get(project.id)
        if project_dir is None:
            project_dir = Path(
                resolve_project_workspace_path(project.workspace_path, project.name)
            )
            project_dirs[project.id] = project_dir
        targets.append(
            EventJournalTarget(
                project_id=session.project_id,
                session_id=session.id,
                task_id=row.task_id,
                project_dir=project_dir,
            )
        )
    return targets
