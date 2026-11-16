"""P42-V17-K provider-free regressions: non-Git Discovery relative-path grounding.

P42-V17-H: the live Discovery provider returned a valid ``search_text`` action
whose scope was the absolute project path, correctly rejected as
``discovery_path_unsafe``.  The prompt carried no orientation because the
project was not a Git work tree.  Discovery now receives a bounded,
workspace-relative listing for confirmed non-Git workspaces; path validation is
unchanged and still fails closed.

All provider traffic is captured or stubbed; sockets are denied.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agents.providers.openai_chat_adapter import _GENERIC_SYSTEM
from app.services.orchestration.planning import repository_orientation as ro
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    MAX_ORIENTATION_BLOCK_BYTES,
    DiscoveryContractError,
    build_discovery_prompt,
    execute_discovery_request,
    parse_discovery_request,
    run_discovery_stage,
)
from app.services.orchestration.planning.repository_orientation import (
    ORIENTATION_BYTE_BUDGET,
    ORIENTATION_PATH_LIMIT,
    ORIENTATION_SCOPE_TRACKED,
    ORIENTATION_SCOPE_WORKSPACE,
    ORIENTATION_UNAVAILABLE_NOT_GIT,
    ORIENTATION_UNAVAILABLE_WORKSPACE_EMPTY,
    ORIENTATION_UNAVAILABLE_WORKSPACE_UNLISTABLE,
    derive_repository_orientation,
    render_repository_orientation,
    workspace_product_paths,
)
from app.tests.test_p42v17g_discovery_generation_contract import (
    _client,
    _runtime,
    _settings,
)

TASK = "Add stock reservation to the inventory service with tests."
CONTEXT = "Inventory Reservation Service: a small Python package."
INVENTORY_FILES = {
    "inventory/__init__.py": "",
    "inventory/models.py": "class Item:\n    pass\n",
    "inventory/service.py": "class Inventory:\n    def add_stock(self):\n        pass\n",
    "tests/test_inventory.py": "def test_add_stock():\n    pass\n",
    ".gitignore": "__pycache__/\n",
}
INVENTORY_LISTING = {
    "inventory/",
    "inventory/__init__.py",
    "inventory/models.py",
    "inventory/service.py",
    "tests/",
    "tests/test_inventory.py",
}


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    attempts: list[object] = []

    def _deny(*args, **kwargs):
        attempts.append(args)
        raise OSError("network denied by P42-V17-K test")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    yield attempts
    assert attempts == [], "provider-free test attempted a network connection"


def _write(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _inventory(tmp_path: Path) -> Path:
    root = _write(tmp_path / "inventory-reservation-service", INVENTORY_FILES)
    (root / "inventory" / "__pycache__").mkdir()
    (root / "inventory" / "__pycache__" / "service.cpython-312.pyc").write_bytes(b"")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "README.md").write_text("cache", encoding="utf-8")
    return root


def _derive(root: Path, task: str = TASK, **kwargs):
    return derive_repository_orientation(
        root,
        task,
        include_import_neighbors=True,
        non_git_workspace_fallback=True,
        **kwargs,
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _assert_relative(paths) -> None:
    for value in paths:
        assert not value.startswith(("/", "~"))
        assert ".." not in value.split("/")
        assert not any(segment.startswith(".") for segment in value.split("/"))


# D1 — Inventory Reservation Service reproduction ---------------------------


def test_d1_inventory_workspace_receives_relative_orientation(tmp_path):
    root = _inventory(tmp_path)
    assert (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
        ).returncode
        != 0
    )

    orientation = _derive(root)

    assert orientation.available is True
    assert orientation.scope == ORIENTATION_SCOPE_WORKSPACE
    assert set(orientation.paths) == INVENTORY_LISTING
    assert orientation.import_neighbors == ()
    _assert_relative(orientation.paths)
    block = render_repository_orientation(orientation)
    assert str(root) not in block
    assert str(tmp_path) not in block
    assert "scope=workspace-listing" in block
    assert "not a Git repository" in block
    assert "absolute paths" in block

    prompt = build_discovery_prompt(TASK, CONTEXT, orientation)
    assert block in prompt
    assert "- inventory/service.py" in prompt
    assert "- tests/test_inventory.py" in prompt
    assert "__pycache__" not in prompt
    assert ".pytest_cache" not in prompt
    assert ".gitignore" not in prompt
    assert len(block.encode("utf-8")) <= MAX_ORIENTATION_BLOCK_BYTES
    assert orientation.bytes_used <= ORIENTATION_BYTE_BUDGET


def test_d1_orientation_survives_project_context_truncation(tmp_path):
    root = _inventory(tmp_path)
    orientation = _derive(root)
    prompt = build_discovery_prompt(TASK, "x" * 5000, orientation)
    assert render_repository_orientation(orientation) in prompt
    assert "x" * 701 not in prompt


def test_d1_fallback_is_opt_in(tmp_path):
    root = _inventory(tmp_path)
    orientation = derive_repository_orientation(root, TASK)
    assert orientation.available is False
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_NOT_GIT


def test_d1_unmatched_literals_still_list_workspace(tmp_path):
    root = _inventory(tmp_path)
    orientation = _derive(root, "Investigate the zzzzqqqq subsystem.")
    assert set(orientation.paths) == INVENTORY_LISTING


# D2 — Git regression -------------------------------------------------------


def test_d2_git_orientation_unchanged_by_fallback_flag(tmp_path):
    root = _write(
        tmp_path / "repo",
        {
            "app/inventory.py": "X = 1\n",
            "app/untracked_inventory.py": "Y = 1\n",
        },
    )
    _git(root, "init", "-q")
    _git(root, "add", "app/inventory.py")
    _git(root, "commit", "-q", "-m", "init")

    with_flag = _derive(root)
    without_flag = derive_repository_orientation(
        root, TASK, include_import_neighbors=True
    )

    assert with_flag == without_flag
    assert with_flag.scope == ORIENTATION_SCOPE_TRACKED
    assert with_flag.paths == ("app/inventory.py",)
    assert "Git-tracked paths only" in render_repository_orientation(with_flag)


def test_d2_failing_git_repo_is_not_masked_by_listing(tmp_path, monkeypatch):
    root = _write(tmp_path / "repo", {"app/inventory.py": "X = 1\n"})
    _git(root, "init", "-q")
    monkeypatch.setattr(ro, "tracked_product_paths", lambda project_dir: None)

    orientation = _derive(root)

    assert orientation.available is False
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_NOT_GIT


# D3 — Path safety ----------------------------------------------------------


def test_d3_symlinks_hidden_and_special_entries_are_excluded(tmp_path):
    outside = _write(tmp_path / "outside", {"secret.py": "TOKEN = 1\n"})
    root = _inventory(tmp_path)
    (root / "link_out").symlink_to(outside, target_is_directory=True)
    (root / "inventory" / "nested_link").symlink_to(outside / "secret.py")
    (root / "inventory" / "loop").symlink_to(root, target_is_directory=True)
    _write(root, {".hidden/inner.py": "", "inventory/.env": "K=V\n"})
    os.mkfifo(root / "inventory" / "pipe")

    paths = workspace_product_paths(root)

    assert set(paths) == INVENTORY_LISTING
    rendered = render_repository_orientation(_derive(root))
    for leaked in ("secret", "link_out", "nested_link", "loop", "hidden", ".env"):
        assert leaked not in rendered
    assert "pipe" not in rendered


def test_d3_spaces_and_unicode_are_listed_relative(tmp_path):
    root = _write(tmp_path / "ws", {"my pkg/módulo.py": "", "日本/a.py": ""})
    paths = workspace_product_paths(root)
    assert paths == ("my pkg/", "日本/", "my pkg/módulo.py", "日本/a.py")
    _assert_relative(paths)


def test_d3_empty_missing_and_non_directory_roots(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".gitignore").write_text("x", encoding="utf-8")
    assert _derive(empty).unavailable_reason == ORIENTATION_UNAVAILABLE_WORKSPACE_EMPTY

    missing = _derive(tmp_path / "missing")
    assert missing.available is False
    assert missing.paths == ()

    file_root = tmp_path / "file_root"
    file_root.write_text("x", encoding="utf-8")
    assert workspace_product_paths(file_root) is None

    link_root = tmp_path / "link_root"
    link_root.symlink_to(_inventory(tmp_path), target_is_directory=True)
    assert workspace_product_paths(link_root) is None
    assert (
        _derive(link_root).unavailable_reason
        == ORIENTATION_UNAVAILABLE_WORKSPACE_UNLISTABLE
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory modes")
def test_d3_inaccessible_root_is_unlistable(tmp_path):
    root = _inventory(tmp_path)
    root.chmod(0)
    try:
        assert workspace_product_paths(root) is None
    finally:
        root.chmod(0o775)


def test_d3_inaccessible_subdirectory_is_skipped(tmp_path, monkeypatch):
    root = _inventory(tmp_path)
    real_scandir = os.scandir

    def _scandir(path):
        if Path(path).name == "tests":
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(ro.os, "scandir", _scandir)
    assert "tests/test_inventory.py" not in workspace_product_paths(root)
    assert "inventory/service.py" in workspace_product_paths(root)


# D4 — Determinism and bounds -----------------------------------------------


def test_d4_listing_is_deterministic_and_breadth_first(tmp_path, monkeypatch):
    root = _write(tmp_path / "ws", {"b/z.py": "", "b/a.py": "", "a.py": ""})
    first = workspace_product_paths(root)
    real_scandir = os.scandir

    class _Reversed:
        def __init__(self, path):
            self._inner = real_scandir(path)
            self._entries = list(self._inner)[::-1]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            self._inner.close()
            return False

    monkeypatch.setattr(ro.os, "scandir", _Reversed)
    assert workspace_product_paths(root) == first == ("a.py", "b/", "b/a.py", "b/z.py")


def test_d4_max_depth(tmp_path):
    deep = "/".join(f"d{index}" for index in range(8)) + "/leaf.py"
    root = _write(tmp_path / "ws", {deep: ""})
    paths = workspace_product_paths(root)
    assert (
        max(len(value.rstrip("/").split("/")) for value in paths)
        == ro.WORKSPACE_LISTING_MAX_DEPTH
    )
    assert not any(value.endswith("leaf.py") for value in paths)


def test_d4_oversized_directory_is_omitted_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(ro, "WORKSPACE_LISTING_MAX_DIR_ENTRIES", 5)
    root = _write(
        tmp_path / "ws",
        {"keep/a.py": "", **{f"big/f{index}.py": "" for index in range(6)}},
    )
    paths = workspace_product_paths(root)
    assert paths == ("big/", "keep/", "keep/a.py")


def test_d4_visited_bound_stops_walk(tmp_path, monkeypatch):
    monkeypatch.setattr(ro, "WORKSPACE_LISTING_MAX_VISITED", 4)
    root = _write(tmp_path / "ws", {"a/x.py": "", "a/y.py": "", "b/z.py": ""})
    # root has 2 entries, a/ has 2 (total 4), b/ would exceed the bound.
    assert workspace_product_paths(root) == ("a/", "b/", "a/x.py", "a/y.py")


def test_d4_emitted_paths_and_bytes_are_bounded(tmp_path):
    root = _write(
        tmp_path / "ws",
        {f"pkg/module_{index:03d}_inventory.py": "" for index in range(300)},
    )
    orientation = _derive(root)
    assert orientation.entries_shown == ORIENTATION_PATH_LIMIT
    assert orientation.entries_total == 301
    assert orientation.truncated is True
    assert orientation.bytes_used <= ORIENTATION_BYTE_BUDGET
    assert _derive(root) == orientation
    block = render_repository_orientation(orientation)
    assert len(block.encode("utf-8")) <= MAX_ORIENTATION_BLOCK_BYTES
    assert "truncated=true" in block


def test_d4_byte_budget_truncates_long_names(tmp_path):
    long_dir = "d" * 200
    root = _write(
        tmp_path / "ws",
        {f"{long_dir}/{'n' * 200}{index:02d}.py": "" for index in range(30)},
    )
    orientation = _derive(root)
    assert orientation.truncated is True
    assert orientation.entries_shown < ORIENTATION_PATH_LIMIT
    assert orientation.bytes_used <= ORIENTATION_BYTE_BUDGET


# D5 — Existing Discovery parser stays fail-closed --------------------------


def test_d5_relative_listed_paths_are_admissible_and_execute(tmp_path):
    root = _inventory(tmp_path)
    request = parse_discovery_request(
        '{"action":"search_text","query":"add_stock",'
        '"paths":["inventory/","tests/test_inventory.py"]}'
    )
    assert request.paths == ("inventory", "tests/test_inventory.py")
    observation = execute_discovery_request(root, request)
    assert {hit.path for hit in observation.hits} == {
        "inventory/service.py",
        "tests/test_inventory.py",
    }
    read = parse_discovery_request(
        '{"action":"read_file","path":"inventory/service.py"}'
    )
    assert execute_discovery_request(root, read).content.startswith("class Inventory")


@pytest.mark.parametrize(
    "payload",
    [
        '{"action":"search_text","query":"def add_stock","paths":['
        '"/root/.openclaw/workspace/vault/projects/inventory-reservation-service"]}',
        '{"action":"search_text","query":"x","paths":["../outside"]}',
        '{"action":"search_text","query":"x","paths":["inventory/../../etc"]}',
        '{"action":"read_file","path":"/etc/passwd"}',
        '{"action":"read_file","path":"~/secret.py"}',
    ],
)
def test_d5_absolute_and_traversal_paths_remain_unsafe(payload):
    with pytest.raises(DiscoveryContractError, match="discovery_path_unsafe"):
        parse_discovery_request(payload)


def test_d5_out_of_workspace_symlink_scope_rejected(tmp_path):
    outside = _write(tmp_path / "outside", {"secret.py": "TOKEN = 1\n"})
    root = _inventory(tmp_path)
    (root / "link_out").symlink_to(outside, target_is_directory=True)
    request = parse_discovery_request(
        '{"action":"search_text","query":"TOKEN","paths":["link_out"]}'
    )
    with pytest.raises(DiscoveryContractError, match="discovery_path_symlink"):
        execute_discovery_request(root, request)


def test_d5_unsupported_action_rejected():
    with pytest.raises(DiscoveryContractError, match="discovery_action_unsupported"):
        parse_discovery_request('{"action":"list_dir","path":"inventory"}')


# D6 — Production call-site integration -------------------------------------


def _ctx(root: Path, runtime) -> SimpleNamespace:
    return SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=runtime,
        prompt=TASK,
        orchestration_state=SimpleNamespace(project_context=CONTEXT, project_dir=root),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )


def test_d6_run_discovery_stage_sends_relative_orientation(
    db_session, tmp_path, monkeypatch
):
    root = _inventory(tmp_path)
    _settings(monkeypatch, tmp_path, l1=False)
    captured = _client(
        monkeypatch,
        content='{"action":"read_file","path":"inventory/service.py"}',
    )
    events: list[dict] = []

    observation = run_discovery_stage(
        ctx=_ctx(root, _runtime(db_session, l1=False)),
        planning_timeout_seconds=240,
        extract_structured_text=lambda value: str(value),
        planner_service=PlannerService,
        emit_phase_event=lambda *args, **kwargs: events.append(kwargs["details"]),
    )

    assert observation.action == "read_file"
    messages = captured["json"]["messages"]
    assert messages[0] == {"role": "system", "content": _GENERIC_SYSTEM}
    user_prompt = messages[1]["content"]
    assert user_prompt == build_discovery_prompt(TASK, CONTEXT, _derive(root))
    assert "scope=workspace-listing" in user_prompt
    assert "- inventory/service.py" in user_prompt
    assert "- tests/test_inventory.py" in user_prompt
    assert str(root) not in user_prompt
    assert events[0]["orientation_available"] is True
    assert events[0]["orientation_scope"] == ORIENTATION_SCOPE_WORKSPACE


def test_d6_absolute_scope_from_provider_still_fails_closed(
    db_session, tmp_path, monkeypatch
):
    root = _inventory(tmp_path)
    _settings(monkeypatch, tmp_path, l1=False)
    _client(
        monkeypatch,
        content=(
            '{"action":"search_text","query":"def add_stock","paths":["'
            + str(root)
            + '"]}'
        ),
    )
    with pytest.raises(DiscoveryContractError, match="discovery_path_unsafe"):
        run_discovery_stage(
            ctx=_ctx(root, _runtime(db_session, l1=False)),
            planning_timeout_seconds=240,
            extract_structured_text=lambda value: str(value),
            planner_service=PlannerService,
            emit_phase_event=lambda *args, **kwargs: None,
        )
