"""Shell routing probes only; never start a real service or initialize a DB."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_dev_route_bypasses_legacy_configuration_and_bootstrap(tmp_path):
    repo = tmp_path / "development-checkout"
    repo.mkdir()
    shutil.copyfile(ROOT / "start.sh", repo / "start.sh")
    (repo / "venv/bin").mkdir(parents=True)
    (repo / "venv/bin/python3").symlink_to(sys.executable)
    (repo / "scripts").mkdir()
    (repo / "scripts/dev_runtime.py").write_text(
        "import json, os, sys\n"
        'print(json.dumps({"args":sys.argv[1:], "legacy":os.environ.get("LEGACY_CHECKOUT_SENTINEL")}))\n'
    )
    (repo / ".env").write_text("LEGACY_CHECKOUT_SENTINEL=should-not-be-loaded\n")
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "LEGACY_CHECKOUT_SENTINEL"
    }
    result = subprocess.run(
        [
            "bash",
            str(repo / "start.sh"),
            "dev",
            "worker-worksheet",
            "--expected-head",
            "reviewed head",
            "--expected-tree",
            "reviewed tree",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {
        "args": [
            "worker-worksheet",
            "--expected-head",
            "reviewed head",
            "--expected-tree",
            "reviewed tree",
        ],
        "legacy": None,
    }
    assert not (repo / "logs").exists()
    assert not (repo / "run").exists()
    assert not (repo / "orchestrator.db").exists()


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
    assert result.returncode == 1
    assert "refuses protected production paths" in result.stderr


def test_help_and_missing_dev_command_have_no_startup_side_effects():
    for args, expected in [(["--help"], 0), (["dev"], 2), (["dev", "--help"], 0)]:
        result = subprocess.run(
            ["bash", str(ROOT / "start.sh"), *args], capture_output=True, text=True
        )
        assert result.returncode == expected, result.stderr
        assert "Starting Orchestrator Network" not in result.stdout


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
