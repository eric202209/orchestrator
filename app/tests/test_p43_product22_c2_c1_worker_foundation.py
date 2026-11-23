"""Focused offline tests for the restricted Worker image contract."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "scripts" / "worker-entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile.worker"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _make_contract_fixture(tmp_path: Path) -> dict[str, str]:
    # The subprocess runs as nobody, so it must be able to traverse the
    # pytest-created parent directory just as it would traverse /opt in the
    # image.
    for parent in tmp_path.parents:
        if parent == Path("/tmp"):
            break
        parent.chmod(0o755)
    tmp_path.chmod(0o755)
    app_root = tmp_path / "release"
    (app_root / "app").mkdir(parents=True)
    (app_root / "app" / "main.py").write_text("# immutable fixture\n", encoding="utf-8")
    for directory in (app_root, app_root / "app"):
        directory.chmod(0o555)
    (app_root / "app" / "main.py").chmod(0o444)

    database_dir = tmp_path / "db"
    runtime_dir = tmp_path / "runtime"
    checkpoint_dir = tmp_path / "checkpoints"
    identity_dir = tmp_path / "identity"
    workspace_dir = tmp_path / "workspace"
    for directory in (
        database_dir,
        runtime_dir,
        checkpoint_dir,
        identity_dir,
        workspace_dir,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    nobody = pwd.getpwnam("nobody")
    for directory in (
        database_dir,
        runtime_dir,
        checkpoint_dir,
        identity_dir,
        workspace_dir,
    ):
        os.chown(directory, nobody.pw_uid, nobody.pw_gid)

    return {
        "WORKER_ENVIRONMENT": "c2-c1-test",
        "WORKER_ISOLATION_MODE": "disposable",
        "WORKER_BROKER_NAMESPACE": "p43-c2-c1-disposable",
        "WORKER_BRIDGE_MODE": "disabled",
        "EXECUTION_ENABLED": "false",
        "CONTROLLED_WORKER_IDENTITY_REQUIRED": "true",
        "CONTROLLED_WORKER_NODE_IDENTITY": "orchestrator-worker-c2-c1",
        "WORKER_IDENTITY_SOURCE": "container-start",
        "WORKER_INSTANCE_ID": "c2-c1-test-instance",
        "HOSTNAME": "container-hostname",
        "WORKER_QUEUE_NAME": "orchestrator-c2-c1-test",
        "CELERY_QUEUE_NAME": "orchestrator-c2-c1-test",
        "CELERY_BROKER_URL": "redis://redis-c2-c1-test:6379/9",
        "CELERY_RESULT_BACKEND": "redis://redis-c2-c1-test:6379/10",
        "DATABASE_URL": f"sqlite:///{database_dir}/orchestrator.db",
        "WORKSPACE_ROOT": str(workspace_dir),
        "RUNTIME_ROOT": str(runtime_dir),
        "CHECKPOINT_DIR": str(checkpoint_dir),
        "WORKER_APP_ROOT": str(app_root),
        "WORKER_SOURCE_ROOT": str(app_root),
        "WORKER_DATABASE_DIR": str(database_dir),
        "WORKER_RUNTIME_ROOT": str(runtime_dir),
        "WORKER_CHECKPOINT_DIR": str(checkpoint_dir),
        "WORKER_IDENTITY_ROOT": str(identity_dir),
        "WORKER_CANDIDATE_PIN_PATH": str(identity_dir / "worker-pin.json"),
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT": str(identity_dir),
        "PLANNING_PROVIDER": "direct_openai_compatible",
        "AGENT_BACKEND": "direct_ollama",
        "PLANNING_BACKEND": "direct_ollama",
        "EXECUTION_BACKEND": "direct_ollama",
        "DEBUG_REPAIR_BACKEND": "direct_ollama",
        "REPAIR_BACKEND": "direct_ollama",
        "COMPLETION_REPAIR_BACKEND": "direct_ollama",
        "WORKER_ENTRYPOINT_CHECK_ONLY": "1",
    }


def _run_entrypoint(
    env: dict[str, str], *, user: pwd.struct_passwd
) -> subprocess.CompletedProcess[str]:
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        ["sh"],
        cwd=REPO_ROOT,
        env=child_env,
        text=True,
        input=ENTRYPOINT.read_text(encoding="utf-8"),
        capture_output=True,
        check=False,
        user=user.pw_uid,
        group=user.pw_gid,
    )


def test_worker_entrypoint_accepts_structured_http_contract(tmp_path: Path) -> None:
    env = _make_contract_fixture(tmp_path)
    result = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert result.returncode == 0, result.stderr
    assert "validation only" in result.stdout


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AGENT_BACKEND", "local_openclaw", "structured HTTP"),
        ("PLANNING_BACKEND", "remote_openclaw_gateway", "structured HTTP"),
        ("CELERY_QUEUE_NAME", "celery", "orchestrator-c2-c1-test"),
        ("WORKER_ENV_FILE", "/project/.env", "curated Worker env file"),
        ("HOST_BRIDGE_SOCKET", "/run/bridge.sock", "HOST_BRIDGE_SOCKET"),
    ],
)
def test_worker_entrypoint_rejects_unsafe_contract(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    env = _make_contract_fixture(tmp_path)
    env[name] = value
    result = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert result.returncode == 78
    assert message in result.stderr


def test_worker_entrypoint_rejects_hostname_reused_as_identity(tmp_path: Path) -> None:
    env = _make_contract_fixture(tmp_path)
    env["HOSTNAME"] = "c2-c1-hostname"
    env["WORKER_INSTANCE_ID"] = env["HOSTNAME"]
    result = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert result.returncode == 78
    assert "hostname" in result.stderr


def test_worker_entrypoint_rejects_full_env_and_bridge_paths(tmp_path: Path) -> None:
    env = _make_contract_fixture(tmp_path)
    env["SECRET_KEY"] = "must-not-be-injected"
    result = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert result.returncode == 78
    assert "SECRET_KEY" in result.stderr


def test_worker_release_and_docker_context_are_read_only_by_contract() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "USER orchestrator-worker" in dockerfile
    assert "COPY --chown=root:root . /opt/orchestrator" in dockerfile
    assert "chmod 0555" in dockerfile
    assert "docker.sock" not in dockerfile
    assert "privileged" not in dockerfile
    assert ".env\n" in dockerignore
    assert ".env.*\n" in dockerignore
    assert "!.env.example" in dockerignore


def test_required_writable_paths_are_not_the_application_release(
    tmp_path: Path,
) -> None:
    env = _make_contract_fixture(tmp_path)
    result = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert result.returncode == 0, result.stderr
    for name in (
        "WORKER_DATABASE_DIR",
        "WORKER_RUNTIME_ROOT",
        "WORKER_CHECKPOINT_DIR",
        "WORKER_IDENTITY_ROOT",
        "WORKSPACE_ROOT",
    ):
        probe = Path(env[name]) / "post-entrypoint-test"
        probe.write_text("disposable\n", encoding="utf-8")
        probe.unlink()


@pytest.mark.skipif(
    os.environ.get("P43_C2_C1_RUN_DOCKER_TESTS") != "1",
    reason="set P43_C2_C1_RUN_DOCKER_TESTS=1 for disposable image tests",
)
def test_built_worker_image_is_non_root_and_has_no_privileged_mounts() -> None:
    image = os.environ.get("P43_C2_C1_WORKER_IMAGE")
    if not image or shutil.which("docker") is None:
        pytest.skip("a prebuilt disposable Worker image and Docker are required")
    command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev",
        "--tmpfs",
        "/var/lib/orchestrator-worker:rw,noexec,nosuid,nodev",
        "--tmpfs",
        "/workspace/authoritative:rw,nosuid,nodev",
        "-e",
        "WORKER_ENTRYPOINT_CHECK_ONLY=1",
        "-e",
        "WORKER_ENVIRONMENT=c2-c1-test",
        "-e",
        "WORKER_ISOLATION_MODE=disposable",
        "-e",
        "WORKER_BROKER_NAMESPACE=p43-c2-c1-disposable",
        "-e",
        "WORKER_BRIDGE_MODE=disabled",
        "-e",
        "EXECUTION_ENABLED=false",
        "-e",
        "CONTROLLED_WORKER_IDENTITY_REQUIRED=true",
        "-e",
        "CONTROLLED_WORKER_NODE_IDENTITY=orchestrator-worker-c2-c1",
        "-e",
        "WORKER_IDENTITY_SOURCE=container-start",
        "-e",
        "WORKER_INSTANCE_ID=c2-c1-docker-test",
        "-e",
        "WORKER_QUEUE_NAME=orchestrator-c2-c1-test",
        "-e",
        "CELERY_QUEUE_NAME=orchestrator-c2-c1-test",
        "-e",
        "CELERY_BROKER_URL=redis://redis-c2-c1-test:6379/9",
        "-e",
        "CELERY_RESULT_BACKEND=redis://redis-c2-c1-test:6379/10",
        "-e",
        "DATABASE_URL=sqlite:////var/lib/orchestrator-worker/db/orchestrator.db",
        "-e",
        "WORKSPACE_ROOT=/workspace/authoritative",
        "-e",
        "RUNTIME_ROOT=/var/lib/orchestrator-worker/runtime",
        "-e",
        "CHECKPOINT_DIR=/var/lib/orchestrator-worker/checkpoints",
        "-e",
        "WORKER_DATABASE_DIR=/var/lib/orchestrator-worker/db",
        "-e",
        "WORKER_RUNTIME_ROOT=/var/lib/orchestrator-worker/runtime",
        "-e",
        "WORKER_CHECKPOINT_DIR=/var/lib/orchestrator-worker/checkpoints",
        "-e",
        "WORKER_IDENTITY_ROOT=/var/lib/orchestrator-worker/identity",
        "-e",
        "WORKER_CANDIDATE_PIN_PATH=/var/lib/orchestrator-worker/identity/worker-pin.json",
        "-e",
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT=/var/lib/orchestrator-worker/identity",
        "-e",
        "WORKER_SOURCE_ROOT=/opt/orchestrator",
        "-e",
        "PLANNING_PROVIDER=direct_openai_compatible",
        "-e",
        "AGENT_BACKEND=direct_ollama",
        "-e",
        "PLANNING_BACKEND=direct_ollama",
        "-e",
        "EXECUTION_BACKEND=direct_ollama",
        "-e",
        "REPAIR_BACKEND=direct_ollama",
        image,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "uid=10001" in result.stdout
