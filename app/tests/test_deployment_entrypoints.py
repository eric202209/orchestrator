"""Regression tests for deployment entrypoint scripts."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def test_knowledge_ingest_script_help_imports_from_unrelated_cwd(tmp_path):
    script = REPO_ROOT / "scripts" / "planning_and_knowledge" / "ingest_knowledge.py"
    env = _clean_subprocess_env()

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy, sys; "
                f"ns = runpy.run_path({str(script)!r}); "
                "assert str(ns['REPO_ROOT']) in sys.path; "
                "import app.config; "
                "print(ns['REPO_ROOT'])"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Ingest knowledge documents" in result.stdout
    assert "--source-dir" in result.stdout
    assert "--qdrant-url" in result.stdout


def test_wsl_ollama_launcher_contract():
    launcher = REPO_ROOT / "scripts" / "wsl-ollama-start.sh"

    assert launcher.exists()
    assert launcher.stat().st_mode & stat.S_IXUSR

    for script_name in ("wsl-start.sh", "scripts/wsl-ollama-start.sh"):
        result = subprocess.run(
            ["bash", "-n", script_name],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    result = subprocess.run(
        ["./wsl-start.sh", "--ollama", "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--build" in result.stdout
    assert "--force-recreate" in result.stdout
    assert "--backend-only" in result.stdout
    assert "--skip-ollama" in result.stdout
    assert "--start-ollama" in result.stdout
    assert "--ingest-knowledge" in result.stdout
