"""Phase 23C: runtime workspace integration (execution redirect) tests.

Covers the dispatch-lifecycle seams added on top of the Phase 23B allocator:
allocation/binding/contract-selection/disposal helpers in
app/services/orchestration/execution/runtime.py (unit-testable without
invoking the full Celery task, per that module's own docstring), plus the
OpenClawSessionService cwd/guard override and the RUNTIME_WORKSPACE_ENABLED
default. No planner, validator decision, or OpenClaw invocation logic is
exercised here beyond the seams this phase touches.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from app.config import settings
from app.models import Project, Session as SessionModel
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.orchestration.execution.runtime import (
    dispose_runtime_workspace_safely,
    maybe_allocate_runtime_workspace,
    maybe_bind_runtime_cwd_override,
    resolve_workspace_contract_args,
)
from app.services.orchestration.validation.workspace_guard import (
    verify_workspace_contract,
)
from app.services.workspace.task_sandbox_allocator import (
    TaskSandbox,
    TaskSandboxError,
    dispose_task_sandbox,
)


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


class TestFeatureFlagDefault:
    def test_runtime_workspace_enabled_defaults_true(self):
        assert settings.RUNTIME_WORKSPACE_ENABLED is True


class TestFeatureFlagOff:
    def test_disabled_allocator_makes_no_allocation(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"

        sandbox = maybe_allocate_runtime_workspace(
            enabled=False,
            project_id=1,
            task_execution_id=1,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )

        assert sandbox is None
        assert not runtime_root.exists()

    def test_existing_dispatch_regression_contract_args_unchanged(self, tmp_path):
        """With no sandbox, contract args must be byte-identical to pre-23C
        behavior: expected_root is the Project Workspace, subfolder and
        allow_project_root_task_dir come straight from the caller."""
        project_workspace = tmp_path / "workspace" / "my-project"
        args = resolve_workspace_contract_args(
            runtime_sandbox=None,
            project_workspace_path=project_workspace,
            task_subfolder="task-42",
            runs_in_canonical_baseline=True,
        )
        assert args == {
            "expected_root": project_workspace,
            "expected_task_subfolder": "task-42",
            "allow_project_root_task_dir": True,
        }

    def test_dispose_noop_when_no_sandbox(self):
        assert dispose_runtime_workspace_safely(None, project_root=None) is False


class TestFeatureFlagOnAllocation:
    def test_enabled_allocator_creates_worktree(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"

        sandbox = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=7,
            task_execution_id=99,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )

        try:
            assert isinstance(sandbox, TaskSandbox)
            assert sandbox.is_git is True
            assert sandbox.path.exists()
            assert (sandbox.path / "README.md").exists()
        finally:
            dispose_task_sandbox(sandbox, project_root=project_repo)

    def test_allocation_failure_raises_not_falls_back(self, tmp_path):
        """Allocation errors must propagate (worker.py's outer exception
        handler owns failure reporting) -- never silently execute in the
        Project Workspace instead."""
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        first = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=3,
            task_execution_id=1,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )
        try:
            with pytest.raises(TaskSandboxError):
                maybe_allocate_runtime_workspace(
                    enabled=True,
                    project_id=3,
                    task_execution_id=1,
                    canonical_baseline_dir=project_repo,
                    executor="openclaw",
                    runtime_root=runtime_root,
                )
        finally:
            dispose_task_sandbox(first, project_root=project_repo)


class TestConcurrentRuntimeWorkspaces:
    def test_multiple_concurrent_runtime_workspaces_do_not_collide(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"

        results = {}
        errors = []

        def _allocate(task_execution_id: int) -> None:
            try:
                results[task_execution_id] = maybe_allocate_runtime_workspace(
                    enabled=True,
                    project_id=42,
                    task_execution_id=task_execution_id,
                    canonical_baseline_dir=project_repo,
                    executor="openclaw",
                    runtime_root=runtime_root,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append((task_execution_id, exc))

        threads = [
            threading.Thread(target=_allocate, args=(task_execution_id,))
            for task_execution_id in (101, 102, 103)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            assert errors == []
            assert len(results) == 3
            paths = {sandbox.path for sandbox in results.values()}
            assert len(paths) == 3
            for sandbox in results.values():
                assert sandbox.path.exists()
        finally:
            for sandbox in results.values():
                dispose_task_sandbox(sandbox, project_root=project_repo)


class TestRuntimeExecutionPathBinding:
    def test_binds_cwd_override_when_sandbox_and_attribute_present(self, tmp_path):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        sandbox = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=5,
            task_execution_id=5,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )

        class _FakeRuntime:
            execution_cwd_override = None

        runtime_service = _FakeRuntime()
        try:
            bound = maybe_bind_runtime_cwd_override(runtime_service, sandbox)
            assert bound is True
            assert runtime_service.execution_cwd_override == str(sandbox.path)
        finally:
            dispose_task_sandbox(sandbox, project_root=project_repo)

    def test_noop_when_no_sandbox(self):
        class _FakeRuntime:
            execution_cwd_override = None

        runtime_service = _FakeRuntime()
        bound = maybe_bind_runtime_cwd_override(runtime_service, None)
        assert bound is False
        assert runtime_service.execution_cwd_override is None

    def test_noop_when_backend_has_no_override_attribute(self, tmp_path):
        """Non-OpenClaw backends (e.g. StubRuntime) have no
        execution_cwd_override attribute; binding must not raise."""
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        sandbox = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=6,
            task_execution_id=6,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )

        class _BackendWithoutOverride:
            pass

        runtime_service = _BackendWithoutOverride()
        try:
            bound = maybe_bind_runtime_cwd_override(runtime_service, sandbox)
            assert bound is False
            assert not hasattr(runtime_service, "execution_cwd_override")
        finally:
            dispose_task_sandbox(sandbox, project_root=project_repo)


class TestOpenClawServiceExecutionCwdOverride:
    def test_resolve_execution_cwd_prefers_override(self, db_session):
        project = Project(name="Override Project")
        db_session.add(project)
        db_session.flush()
        session = SessionModel(name="Override Session", project_id=project.id)
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)

        service = OpenClawSessionService(db_session, session.id)
        assert service.execution_cwd_override is None

        service.execution_cwd_override = "/tmp/some/sandbox/path"
        assert service._resolve_execution_cwd() == "/tmp/some/sandbox/path"

    def test_resolve_project_root_for_workspace_guard_prefers_override(
        self, db_session
    ):
        project = Project(name="Guard Override Project")
        db_session.add(project)
        db_session.flush()
        session = SessionModel(name="Guard Override Session", project_id=project.id)
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)

        service = OpenClawSessionService(db_session, session.id)
        service.execution_cwd_override = "/tmp/some/sandbox/path"

        assert (
            service._resolve_project_root_for_workspace_guard()
            == "/tmp/some/sandbox/path"
        )

    def test_resolve_execution_cwd_falls_back_without_override(self, db_session):
        project = Project(name="No Override Project")
        db_session.add(project)
        db_session.flush()
        session = SessionModel(name="No Override Session", project_id=project.id)
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)

        service = OpenClawSessionService(db_session, session.id)
        # No override set: falls through to the pre-23C derivation path,
        # which for a project with no workspace_path/task returns a real
        # (non-None) resolved path rather than raising.
        result = service._resolve_execution_cwd()
        assert result is not None


class TestWorkspaceContractRuntimeMode:
    def test_contract_passes_when_task_dir_matches_sandbox(self, tmp_path):
        sandbox_path = tmp_path / "runtime" / "tasks" / "1" / "1"
        sandbox_path.mkdir(parents=True)

        class _FakeSandbox:
            path = sandbox_path

        args = resolve_workspace_contract_args(
            runtime_sandbox=_FakeSandbox(),
            project_workspace_path=tmp_path / "workspace" / "proj",
            task_subfolder="task-1",
            runs_in_canonical_baseline=True,
        )
        result = verify_workspace_contract(
            expected_root=args["expected_root"],
            task_dir=sandbox_path,
            expected_task_subfolder=args["expected_task_subfolder"],
            allow_project_root_task_dir=args["allow_project_root_task_dir"],
        )
        assert result["ok"] is True

    def test_contract_fails_if_task_dir_is_project_workspace_while_sandbox_active(
        self, tmp_path
    ):
        """Regression guard: if redirection is ever silently skipped while a
        sandbox is recorded active, the contract must catch the mismatch
        rather than silently validating against the Project Workspace."""
        sandbox_path = tmp_path / "runtime" / "tasks" / "1" / "1"
        sandbox_path.mkdir(parents=True)
        project_workspace = tmp_path / "workspace" / "proj"
        project_workspace.mkdir(parents=True)

        class _FakeSandbox:
            path = sandbox_path

        args = resolve_workspace_contract_args(
            runtime_sandbox=_FakeSandbox(),
            project_workspace_path=project_workspace,
            task_subfolder="task-1",
            runs_in_canonical_baseline=True,
        )
        result = verify_workspace_contract(
            expected_root=args["expected_root"],
            task_dir=project_workspace,
            expected_task_subfolder=args["expected_task_subfolder"],
            allow_project_root_task_dir=args["allow_project_root_task_dir"],
        )
        assert result["ok"] is False


class TestCleanupOutcomes:
    """Cleanup must run identically for success, failure, cancellation,
    timeout, and exception -- Goal 5 requires all five to reach disposal
    with no leaked runtime directory. Each test simulates worker.py's
    try/finally shape: allocate, then hit one of the five outcomes, then
    dispose from `finally`."""

    def _allocate(self, tmp_path, task_execution_id: int):
        project_repo = _init_git_repo(tmp_path / "project")
        runtime_root = tmp_path / "runtime"
        sandbox = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=1,
            task_execution_id=task_execution_id,
            canonical_baseline_dir=project_repo,
            executor="openclaw",
            runtime_root=runtime_root,
        )
        return project_repo, sandbox

    def test_successful_completion_cleanup(self, tmp_path):
        project_repo, sandbox = self._allocate(tmp_path, 1)
        sandbox_path = sandbox.path
        try:
            pass  # simulated successful step loop
        finally:
            disposed = dispose_runtime_workspace_safely(
                sandbox, project_root=project_repo
            )
        assert disposed is True
        assert not sandbox_path.exists()

    def test_failed_completion_cleanup(self, tmp_path):
        project_repo, sandbox = self._allocate(tmp_path, 2)
        sandbox_path = sandbox.path
        try:
            raise RuntimeError("simulated step failure")
        except RuntimeError:
            pass
        finally:
            disposed = dispose_runtime_workspace_safely(
                sandbox, project_root=project_repo
            )
        assert disposed is True
        assert not sandbox_path.exists()

    def test_cancelled_execution_cleanup(self, tmp_path):
        project_repo, sandbox = self._allocate(tmp_path, 3)
        sandbox_path = sandbox.path

        class _CancelledError(Exception):
            pass

        try:
            raise _CancelledError("execution cancelled")
        except _CancelledError:
            pass
        finally:
            disposed = dispose_runtime_workspace_safely(
                sandbox, project_root=project_repo
            )
        assert disposed is True
        assert not sandbox_path.exists()

    def test_timeout_cleanup(self, tmp_path):
        from billiard.exceptions import SoftTimeLimitExceeded

        project_repo, sandbox = self._allocate(tmp_path, 4)
        sandbox_path = sandbox.path
        try:
            raise SoftTimeLimitExceeded()
        except SoftTimeLimitExceeded:
            pass
        finally:
            disposed = dispose_runtime_workspace_safely(
                sandbox, project_root=project_repo
            )
        assert disposed is True
        assert not sandbox_path.exists()

    def test_exception_cleanup(self, tmp_path):
        project_repo, sandbox = self._allocate(tmp_path, 5)
        sandbox_path = sandbox.path
        with pytest.raises(ValueError):
            try:
                raise ValueError("unexpected error")
            finally:
                disposed = dispose_runtime_workspace_safely(
                    sandbox, project_root=project_repo
                )
        assert disposed is True
        assert not sandbox_path.exists()

    def test_dispose_never_raises_even_if_underlying_dispose_fails(
        self, tmp_path, monkeypatch
    ):
        project_repo, sandbox = self._allocate(tmp_path, 6)

        def _boom(*_args, **_kwargs):
            raise OSError("simulated disposal failure")

        monkeypatch.setattr(
            "app.services.orchestration.execution.runtime.dispose_task_sandbox",
            _boom,
        )
        # Must not raise despite the underlying disposal failing.
        disposed = dispose_runtime_workspace_safely(sandbox, project_root=project_repo)
        assert disposed is True

        # Clean up for real so the test doesn't leak the fixture directory.
        monkeypatch.undo()
        dispose_task_sandbox(sandbox, project_root=project_repo)
