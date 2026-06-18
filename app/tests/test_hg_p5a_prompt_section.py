from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import HumanGuidanceUsage, LogEntry, Project, User
from app.services.human_guidance_activation_service import set_project_activation
from app.services.human_guidance_service import create_guidance
from app.services.orchestration.context.assembly import (
    render_active_human_guidance_section,
    assemble_execution_prompt,
)
from app.services.prompt_templates import OrchestrationState


@pytest.fixture()
def hg_user(db_session: Session) -> User:
    user = User(email="hg-p5a@example.com", hashed_password="hashed", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def hg_project(db_session: Session, hg_user: User, tmp_path) -> Project:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "src").mkdir()
    (project_dir / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = Project(
        name="hg-p5a-project",
        workspace_path=str(project_dir),
        user_id=hg_user.id,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def _add_guidance(
    db: Session,
    *,
    user_id: int,
    project_id: int,
    message: str,
    purpose_targets: list[str] | None = None,
    backend_targets: list[str] | None = None,
    model_targets: list[str] | None = None,
    priority: int = 0,
):
    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        purpose_targets=purpose_targets,
        backend_targets=backend_targets,
        model_targets=model_targets,
        priority=priority,
    )
    return entry


def _ctx(
    db: Session,
    project: Project,
    *,
    backend: str = "local_openclaw",
    model_family: str = "qwen",
    task_id: int = 501,
    plan_position: int = 1,
):
    project_dir = project.workspace_path
    state = OrchestrationState(
        session_id="701",
        task_description="Implement the current step",
        project_name=project.name,
        project_context="Existing project context.",
        task_id=task_id,
    )
    state._project_dir_override = project_dir
    runtime = SimpleNamespace(
        get_backend_metadata=lambda: {
            "backend": backend,
            "model_family": model_family,
        }
    )
    return SimpleNamespace(
        db=db,
        project=project,
        task=SimpleNamespace(id=task_id, plan_position=plan_position),
        runtime_service=runtime,
        execution_backend=backend,
        prompt="Implement the current step",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=state,
    )


def _step():
    return {
        "step_number": 1,
        "description": "Edit source",
        "commands": ["python -m pytest"],
        "verification": "python -m pytest",
        "rollback": None,
        "expected_files": ["src/main.py"],
    }


@pytest.fixture(autouse=True)
def _enable_hg_table(monkeypatch):
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)


def test_execution_prompt_includes_execution_purpose_guidance(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Use dependency injection for execution code.",
        purpose_targets=["execution"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "## HUMAN GUIDANCE" in prompt
    assert "Use dependency injection for execution code." in prompt


def test_execution_prompt_includes_all_purpose_guidance(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="All-purpose rule applies everywhere.",
        purpose_targets=["all"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "All-purpose rule applies everywhere." in prompt


def test_execution_prompt_excludes_planning_only_guidance(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Planning-only rule must not reach execution.",
        purpose_targets=["planning"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "## HUMAN GUIDANCE" not in prompt
    assert "Planning-only rule must not reach execution." not in prompt


def test_execution_prompt_excludes_repair_only_guidance(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Repair-only rule must not reach execution.",
        purpose_targets=["repair"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "Repair-only rule must not reach execution." not in prompt


def test_disabled_activation_returns_no_section(db_session, hg_user, hg_project):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Disabled activation hides this rule.",
        purpose_targets=["execution"],
    )
    set_project_activation(
        db_session,
        hg_project.id,
        {
            "table_enabled": True,
            "persistence_enabled": False,
            "render_enabled": False,
            "injection_enabled": False,
            "conflict_detection_enabled": False,
        },
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "Disabled activation hides this rule." not in prompt


def test_table_flag_off_returns_no_section(
    db_session, hg_user, hg_project, monkeypatch
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Table-off rule must not render.",
        purpose_targets=["execution"],
    )
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False)

    section = render_active_human_guidance_section(
        db_session,
        project_id=hg_project.id,
        session_id=701,
        task_id=501,
        user_id=hg_user.id,
        backend="local_openclaw",
        model_family="qwen",
        purpose="execution",
        max_chars=900,
    )

    assert section == ""


def test_backend_mismatch_excludes_guidance(db_session, hg_user, hg_project):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Direct Ollama only.",
        purpose_targets=["execution"],
        backend_targets=["direct_ollama"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "Direct Ollama only." not in prompt


def test_model_mismatch_excludes_guidance(db_session, hg_user, hg_project):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Claude only.",
        purpose_targets=["execution"],
        model_targets=["claude"],
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "Claude only." not in prompt


def test_budget_trimming_records_selected_and_trimmed_usage(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="First execution guidance " + ("A" * 70),
        purpose_targets=["execution"],
        priority=10,
    )
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Second execution guidance " + ("B" * 70),
        purpose_targets=["execution"],
        priority=1,
    )

    section = render_active_human_guidance_section(
        db_session,
        project_id=hg_project.id,
        session_id=701,
        task_id=501,
        user_id=hg_user.id,
        backend="local_openclaw",
        model_family="qwen",
        purpose="execution",
        max_chars=260,
    )

    assert "First execution guidance" in section
    assert "Second execution guidance" not in section
    usages = db_session.query(HumanGuidanceUsage).all()
    assert any(row.selected and row.rendered for row in usages)
    assert any(row.trimmed and not row.rendered for row in usages)


def test_task1_local_openclaw_execution_prompt_receives_guidance(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Task 1 local OpenClaw must see this.",
        purpose_targets=["execution"],
        backend_targets=["local_openclaw"],
        model_targets=["qwen"],
    )

    prompt = assemble_execution_prompt(
        _ctx(
            db_session,
            hg_project,
            backend="local_openclaw",
            model_family="qwen",
            plan_position=1,
        ),
        _step(),
    )

    assert "Task 1 local OpenClaw must see this." in prompt


def test_prompt_section_failures_are_non_fatal(db_session, hg_project):
    with patch(
        "app.services.human_guidance_service.collect_active_guidance",
        side_effect=RuntimeError("boom"),
    ):
        section = render_active_human_guidance_section(
            db_session,
            project_id=hg_project.id,
            session_id=701,
            task_id=501,
            user_id=hg_project.user_id,
            backend="local_openclaw",
            model_family="qwen",
            purpose="execution",
            max_chars=900,
        )

    assert section == ""


def test_prompt_section_does_not_create_operator_guidance_log_entry(
    db_session, hg_user, hg_project
):
    _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Render without LogEntry.",
        purpose_targets=["execution"],
    )

    assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    rows = (
        db_session.query(LogEntry)
        .filter(LogEntry.message.like("[OPERATOR_GUIDANCE]%"))
        .all()
    )
    assert rows == []


def test_recent_operator_guidance_behavior_unchanged(
    db_session, hg_user, hg_project, monkeypatch
):
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False)
    db_session.add(
        LogEntry(
            session_id=701,
            task_id=501,
            level="INFO",
            message="[OPERATOR_GUIDANCE] Keep the existing operator path.",
            log_metadata="{}",
        )
    )
    db_session.commit()

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    assert "Keep the existing operator path." in prompt
    assert "## HUMAN GUIDANCE" not in prompt
