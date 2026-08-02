"""Phase 22B-1X1 §8: truthful workspace-cleanup reporting.

R5 recorded "sandbox disposed" and then "workspace un-restored" for the same
dispatch. Both statements were about different workspaces; only the exact
state is reportable.
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from app.services.orchestration.execution.runtime import (
    dispose_runtime_workspace_safely,
)
from app.services.orchestration.phases.failure_flow import _prepare_retry_workspace
from app.services.workspace.task_sandbox_allocator import (
    WORKSPACE_FILE_STATE_RESTORED_BRANCH_CLEANUP_INCOMPLETE,
    WORKSPACE_SANDBOX_FULLY_DISPOSED,
    allocate_task_sandbox,
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


class TestDisposalStatusReporting:
    def test_complete_cleanup_reports_fully_disposed(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        sandbox = allocate_task_sandbox(
            repo,
            project_id=1,
            task_execution_id=310,
            runtime_root=tmp_path / "runtime",
        )
        disposal = dispose_task_sandbox(sandbox, project_root=repo)
        assert disposal.workspace_status == WORKSPACE_SANDBOX_FULLY_DISPOSED

    def test_retained_branch_is_not_reported_as_fully_disposed(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        sandbox = allocate_task_sandbox(
            repo,
            project_id=1,
            task_execution_id=311,
            runtime_root=tmp_path / "runtime",
        )
        # An operator worktree holding the same branch blocks branch cleanup.
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--force",
                str(tmp_path / "operator"),
                sandbox.branch,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        disposal = dispose_task_sandbox(sandbox, project_root=repo)

        assert disposal.worktree_removed is True
        assert disposal.branch_deleted is False
        assert disposal.cleanup_complete is False
        assert (
            disposal.workspace_status
            == WORKSPACE_FILE_STATE_RESTORED_BRANCH_CLEANUP_INCOMPLETE
        )

    def test_canonical_workspace_is_untouched_by_disposal(self, tmp_path):
        repo = _init_git_repo(tmp_path / "project")
        canonical_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        sandbox = allocate_task_sandbox(
            repo,
            project_id=1,
            task_execution_id=312,
            runtime_root=tmp_path / "runtime",
        )
        (sandbox.path / "scratch.txt").write_text("sandbox only\n", encoding="utf-8")

        dispose_task_sandbox(sandbox, project_root=repo)

        assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"
        assert not (repo / "scratch.txt").exists()
        assert (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == canonical_head
        )

    def test_incomplete_cleanup_is_logged_by_the_dispatch_helper(
        self, tmp_path, caplog
    ):
        repo = _init_git_repo(tmp_path / "project")
        sandbox = allocate_task_sandbox(
            repo,
            project_id=1,
            task_execution_id=313,
            runtime_root=tmp_path / "runtime",
        )
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--force",
                str(tmp_path / "operator"),
                sandbox.branch,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        with caplog.at_level(logging.WARNING):
            assert dispose_runtime_workspace_safely(sandbox, project_root=repo) is True

        assert WORKSPACE_FILE_STATE_RESTORED_BRANCH_CLEANUP_INCOMPLETE in caplog.text


class TestRetryReportingVocabulary:
    def _run(self, *, runtime_workspace_used):
        records = []

        def record_live_log_fn(db, session_id, task_id, level, message, **kwargs):
            records.append({"level": level, "message": message, **kwargs})

        ctx = SimpleNamespace(
            db=None,
            session=SimpleNamespace(instance_id="abc"),
            session_id=111,
            task_id=164,
            orchestration_state=None,
            runtime_workspace_used=runtime_workspace_used,
        )
        restored, retry_kwargs, blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("boom"),
            restore_workspace_snapshot_if_needed=None,
            record_live_log_fn=record_live_log_fn,
            logger=logging.getLogger(__name__),
            self_task=SimpleNamespace(request=None),
        )
        assert restored is False
        return records[-1]

    def test_sandbox_dispatch_does_not_claim_the_project_workspace_is_dirty(self):
        record = self._run(runtime_workspace_used=True)
        assert "un-restored" not in record["message"]
        assert (
            record["metadata"]["workspace_status"]
            == "canonical_workspace_unchanged_sandbox_dispatch"
        )
        assert record["metadata"]["runtime_workspace_used"] is True

    def test_canonical_baseline_dispatch_still_reports_unrestored_project_workspace(
        self,
    ):
        record = self._run(runtime_workspace_used=False)
        assert "Project Workspace un-restored" in record["message"]
        assert (
            record["metadata"]["workspace_status"] == "project_workspace_not_restored"
        )
