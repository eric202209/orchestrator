"""P42-V17-O provider-free regressions: nested runtime workspace orientation.

P42-V17-L Phase D: TE 395 ran in ``runtime/tasks/178/395``, a non-Git
filtered copy nested inside the Orchestrator checkout (which ignores
``/runtime/``).  Git resolved to the Orchestrator repository, tracked nothing
under the workspace, and orientation ended with
``no_tracked_path_matched_a_task_literal`` instead of the V17-K workspace
listing.  These tests reproduce that layout with temporary repositories only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.services.orchestration.planning import repository_orientation as ro
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    build_discovery_prompt,
    execute_discovery_request,
    parse_discovery_request,
)
from app.services.orchestration.planning.repository_orientation import (
    ORIENTATION_BYTE_BUDGET,
    ORIENTATION_PATH_LIMIT,
    ORIENTATION_SCOPE_TRACKED,
    ORIENTATION_SCOPE_WORKSPACE,
    ORIENTATION_UNAVAILABLE_NO_CANDIDATES,
    ORIENTATION_UNAVAILABLE_WORKSPACE_EMPTY,
    derive_repository_orientation,
    render_repository_orientation,
)
from app.tests.test_p42v17k_non_git_discovery_path_grounding import (  # noqa: F401
    INVENTORY_FILES,
    INVENTORY_LISTING,
    TASK,
    _assert_relative,
    _deny_network,
    _derive,
    _git,
    _inventory,
    _write,
)

REQUIRED = {"inventory/models.py", "inventory/service.py", "tests/test_inventory.py"}
OUTER_FILES = {
    ".gitignore": "/runtime/\n__pycache__/\n",
    "app/services/inventory_orchestrator.py": "X = 1\n",
    "app/tests/test_inventory_orchestrator.py": "def test_x():\n    pass\n",
}


def _outer_checkout(tmp_path: Path) -> Path:
    outer = _write(tmp_path / "orchestrator", OUTER_FILES)
    _git(outer, "init", "-q")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "init")
    return outer


def _te395(tmp_path: Path) -> tuple[Path, Path]:
    outer = _outer_checkout(tmp_path)
    workspace = _write(outer / "runtime" / "tasks" / "178" / "395", INVENTORY_FILES)
    (workspace / "inventory" / "__pycache__").mkdir()
    (workspace / ".agent").mkdir()
    (workspace / ".agent" / "note.md").write_text("x", encoding="utf-8")
    return outer, workspace


def _toplevel(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# Standalone non-Git workspace (V17-K behavior unchanged) --------------------


def test_standalone_non_git_workspace_uses_listing(tmp_path):
    orientation = _derive(_inventory(tmp_path))
    assert orientation.scope == ORIENTATION_SCOPE_WORKSPACE
    assert set(orientation.paths) == INVENTORY_LISTING


# TE 395-equivalent canonical runtime layout ---------------------------------


def test_te395_nested_runtime_workspace_uses_workspace_listing(tmp_path):
    outer, workspace = _te395(tmp_path)
    # The defect precondition: Git resolves to the unrelated ancestor checkout.
    assert _toplevel(workspace) == str(outer.resolve())

    orientation = _derive(workspace)

    assert orientation.available is True
    assert orientation.scope == ORIENTATION_SCOPE_WORKSPACE
    assert orientation.unavailable_reason is None
    assert REQUIRED <= set(orientation.paths)
    assert set(orientation.paths) == INVENTORY_LISTING
    assert orientation.import_neighbors == ()
    _assert_relative(orientation.paths)
    assert not any("orchestrator" in value for value in orientation.paths)
    assert not any(
        value.startswith(("app/", "runtime/")) for value in orientation.paths
    )

    block = render_repository_orientation(orientation)
    assert str(outer) not in block
    assert str(workspace) not in block
    assert "scope=workspace-listing" in block
    prompt = build_discovery_prompt(TASK, "", orientation)
    for value in REQUIRED:
        assert f"- {value}" in prompt


def test_te395_without_fallback_flag_is_unchanged(tmp_path):
    _, workspace = _te395(tmp_path)
    orientation = derive_repository_orientation(workspace, TASK)
    assert orientation.available is False
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_NO_CANDIDATES


def test_te395_listed_paths_execute_relative_to_workspace(tmp_path):
    _, workspace = _te395(tmp_path)
    orientation = _derive(workspace)
    assert "inventory/service.py" in orientation.paths

    read = parse_discovery_request(
        '{"action":"read_file","path":"inventory/service.py"}'
    )
    assert execute_discovery_request(workspace, read).content.startswith(
        "class Inventory"
    )
    for payload in (
        f'{{"action":"read_file","path":"{workspace}/inventory/service.py"}}',
        '{"action":"read_file","path":"../../../../app/services/x.py"}',
    ):
        with pytest.raises(DiscoveryContractError, match="discovery_path_unsafe"):
            parse_discovery_request(payload)


def test_te395_empty_workspace_is_reported_empty(tmp_path):
    outer = _outer_checkout(tmp_path)
    workspace = outer / "runtime" / "tasks" / "178" / "396"
    (workspace / ".agent").mkdir(parents=True)
    (workspace / "__pycache__").mkdir()
    orientation = _derive(workspace)
    assert orientation.available is False
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_WORKSPACE_EMPTY


def test_te395_symlinks_hidden_and_bounds_preserved(tmp_path):
    outer, workspace = _te395(tmp_path)
    (workspace / "outer_link.py").symlink_to(
        outer / "app" / "services" / "inventory_orchestrator.py"
    )
    (workspace / "outer_dir").symlink_to(outer / "app", target_is_directory=True)
    (workspace / ".hidden.py").write_text("x", encoding="utf-8")
    _write(workspace, {f"bulk/inventory_{i:03d}.py": "" for i in range(200)})

    orientation = _derive(workspace)

    assert orientation.scope == ORIENTATION_SCOPE_WORKSPACE
    assert not {"outer_link.py", "outer_dir/", ".hidden.py"} & set(orientation.paths)
    assert not any(value.startswith("outer_dir") for value in orientation.paths)
    assert orientation.entries_shown <= ORIENTATION_PATH_LIMIT
    assert orientation.bytes_used <= ORIENTATION_BYTE_BUDGET
    assert orientation.truncated is True
    _assert_relative(orientation.paths)


# Legitimate Git-backed projects keep Git-tracked orientation ----------------


def test_subdirectory_of_legitimate_git_project_stays_tracked(tmp_path):
    outer = _outer_checkout(tmp_path)
    _write(outer, {"app/services/inventory_untracked.py": "Y = 1\n"})
    subdir = outer / "app" / "services"

    with_flag = _derive(subdir)
    without_flag = derive_repository_orientation(
        subdir, TASK, include_import_neighbors=True
    )

    assert with_flag == without_flag
    assert with_flag.scope == ORIENTATION_SCOPE_TRACKED
    assert with_flag.paths == ("inventory_orchestrator.py",)


def test_untracked_unignored_subdirectory_is_not_disowned(tmp_path):
    # Git top-level differing from the workspace is not by itself non-Git.
    outer = _outer_checkout(tmp_path)
    fresh = _write(outer / "inventory_new", INVENTORY_FILES)
    orientation = _derive(fresh)
    assert orientation.available is False
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_NO_CANDIDATES


def test_force_tracked_file_under_ignored_dir_stays_tracked(tmp_path):
    outer, workspace = _te395(tmp_path)
    _git(outer, "add", "-f", "runtime/tasks/178/395/inventory/service.py")
    orientation = _derive(workspace)
    assert orientation.scope == ORIENTATION_SCOPE_TRACKED
    assert orientation.paths == ("inventory/service.py",)


def test_nested_repository_with_own_identity_stays_tracked(tmp_path):
    outer = _outer_checkout(tmp_path)
    nested = _write(
        outer / "runtime" / "tasks" / "178" / "397",
        {
            "inventory/service.py": "X = 1\n",
            "inventory/untracked_inventory.py": "Y = 1\n",
        },
    )
    _git(nested, "init", "-q")
    _git(nested, "add", "inventory/service.py")
    _git(nested, "commit", "-q", "-m", "init")

    orientation = _derive(nested)

    assert orientation.scope == ORIENTATION_SCOPE_TRACKED
    assert orientation.paths == ("inventory/service.py",)


def test_git_backed_runtime_worktree_stays_tracked(tmp_path):
    project = _write(tmp_path / "project", {"inventory/service.py": "X = 1\n"})
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "init")
    outer = _outer_checkout(tmp_path)
    sandbox = outer / "runtime" / "tasks" / "178" / "398"
    sandbox.parent.mkdir(parents=True)
    _git(project, "worktree", "add", "-q", "-b", "orchestrator/task-398", str(sandbox))
    assert (sandbox / ".git").is_file()

    orientation = _derive(sandbox)

    assert orientation.scope == ORIENTATION_SCOPE_TRACKED
    assert orientation.paths == ("inventory/service.py",)


# Fail-safe helper behavior --------------------------------------------------


def test_check_ignore_errors_do_not_disown(tmp_path, monkeypatch):
    _, workspace = _te395(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(ro.subprocess, "run", _boom)
    assert ro._ignored_by_enclosing_git_checkout(workspace) is False


def test_check_ignore_not_reached_for_own_git_entry(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    calls: list[object] = []
    monkeypatch.setattr(ro.subprocess, "run", lambda *a, **k: calls.append(a))
    assert ro._ignored_by_enclosing_git_checkout(workspace) is False
    assert calls == []
    assert os.path.exists(workspace / ".git")
