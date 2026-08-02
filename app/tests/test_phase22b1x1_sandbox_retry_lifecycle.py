"""Phase 22B-1X1: managed sandbox branch lifecycle and retry re-allocation.

The R5 dogfood attempt failed deterministically because disposal removed the
worktree but kept ``orchestrator/task-244``, so every Celery retry re-ran
``git worktree add -b orchestrator/task-244`` against an existing branch.
"""

from __future__ import annotations

import subprocess

import pytest

from app.services.workspace.sandbox_branch_maintenance import (
    cleanup_sandbox_branches,
    inventory_sandbox_branches,
)
from app.services.workspace.task_sandbox_allocator import (
    TaskSandboxError,
    allocate_task_sandbox,
    delete_managed_sandbox_branch,
    dispose_task_sandbox,
    managed_sandbox_branch,
    managed_sandbox_branch_execution_id,
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


def _branches(repo_dir):
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


@pytest.fixture()
def repo(tmp_path):
    return _init_git_repo(tmp_path / "project")


@pytest.fixture()
def runtime_root(tmp_path):
    return tmp_path / "runtime"


class TestManagedBranchIdentity:
    def test_managed_branch_name_and_owner(self):
        assert managed_sandbox_branch(244) == "orchestrator/task-244"
        assert managed_sandbox_branch_execution_id("orchestrator/task-244") == 244

    @pytest.mark.parametrize(
        "name",
        [
            "main",
            "orchestrator/task-244-manual",
            "orchestrator/task-",
            "feature/orchestrator/task-244",
            "",
        ],
    )
    def test_unmanaged_names_are_not_owned(self, name):
        assert managed_sandbox_branch_execution_id(name) is None


class TestBranchDeletionSafety:
    def test_unrelated_similar_prefix_branch_is_protected(self, repo):
        subprocess.run(
            ["git", "branch", "orchestrator/task-244-manual"], cwd=repo, check=True
        )
        result = delete_managed_sandbox_branch(repo, "orchestrator/task-244-manual")
        assert result == {
            "deleted": False,
            "reason": "branch_not_managed",
            "branch": "orchestrator/task-244-manual",
        }
        assert "orchestrator/task-244-manual" in _branches(repo)

    def test_canonical_branch_is_never_deleted(self, repo):
        canonical = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert delete_managed_sandbox_branch(repo, canonical)["deleted"] is False
        assert canonical in _branches(repo)

    def test_branch_owner_mismatch_is_refused(self, repo):
        subprocess.run(["git", "branch", "orchestrator/task-9"], cwd=repo, check=True)
        result = delete_managed_sandbox_branch(
            repo, "orchestrator/task-9", expected_task_execution_id=10
        )
        assert result["reason"] == "branch_owner_mismatch"
        assert "orchestrator/task-9" in _branches(repo)

    def test_checked_out_branch_is_refused(self, repo, runtime_root):
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=77, runtime_root=runtime_root
        )
        result = delete_managed_sandbox_branch(repo, sandbox.branch)
        assert result["reason"] == "branch_checked_out"
        assert sandbox.branch in _branches(repo)

    def test_absent_branch_is_idempotent(self, repo):
        assert delete_managed_sandbox_branch(repo, "orchestrator/task-4242") == {
            "deleted": False,
            "reason": "branch_absent",
            "branch": "orchestrator/task-4242",
        }


class TestDisposalContract:
    def test_disposal_removes_worktree_and_managed_branch(self, repo, runtime_root):
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=101, runtime_root=runtime_root
        )
        assert sandbox.branch in _branches(repo)

        disposal = dispose_task_sandbox(sandbox, project_root=repo)

        assert disposal.worktree_removed is True
        assert disposal.branch_deleted is True
        assert disposal.cleanup_complete is True
        assert disposal.workspace_status == "sandbox_fully_disposed"
        assert sandbox.branch not in _branches(repo)
        assert not sandbox.path.exists()

    def test_repeated_disposal_is_idempotent(self, repo, runtime_root):
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=102, runtime_root=runtime_root
        )
        dispose_task_sandbox(sandbox, project_root=repo)
        again = dispose_task_sandbox(sandbox, project_root=repo)
        assert again.cleanup_complete is True
        assert again.branch_reason == "branch_absent"

    def test_interrupted_disposal_leaves_recoverable_state(self, repo, runtime_root):
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=103, runtime_root=runtime_root
        )
        # Simulate a disposal interrupted after the directory was destroyed but
        # before Git metadata or the branch were cleaned up.
        import shutil

        shutil.rmtree(sandbox.path)
        disposal = dispose_task_sandbox(sandbox, project_root=repo)
        assert disposal.worktree_removed is True
        assert disposal.branch_deleted is True
        assert sandbox.branch not in _branches(repo)

    def test_non_git_sandbox_disposal_reports_complete(self, tmp_path, runtime_root):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "file.txt").write_text("x", encoding="utf-8")
        sandbox = allocate_task_sandbox(
            plain, project_id=1, task_execution_id=104, runtime_root=runtime_root
        )
        disposal = dispose_task_sandbox(sandbox, project_root=plain)
        assert disposal.cleanup_complete is True
        assert not sandbox.path.exists()


class TestRetryReallocation:
    def test_allocate_dispose_reallocate_same_execution(self, repo, runtime_root):
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=244, runtime_root=runtime_root
        )
        dispose_task_sandbox(sandbox, project_root=repo)

        retried = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=244, runtime_root=runtime_root
        )
        assert retried.branch == "orchestrator/task-244"
        assert retried.path.exists()

        final = dispose_task_sandbox(retried, project_root=repo)
        assert final.cleanup_complete is True
        assert "orchestrator/task-244" not in _branches(repo)

    def test_branch_residue_without_worktree_does_not_block_retry(
        self, repo, runtime_root
    ):
        # Exactly the R5 residue: branch exists, no worktree, no sandbox dir.
        subprocess.run(["git", "branch", "orchestrator/task-244"], cwd=repo, check=True)
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=244, runtime_root=runtime_root
        )
        assert sandbox.path.exists()
        dispose_task_sandbox(sandbox, project_root=repo)
        assert "orchestrator/task-244" not in _branches(repo)

    def test_live_worktree_on_same_branch_blocks_allocation_with_evidence(
        self, repo, runtime_root
    ):
        allocate_task_sandbox(
            repo, project_id=1, task_execution_id=250, runtime_root=runtime_root
        )
        with pytest.raises(TaskSandboxError) as excinfo:
            allocate_task_sandbox(
                repo, project_id=2, task_execution_id=250, runtime_root=runtime_root
            )
        assert "branch_checked_out" in str(excinfo.value)

    def test_repeated_retry_allocation_cycles(self, repo, runtime_root):
        for _ in range(3):
            sandbox = allocate_task_sandbox(
                repo, project_id=1, task_execution_id=260, runtime_root=runtime_root
            )
            dispose_task_sandbox(sandbox, project_root=repo)
        assert "orchestrator/task-260" not in _branches(repo)

    def test_retry_budget_is_not_consumed_by_branch_residue(self, repo, runtime_root):
        """The R5 failure mode: every retry died on the same residual branch.

        Allocation must now succeed on each attempt, so no retry is spent on a
        deterministic branch-residue error.
        """
        attempts = []
        for _ in range(4):
            try:
                sandbox = allocate_task_sandbox(
                    repo, project_id=1, task_execution_id=244, runtime_root=runtime_root
                )
            except TaskSandboxError as exc:
                attempts.append(f"failed:{exc}")
                continue
            attempts.append("allocated")
            # Simulate a retryable failure: dispose without any completion.
            dispose_task_sandbox(sandbox, project_root=repo)

        assert attempts == ["allocated"] * 4
        assert "orchestrator/task-244" not in _branches(repo)

    def test_allocation_failure_after_branch_creation_leaves_no_residue(
        self, repo, runtime_root, monkeypatch
    ):
        """A worktree add that creates the branch and then fails must not wedge
        the next attempt."""
        import app.services.workspace.task_sandbox_allocator as allocator

        real_run = allocator.subprocess.run

        def failing_worktree_add(args, **kwargs):
            if args[:3] == ["git", "worktree", "add"]:
                # Create the branch, then report the add as failed.
                real_run(["git", "branch", args[4]], cwd=kwargs.get("cwd"), check=True)
                return subprocess.CompletedProcess(args, 1, "", "simulated add failure")
            return real_run(args, **kwargs)

        monkeypatch.setattr(allocator.subprocess, "run", failing_worktree_add)
        with pytest.raises(TaskSandboxError):
            allocate_task_sandbox(
                repo, project_id=1, task_execution_id=270, runtime_root=runtime_root
            )
        monkeypatch.undo()

        # The next attempt clears its own residue and succeeds.
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=270, runtime_root=runtime_root
        )
        assert sandbox.path.exists()
        assert dispose_task_sandbox(sandbox, project_root=repo).cleanup_complete is True

    def test_worktree_remove_failure_falls_back_and_still_cleans_branch(
        self, repo, runtime_root, monkeypatch
    ):
        import app.services.workspace.task_sandbox_allocator as allocator

        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=280, runtime_root=runtime_root
        )
        real_run = allocator.subprocess.run

        def failing_worktree_remove(args, **kwargs):
            if args[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(
                    args, 1, "", "simulated remove failure"
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr(allocator.subprocess, "run", failing_worktree_remove)
        disposal = dispose_task_sandbox(sandbox, project_root=repo)

        assert disposal.worktree_remove_fallback is True
        assert disposal.worktree_removed is True
        assert disposal.branch_deleted is True
        assert disposal.cleanup_complete is True
        assert "orchestrator/task-280" not in _branches(repo)

    def test_worktree_without_the_expected_branch_is_not_touched(
        self, repo, runtime_root
    ):
        """A sandbox record naming a branch the worktree does not hold must not
        cause an unrelated branch to be deleted."""
        sandbox = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=290, runtime_root=runtime_root
        )
        subprocess.run(["git", "branch", "operator/unrelated"], cwd=repo, check=True)
        sandbox.branch = "operator/unrelated"

        disposal = dispose_task_sandbox(sandbox, project_root=repo)

        assert disposal.branch_deleted is False
        assert disposal.branch_reason == "branch_not_managed"
        assert disposal.cleanup_complete is False
        assert "operator/unrelated" in _branches(repo)
        # The real managed branch is now orphaned residue, and the next
        # allocation for the same execution still recovers.
        assert "orchestrator/task-290" in _branches(repo)
        recovered = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=290, runtime_root=runtime_root
        )
        assert recovered.path.exists()


class TestHistoricalBranchInventory:
    def test_inventory_reports_managed_residue_only(self, repo, runtime_root):
        for branch in ("orchestrator/task-241", "orchestrator/task-242"):
            subprocess.run(["git", "branch", branch], cwd=repo, check=True)
        subprocess.run(["git", "branch", "operator/keep-me"], cwd=repo, check=True)
        live = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=243, runtime_root=runtime_root
        )

        inventory = inventory_sandbox_branches(repo)
        names = {item["branch"] for item in inventory["branches"]}
        assert names == {
            "orchestrator/task-241",
            "orchestrator/task-242",
            "orchestrator/task-243",
        }
        by_name = {item["branch"]: item for item in inventory["branches"]}
        assert by_name["orchestrator/task-241"]["safe_to_remove"] is True
        assert by_name["orchestrator/task-241"]["worktree_present"] is False
        assert by_name["orchestrator/task-243"]["worktree_present"] is True
        assert by_name["orchestrator/task-243"]["safe_to_remove"] is False
        assert by_name["orchestrator/task-243"]["checked_out_in"] == str(live.path)

    def test_cleanup_is_read_only_without_apply(self, repo):
        subprocess.run(["git", "branch", "orchestrator/task-241"], cwd=repo, check=True)
        report = cleanup_sandbox_branches(repo)
        assert report["applied"] is False
        assert report["deleted_count"] == 0
        assert report["results"] == [
            {
                "branch": "orchestrator/task-241",
                "deleted": False,
                "reason": "would_delete",
            }
        ]
        assert "orchestrator/task-241" in _branches(repo)

    def test_cleanup_applies_with_before_after_ledger(self, repo, runtime_root):
        for branch in ("orchestrator/task-241", "orchestrator/task-242"):
            subprocess.run(["git", "branch", branch], cwd=repo, check=True)
        subprocess.run(["git", "branch", "operator/keep-me"], cwd=repo, check=True)
        live = allocate_task_sandbox(
            repo, project_id=1, task_execution_id=243, runtime_root=runtime_root
        )

        report = cleanup_sandbox_branches(repo, apply=True)

        assert report["deleted_count"] == 2
        assert report["before"]["count"] == 3
        assert report["after"]["count"] == 1
        remaining = _branches(repo)
        assert "orchestrator/task-241" not in remaining
        assert "orchestrator/task-242" not in remaining
        assert "operator/keep-me" in remaining
        assert live.branch in remaining

    def test_cleanup_restricted_to_requested_branches(self, repo):
        for branch in ("orchestrator/task-241", "orchestrator/task-242"):
            subprocess.run(["git", "branch", branch], cwd=repo, check=True)
        report = cleanup_sandbox_branches(
            repo, branches=["orchestrator/task-241"], apply=True
        )
        assert report["deleted_count"] == 1
        assert "orchestrator/task-241" not in _branches(repo)
        assert "orchestrator/task-242" in _branches(repo)
