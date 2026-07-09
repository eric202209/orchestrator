"""Phase 23B: Task Execution Sandbox allocator + collision audit tests.

Infrastructure-only
§8 Stage 1/2. None of this is wired into dispatch -- no existing execution
path is exercised or changed by these tests.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.models import Project
from app.services.workspace.system_settings import (
    DEFAULT_RUNTIME_ROOT,
    RUNTIME_ROOT_KEY,
    get_effective_runtime_root,
    set_setting_value,
)
from app.services.workspace.task_sandbox_allocator import (
    RUNTIME_SCHEMA_VERSION,
    TaskSandboxError,
    allocate_task_sandbox,
    dispose_task_sandbox,
    runtime_task_dir,
)
from scripts.maintenance.workspace_collision_audit import run_audit


def _init_git_repo(repo_dir):
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    return repo_dir


class TestRuntimePathGeneration:
    def test_runtime_task_dir_is_pure_path_math(self, tmp_path):
        runtime_root = tmp_path / "runtime"
        result = runtime_task_dir(runtime_root, project_id=3, task_execution_id=98)
        assert result == runtime_root / "tasks" / "3" / "98"
        assert not result.exists()

    def test_different_task_ids_never_collide(self, tmp_path):
        runtime_root = tmp_path / "runtime"
        first = runtime_task_dir(runtime_root, project_id=3, task_execution_id=1)
        second = runtime_task_dir(runtime_root, project_id=3, task_execution_id=2)
        assert first != second


class TestRuntimeRootSetting:
    def test_default_runtime_root(self, db_session):
        from pathlib import Path

        root = get_effective_runtime_root(db_session)
        assert root == Path(DEFAULT_RUNTIME_ROOT).expanduser().resolve()
        assert root.is_absolute()

    def test_configured_runtime_root_overrides_default(self, db_session, tmp_path):
        configured = tmp_path / "custom-runtime"
        set_setting_value(db_session, RUNTIME_ROOT_KEY, str(configured))
        root = get_effective_runtime_root(db_session)
        assert root == configured.resolve()


class TestGitAllocation:
    def test_allocate_creates_worktree_with_metadata(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"

        sandbox = allocate_task_sandbox(
            project_repo,
            project_id=3,
            task_execution_id=98,
            executor="openclaw",
            runtime_root=runtime_root,
        )

        try:
            assert sandbox.is_git is True
            assert sandbox.path.exists()
            assert (sandbox.path / "README.md").exists()
            assert sandbox.branch == "orchestrator/task-98"

            metadata = sandbox.read_metadata()
            assert metadata["runtime_schema_version"] == RUNTIME_SCHEMA_VERSION
            assert metadata["project_id"] == 3
            assert metadata["task_execution_id"] == 98
            assert metadata["executor"] == "openclaw"
            assert metadata["runtime_state"] == "allocated"
            assert metadata["base_commit"]

            listing = subprocess.run(
                ["git", "worktree", "list"],
                cwd=project_repo,
                capture_output=True,
                text=True,
                check=True,
            )
            assert str(sandbox.path) in listing.stdout
        finally:
            dispose_task_sandbox(sandbox, project_root=project_repo)

    def test_dispose_removes_worktree(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        sandbox = allocate_task_sandbox(
            project_repo,
            project_id=1,
            task_execution_id=1,
            runtime_root=runtime_root,
        )

        dispose_task_sandbox(sandbox, project_root=project_repo)

        assert not sandbox.path.exists()
        listing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=project_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert str(sandbox.path) not in listing.stdout

    def test_concurrent_allocation_for_two_tasks_does_not_collide(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"

        sandbox_a = allocate_task_sandbox(
            project_repo, project_id=3, task_execution_id=10, runtime_root=runtime_root
        )
        sandbox_b = allocate_task_sandbox(
            project_repo, project_id=3, task_execution_id=11, runtime_root=runtime_root
        )

        try:
            assert sandbox_a.path != sandbox_b.path
            assert sandbox_a.path.exists()
            assert sandbox_b.path.exists()
        finally:
            dispose_task_sandbox(sandbox_a, project_root=project_repo)
            dispose_task_sandbox(sandbox_b, project_root=project_repo)

    def test_reallocating_same_task_id_raises(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        sandbox = allocate_task_sandbox(
            project_repo, project_id=3, task_execution_id=42, runtime_root=runtime_root
        )
        try:
            with pytest.raises(TaskSandboxError):
                allocate_task_sandbox(
                    project_repo,
                    project_id=3,
                    task_execution_id=42,
                    runtime_root=runtime_root,
                )
        finally:
            dispose_task_sandbox(sandbox, project_root=project_repo)


class TestNonGitAllocation:
    def test_allocate_falls_back_to_copy(self, tmp_path):
        project_dir = tmp_path / "plain-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("data\n", encoding="utf-8")
        (project_dir / "node_modules").mkdir()
        (project_dir / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
        runtime_root = tmp_path / "runtime"

        sandbox = allocate_task_sandbox(
            project_dir, project_id=5, task_execution_id=7, runtime_root=runtime_root
        )

        try:
            assert sandbox.is_git is False
            assert sandbox.branch is None
            assert (sandbox.path / "file.txt").read_text(encoding="utf-8") == "data\n"
            assert not (sandbox.path / "node_modules").exists()

            metadata = sandbox.read_metadata()
            assert metadata["base_commit"] is None
            assert metadata["runtime_state"] == "allocated"
        finally:
            dispose_task_sandbox(sandbox)

    def test_dispose_removes_copied_directory(self, tmp_path):
        project_dir = tmp_path / "plain-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("data\n", encoding="utf-8")
        runtime_root = tmp_path / "runtime"

        sandbox = allocate_task_sandbox(
            project_dir, project_id=5, task_execution_id=8, runtime_root=runtime_root
        )
        dispose_task_sandbox(sandbox)

        assert not sandbox.path.exists()


class TestMetadataLifecycle:
    def test_update_runtime_state_round_trips(self, tmp_path):
        project_dir = tmp_path / "plain-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("data\n", encoding="utf-8")
        runtime_root = tmp_path / "runtime"

        sandbox = allocate_task_sandbox(
            project_dir, project_id=5, task_execution_id=9, runtime_root=runtime_root
        )
        try:
            sandbox.update_runtime_state("running")
            metadata = sandbox.read_metadata()
            assert metadata["runtime_state"] == "running"
            assert metadata["runtime_schema_version"] == RUNTIME_SCHEMA_VERSION

            raw = json.loads(sandbox.metadata_path.read_text(encoding="utf-8"))
            assert raw == metadata
        finally:
            dispose_task_sandbox(sandbox)

    def test_invalid_runtime_state_rejected(self, tmp_path):
        project_dir = tmp_path / "plain-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("data\n", encoding="utf-8")
        runtime_root = tmp_path / "runtime"

        sandbox = allocate_task_sandbox(
            project_dir, project_id=5, task_execution_id=12, runtime_root=runtime_root
        )
        try:
            with pytest.raises(TaskSandboxError):
                sandbox.update_runtime_state("not_a_real_state")
        finally:
            dispose_task_sandbox(sandbox)


class TestCollisionAudit:
    def test_reports_no_collisions_for_distinct_paths(self, db_session, tmp_path):
        project_a = Project(name="A", workspace_path=str(tmp_path / "a"))
        project_b = Project(name="B", workspace_path=str(tmp_path / "b"))
        db_session.add_all([project_a, project_b])
        db_session.commit()

        report = run_audit(db_session)

        assert report.total_projects == 2
        assert report.collisions == []

    def test_reports_collision_for_shared_path(self, db_session, tmp_path):
        shared = str(tmp_path / "shared")
        project_a = Project(name="A", workspace_path=shared)
        project_b = Project(name="B", workspace_path=shared)
        db_session.add_all([project_a, project_b])
        db_session.commit()

        report = run_audit(db_session)

        assert len(report.collisions) == 1
        group = report.collisions[0]
        assert sorted(group.project_ids) == sorted([project_a.id, project_b.id])

    def test_audit_does_not_modify_projects(self, db_session, tmp_path):
        project = Project(name="A", workspace_path=str(tmp_path / "a"))
        db_session.add(project)
        db_session.commit()
        before = (project.name, project.workspace_path)

        run_audit(db_session)

        db_session.refresh(project)
        after = (project.name, project.workspace_path)
        assert before == after

    def test_excludes_soft_deleted_projects(self, db_session, tmp_path):
        from datetime import UTC, datetime

        shared = str(tmp_path / "shared")
        project_a = Project(name="A", workspace_path=shared)
        project_b = Project(
            name="B", workspace_path=shared, deleted_at=datetime.now(UTC)
        )
        db_session.add_all([project_a, project_b])
        db_session.commit()

        report = run_audit(db_session)

        assert report.total_projects == 1
        assert report.collisions == []
