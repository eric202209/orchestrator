from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import (
    GuidanceStatus,
    HumanGuidanceConflict,
    LogEntry,
    Project,
    User,
)
from app.services.human_guidance.service import (
    archive_guidance,
    create_guidance,
    update_guidance,
)
from app.services.orchestration.context.assembly import (
    assemble_execution_prompt,
    render_guidance_remediation_section,
)
from app.services.orchestration.prompt_templates import OrchestrationState


@pytest.fixture()
def hg_user(db_session: Session) -> User:
    user = User(email="hg-p5b@example.com", hashed_password="hashed", is_active=True)
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
        name="hg-p5b-project",
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
    message: str = "Avoid mutable defaults.",
):
    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        purpose_targets=["execution"],
    )
    return entry


def _add_conflict(
    db: Session,
    *,
    guidance_id: int | None,
    project_id: int,
    session_id: int = 701,
    task_id: int = 400,
    pattern: str = "mutable_default",
    status: str = "open",
    source: str = "post_write_check",
    message: str = "Avoid mutable defaults.",
    excerpt: str = "def fn(items=[]): pass",
    detected_at: datetime | None = None,
):
    row = HumanGuidanceConflict(
        guidance_id=guidance_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        task_title=f"Task {task_id}",
        guidance_scope="project",
        guidance_message=message,
        conflict_excerpt=excerpt,
        conflict_patterns=json.dumps([pattern]),
        severity="advisory",
        status=status,
        source=source,
        detected_at=detected_at or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ctx(
    db: Session,
    project: Project,
    *,
    task_id: int = 501,
):
    state = OrchestrationState(
        session_id="701",
        task_description="Implement the current step",
        project_name=project.name,
        project_context="Existing project context.",
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
        task=SimpleNamespace(id=task_id, plan_position=2),
        runtime_service=runtime,
        execution_backend="local_openclaw",
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


def _render(db, project_id: int, *, task_id: int = 501, max_entries=3, max_chars=800):
    return render_guidance_remediation_section(
        db,
        project_id=project_id,
        session_id=701,
        task_id=task_id,
        max_entries=max_entries,
        max_chars=max_chars,
    )


def test_renders_open_post_write_check_conflict(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
    )

    section = _render(db_session, hg_project.id)

    assert "## GUIDANCE REMEDIATION" in section
    assert "Avoid mutable defaults." in section
    assert "pattern=mutable_default" in section
    assert "previous task=400" in section
    assert "def fn(items=[]): pass" in section


def test_excludes_resolved_conflict(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        status="resolved",
    )

    assert _render(db_session, hg_project.id) == ""


def test_excludes_ignored_conflict(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        status="ignored",
    )

    assert _render(db_session, hg_project.id) == ""


def test_excludes_archived_or_disabled_guidance(db_session, hg_user, hg_project):
    archived = _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Archived guidance.",
    )
    disabled = _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Disabled guidance.",
    )
    archive_guidance(db_session, archived.id)
    update_guidance(
        db_session,
        disabled.id,
        status=GuidanceStatus.DISABLED,
        changed_by="test",
    )
    _add_conflict(
        db_session,
        guidance_id=archived.id,
        project_id=hg_project.id,
        message="Archived guidance.",
    )
    _add_conflict(
        db_session,
        guidance_id=disabled.id,
        project_id=hg_project.id,
        pattern="stdout_vs_logging",
        message="Disabled guidance.",
    )

    section = _render(db_session, hg_project.id)

    assert "Archived guidance." not in section
    assert "Disabled guidance." not in section
    assert section == ""


def test_deduplicates_same_guidance_and_pattern(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        excerpt="first",
        detected_at=datetime.now(UTC),
    )
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        excerpt="second",
        detected_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    section = _render(db_session, hg_project.id)

    assert section.count("pattern=mutable_default") == 1
    assert "second" in section


def test_caps_max_entries(db_session, hg_user, hg_project):
    for index in range(5):
        guidance = _add_guidance(
            db_session,
            user_id=hg_user.id,
            project_id=hg_project.id,
            message=f"Guidance {index}",
        )
        _add_conflict(
            db_session,
            guidance_id=guidance.id,
            project_id=hg_project.id,
            pattern=f"pattern_{index}",
            message=f"Guidance {index}",
            task_id=390 + index,
            detected_at=datetime.now(UTC) + timedelta(seconds=index),
        )

    section = _render(db_session, hg_project.id, max_entries=3)

    assert section.count("- Guidance") == 3
    assert "Guidance 4" in section
    assert "Guidance 3" in section
    assert "Guidance 2" in section
    assert "Guidance 1" not in section


def test_caps_max_chars(db_session, hg_user, hg_project):
    guidance = _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Very long guidance " + ("A" * 400),
    )
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        excerpt="Very long excerpt " + ("B" * 400),
    )

    section = _render(db_session, hg_project.id, max_chars=220)

    assert len(section) <= 220
    assert section.endswith("...")


def test_appears_in_execution_prompt_after_human_guidance(
    db_session, hg_user, hg_project, monkeypatch
):
    monkeypatch.setattr("app.config.settings.HUMAN_GUIDANCE_TABLE_ENABLED", True)
    guidance = _add_guidance(
        db_session,
        user_id=hg_user.id,
        project_id=hg_project.id,
        message="Active execution guidance.",
    )
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        message="Active execution guidance.",
    )

    prompt = assemble_execution_prompt(_ctx(db_session, hg_project), _step())

    human_idx = prompt.index("## HUMAN GUIDANCE")
    remediation_idx = prompt.index("## GUIDANCE REMEDIATION")
    task_idx = prompt.index("**Step:**")
    assert human_idx < remediation_idx < task_idx


def test_does_not_appear_when_no_conflicts(db_session, hg_project):
    assert _render(db_session, hg_project.id) == ""


def test_does_not_write_operator_guidance_log_entry(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
    )

    _render(db_session, hg_project.id)

    rows = (
        db_session.query(LogEntry)
        .filter(LogEntry.message.like("[OPERATOR_GUIDANCE]%"))
        .all()
    )
    assert rows == []


def test_non_fatal_on_db_failure():
    class BrokenDb:
        def query(self, *_args, **_kwargs):
            raise RuntimeError("db unavailable")

    section = render_guidance_remediation_section(
        BrokenDb(),
        project_id=1,
        session_id=701,
        task_id=501,
    )

    assert section == ""


def test_current_task_conflict_excluded_if_applicable(db_session, hg_user, hg_project):
    guidance = _add_guidance(db_session, user_id=hg_user.id, project_id=hg_project.id)
    _add_conflict(
        db_session,
        guidance_id=guidance.id,
        project_id=hg_project.id,
        task_id=501,
    )

    assert _render(db_session, hg_project.id, task_id=501) == ""
