"""Phase 23D: Runtime Executor Context & dynamic workspace binding tests.

Covers the seams added on top of Phase 23C's runtime workspace redirect:

- RuntimeExecutorContext (Goal 1): single execution-authority object.
- build_runtime_executor_context / maybe_bind_runtime_cwd_override (Goal 2):
  one constructed context feeding both orchestration_state.project_dir and
  OpenClawSessionService.execution_cwd_override, instead of two independently
  set attributes.
- executor_workspace_binding.bind_openclaw_workspace (Goal 3): ephemeral,
  per-invocation OpenClaw config binding, without ever writing to the real
  openclaw.json and without registering a new agent id.
- OpenClawSessionService.bind_runtime_workspace / release_runtime_workspace_binding
  (Goals 3/4): fail-closed when no template agent matches, no-op for a
  non-sandboxed context, and env propagation via _apply_workspace_binding_env.

No planner, validator, repair, or promotion logic is touched or exercised
here.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.config import settings
from app.models import Project, Session as SessionModel
from app.services.agents.openclaw_service import (
    OpenClawAgentSelectionError,
    OpenClawSessionService,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
)
from app.services.orchestration.execution.runtime import (
    build_runtime_executor_context,
    maybe_bind_runtime_cwd_override,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.workspace.task_sandbox_allocator import TaskSandbox


def _fake_sandbox(tmp_path, *, project_id=1, task_execution_id=1) -> TaskSandbox:
    sandbox_dir = (
        tmp_path / "runtime" / "tasks" / str(project_id) / str(task_execution_id)
    )
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    sandbox = TaskSandbox(
        path=sandbox_dir,
        project_id=project_id,
        task_execution_id=task_execution_id,
        executor="openclaw",
        is_git=False,
    )
    sandbox.write_metadata(
        {
            "runtime_schema_version": 1,
            "project_id": project_id,
            "task_execution_id": task_execution_id,
            "executor": "openclaw",
            "created_at": "2026-07-09T00:00:00+00:00",
            "base_commit": "deadbeef",
            "runtime_state": "allocated",
        }
    )
    return sandbox


def _write_openclaw_config(path, *, agent_id: str, workspace) -> None:
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {
                            "id": agent_id,
                            "workspace": str(workspace),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


class TestRuntimeExecutorContext:
    """Goal 1: single execution-authority object."""

    def test_for_sandbox_carries_every_field(self, tmp_path):
        sandbox = _fake_sandbox(tmp_path, project_id=3, task_execution_id=9)
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()

        context = RuntimeExecutorContext.for_sandbox(
            sandbox,
            project_workspace=project_workspace,
            runtime_root=tmp_path / "runtime",
        )

        assert context.is_sandboxed is True
        assert context.executor == "openclaw"
        assert context.runtime_workspace == sandbox.path
        assert context.project_workspace == project_workspace
        assert context.project_id == 3
        assert context.task_execution_id == 9
        assert context.base_commit == "deadbeef"
        assert context.sandbox is sandbox

    def test_for_project_workspace_runtime_equals_project(self, tmp_path):
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()

        context = RuntimeExecutorContext.for_project_workspace(
            project_workspace=project_workspace,
            executor="openclaw",
            project_id=5,
            task_execution_id=11,
        )

        assert context.is_sandboxed is False
        assert context.sandbox is None
        assert (
            context.runtime_workspace == context.project_workspace == project_workspace
        )
        assert context.base_commit is None


class TestBuildRuntimeExecutorContext:
    """Goal 2: one factory replaces the three duplicated worker.py branches."""

    def test_builds_sandbox_context_when_sandbox_given(self, tmp_path):
        sandbox = _fake_sandbox(tmp_path)
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()

        context = build_runtime_executor_context(
            sandbox=sandbox,
            project_workspace=project_workspace,
            executor="openclaw",
            project_id=1,
            task_execution_id=1,
            runtime_root=tmp_path / "runtime",
        )
        assert context.is_sandboxed is True
        assert context.runtime_workspace == sandbox.path

    def test_builds_project_workspace_context_when_no_sandbox(self, tmp_path):
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()

        context = build_runtime_executor_context(
            sandbox=None,
            project_workspace=project_workspace,
            executor="openclaw",
            project_id=1,
            task_execution_id=1,
        )
        assert context.is_sandboxed is False
        assert context.runtime_workspace == project_workspace


class TestMaybeBindRuntimeCwdOverrideBackCompat:
    """maybe_bind_runtime_cwd_override must accept both the new
    RuntimeExecutorContext and the pre-23D bare TaskSandbox convention."""

    def test_binds_from_context(self, tmp_path):
        sandbox = _fake_sandbox(tmp_path)
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )

        class _FakeRuntime:
            execution_cwd_override = None

        runtime_service = _FakeRuntime()
        bound = maybe_bind_runtime_cwd_override(runtime_service, context)
        assert bound is True
        assert runtime_service.execution_cwd_override == str(sandbox.path)

    def test_binds_from_bare_sandbox_back_compat(self, tmp_path):
        sandbox = _fake_sandbox(tmp_path)

        class _FakeRuntime:
            execution_cwd_override = None

        runtime_service = _FakeRuntime()
        bound = maybe_bind_runtime_cwd_override(runtime_service, sandbox)
        assert bound is True
        assert runtime_service.execution_cwd_override == str(sandbox.path)

    def test_noop_when_context_is_none(self):
        class _FakeRuntime:
            execution_cwd_override = None

        runtime_service = _FakeRuntime()
        assert maybe_bind_runtime_cwd_override(runtime_service, None) is False
        assert runtime_service.execution_cwd_override is None


class TestExecutorWorkspaceBindingLayer:
    """Goal 3/4: ephemeral, per-invocation OpenClaw config binding."""

    def test_binds_ephemeral_config_without_touching_real_file(self, tmp_path):
        real_config_path = tmp_path / "openclaw.json"
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        _write_openclaw_config(
            real_config_path, agent_id="orchestrator", workspace=project_workspace
        )
        real_config_before = real_config_path.read_text(encoding="utf-8")

        sandbox = _fake_sandbox(tmp_path)
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )

        binding = bind_openclaw_workspace(context, real_config_path=real_config_path)
        try:
            assert binding.agent_id == "orchestrator"
            assert binding.config_path != real_config_path
            bound_config = json.loads(binding.config_path.read_text(encoding="utf-8"))
            bound_agent = bound_config["agents"]["list"][0]
            assert bound_agent["workspace"] == str(sandbox.path)

            # The real, persistent config is untouched.
            assert real_config_path.read_text(encoding="utf-8") == real_config_before
            real_config_after = json.loads(real_config_path.read_text(encoding="utf-8"))
            assert real_config_after["agents"]["list"][0]["workspace"] == str(
                project_workspace
            )
        finally:
            binding.release()
        assert not binding.config_path.exists()

    def test_fails_closed_when_no_template_agent_matches(self, tmp_path):
        real_config_path = tmp_path / "openclaw.json"
        _write_openclaw_config(
            real_config_path,
            agent_id="orchestrator",
            workspace=tmp_path / "some-other-project",
        )
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        sandbox = _fake_sandbox(tmp_path)
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )

        with pytest.raises(ExecutorWorkspaceBindingError):
            bind_openclaw_workspace(context, real_config_path=real_config_path)

    def test_release_never_raises_on_missing_dir(self, tmp_path):
        real_config_path = tmp_path / "openclaw.json"
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        _write_openclaw_config(
            real_config_path, agent_id="orchestrator", workspace=project_workspace
        )
        sandbox = _fake_sandbox(tmp_path)
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )
        binding = bind_openclaw_workspace(context, real_config_path=real_config_path)
        binding.release()
        binding.release()  # must not raise on double release


class TestOpenClawSessionServiceRuntimeWorkspaceBinding:
    def _make_service(self, db_session) -> OpenClawSessionService:
        project = Project(name="Binding Project")
        db_session.add(project)
        db_session.flush()
        session = SessionModel(name="Binding Session", project_id=project.id)
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return OpenClawSessionService(db_session, session.id)

    def test_noop_for_non_sandboxed_context(self, db_session):
        service = self._make_service(db_session)
        project_workspace_path = "/tmp/does-not-matter"
        context = RuntimeExecutorContext.for_project_workspace(
            project_workspace=project_workspace_path,
            executor="openclaw",
            project_id=1,
            task_execution_id=1,
        )
        service.bind_runtime_workspace(context)
        assert service._openclaw_config_path_override is None
        # Must not add anything to a subprocess env either.
        env = service._apply_workspace_binding_env({})
        assert "OPENCLAW_CONFIG_PATH" not in env

    def test_noop_for_none_context(self, db_session):
        service = self._make_service(db_session)
        service.bind_runtime_workspace(None)
        assert service._openclaw_config_path_override is None

    def test_binds_and_releases_for_sandboxed_context(
        self, db_session, tmp_path, monkeypatch
    ):
        service = self._make_service(db_session)
        real_config_path = tmp_path / "openclaw.json"
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        _write_openclaw_config(
            real_config_path, agent_id="orchestrator", workspace=project_workspace
        )
        monkeypatch.setattr(service, "_openclaw_config_path", lambda: real_config_path)

        sandbox = _fake_sandbox(tmp_path)
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )

        service.bind_runtime_workspace(context)
        assert service._openclaw_config_path_override is not None
        bound_path = service._openclaw_config_path_override
        assert bound_path.exists()

        env = service._apply_workspace_binding_env({})
        assert env["OPENCLAW_CONFIG_PATH"] == str(bound_path)

        service.release_runtime_workspace_binding()
        assert service._openclaw_config_path_override is None
        assert not bound_path.exists()

        # Idempotent: releasing again must not raise.
        service.release_runtime_workspace_binding()

    def test_fails_closed_as_openclaw_agent_selection_error(
        self, db_session, tmp_path, monkeypatch
    ):
        service = self._make_service(db_session)
        real_config_path = tmp_path / "openclaw.json"
        _write_openclaw_config(
            real_config_path,
            agent_id="orchestrator",
            workspace=tmp_path / "unrelated-project",
        )
        monkeypatch.setattr(service, "_openclaw_config_path", lambda: real_config_path)

        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        sandbox = _fake_sandbox(tmp_path)
        context = RuntimeExecutorContext.for_sandbox(
            sandbox, project_workspace=project_workspace
        )

        with pytest.raises(OpenClawAgentSelectionError):
            service.bind_runtime_workspace(context)
        # Fail-closed: no partial override left behind.
        assert service._openclaw_config_path_override is None


class TestConcurrentRuntimeWorkspaceBinding:
    """Two overlapping dispatches against the same project must each get
    their own ephemeral config, never share or clobber one another's."""

    def test_two_concurrent_bindings_do_not_collide(self, tmp_path):
        real_config_path = tmp_path / "openclaw.json"
        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        _write_openclaw_config(
            real_config_path, agent_id="orchestrator", workspace=project_workspace
        )

        results = {}
        errors = []

        def _bind(task_execution_id: int) -> None:
            try:
                sandbox = _fake_sandbox(
                    tmp_path, project_id=1, task_execution_id=task_execution_id
                )
                context = RuntimeExecutorContext.for_sandbox(
                    sandbox, project_workspace=project_workspace
                )
                results[task_execution_id] = bind_openclaw_workspace(
                    context, real_config_path=real_config_path
                )
            except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
                errors.append((task_execution_id, exc))

        threads = [threading.Thread(target=_bind, args=(tid,)) for tid in (201, 202)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            assert errors == []
            assert len(results) == 2
            config_paths = {b.config_path for b in results.values()}
            assert len(config_paths) == 2
            for task_execution_id, binding in results.items():
                bound_config = json.loads(
                    binding.config_path.read_text(encoding="utf-8")
                )
                expected_workspace = str(
                    project_workspace.parent
                    / "runtime"
                    / "tasks"
                    / "1"
                    / str(task_execution_id)
                )
                assert (
                    bound_config["agents"]["list"][0]["workspace"] == expected_workspace
                )
        finally:
            for binding in results.values():
                binding.release()

        # Real config never mutated by either concurrent binder.
        real_config_after = json.loads(real_config_path.read_text(encoding="utf-8"))
        assert real_config_after["agents"]["list"][0]["workspace"] == str(
            project_workspace
        )


class TestFeatureFlagOffRuntimeWorkspaceBinding:
    def test_flag_off_never_reaches_binding(self, db_session, tmp_path):
        """With RUNTIME_WORKSPACE_ENABLED False, worker.py never allocates a
        sandbox, so build_runtime_executor_context always produces a
        non-sandboxed context and bind_runtime_workspace is a no-op --
        byte-identical to pre-23D dispatch."""
        assert settings.RUNTIME_WORKSPACE_ENABLED is False

        project_workspace = tmp_path / "project"
        project_workspace.mkdir()
        context = build_runtime_executor_context(
            sandbox=None,
            project_workspace=project_workspace,
            executor="openclaw",
            project_id=1,
            task_execution_id=1,
        )
        assert context.is_sandboxed is False

        project = Project(name="Flag Off Project")
        db_session.add(project)
        db_session.flush()
        session = SessionModel(name="Flag Off Session", project_id=project.id)
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        service = OpenClawSessionService(db_session, session.id)

        service.bind_runtime_workspace(context)
        assert service._openclaw_config_path_override is None
