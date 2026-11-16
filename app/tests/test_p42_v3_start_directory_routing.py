"""Shell routing probes only; never start a real service or initialize a DB."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_dev_route_is_retired_before_any_bootstrap(tmp_path):
    repo = tmp_path / "development-checkout"
    repo.mkdir()
    shutil.copyfile(ROOT / "start.sh", repo / "start.sh")
    result = subprocess.run(
        ["bash", str(repo / "start.sh"), "dev", "start"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78, result.stderr
    assert "retired" in result.stderr
    assert not (repo / "logs").exists()
    assert not (repo / "run").exists()
    assert not (repo / "orchestrator.db").exists()


def test_direct_dev_runtime_entrypoint_is_retired(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/dev_runtime.py"), "start"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78
    assert "retired" in result.stderr


@pytest.mark.parametrize(
    "root",
    [
        "/root/.openclaw/workspace/vault/orchestrator-production",
        "/root/.openclaw/workspace/vault/orchestrator-production/nested",
        "/root/.openclaw/workspace/vault/orchestrator-production-runtime",
    ],
)
def test_dev_route_refuses_protected_paths_before_any_action(root):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; PROJECT_ROOT="$2"; dispatch_startup dev start',
            "routing-test",
            str(ROOT / "start.sh"),
            root,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78
    assert "retired" in result.stderr


@pytest.mark.parametrize(
    "args, expected",
    [
        (["--help"], 0),
        (["-h"], 0),
        (["dev"], 78),
        (["dev", "--help"], 78),
        (["dev", "-h"], 78),
    ],
)
def test_help_and_missing_dev_command_have_no_startup_side_effects(
    tmp_path, args, expected
):
    repo = tmp_path / "development-checkout"
    repo.mkdir()
    shutil.copyfile(ROOT / "start.sh", repo / "start.sh")
    (repo / ".env").write_text("exit 99\n")
    before = set(repo.iterdir())
    result = subprocess.run(
        ["bash", str(repo / "start.sh"), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr
    if args[0] == "dev":
        assert "retired" in result.stderr
    else:
        assert "Usage:" in result.stdout + result.stderr
    assert "Starting Orchestrator Network" not in result.stdout
    assert set(repo.iterdir()) == before


def test_dev_start_is_retired_without_creating_runtime(tmp_path):
    repo = tmp_path / "development-checkout"
    repo.mkdir()
    shutil.copyfile(ROOT / "start.sh", repo / "start.sh")
    result = subprocess.run(
        ["bash", str(repo / "start.sh"), "dev", "start"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78
    assert "retired" in result.stderr
    assert not (repo / "venv").exists()
    assert not (repo / "logs").exists()
    assert not (repo / "run").exists()


@pytest.mark.parametrize(
    "args, expected",
    [([], "legacy"), (["start_worker_only", "fixture"], "worker:fixture")],
)
def test_existing_conventional_and_worker_only_dispatch_unchanged(args, expected):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; main() { printf legacy; }; start_worker_only() { printf "worker:%s" "$1"; }; dispatch_startup "$@"',
            "routing-test",
            str(ROOT / "start.sh"),
            *args,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
