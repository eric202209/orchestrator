#!/usr/bin/env python3
"""Run a disposable, loopback-only Orchestrator development runtime.

This launcher deliberately creates a clean process environment. It never reads
the repository's ``.env`` file and never inherits credentials or provider
settings from the invoking shell.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


API_PORT = 18740
REDIS_PORT = 16379
QDRANT_PORT = 16333
QUEUE_NAME = "phase40b-r1-dev"
EVALUATOR_EMAIL = "eval@local.dev"
PROJECT_NAME = "Phase 40B-R1 Development Runtime"
OWNER_FILE = ".dev-runtime-owner.json"
STATE_FILE = "runtime-state.json"
SCHEMA_VERSION = 1
STARTUP_TIMEOUT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 10
# The one operator-authorized inference service: the GX10 host Ollama, reached
# from this container through its pre-configured host.docker.internal mapping.
# It is fixed here so the runtime can never be pointed at another server.
AUTHORIZED_OLLAMA_URL = "http://host.docker.internal:11434"
INFERENCE_MODEL_ENV = "ORCHESTRATOR_DEV_OLLAMA_MODEL"
INFERENCE_BACKEND = "openai_chat_completions"
INFERENCE_CONTEXT_TOKENS = "32768"
OWNED_TOP_LEVEL_ENTRIES = {
    OWNER_FILE,
    STATE_FILE,
    "artifacts",
    "database",
    "home",
    "logs",
    "openclaw-home",
    "process-cwd",
    "qdrant",
    "redis",
    "run",
    "runtime",
    "tmp",
    "workspace",
    "xdg",
}


class RuntimeToolError(RuntimeError):
    """Raised when runtime safety or lifecycle checks fail."""


@dataclass(frozen=True)
class RuntimeConfig:
    repo_root: Path
    root: Path
    api_port: int = API_PORT
    redis_port: int = REDIS_PORT
    qdrant_port: int = QDRANT_PORT
    queue_name: str = QUEUE_NAME

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root", self.repo_root.resolve())
        object.__setattr__(self, "root", self.root.resolve())

    @classmethod
    def for_repo(cls, repo_root: Path) -> "RuntimeConfig":
        repo_root = repo_root.resolve()
        return cls(
            repo_root=repo_root,
            root=repo_root / ".dev-runtime" / "phase40b-r1",
        )

    @property
    def database_file(self) -> Path:
        return self.root / "database" / "orchestrator-dev.sqlite3"

    @property
    def process_cwd(self) -> Path:
        return self.root / "process-cwd"

    @property
    def worker_name(self) -> str:
        return f"phase40b-r1@{socket.gethostname()}"

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def qdrant_url(self) -> str:
        return f"http://127.0.0.1:{self.qdrant_port}"


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _require_runtime_root(config: RuntimeConfig) -> None:
    expected = config.repo_root / ".dev-runtime" / "phase40b-r1"
    if config.root != expected:
        raise RuntimeToolError(
            "runtime root must be exactly the checkout-local .dev-runtime/phase40b-r1 directory"
        )
    if config.root == config.repo_root or not _inside(config.root, config.repo_root):
        raise RuntimeToolError("runtime root escapes the development checkout")
    for path in (config.repo_root / ".dev-runtime", config.root):
        if path.is_symlink():
            raise RuntimeToolError(f"refusing symlinked runtime path: {path}")


def _as_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _require_path_under_root(name: str, value: str, root: Path) -> None:
    path = _as_path(value)
    if not _inside(path, root):
        raise RuntimeToolError(f"{name} must resolve inside the development runtime")


def _require_loopback_url(
    name: str, value: str, *, port: int, path: str | None = None
) -> None:
    parts = urlsplit(value)
    try:
        actual_port = parts.port
    except ValueError as exc:
        raise RuntimeToolError(f"{name} has an invalid port") from exc
    if (
        parts.scheme not in {"http", "redis"}
        or parts.hostname != "127.0.0.1"
        or actual_port != port
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or (path is not None and parts.path != path)
    ):
        raise RuntimeToolError(f"{name} must target its assigned loopback endpoint")


def build_environment(
    config: RuntimeConfig, *, secret_key: str | None = None
) -> dict[str, str]:
    """Return the complete, credential-sanitized environment for app processes."""

    secret_key = secret_key or secrets.token_urlsafe(48)
    tool_dirs = [
        str(Path(sys.executable).resolve().parent),
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    current_path = os.environ.get("PATH", "")
    for item in current_path.split(os.pathsep):
        if item and (Path(item) / "redis-server").exists():
            tool_dirs.append(item)
        if item and (Path(item) / "qdrant").exists():
            tool_dirs.append(item)
    path_value = os.pathsep.join(dict.fromkeys(tool_dirs))
    root = config.root
    app_env = {
        "PATH": path_value,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": "UTC",
        "PYTHONPATH": str(config.repo_root),
        "PYTHONNOUSERSITE": "1",
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
        "XDG_CACHE_HOME": str(root / "xdg" / "cache"),
        "OPENCLAW_HOME": str(root / "openclaw-home"),
        "OPENCLAW_STATE_DIR": str(root / "openclaw-home"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "SESSION_DIR": str(root / "runtime" / "sessions"),
        "ARTIFACTS_DIR": str(root / "artifacts"),
        "HOST": "127.0.0.1",
        "PORT": str(config.api_port),
        "DATABASE_URL": f"sqlite:///{config.database_file}",
        "SECRET_KEY": secret_key,
        "ADMIN_EMAILS": EVALUATOR_EMAIL,
        "CELERY_BROKER_URL": f"redis://127.0.0.1:{config.redis_port}/0",
        "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{config.redis_port}/1",
        "CELERY_QUEUE_NAME": config.queue_name,
        "RUNTIME_ROOT": str(root / "runtime"),
        "CHECKPOINT_DIR": str(root / "artifacts" / "checkpoints"),
        "CANDIDATE_CONTENT_DIR": str(root / "artifacts" / "candidate-content"),
        "ONE_SHOT_EXECUTION_ENABLED": "false",
        "ONE_SHOT_SUPERVISOR_SOCKET": str(root / "run" / "supervisor.sock"),
        "WORKER_SOURCE_ROOT": str(config.repo_root),
        "WORKER_CANDIDATE_PIN_PATH": "",
        "OPENCLAW_RUNNER_AGENT_ID": "",
        "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:0",
        "OPENCLAW_API_KEY": "",
        "OPENCLAW_CLI_PATH": "",
        "OPENCLAW_CLI_ARGS": "",
        "AGENT_BACKEND": "stub_success",
        "AGENT_SECONDARY_BACKEND": "",
        "ENABLE_TEST_RUNTIME_BACKENDS": "true",
        "PLANNING_PROVIDER": "disabled",
        "PLANNING_PROVIDER_RESPONSE_EVIDENCE_CAPTURE": "false",
        "DEBUG_REPAIR_PROVIDER_RESPONSE_EVIDENCE_CAPTURE": "false",
        "PLANNING_JSON_SCHEMA_MODE_ENABLED": "false",
        "PLANNING_BACKEND": "stub_success",
        "EXECUTION_BACKEND": "stub_success",
        "DEBUG_REPAIR_BACKEND": "stub_success",
        "REPAIR_BACKEND": "stub_success",
        "COMPLETION_REPAIR_BACKEND": "stub_success",
        "PLANNING_REPAIR_ENABLED": "false",
        "DEBUG_REPAIR_DIRECT_ENABLED": "false",
        "PLANNING_REPAIR_BASE_URL": "http://127.0.0.1:0/v1",
        "PLANNING_REPAIR_API_KEY": "",
        "DEBUG_REPAIR_BASE_URL": "http://127.0.0.1:0/v1",
        "DEBUG_REPAIR_API_KEY": "",
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": "http://127.0.0.1:0/v1",
        "OPENAI_CHAT_COMPLETIONS_BASE_URL": "http://127.0.0.1:0/v1",
        "OPENAI_CHAT_COMPLETIONS_API_KEY": "",
        "PLANNING_DIRECT_BASE_URL": "http://127.0.0.1:0/v1",
        "PLANNING_DIRECT_API_KEY": "",
        "OLLAMA_BASE_URL": "http://127.0.0.1:0",
        "OLLAMA_AGENT_MODEL": "",
        "EMBEDDING_PROVIDER": "disabled",
        "QDRANT_URL": config.qdrant_url,
        "QDRANT_COLLECTION_NAME": "phase40b_r1_dev",
        "LANGFUSE_ENABLED": "false",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "TRACE_EXPORTER_BACKEND": "local_json",
        "WORKSPACE_REVIEW_POLICY": "hold_all",
    }
    model = inference_model()
    if model:
        chat_url = f"{AUTHORIZED_OLLAMA_URL}/v1"
        app_env.update(
            {
                "AGENT_BACKEND": INFERENCE_BACKEND,
                "ENABLE_TEST_RUNTIME_BACKENDS": "false",
                "LOW_RESOURCE_SINGLE_MODEL": "true",
                "PLANNING_PROVIDER": "direct_openai_compatible",
                "PLANNING_PROVIDER_RESPONSE_EVIDENCE_CAPTURE": "true",
                "DEBUG_REPAIR_PROVIDER_RESPONSE_EVIDENCE_CAPTURE": "true",
                "PLANNING_JSON_SCHEMA_MODE_ENABLED": "true",
                "PLANNING_BACKEND": INFERENCE_BACKEND,
                "EXECUTION_BACKEND": INFERENCE_BACKEND,
                "DEBUG_REPAIR_BACKEND": INFERENCE_BACKEND,
                "REPAIR_BACKEND": INFERENCE_BACKEND,
                "COMPLETION_REPAIR_BACKEND": INFERENCE_BACKEND,
                "PLANNING_DIRECT_BASE_URL": chat_url,
                "PLANNING_DIRECT_MODEL": model,
                "OPENAI_CHAT_COMPLETIONS_BASE_URL": chat_url,
                "OPENAI_CHAT_COMPLETIONS_MODEL": model,
                "PLANNER_MODEL": model,
                "EXECUTION_MODEL": model,
                "AGENT_MODEL": model,
                "EXECUTION_CONTEXT_TOKENS": INFERENCE_CONTEXT_TOKENS,
                "PLANNING_REPAIR_CONTEXT_TOKENS": INFERENCE_CONTEXT_TOKENS,
                "DEBUG_REPAIR_CONTEXT_TOKENS": INFERENCE_CONTEXT_TOKENS,
                "PLANNING_REPAIR_ENABLED": "true",
                "PLANNING_REPAIR_BASE_URL": chat_url,
                "PLANNING_REPAIR_MODEL": model,
                "DEBUG_REPAIR_DIRECT_ENABLED": "true",
                "DEBUG_REPAIR_BASE_URL": chat_url,
                "DEBUG_REPAIR_MODEL": model,
                "COMPLETION_REPAIR_MODEL": model,
            }
        )
    return app_env


def inference_model() -> str:
    """Return the opt-in development model, or "" for the fail-closed stub."""

    return os.environ.get(INFERENCE_MODEL_ENV, "").strip()


def provider_summary() -> str:
    model = inference_model()
    if model:
        return f"{INFERENCE_BACKEND} {model} at {AUTHORIZED_OLLAMA_URL}/v1"
    return "planning disabled; deterministic stub configured"


def validate_isolation(config: RuntimeConfig, env: dict[str, str]) -> None:
    """Reject any configuration that can cross a development boundary."""

    _require_runtime_root(config)
    root = config.root
    expected_paths = {
        "DATABASE_URL": str(config.database_file),
        "RUNTIME_ROOT": str(root / "runtime"),
        "CHECKPOINT_DIR": str(root / "artifacts" / "checkpoints"),
        "CANDIDATE_CONTENT_DIR": str(root / "artifacts" / "candidate-content"),
        "ONE_SHOT_SUPERVISOR_SOCKET": str(root / "run" / "supervisor.sock"),
        "HOME": str(root / "home"),
        "OPENCLAW_HOME": str(root / "openclaw-home"),
        "OPENCLAW_STATE_DIR": str(root / "openclaw-home"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "SESSION_DIR": str(root / "runtime" / "sessions"),
        "ARTIFACTS_DIR": str(root / "artifacts"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
        "XDG_CACHE_HOME": str(root / "xdg" / "cache"),
    }
    if env.get("DATABASE_URL") != f"sqlite:///{config.database_file}":
        raise RuntimeToolError("DATABASE_URL must use the development SQLite database")
    for name in (
        "RUNTIME_ROOT",
        "CHECKPOINT_DIR",
        "CANDIDATE_CONTENT_DIR",
        "ONE_SHOT_SUPERVISOR_SOCKET",
        "HOME",
        "OPENCLAW_HOME",
        "OPENCLAW_STATE_DIR",
        "WORKSPACE_ROOT",
        "SESSION_DIR",
        "ARTIFACTS_DIR",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        value = env.get(name, "")
        if value != expected_paths[name]:
            raise RuntimeToolError(f"{name} must use its isolated development path")
        _require_path_under_root(name, value, root)

    _require_loopback_url(
        "CELERY_BROKER_URL",
        env.get("CELERY_BROKER_URL", ""),
        port=config.redis_port,
        path="/0",
    )
    _require_loopback_url(
        "CELERY_RESULT_BACKEND",
        env.get("CELERY_RESULT_BACKEND", ""),
        port=config.redis_port,
        path="/1",
    )
    _require_loopback_url(
        "QDRANT_URL", env.get("QDRANT_URL", ""), port=config.qdrant_port, path=""
    )
    for name in ("OPENCLAW_GATEWAY_URL", "OLLAMA_BASE_URL", "OPENAI_BASE_URL"):
        _require_loopback_url(name, env.get(name, ""), port=0)
    generation_urls = (
        "OPENAI_CHAT_COMPLETIONS_BASE_URL",
        "PLANNING_DIRECT_BASE_URL",
        "PLANNING_REPAIR_BASE_URL",
        "DEBUG_REPAIR_BASE_URL",
    )
    inference = env.get("PLANNING_PROVIDER") != "disabled"
    if inference:
        for name in generation_urls:
            if env.get(name) != f"{AUTHORIZED_OLLAMA_URL}/v1":
                raise RuntimeToolError(
                    f"{name} must target only the authorized Ollama endpoint"
                )
    else:
        for name in generation_urls:
            _require_loopback_url(name, env.get(name, ""), port=0, path="/v1")
    if env.get("HOST") != "127.0.0.1" or env.get("PORT") != str(config.api_port):
        raise RuntimeToolError("API must bind the assigned loopback port")
    if (
        env.get("CELERY_QUEUE_NAME") != config.queue_name
        or config.queue_name == "celery"
    ):
        raise RuntimeToolError(
            "Celery queue must be unique to this development runtime"
        )
    if env.get("ONE_SHOT_EXECUTION_ENABLED", "").lower() != "false":
        raise RuntimeToolError("one-shot execution must remain disabled")
    if inference:
        if (
            env.get("PLANNING_PROVIDER") != "direct_openai_compatible"
            or env.get("AGENT_BACKEND") != INFERENCE_BACKEND
            or env.get("LOW_RESOURCE_SINGLE_MODEL", "").lower() != "true"
        ):
            raise RuntimeToolError(
                "development inference must use the single-model direct profile"
            )
        if env.get("ENABLE_TEST_RUNTIME_BACKENDS", "").lower() != "false":
            raise RuntimeToolError(
                "test-only runtime backends must be off for real inference"
            )
    else:
        if env.get("AGENT_BACKEND") != "stub_success":
            raise RuntimeToolError(
                "development agent backend must be the explicit test stub"
            )
        if env.get("ENABLE_TEST_RUNTIME_BACKENDS", "").lower() != "true":
            raise RuntimeToolError(
                "test-only runtime backend gate must be enabled explicitly"
            )
    if env.get("OPENCLAW_CLI_PATH") or env.get("OPENCLAW_CLI_ARGS"):
        raise RuntimeToolError(
            "OpenClaw CLI must be disabled in the development runtime"
        )
    if env.get("OPENCLAW_API_KEY") or env.get("OPENAI_API_KEY"):
        raise RuntimeToolError(
            "provider credentials must be empty in the development runtime"
        )
    if env.get("ADMIN_EMAILS") != EVALUATOR_EMAIL:
        raise RuntimeToolError(
            "only the development evaluator may be configured as admin"
        )
    if env.get("PYTHONPATH") != str(config.repo_root):
        raise RuntimeToolError(
            "Python source path must resolve to this development checkout"
        )
    if len(env.get("SECRET_KEY", "")) < 32:
        raise RuntimeToolError("development SECRET_KEY must be unique and nonempty")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o666)
    temporary.replace(path)
    path.chmod(0o666)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeToolError(f"cannot read runtime ownership record: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeToolError(f"invalid runtime ownership record: {path}")
    return value


def _owner_path(config: RuntimeConfig) -> Path:
    return config.root / OWNER_FILE


def _state_path(config: RuntimeConfig) -> Path:
    return config.root / STATE_FILE


def _owner_record(config: RuntimeConfig) -> dict[str, Any]:
    owner_path = _owner_path(config)
    if not owner_path.exists():
        raise RuntimeToolError("development runtime ownership evidence is missing")
    record = _read_json(owner_path)
    if (
        record.get("schema") != SCHEMA_VERSION
        or record.get("repo_root") != str(config.repo_root)
        or record.get("runtime_root") != str(config.root)
        or not record.get("instance_id")
    ):
        raise RuntimeToolError(
            "development runtime ownership evidence does not match this checkout"
        )
    return record


def _ensure_owner(config: RuntimeConfig) -> dict[str, Any]:
    if _owner_path(config).exists():
        return _owner_record(config)
    if config.root.exists() and any(config.root.iterdir()):
        raise RuntimeToolError(
            "refusing to claim a nonempty runtime directory without ownership evidence"
        )
    record = {
        "schema": SCHEMA_VERSION,
        "instance_id": str(uuid.uuid4()),
        "repo_root": str(config.repo_root),
        "runtime_root": str(config.root),
        "created_at_epoch": time.time(),
    }
    _write_json(_owner_path(config), record)
    return record


def _validate_existing_root(config: RuntimeConfig) -> None:
    _require_runtime_root(config)
    if config.root.exists():
        _owner_record(config)
        if (config.process_cwd / ".env").exists():
            raise RuntimeToolError(
                "refusing an unexpected .env in the isolated process directory"
            )


def _make_directories(config: RuntimeConfig) -> None:
    directories = [
        config.root,
        config.root / "database",
        config.root / "redis",
        config.root / "qdrant",
        config.root / "workspace",
        config.root / "workspace" / "phase40b-r1-project",
        config.root / "artifacts",
        config.root / "artifacts" / "checkpoints",
        config.root / "artifacts" / "candidate-content",
        config.root / "runtime",
        config.root / "runtime" / "sessions",
        config.root / "run",
        config.root / "logs",
        config.root / "home",
        config.root / "openclaw-home",
        config.root / "tmp",
        config.root / "xdg" / "config",
        config.root / "xdg" / "cache",
        config.process_cwd,
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o777)


def _which_required(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise RuntimeToolError(f"required development binary is unavailable: {name}")
    # Preserve the executable name: packaged Redis multi-call binaries select
    # server mode from argv[0], and resolving the symlink can change that mode.
    return str(Path(binary).absolute())


def _qdrant_binary(config: RuntimeConfig) -> str | None:
    configured = os.environ.get("ORCHESTRATOR_DEV_QDRANT_BIN", "").strip()
    binary = configured or shutil.which("qdrant")
    if not binary:
        return None
    candidate = Path(binary).expanduser().absolute()
    resolved = candidate.resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeToolError("ORCHESTRATOR_DEV_QDRANT_BIN is not an executable file")
    protected_checkout = config.repo_root.parent / "orchestrator-production"
    if _inside(resolved, protected_checkout.resolve()):
        raise RuntimeToolError(
            "Qdrant binary must not come from the protected production checkout"
        )
    return str(candidate)


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def preflight(config: RuntimeConfig) -> dict[str, Any]:
    _require_runtime_root(config)
    env = build_environment(config)
    validate_isolation(config, env)
    if config.root.exists():
        _validate_existing_root(config)
    if config.process_cwd.exists() and (config.process_cwd / ".env").exists():
        raise RuntimeToolError(
            "refusing an unexpected .env in the isolated process directory"
        )
    redis_binary = _which_required("redis-server")
    qdrant_binary = _qdrant_binary(config)
    service_ports = (
        config.api_port,
        config.redis_port,
        config.qdrant_port,
        config.qdrant_port + 1,
    )
    occupied = [port for port in service_ports if not _port_available(port)]
    if occupied:
        raise RuntimeToolError(f"development loopback port(s) unavailable: {occupied}")
    if len(set(service_ports)) != len(service_ports):
        raise RuntimeToolError("API, Redis, and Qdrant ports must be distinct")
    return {
        "safe": True,
        "repo_root": str(config.repo_root),
        "runtime_root": str(config.root),
        "api_url": config.api_url,
        "redis_port": config.redis_port,
        "qdrant_url": config.qdrant_url,
        "queue": config.queue_name,
        "worker": config.worker_name,
        "provider": provider_summary(),
        "redis_binary": redis_binary,
        "qdrant_binary": qdrant_binary,
        "qdrant_mode": "isolated_process" if qdrant_binary else "disabled_no_binary",
    }


def _proc_snapshot(pid: int) -> tuple[int, int, str] | None:
    stat_path = Path(f"/proc/{pid}/stat")
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        stat_text = stat_path.read_text()
        close_paren = stat_text.rfind(")")
        fields = stat_text[close_paren + 2 :].split()
        process_group = int(fields[2])
        start_ticks = int(fields[19])
        cmdline = (
            cmdline_path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        )
        return process_group, start_ticks, cmdline
    except (OSError, ValueError, IndexError):
        return None


def _process_identity(record: dict[str, Any]) -> tuple[str, str]:
    pid = int(record["pid"])
    snapshot = _proc_snapshot(pid)
    if snapshot is None:
        return "stopped", "process is absent"
    process_group, start_ticks, cmdline = snapshot
    if start_ticks != int(record["start_ticks"]):
        return "unknown", "PID start time changed"
    if process_group != int(record["process_group"]):
        return "unknown", "process group changed"
    expected = [str(item) for item in record.get("expected_cmdline", [])]
    if not all(item in cmdline for item in expected):
        return "unknown", "process command identity changed"
    return "owned", "PID, start time, process group, and command identity match"


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_group_has_live_members(process_group: int) -> bool:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return _process_group_exists(process_group)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text()
            close_paren = text.rfind(")")
            fields = text[close_paren + 2 :].split()
            state = fields[0]
            member_group = int(fields[2])
        except (OSError, ValueError, IndexError):
            continue
        if member_group == process_group and state != "Z":
            return True
    return False


def _record_for_process(
    kind: str, process: subprocess.Popen[Any], expected_cmdline: list[str]
) -> dict[str, Any]:
    snapshot = _proc_snapshot(process.pid)
    if snapshot is None:
        raise RuntimeToolError(f"{kind} exited before ownership could be recorded")
    process_group, start_ticks, _cmdline = snapshot
    if process_group != process.pid:
        raise RuntimeToolError(f"{kind} did not start in its private process group")
    return {
        "kind": kind,
        "pid": process.pid,
        "process_group": process_group,
        "start_ticks": start_ticks,
        "expected_cmdline": expected_cmdline,
    }


def _load_state(config: RuntimeConfig, owner: dict[str, Any]) -> dict[str, Any]:
    if not _state_path(config).exists():
        return {
            "schema": SCHEMA_VERSION,
            "instance_id": owner["instance_id"],
            "status": "stopped",
            "processes": {},
        }
    state = _read_json(_state_path(config))
    if (
        state.get("schema") != SCHEMA_VERSION
        or state.get("instance_id") != owner["instance_id"]
    ):
        raise RuntimeToolError("runtime state does not match ownership evidence")
    if not isinstance(state.get("processes"), dict):
        raise RuntimeToolError("runtime process state is invalid")
    return state


def _save_state(config: RuntimeConfig, state: dict[str, Any]) -> None:
    _write_json(_state_path(config), state)


def _spawn(
    config: RuntimeConfig,
    state: dict[str, Any],
    kind: str,
    command: list[str],
    process_env: dict[str, str],
    expected_cmdline: list[str],
) -> subprocess.Popen[Any]:
    log_path = config.root / "logs" / f"{kind}.log"
    with log_path.open("ab") as log_stream:
        log_path.chmod(0o666)
        process = subprocess.Popen(
            command,
            cwd=config.process_cwd,
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = _record_for_process(kind, process, expected_cmdline)
    state.setdefault("processes", {})[kind] = record
    state["status"] = "starting"
    _save_state(config, state)
    return process


def _stop_one(record: dict[str, Any]) -> None:
    identity, reason = _process_identity(record)
    process_group = int(record["process_group"])
    if identity == "stopped":
        if _process_group_has_live_members(process_group):
            raise RuntimeToolError(
                f"{record['kind']} leader is absent but its process group remains; refusing cleanup"
            )
        return
    if identity != "owned":
        raise RuntimeToolError(f"refusing to stop {record['kind']}: {reason}")
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_has_live_members(process_group):
            return
        time.sleep(0.1)
    snapshot = _proc_snapshot(int(record["pid"]))
    if snapshot is None or snapshot[1] != int(record["start_ticks"]):
        raise RuntimeToolError(
            f"{record['kind']} leader exited while its process group remains; refusing SIGKILL"
        )
    if snapshot[0] != process_group:
        raise RuntimeToolError(
            f"refusing SIGKILL after {record['kind']} process group changed"
        )
    os.killpg(process_group, signal.SIGKILL)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_has_live_members(process_group):
            return
        time.sleep(0.1)
    if _process_group_has_live_members(process_group):
        raise RuntimeToolError(f"{record['kind']} process group did not stop")


def _stop_processes(config: RuntimeConfig, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    processes = state.get("processes", {})
    for kind in ("api", "worker", "qdrant", "redis"):
        record = processes.get(kind)
        if not record:
            continue
        try:
            _stop_one(record)
        except RuntimeToolError as exc:
            errors.append(str(exc))
    if not errors:
        state["status"] = "stopped"
        _save_state(config, state)
    return errors


def _rollback_startup(
    config: RuntimeConfig, state: dict[str, Any], exc: Exception
) -> list[str]:
    state["status"] = "startup_failed"
    state["error"] = f"{type(exc).__name__}: {exc}"
    state["failed_at_epoch"] = time.time()
    _save_state(config, state)
    cleanup_errors = _stop_processes(config, state)
    state["status"] = (
        "startup_failed_cleaned"
        if not cleanup_errors
        else "startup_failed_cleanup_incomplete"
    )
    state["cleanup_errors"] = cleanup_errors
    _save_state(config, state)
    return cleanup_errors


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            content = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            content = None
        return exc.code, content


def _api_ready(config: RuntimeConfig) -> bool:
    try:
        status, _payload = _http_json(f"{config.api_url}/health")
        return status == 200
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _redis_ping(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as conn:
            conn.sendall(b"*1\r\n$4\r\nPING\r\n")
            return conn.recv(32).startswith(b"+PONG")
    except OSError:
        return False


def _qdrant_ready(config: RuntimeConfig) -> bool:
    for endpoint in ("/readyz", "/healthz"):
        try:
            with urllib.request.urlopen(
                f"{config.qdrant_url}{endpoint}", timeout=1
            ) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            continue
    return False


def _wait_until(
    config: RuntimeConfig,
    kind: str,
    check: Callable[[], bool],
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = (
            _load_state(config, _owner_record(config)).get("processes", {}).get(kind)
        )
        if record:
            identity, reason = _process_identity(record)
            if identity != "owned":
                raise RuntimeToolError(f"{kind} failed during startup: {reason}")
        if check():
            return
        time.sleep(0.25)
    raise RuntimeToolError(f"{kind} did not become ready within {timeout:g} seconds")


def initialize_database(config: RuntimeConfig, env: dict[str, str]) -> None:
    """Initialize only the database path validated for this development runtime."""

    validate_isolation(config, env)
    command = [
        sys.executable,
        "-c",
        "from app.database import init_db; init_db()",
    ]
    result = subprocess.run(
        command,
        cwd=config.process_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not config.database_file.is_file():
        raise RuntimeToolError("development database initialization failed")
    config.database_file.chmod(0o666)


def _mint_dev_token(config: RuntimeConfig, env: dict[str, str]) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.auth import create_access_token; "
            f"print(create_access_token(data={{'sub': {EVALUATOR_EMAIL!r}}}))",
        ],
        cwd=config.process_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeToolError(
            "could not mint the ephemeral development evaluator token"
        )
    return token


def _ensure_evaluator_and_project(config: RuntimeConfig, env: dict[str, str]) -> None:
    # Live verification uses the designated existing account. Do not create an
    # account or exercise password registration/login as a startup side effect.
    token = _mint_dev_token(config, env)
    try:
        me_status, me = _http_json(f"{config.api_url}/api/v1/auth/me", token=token)
        if me_status != 200 or (me or {}).get("email") != EVALUATOR_EMAIL:
            raise RuntimeToolError("development evaluator identity did not resolve")

        projects_status, projects = _http_json(
            f"{config.api_url}/api/v1/projects?skip=0&limit=100", token=token
        )
        if projects_status != 200 or not isinstance(projects, list):
            raise RuntimeToolError("could not list development projects")
        workspace = config.root / "workspace" / "phase40b-r1-project"

        def resolved_project_workspace(value: str) -> Path:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = config.root / "workspace" / path
            return path.resolve()

        matching = [item for item in projects if item.get("name") == PROJECT_NAME]
        if matching:
            existing_path = resolved_project_workspace(
                str(matching[0].get("workspace_path") or "")
            )
            if existing_path != workspace.resolve():
                raise RuntimeToolError(
                    "existing development Project points outside its isolated workspace"
                )
            return
        project_status, project = _http_json(
            f"{config.api_url}/api/v1/projects",
            method="POST",
            token=token,
            payload={
                "name": PROJECT_NAME,
                "description": "Temporary Phase 40B-R1 isolated runtime Project.",
                "branch": "main",
                "workspace_path": str(workspace),
            },
        )
        if project_status != 201 or (project or {}).get("name") != PROJECT_NAME:
            raise RuntimeToolError(
                f"development Project creation returned HTTP {project_status}"
            )
        created_path = resolved_project_workspace(
            str((project or {}).get("workspace_path") or "")
        )
        if created_path != workspace.resolve() or not _inside(
            created_path, config.root
        ):
            raise RuntimeToolError("created development Project escaped its workspace")
    finally:
        token = ""


def _worker_ping(config: RuntimeConfig, env: dict[str, str]) -> bool:
    code = (
        "import json; from app.celery_app import celery_app; "
        f"reply=celery_app.control.inspect(destination=[{config.worker_name!r}], "
        "timeout=1.0).ping() or {}; print(json.dumps(reply))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=config.process_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False
    return (
        config.worker_name in payload
        and payload[config.worker_name].get("ok") == "pong"
    )


def _redis_environment(config: RuntimeConfig) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(config.root / "home"),
        "TMPDIR": str(config.root / "tmp"),
        "TZ": "UTC",
    }


def _qdrant_environment(config: RuntimeConfig) -> dict[str, str]:
    env = _redis_environment(config)
    env.update(
        {
            "QDRANT__SERVICE__HOST": "127.0.0.1",
            "QDRANT__SERVICE__HTTP_PORT": str(config.qdrant_port),
            "QDRANT__SERVICE__GRPC_PORT": str(config.qdrant_port + 1),
            "QDRANT__STORAGE__STORAGE_PATH": str(config.root / "qdrant"),
            "QDRANT__TELEMETRY_DISABLED": "true",
        }
    )
    return env


def start_runtime(config: RuntimeConfig) -> dict[str, Any]:
    preflight_info = preflight(config)
    owner = _ensure_owner(config)
    _make_directories(config)
    state = _load_state(config, owner)
    live = []
    unknown = []
    for name, record in state.get("processes", {}).items():
        identity, reason = _process_identity(record)
        if identity == "owned":
            live.append(name)
        elif identity == "unknown":
            unknown.append(f"{name}: {reason}")
    if unknown:
        raise RuntimeToolError(
            "runtime has processes with unproven ownership: " + "; ".join(unknown)
        )
    if live:
        raise RuntimeToolError(
            "development runtime already has live processes: " + ", ".join(live)
        )

    state["processes"] = {}
    state["status"] = "starting"
    state["preflight"] = preflight_info
    state["started_at_epoch"] = time.time()
    state["error"] = None
    _save_state(config, state)
    app_env = build_environment(config)
    validate_isolation(config, app_env)
    try:
        redis = _which_required("redis-server")
        redis_command = [
            redis,
            "--bind",
            "127.0.0.1",
            "--protected-mode",
            "yes",
            "--port",
            str(config.redis_port),
            "--dir",
            str(config.root / "redis"),
            "--pidfile",
            str(config.root / "redis" / "redis.pid"),
            "--appendonly",
            "no",
            "--save",
            "",
            "--daemonize",
            "no",
        ]
        _spawn(
            config,
            state,
            "redis",
            redis_command,
            _redis_environment(config),
            ["redis-server"],
        )
        _wait_until(config, "redis", lambda: _redis_ping(config.redis_port))

        qdrant = _qdrant_binary(config)
        if qdrant:
            _spawn(
                config,
                state,
                "qdrant",
                [qdrant],
                _qdrant_environment(config),
                ["qdrant"],
            )
            _wait_until(config, "qdrant", lambda: _qdrant_ready(config))
            state["qdrant_mode"] = "isolated_process"
        else:
            state["qdrant_mode"] = "disabled_no_binary"
            state["qdrant_note"] = (
                "Qdrant endpoint remains an isolated loopback URL; no Qdrant process was started"
            )
            _save_state(config, state)

        initialize_database(config, app_env)

        api_command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(config.api_port),
            "--no-access-log",
        ]
        _spawn(
            config,
            state,
            "api",
            api_command,
            app_env,
            ["uvicorn", "app.main:app", "127.0.0.1", str(config.api_port)],
        )
        _wait_until(config, "api", lambda: _api_ready(config))
        _ensure_evaluator_and_project(config, app_env)

        worker_command = [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.celery_app",
            "worker",
            "--loglevel=INFO",
            "--hostname",
            config.worker_name,
            "--queues",
            config.queue_name,
            "--pool=solo",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
        _spawn(
            config,
            state,
            "worker",
            worker_command,
            app_env,
            [config.worker_name],
        )
        _wait_until(config, "worker", lambda: _worker_ping(config, app_env))
        state["status"] = "ready"
        state["ready_at_epoch"] = time.time()
        _save_state(config, state)
        return status_report(config)
    except Exception as exc:
        cleanup_errors = _rollback_startup(config, state, exc)
        if cleanup_errors:
            raise RuntimeToolError(
                f"startup failed ({exc}); owned-process cleanup was incomplete: {cleanup_errors}"
            ) from exc
        raise


def status_report(config: RuntimeConfig) -> dict[str, Any]:
    _require_runtime_root(config)
    if not config.root.exists() or not _owner_path(config).exists():
        return {"status": "stopped", "owned": False, "runtime_root": str(config.root)}
    owner = _owner_record(config)
    state = _load_state(config, owner)
    processes: dict[str, Any] = {}
    for name, record in state.get("processes", {}).items():
        identity, reason = _process_identity(record)
        processes[name] = {
            "ownership": identity,
            "pid": record.get("pid"),
            "detail": reason,
        }
    return {
        "status": state.get("status", "unknown"),
        "owned": True,
        "runtime_root": str(config.root),
        "api_ready": processes.get("api", {}).get("ownership") == "owned"
        and _api_ready(config),
        "redis_ready": processes.get("redis", {}).get("ownership") == "owned"
        and _redis_ping(config.redis_port),
        "qdrant_ready": processes.get("qdrant", {}).get("ownership") == "owned"
        and _qdrant_ready(config),
        "qdrant_mode": state.get("qdrant_mode", "unknown"),
        "worker_ready": (
            _worker_ping(config, build_environment(config))
            if processes.get("worker", {}).get("ownership") == "owned"
            else False
        ),
        "queue": config.queue_name,
        "worker": config.worker_name,
        "provider": provider_summary(),
        "processes": processes,
    }


def stop_runtime(config: RuntimeConfig) -> dict[str, Any]:
    _require_runtime_root(config)
    if not config.root.exists() or not _owner_path(config).exists():
        return {"status": "stopped", "owned": False, "runtime_root": str(config.root)}
    owner = _owner_record(config)
    state = _load_state(config, owner)
    errors = _stop_processes(config, state)
    if errors:
        raise RuntimeToolError(
            "could not safely stop all owned processes: " + "; ".join(errors)
        )
    state["stopped_at_epoch"] = time.time()
    _save_state(config, state)
    return status_report(config)


def _validate_cleanup_tree(config: RuntimeConfig) -> None:
    entries = {item.name for item in config.root.iterdir()}
    unknown = entries - OWNED_TOP_LEVEL_ENTRIES
    if unknown:
        raise RuntimeToolError(
            "cleanup refused unknown runtime entries: " + ", ".join(sorted(unknown))
        )
    device = config.root.stat().st_dev
    for current, directories, files in os.walk(config.root, followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            if path.is_symlink():
                raise RuntimeToolError(
                    f"cleanup refused symlink in runtime tree: {path}"
                )
            if path.stat().st_dev != device:
                raise RuntimeToolError(f"cleanup refused mounted runtime entry: {path}")


def cleanup_runtime(
    config: RuntimeConfig, *, preserve_evidence: bool = False
) -> dict[str, Any]:
    _require_runtime_root(config)
    if not config.root.exists() or not _owner_path(config).exists():
        raise RuntimeToolError(
            "cleanup requires positive development ownership evidence"
        )
    _owner_record(config)
    stopped = stop_runtime(config)
    live = [
        name
        for name, item in stopped.get("processes", {}).items()
        if item.get("ownership") != "stopped"
    ]
    if live:
        raise RuntimeToolError(
            "cleanup refused because a process is not proven stopped"
        )
    if preserve_evidence:
        return {**stopped, "evidence_preserved": True}
    _validate_cleanup_tree(config)
    shutil.rmtree(config.root)
    return {"status": "cleaned", "owned": True, "runtime_root": str(config.root)}


def _default_config() -> RuntimeConfig:
    return RuntimeConfig.for_repo(Path(__file__).resolve().parents[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("preflight", "start", "status", "stop", "cleanup")
    )
    parser.add_argument(
        "--preserve-evidence",
        action="store_true",
        help="for cleanup, stop services but retain the owned runtime state and logs",
    )
    args = parser.parse_args(argv)
    config = _default_config()
    try:
        if args.command == "preflight":
            result = preflight(config)
        elif args.command == "start":
            result = start_runtime(config)
        elif args.command == "status":
            result = status_report(config)
        elif args.command == "stop":
            result = stop_runtime(config)
        else:
            result = cleanup_runtime(config, preserve_evidence=args.preserve_evidence)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeToolError, OSError, subprocess.SubprocessError) as exc:
        print(f"development runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
