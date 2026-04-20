from __future__ import annotations

from pathlib import Path

import app.services.project_isolation_service as project_isolation_service
import app.services.prompt_templates as prompt_templates
import app.services.system_settings as system_settings
from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.openclaw_service import OpenClawSessionService
from app.services.orchestration.task_rules import (
    get_task_report_path,
    should_execute_in_canonical_project_root,
)
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.session_runtime_service import ensure_task_workspace


def _patch_workspace_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(
        project_isolation_service, "get_effective_workspace_root", lambda: root
    )
    monkeypatch.setattr(prompt_templates, "get_effective_workspace_root", lambda: root)
    monkeypatch.setattr(system_settings, "get_effective_workspace_root", lambda: root)


def _seed_project_session_and_task(
    db_session,
    *,
    project_name: str,
    title: str,
    description: str,
    plan_position: int | None,
):
    project = Project(name=project_name, workspace_path=project_name)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name=f"{project_name} Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title=title,
        description=description,
        status=TaskStatus.PENDING,
        plan_position=plan_position,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    return project, session, task


def test_ordered_tasks_execute_in_project_root(monkeypatch, db_session, tmp_path):
    _patch_workspace_root(monkeypatch, tmp_path)
    project, session, task = _seed_project_session_and_task(
        db_session,
        project_name="canonical-project",
        title="Inspect current project architecture",
        description="Review current structure and data flow",
        plan_position=1,
    )

    workspace = ensure_task_workspace(db_session, session, task.id)
    project_root = resolve_project_workspace_path(project.workspace_path, project.name)

    assert should_execute_in_canonical_project_root(
        task,
        getattr(task, "execution_profile", None),
        task.title,
        task.description,
    )
    assert workspace["workspace_path"] == str(project_root)
    assert task.task_subfolder is not None

    service = OpenClawSessionService(db_session, session.id, task.id)
    assert service._resolve_execution_cwd() == str(project_root)


def test_manual_tasks_still_use_isolated_subfolder(monkeypatch, db_session, tmp_path):
    _patch_workspace_root(monkeypatch, tmp_path)
    project, session, task = _seed_project_session_and_task(
        db_session,
        project_name="manual-project",
        title="Ad hoc cleanup",
        description="Manual one-off fix outside the ordered plan",
        plan_position=None,
    )

    workspace = ensure_task_workspace(db_session, session, task.id)
    project_root = resolve_project_workspace_path(project.workspace_path, project.name)

    assert not should_execute_in_canonical_project_root(
        task,
        getattr(task, "execution_profile", None),
        task.title,
        task.description,
    )
    assert workspace["workspace_path"] == str(project_root / task.task_subfolder)


def test_ordered_task_reports_live_in_project_root(monkeypatch, db_session, tmp_path):
    _patch_workspace_root(monkeypatch, tmp_path)
    project, _session, task = _seed_project_session_and_task(
        db_session,
        project_name="report-project",
        title="Implement the core changes",
        description="Apply the ordered implementation updates",
        plan_position=3,
    )

    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    expected = project_root / f"task_report_{task.id}.md"

    assert get_task_report_path(project_root, task) == expected
