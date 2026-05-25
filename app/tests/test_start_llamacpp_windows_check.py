"""Regression tests for the Windows llama.cpp startup preflight script."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(
    sys.platform == "win32", reason="requires bash to execute wsl-start.sh"
)
def test_start_llamacpp_windows_check_accepts_crlf_env_and_strict_defaults(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
url="${@: -1}"
case "$url" in
  *:11434/api/tags) exit 22 ;;
  *:8001/v1/models) exit 0 ;;
  *localhost:8080/health)
    printf '%s' '{"status":"healthy","checks":{"api":"ok","database":"ok","redis":"ok"},"details":{"runtime_profile":"low_resource"}}'
    exit 0
    ;;
  *localhost:8080/docs) exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "powershell.exe", "#!/usr/bin/env bash\nexit 0\n")

    orchestrator_dir = tmp_path / "orchestrator"
    orchestrator_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    llama_exe = tmp_path / "llama-server.exe"
    llama_exe.write_text("", encoding="utf-8")
    (orchestrator_dir / "docker-compose.windows.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (orchestrator_dir / ".env").write_bytes(
        (
            "AGENT_BACKEND=openai_responses_api\r\n"
            "OPENAI_BASE_URL=http://host.docker.internal:8001/v1\r\n"
            "OPENAI_API_KEY=dummy\r\n"
            "AGENT_MODEL=local\r\n"
            "PLANNING_REPAIR_ENABLED=True\r\n"
            "PLANNING_REPAIR_BASE_URL=http://host.docker.internal:8001/v1\r\n"
            "PLANNING_REPAIR_MODEL=local\r\n"
            "EMBEDDING_PROVIDER=ollama\r\n"
            "RUNTIME_PROFILE=low_resource\r\n"
            f"WORKSPACE_ROOT={projects_dir}\r\n"
        ).encode("utf-8")
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "ORCHESTRATOR_DIR": str(orchestrator_dir),
        "LLAMA_EXE_WIN": str(llama_exe),
        "LLAMA_CTX": "4096",
    }
    result = subprocess.run(
        [str(repo_root / "wsl-start.sh"), "--check", "--backend-only"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Check complete:" in result.stdout
    assert "0 failure(s)" in result.stdout
    assert "Ollama not reachable" in result.stdout
    assert "RUNTIME_PROFILE=low_resource" in result.stdout
    assert "Backend runtime_profile=low_resource" in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32", reason="requires bash to execute wsl-start.sh"
)
def test_start_llamacpp_windows_startup_strips_crlf_from_projects_dir(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
url="${@: -1}"
case "$url" in
  *:11434/api/tags) exit 22 ;;
  *:8001/v1/models) exit 0 ;;
  *localhost:8080/health) exit 0 ;;
  *localhost:8080/docs) exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
if [ "$1" = "compose" ]; then
  exit 0
fi
exit 0
""",
    )
    _write_executable(fake_bin / "powershell.exe", "#!/usr/bin/env bash\nexit 0\n")

    orchestrator_dir = tmp_path / "orchestrator"
    orchestrator_dir.mkdir()
    projects_dir = tmp_path / "projects"
    llama_exe = tmp_path / "llama-server.exe"
    llama_exe.write_text("", encoding="utf-8")
    (orchestrator_dir / "docker-compose.windows.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (orchestrator_dir / ".env").write_bytes(
        (
            "AGENT_BACKEND=openai_responses_api\r\n"
            "OPENAI_BASE_URL=http://host.docker.internal:8001/v1\r\n"
            "OPENAI_API_KEY=dummy\r\n"
            "AGENT_MODEL=local\r\n"
            "PLANNING_REPAIR_ENABLED=True\r\n"
            "PLANNING_REPAIR_BASE_URL=http://host.docker.internal:8001/v1\r\n"
            "PLANNING_REPAIR_MODEL=local\r\n"
            "EMBEDDING_PROVIDER=ollama\r\n"
            "RUNTIME_PROFILE=low_resource\r\n"
            f"WORKSPACE_ROOT={projects_dir}\r\n"
        ).encode("utf-8")
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "ORCHESTRATOR_DIR": str(orchestrator_dir),
        "LLAMA_EXE_WIN": str(llama_exe),
        "LLAMA_CTX": "4096",
        "EXPECTED_OLLAMA_ABSENT": "true",
    }
    result = subprocess.run(
        [str(repo_root / "wsl-start.sh"), "--backend-only"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert projects_dir.is_dir()
    assert not (tmp_path / "projects\r").exists()
