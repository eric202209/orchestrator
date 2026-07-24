"""Focused Phase 29D-2 authority and C9 compatibility coverage.

These tests use only disposable ``tmp_path`` fixtures and monkeypatched
read-only Git observations.  They never run a candidate command or Git
mutation.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

import pytest

from app.db_migrations import _migration_047_workspace_base_state_apply_attempt_boundary
from app.models import Base, Project
from app.services.execution.candidate_content import (
    CHANGESET_MEDIA_TYPE,
    _json_projection,
    normalize_media_type,
)
from app.services.execution.workspace_authority import (
    WorkspaceAuthorityError,
    WorkspaceTargetService,
    _paths_conflict,
    inspect_workspace_target,
)


def _fake_git(monkeypatch):
    def run_git(root, args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(root).encode()
        if args == ("rev-parse", "HEAD"):
            return ("a" * 40).encode()
        if args == ("status", "--porcelain=v1", "-z"):
            return b""
        raise AssertionError(args)

    monkeypatch.setattr("app.services.execution.workspace_authority._run_git", run_git)


def test_changeset_media_type_remains_bounded_strict_json():
    payload = b'{"format":"orchestrator-changeset/1"}'
    assert normalize_media_type(CHANGESET_MEDIA_TYPE) == CHANGESET_MEDIA_TYPE
    projection, projection_hash = _json_projection(payload, CHANGESET_MEDIA_TYPE)
    assert projection == {"format": "orchestrator-changeset/1"}
    assert len(projection_hash) == 64


def test_target_registration_normalizes_realpath_and_replays(
    db_session, tmp_path, monkeypatch
):
    _fake_git(monkeypatch)
    monkeypatch.setattr(
        "app.services.execution.workspace_authority.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    project = Project(name="authority-project", workspace_path="project")
    db_session.add(project)
    db_session.flush()

    service = WorkspaceTargetService(db_session)
    first = service.register(project.id, registration_idempotency_key="target-1")
    replay = service.register(project.id, registration_idempotency_key="target-1")
    assert first.target.normalized_realpath == str(root.resolve())
    assert first.target.target_identity.startswith("workspace-sha256:")
    assert replay.replayed is True


def test_target_registration_fails_for_missing_or_non_git_target(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.execution.workspace_authority.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )
    project = Project(name="missing-project", workspace_path="missing")
    db_session.add(project)
    db_session.flush()
    with pytest.raises(WorkspaceAuthorityError) as missing:
        inspect_workspace_target(project, db_session)
    assert missing.value.code == "workspace_target_missing"

    plain = tmp_path / "plain"
    plain.mkdir()
    project.workspace_path = "plain"
    db_session.flush()
    with pytest.raises(WorkspaceAuthorityError) as non_git:
        inspect_workspace_target(project, db_session)
    assert non_git.value.code == "workspace_non_git_unsupported"


def test_active_project_workspace_collision_fails_closed(
    db_session, tmp_path, monkeypatch
):
    _fake_git(monkeypatch)
    monkeypatch.setattr(
        "app.services.execution.workspace_authority.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )
    root = tmp_path / "shared"
    root.mkdir()
    (root / ".git").mkdir()
    first = Project(name="first", workspace_path="shared")
    db_session.add(first)
    db_session.flush()
    WorkspaceTargetService(db_session).register(
        first.id, registration_idempotency_key="target-first"
    )
    second = Project(name="second", workspace_path="shared")
    db_session.add(second)
    db_session.flush()
    with pytest.raises(WorkspaceAuthorityError) as collision:
        WorkspaceTargetService(db_session).register(
            second.id, registration_idempotency_key="target-second"
        )
    assert collision.value.code == "workspace_target_collision"


def test_path_conflict_detects_parent_child_overlap():
    assert _paths_conflict("src/main.py", "src")
    assert _paths_conflict("src", "src/main.py")
    assert not _paths_conflict("src/main.py", "src/test.py")


def test_phase29d2_migration_is_fresh_and_replay_safe(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    _migration_047_workspace_base_state_apply_attempt_boundary(engine)
    _migration_047_workspace_base_state_apply_attempt_boundary(engine)
    names = set(inspect(engine).get_table_names())
    assert {
        "execution_workspace_targets",
        "execution_workspace_base_states",
        "execution_workspace_path_observations",
        "execution_task_apply_approvals",
        "execution_task_apply_attempts",
        "execution_task_apply_precondition_verifications",
    } <= names
