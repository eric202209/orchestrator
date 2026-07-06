from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import HumanGuidanceConflict, LogEntry, Project, User
from app.services.human_guidance.post_write_checker import (
    run_post_write_check_if_enabled,
)
from app.services.human_guidance.service import create_guidance
from app.services.orchestration.context.assembly import assemble_execution_prompt
from app.services.orchestration.prompt_templates import OrchestrationState


def _make_user(db, email: str = "hg-p5d@example.com") -> User:
    user = User(email=email, hashed_password="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_project(db, user_id: int, tmp_path) -> Project:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "src").mkdir()
    project = Project(
        name="hg-p5d-project",
        workspace_path=str(project_dir),
        user_id=user_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _enable_all_flags(db, project_id: int) -> None:
    from app.services.human_guidance.activation import set_project_activation

    set_project_activation(
        db,
        project_id,
        {
            "table_enabled": True,
            "persistence_enabled": True,
            "render_enabled": True,
            "injection_enabled": True,
            "conflict_detection_enabled": True,
        },
    )


def _write_bad_file(tmp_path) -> str:
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def bad(items=[]): return items\n", encoding="utf-8")
    return str(bad_file)


def _ctx(db, project: Project, task_id: int):
    state = OrchestrationState(
        session_id="900",
        task_description="Implement the next step",
        project_name=project.name,
        project_context="",
        task_id=task_id,
    )
    state._project_dir_override = project.workspace_path
    runtime = SimpleNamespace(
        get_backend_metadata=lambda: {
            "backend": "local_openclaw",
            "model_family": "qwen",
        }
    )
    return SimpleNamespace(
        db=db,
        project=project,
        session_id=900,
        task_id=task_id,
        task=SimpleNamespace(id=task_id, title="Next step", plan_position=2),
        runtime_service=runtime,
        execution_backend="local_openclaw",
        guidance_backend="local_openclaw",
        guidance_model_family="qwen",
        orchestration_state=state,
        prompt="Implement the next step",
        execution_profile="full_lifecycle",
        workflow_profile="default",
    )


def _step():
    return {
        "step_number": 1,
        "description": "Edit source",
        "commands": ["python -m pytest"],
        "verification": "python -m pytest",
        "rollback": None,
        "expected_files": ["bad.py"],
    }


@pytest.mark.usefixtures("db_session")
def test_post_write_branch_creates_conflict_and_remediation_prompt(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)
    monkeypatch.setattr(settings, "WORKING_MEMORY_INJECTION_ENABLED", True)

    user = _make_user(db_session)
    project = _make_project(db_session, user.id, tmp_path)
    _enable_all_flags(db_session, project.id)
    guidance = create_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="Use None for optional list defaults. Never use [] as a default argument.",
        priority=100,
        backend_targets=["local_openclaw"],
        model_targets=["all"],
        purpose_targets=["execution", "all"],
    )[0]

    changed_file = _write_bad_file(tmp_path)
    ctx = _ctx(db_session, project, task_id=900)

    run_post_write_check_if_enabled(ctx, reported_changed_files=[changed_file])

    conflict = (
        db_session.query(HumanGuidanceConflict)
        .filter(
            HumanGuidanceConflict.project_id == project.id,
            HumanGuidanceConflict.task_id == 900,
            HumanGuidanceConflict.source == "post_write_check",
            HumanGuidanceConflict.status == "open",
        )
        .first()
    )
    assert conflict is not None
    assert conflict.guidance_id == guidance.id
    assert conflict.severity == "advisory"
    assert "mutable_default" in json.loads(conflict.conflict_patterns)

    warning = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.task_id == 900,
            LogEntry.level == "WARNING",
            LogEntry.message.like("%[GUIDANCE_POST_WRITE_WARNING]%"),
        )
        .first()
    )
    assert warning is not None

    prompt = assemble_execution_prompt(_ctx(db_session, project, task_id=901), _step())
    assert "## HUMAN GUIDANCE" in prompt
    assert "## GUIDANCE REMEDIATION" in prompt
    assert "previous task=900" in prompt
    assert "pattern=mutable_default" in prompt
    assert "[OPERATOR_GUIDANCE]" not in prompt

    operator_rows = (
        db_session.query(LogEntry)
        .filter(LogEntry.message.like("[OPERATOR_GUIDANCE]%"))
        .all()
    )
    assert operator_rows == []
