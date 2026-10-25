"""Isolation and ownership regressions for the disposable dev runtime."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_checkout(tmp_path: Path) -> dev_runtime.RuntimeConfig:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    (repo_root / "app").symlink_to(REPO_ROOT / "app", target_is_directory=True)
    return dev_runtime.RuntimeConfig(
        repo_root=repo_root,
        root=repo_root / ".dev-runtime" / "phase40b-r1",
    )


def _prepare(config: dev_runtime.RuntimeConfig) -> None:
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)


def test_rejects_database_path_outside_runtime(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    env = dev_runtime.build_environment(config)
    env["DATABASE_URL"] = (
        "sqlite:////root/.openclaw/workspace/vault/orchestrator-production/orchestrator.db"
    )

    with pytest.raises(dev_runtime.RuntimeToolError, match="development SQLite"):
        dev_runtime.validate_isolation(config, env)


def test_rejects_production_broker_endpoint(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    env = dev_runtime.build_environment(config)
    env["CELERY_BROKER_URL"] = "redis://127.0.0.1:6379/0"

    with pytest.raises(dev_runtime.RuntimeToolError, match="assigned loopback"):
        dev_runtime.validate_isolation(config, env)


def test_rejects_shared_default_celery_queue(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    env = dev_runtime.build_environment(config)
    env["CELERY_QUEUE_NAME"] = "celery"

    with pytest.raises(dev_runtime.RuntimeToolError, match="queue must be unique"):
        dev_runtime.validate_isolation(config, env)


def test_development_database_initializes_only_inside_owned_runtime(
    tmp_path: Path,
) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    env = dev_runtime.build_environment(config)

    dev_runtime.initialize_database(config, env)

    assert config.database_file.is_file()
    assert config.root in config.database_file.parents
    with sqlite3.connect(config.database_file) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"users", "projects"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_preflight_can_disable_qdrant_without_referencing_shared_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.setattr(
        dev_runtime, "_which_required", lambda name: "/usr/bin/redis-server"
    )
    monkeypatch.setattr(dev_runtime, "_qdrant_binary", lambda _config: None)
    monkeypatch.setattr(dev_runtime, "_port_available", lambda _port: True)

    result = dev_runtime.preflight(config)

    assert result["qdrant_mode"] == "disabled_no_binary"
    assert result["qdrant_url"] == "http://127.0.0.1:16333"
    assert str(config.root / "qdrant") not in result["qdrant_url"]


def test_redis_executable_symlink_name_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "redis-multicall"
    target.write_text("binary")
    target.chmod(0o755)
    server_name = tmp_path / "redis-server"
    server_name.symlink_to(target)
    monkeypatch.setattr(dev_runtime.shutil, "which", lambda _name: str(server_name))

    assert dev_runtime._which_required("redis-server") == str(server_name)


def test_celery_uses_configured_development_queue(tmp_path: Path) -> None:
    config = dev_runtime.RuntimeConfig.for_repo(REPO_ROOT)
    env = dev_runtime.build_environment(config)
    process_cwd = tmp_path / "cwd"
    process_cwd.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.celery_app import celery_app; "
            "from app.tasks.worker import execute_orchestration_task, "
            "_capacity_admission_backend; "
            "print(celery_app.conf.task_default_queue + '|' + "
            "execute_orchestration_task.queue + '|' + "
            "str(_capacity_admission_backend('stub_success') is None))",
        ],
        cwd=process_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{config.queue_name}|{config.queue_name}|True"


def test_worker_identity_and_queue_are_development_specific(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    env = dev_runtime.build_environment(config)

    dev_runtime.validate_isolation(config, env)

    assert config.worker_name.startswith("phase40b-r1@")
    assert config.worker_name != "celery@production"
    assert env["CELERY_QUEUE_NAME"] == "phase40b-r1-dev"


def test_openclaw_profile_isolated_and_provider_urls_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.setenv("OPENCLAW_API_KEY", "production-credential-marker")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://shared-ollama.example")
    monkeypatch.setenv("HOME", "/root/.openclaw")

    env = dev_runtime.build_environment(config)
    dev_runtime.validate_isolation(config, env)

    assert env["HOME"] == str(config.root / "home")
    assert env["OPENCLAW_HOME"] == str(config.root / "openclaw-home")
    assert env["OPENCLAW_STATE_DIR"] == str(config.root / "openclaw-home")
    assert env["OPENCLAW_CLI_PATH"] == ""
    assert env["OPENCLAW_API_KEY"] == ""
    assert env["PLANNING_PROVIDER"] == "disabled"
    assert all("production-credential-marker" not in value for value in env.values())
    assert "shared-ollama.example" not in " ".join(env.values())


def test_planning_provider_rejects_disabled_provider_without_fallback(
    tmp_path: Path,
) -> None:
    config = dev_runtime.RuntimeConfig.for_repo(REPO_ROOT)
    env = dev_runtime.build_environment(config)
    process_cwd = tmp_path / "cwd"
    process_cwd.mkdir()
    code = """
from app.services.planning.providers.selection import (
    create_planning_provider,
    UnsupportedPlanningProviderError,
)
try:
    create_planning_provider(None)
except UnsupportedPlanningProviderError:
    print("closed")
else:
    raise SystemExit("provider unexpectedly selected")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=process_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"


def test_startup_failure_rolls_back_only_owned_processes(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    process = dev_runtime._spawn(
        config,
        state,
        "api",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        os.environ.copy(),
        ["time.sleep(60)"],
    )
    record = state["processes"]["api"]

    errors = dev_runtime._rollback_startup(
        config, state, RuntimeError("simulated readiness failure")
    )
    process.wait(timeout=5)

    assert errors == []
    assert state["status"] == "startup_failed_cleaned"
    assert dev_runtime._process_identity(record)[0] == "stopped"


def test_pid_start_time_mismatch_is_not_stopped(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    record = dev_runtime._record_for_process("test", process, ["time.sleep(60)"])
    altered = {**record, "start_ticks": record["start_ticks"] + 1}
    try:
        assert dev_runtime._process_identity(record)[0] == "owned"
        assert dev_runtime._process_identity(altered)[0] == "unknown"
        with pytest.raises(dev_runtime.RuntimeToolError, match="start time changed"):
            dev_runtime._stop_one(altered)
        assert process.poll() is None
    finally:
        if process.poll() is None:
            dev_runtime._stop_one(record)
        process.wait(timeout=5)


def test_cleanup_refuses_runtime_without_ownership_evidence(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    config.root.mkdir(parents=True)
    (config.root / "unowned-data").write_text("retain")

    with pytest.raises(
        dev_runtime.RuntimeToolError, match="positive development ownership"
    ):
        dev_runtime.cleanup_runtime(config)

    assert (config.root / "unowned-data").read_text() == "retain"


def test_cleanup_preserves_unknown_entries_inside_runtime_root(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    unknown = config.root / "operator-data"
    unknown.write_text("retain")

    with pytest.raises(dev_runtime.RuntimeToolError, match="unknown runtime entries"):
        dev_runtime.cleanup_runtime(config)

    assert unknown.read_text() == "retain"


def test_status_does_not_probe_or_claim_unowned_loopback_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    state["processes"] = {
        name: {
            "kind": name,
            "pid": 99_999_999,
            "process_group": 99_999_999,
            "start_ticks": 1,
            "expected_cmdline": [],
        }
        for name in ("api", "redis", "qdrant", "worker")
    }
    dev_runtime._save_state(config, state)

    def forbidden_probe(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("status must not probe a port without owned PID evidence")

    monkeypatch.setattr(dev_runtime, "_api_ready", forbidden_probe)
    monkeypatch.setattr(dev_runtime, "_redis_ping", forbidden_probe)
    monkeypatch.setattr(dev_runtime, "_qdrant_ready", forbidden_probe)

    result = dev_runtime.status_report(config)

    assert result["api_ready"] is False
    assert result["redis_ready"] is False
    assert result["qdrant_ready"] is False
    assert result["worker_ready"] is False


def test_stub_runtime_accepts_planning_invocation_options() -> None:
    import asyncio

    from app.services.agents.stub_runtime import StubRuntime

    runtime = StubRuntime(None, 1, 1, backend_id="stub_success")

    result = asyncio.run(
        runtime.execute_task(
            "discover",
            timeout_seconds=5,
            reuse_task_session=False,
            diagnostic_label="PLANNING_DISCOVERY",
            invocation_options=None,
        )
    )

    assert result["status"] == "completed"


def test_inference_profile_targets_only_authorized_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.setenv(dev_runtime.INFERENCE_MODEL_ENV, "qwen3-coder:30b")
    monkeypatch.setenv("OPENAI_API_KEY", "production-credential-marker")

    env = dev_runtime.build_environment(config)
    dev_runtime.validate_isolation(config, env)

    chat_url = "http://host.docker.internal:11434/v1"
    assert env["PLANNING_PROVIDER"] == "direct_openai_compatible"
    assert env["PLANNING_DIRECT_BASE_URL"] == chat_url
    assert env["OPENAI_CHAT_COMPLETIONS_BASE_URL"] == chat_url
    assert env["EXECUTION_BACKEND"] == "openai_chat_completions"
    assert env["EXECUTION_MODEL"] == "qwen3-coder:30b"
    assert env["LOW_RESOURCE_SINGLE_MODEL"] == "true"
    assert env["ENABLE_TEST_RUNTIME_BACKENDS"] == "false"
    assert env["OPENCLAW_CLI_PATH"] == ""
    assert env["ONE_SHOT_EXECUTION_ENABLED"] == "false"
    assert all("production-credential-marker" not in value for value in env.values())


def test_inference_profile_rejects_unapproved_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.setenv(dev_runtime.INFERENCE_MODEL_ENV, "qwen3-coder:30b")
    env = dev_runtime.build_environment(config)
    env["PLANNING_DIRECT_BASE_URL"] = "http://10.0.0.5:11434/v1"

    with pytest.raises(dev_runtime.RuntimeToolError, match="authorized Ollama"):
        dev_runtime.validate_isolation(config, env)


def test_default_profile_remains_fail_closed_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.delenv(dev_runtime.INFERENCE_MODEL_ENV, raising=False)
    env = dev_runtime.build_environment(config)
    env["PLANNING_DIRECT_BASE_URL"] = "http://host.docker.internal:11434/v1"

    with pytest.raises(dev_runtime.RuntimeToolError, match="assigned loopback"):
        dev_runtime.validate_isolation(config, env)
