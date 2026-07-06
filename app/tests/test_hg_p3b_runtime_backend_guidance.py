"""HG-P3b runtime provider/model-aware guidance collection."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Project, Session as SessionModel, User
from app.services.human_guidance.conflicts import detect_guidance_task_conflicts
from app.services.human_guidance.plan_validator import (
    check_plan_guidance_violations_if_enabled,
)
from app.services.human_guidance.service import (
    collect_active_guidance,
    create_guidance,
    resolve_guidance_runtime_target,
)
from app.services.orchestration.working_memory import _FILENAME, write_working_memory


@pytest.fixture()
def user(db_session: Session) -> User:
    row = User(
        email="hg-p3b@example.com",
        hashed_password="hashed",
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    row = Project(name="hg-p3b-project", workspace_path=None, user_id=user.id)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    row = SessionModel(
        project_id=project.id,
        name="hg-p3b-session",
        status="running",
        is_active=True,
        instance_id="hg-p3b-instance",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _messages(entries: list[dict]) -> set[str]:
    return {str(entry["message"]) for entry in entries}


def _seed_targeted_guidance(db: Session, user: User, project: Project) -> None:
    create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="all providers all models",
        backend_targets=["all"],
        model_targets=["all"],
    )
    create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="direct ollama provider",
        backend_targets=["direct_ollama"],
        model_targets=["all"],
    )
    create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="openclaw provider",
        backend_targets=["local_openclaw"],
        model_targets=["all"],
    )
    create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="qwen model family",
        backend_targets=["all"],
        model_targets=["qwen"],
    )
    create_guidance(
        db,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="llama model family",
        backend_targets=["all"],
        model_targets=["llama"],
    )


def test_resolve_guidance_runtime_target_keeps_provider_and_model_separate():
    direct_qwen = resolve_guidance_runtime_target(
        backend="direct_ollama",
        runtime_metadata={"model": "qwen3:8b", "model_family": "qwen"},
    )
    assert direct_qwen == {
        "backend": "direct_ollama",
        "model_name": "qwen3:8b",
        "model_family": "qwen",
    }

    openclaw_qwen = resolve_guidance_runtime_target(
        backend="local_openclaw",
        runtime_metadata={"model_family": "qwen-local"},
    )
    assert openclaw_qwen["backend"] == "local_openclaw"
    assert openclaw_qwen["model_family"] == "qwen"

    unknown = resolve_guidance_runtime_target(backend="unavailable_backend")
    assert unknown["backend"] == "unavailable_backend"
    assert unknown["model_family"] == "unknown"


def test_direct_ollama_qwen_matches_provider_and_model_targets(
    db_session: Session, user: User, project: Project
):
    _seed_targeted_guidance(db_session, user, project)
    target = resolve_guidance_runtime_target(
        backend="direct_ollama",
        runtime_metadata={"model_family": "qwen3-coder:30b"},
    )

    entries = collect_active_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        session_id=None,
        task_id=None,
        backend=target["backend"],
        model_family=target["model_family"],
    )

    assert _messages(entries) == {
        "all providers all models",
        "direct ollama provider",
        "qwen model family",
    }


def test_local_openclaw_qwen_matches_openclaw_and_qwen_model_targets(
    db_session: Session, user: User, project: Project
):
    _seed_targeted_guidance(db_session, user, project)
    target = resolve_guidance_runtime_target(
        backend="local_openclaw",
        runtime_metadata={"model_family": "qwen-local"},
    )

    entries = collect_active_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        session_id=None,
        task_id=None,
        backend=target["backend"],
        model_family=target["model_family"],
    )

    assert _messages(entries) == {
        "all providers all models",
        "openclaw provider",
        "qwen model family",
    }


def test_unknown_provider_model_only_matches_all_targets_and_default_is_unfiltered(
    db_session: Session, user: User, project: Project
):
    _seed_targeted_guidance(db_session, user, project)
    target = resolve_guidance_runtime_target(backend="unavailable_backend")

    unknown_entries = collect_active_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        session_id=None,
        task_id=None,
        backend=target["backend"],
        model_family=target["model_family"],
    )
    default_entries = collect_active_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        session_id=None,
        task_id=None,
    )

    assert _messages(unknown_entries) == {"all providers all models"}
    assert _messages(default_entries) == {
        "all providers all models",
        "direct ollama provider",
        "openclaw provider",
        "qwen model family",
        "llama model family",
    }


def test_conflict_detection_uses_provider_and_model_filters(
    db_session: Session,
    user: User,
    project: Project,
    running_session: SessionModel,
):
    create_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="All output must go to stdout. Never use logging.",
        backend_targets=["direct_ollama"],
        model_targets=["qwen"],
    )
    create_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="Never use mutable default arguments.",
        backend_targets=["local_openclaw"],
        model_targets=["qwen"],
    )

    direct_warnings = detect_guidance_task_conflicts(
        db_session,
        project_id=project.id,
        session_id=running_session.id,
        task_id=None,
        user_id=user.id,
        task_title="Add logging.getLogger calls.",
        task_description="",
        backend="direct_ollama",
        model_family="qwen",
    )
    openclaw_warnings = detect_guidance_task_conflicts(
        db_session,
        project_id=project.id,
        session_id=running_session.id,
        task_id=None,
        user_id=user.id,
        task_title="Add logging.getLogger calls.",
        task_description="",
        backend="local_openclaw",
        model_family="qwen",
    )

    assert len(direct_warnings) == 1
    assert openclaw_warnings == []


def test_p2b_validator_uses_provider_and_model_filters(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    user: User,
    project: Project,
    running_session: SessionModel,
):
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)
    create_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="All output must go to stdout. Never use logging.",
        backend_targets=["direct_ollama"],
        model_targets=["qwen"],
    )
    create_guidance(
        db_session,
        user_id=user.id,
        project_id=project.id,
        scope="project",
        message="Never use mutable default arguments.",
        backend_targets=["local_openclaw"],
        model_targets=["qwen"],
    )
    plan = [
        {
            "step_number": 1,
            "description": "write",
            "ops": [
                {
                    "op": "write_file",
                    "path": "foo.py",
                    "content": "import logging\nlogger = logging.getLogger(__name__)",
                }
            ],
            "commands": [],
        }
    ]

    direct_violations = check_plan_guidance_violations_if_enabled(
        db_session,
        project_id=project.id,
        session_id=running_session.id,
        task_id=None,
        user_id=user.id,
        plan_steps=plan,
        backend="direct_ollama",
        model_family="qwen",
    )
    openclaw_violations = check_plan_guidance_violations_if_enabled(
        db_session,
        project_id=project.id,
        session_id=running_session.id,
        task_id=None,
        user_id=user.id,
        plan_steps=plan,
        backend="local_openclaw",
        model_family="qwen",
    )

    assert any("stdout_vs_logging" in violation for violation in direct_violations)
    assert openclaw_violations == []


def test_write_working_memory_table_path_uses_provider_and_model_filters(
    db_session: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    user: User,
    project: Project,
    running_session: SessionModel,
):
    monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
    _seed_targeted_guidance(db_session, user, project)
    state = MagicMock()
    state.project_dir = str(tmp_path)
    state.session_id = running_session.id
    state.plan = []
    state.changed_files = []
    state.validation_history = []
    state.project_context = ""
    task = MagicMock()
    task.id = 1
    task.title = "provider/model filtered wm"
    logger = MagicMock()

    write_working_memory(
        orchestration_state=state,
        task=task,
        summary="done",
        logger=logger,
        db=db_session,
        guidance_backend="local_openclaw",
        guidance_model_family="qwen",
    )

    wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
    assert _messages(wm["human_guidance"]) == {
        "all providers all models",
        "openclaw provider",
        "qwen model family",
    }
