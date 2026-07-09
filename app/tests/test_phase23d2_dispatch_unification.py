"""Phase 23D-2 — Runtime Dispatch Unification.

Every dispatch entry point (fresh dispatch, manual retry, workflow
auto-advance, checkpoint/recovery resume, and the two direct-execution
endpoints) must reach the same Runtime Workspace selection logic:
``maybe_allocate_runtime_workspace`` -> ``build_runtime_executor_context``
-> ``maybe_bind_runtime_cwd_override`` -> ``dispose_runtime_workspace_safely``.

worker.py's branch wiring itself is exercised live (see the Phase 23D-2
report); these tests pin the helper-level invariants the unification relies
on, in particular that a resume execution is indistinguishable from a fresh
dispatch at the allocation layer (same keying, same contract arguments,
same disposal), since Phase 23D-2 removed the resume branch's sandbox
bypass.
"""

import subprocess

import pytest

from app.services.orchestration.execution.runtime import (
    build_runtime_executor_context,
    dispose_runtime_workspace_safely,
    maybe_allocate_runtime_workspace,
    maybe_bind_runtime_cwd_override,
    resolve_workspace_contract_args,
)


@pytest.fixture()
def git_project(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("phase23d2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        cwd=project_dir,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"],
        cwd=project_dir,
        check=True,
    )
    return project_dir


class _FakeOpenClawRuntime:
    execution_cwd_override = None


class TestResumeDispatchParity:
    """A resume execution allocates through the same authority as a fresh
    dispatch: a later task_execution_id for the same project allocates its
    own sandbox after the first one was disposed (discard-on-dispose), and
    the workspace contract validates against that sandbox, never the
    Project Workspace."""

    def test_resume_reallocates_after_disposal_and_contract_targets_sandbox(
        self, git_project, tmp_path
    ):
        runtime_root = tmp_path / "runtime"

        first = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=101,
            task_execution_id=1,
            canonical_baseline_dir=git_project,
            executor="openclaw",
            runtime_root=runtime_root,
        )
        assert first is not None and first.path.exists()
        dispose_runtime_workspace_safely(first, project_root=git_project)
        assert not first.path.exists()

        # The resume attempt re-enters the same allocation call with its own
        # task_execution_id -- no re-attach, no Project Workspace fallback.
        resumed = maybe_allocate_runtime_workspace(
            enabled=True,
            project_id=101,
            task_execution_id=2,
            canonical_baseline_dir=git_project,
            executor="openclaw",
            runtime_root=runtime_root,
        )
        assert resumed is not None
        assert resumed.path != first.path
        assert resumed.path.exists()

        context = build_runtime_executor_context(
            sandbox=resumed,
            project_workspace=git_project,
            executor="openclaw",
            project_id=101,
            task_execution_id=2,
            runtime_root=runtime_root,
        )
        assert context.is_sandboxed
        assert context.runtime_workspace == resumed.path
        assert context.project_workspace == git_project

        contract_args = resolve_workspace_contract_args(
            runtime_context=context,
            project_workspace_path=git_project,
            task_subfolder=None,
            runs_in_canonical_baseline=True,
        )
        assert contract_args["expected_root"] == resumed.path
        assert contract_args["expected_root"] != git_project

        runtime = _FakeOpenClawRuntime()
        assert maybe_bind_runtime_cwd_override(runtime, context) is True
        assert runtime.execution_cwd_override == str(resumed.path)

        dispose_runtime_workspace_safely(resumed, project_root=git_project)
        assert not resumed.path.exists()

    def test_flag_off_resume_context_is_project_workspace(self, git_project):
        sandbox = maybe_allocate_runtime_workspace(
            enabled=False,
            project_id=101,
            task_execution_id=3,
            canonical_baseline_dir=git_project,
            executor="openclaw",
        )
        assert sandbox is None
        context = build_runtime_executor_context(
            sandbox=None,
            project_workspace=git_project,
            executor="openclaw",
            project_id=101,
            task_execution_id=3,
        )
        assert not context.is_sandboxed
        assert context.runtime_workspace == git_project


class TestWorkerResumeBranchUnified:
    """Regression guard: worker.py must not carry a separate, allocation-free
    resume branch. The canonical-baseline allocation site is the single
    authority for every canonical dispatch, resume included."""

    def test_worker_has_single_canonical_allocation_site(self):
        import inspect

        import app.tasks.worker as worker

        source = inspect.getsource(worker.execute_orchestration_task)
        # Exactly one allocation call inside the dispatch task.
        assert source.count("_maybe_allocate_runtime_workspace(") == 1
        # The pre-23D-2 resume bypass branch (a second canonical-baseline
        # branch that never allocated) is gone.
        assert (
            "elif runs_in_canonical_baseline and project and is_resume_execution"
            not in source
        )
