"""PR20-A7 process-identity and startup-boundary regressions."""

from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]


def _record() -> dict[str, object]:
    return {
        "kind": "redis",
        "pid": 1234,
        "process_group": 1234,
        "start_ticks": 5678,
        "expected_cmdline": ["redis-server"],
    }


def _fake_checkout(tmp_path: Path) -> dev_runtime.RuntimeConfig:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    (repo_root / "app").symlink_to(REPO_ROOT / "app", target_is_directory=True)
    return dev_runtime.RuntimeConfig(
        repo_root=repo_root,
        root=repo_root / ".dev-runtime" / "phase40b-r1",
    )


def test_normal_redis_command_identity_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (
            1234,
            5678,
            "/usr/bin/redis-server --bind 127.0.0.1 --port 16379",
        ),
    )

    assert dev_runtime._process_identity(record) == (
        "owned",
        "PID, start time, process group, and command identity match",
    )


def test_a6_error_is_reproduced_by_missing_expected_command_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterizes the failure predicate; A6's raw cmdline was not retained."""
    record = _record()
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (1234, 5678, "/usr/bin/redis-check-rdb --version"),
    )

    assert dev_runtime._process_identity(record) == (
        "unknown",
        "process command identity changed",
    )


@pytest.mark.parametrize(
    "cmdline",
    [
        "/usr/bin/redis-server --bind 127.0.0.1 --port 16379",
        "redis-server 127.0.0.1:16379",
    ],
)
def test_observed_redis_process_title_forms_keep_identity(
    monkeypatch: pytest.MonkeyPatch, cmdline: str
) -> None:
    record = _record()
    monkeypatch.setattr(
        dev_runtime, "_proc_snapshot", lambda _pid: (1234, 5678, cmdline)
    )

    assert dev_runtime._process_identity(record)[0] == "owned"


def test_pid_reuse_with_changed_start_ticks_is_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (1234, 5679, "/usr/bin/redis-server --port 16379"),
    )

    assert dev_runtime._process_identity(record) == (
        "unknown",
        "PID start time changed",
    )


def test_changed_recorded_start_time_is_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {**_record(), "start_ticks": 1}
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (1234, 5678, "/usr/bin/redis-server --port 16379"),
    )

    assert dev_runtime._process_identity(record) == (
        "unknown",
        "PID start time changed",
    )


def test_unexpected_executable_substitution_is_not_owned() -> None:
    process = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    substituted = dev_runtime._record_for_process("redis", process, ["redis-server"])
    try:
        assert dev_runtime._process_identity(substituted) == (
            "unknown",
            "process command identity changed",
        )
        with pytest.raises(
            dev_runtime.RuntimeToolError,
            match="refusing to stop redis: process command identity changed",
        ):
            dev_runtime._stop_one(substituted)
        assert process.poll() is None

        verified_fixture = {**substituted, "expected_cmdline": ["sleep"]}
        dev_runtime._stop_one(verified_fixture)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            verified_fixture = {**substituted, "expected_cmdline": ["sleep"]}
            dev_runtime._stop_one(verified_fixture)
            process.wait(timeout=5)


def test_unrelated_process_on_redis_port_blocks_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = dataclasses.replace(_fake_checkout(tmp_path), redis_port=port)
        actual_port_check = dev_runtime._port_available
        monkeypatch.setattr(
            dev_runtime,
            "_which_required",
            lambda _name: "/usr/bin/redis-server",
        )
        monkeypatch.setattr(dev_runtime, "_qdrant_binary", lambda _config: None)
        monkeypatch.setattr(
            dev_runtime,
            "_port_available",
            lambda candidate: (
                actual_port_check(candidate) if candidate == port else True
            ),
        )

        with pytest.raises(
            dev_runtime.RuntimeToolError, match=r"port\(s\) unavailable"
        ):
            dev_runtime.preflight(config)


def test_stale_runtime_instance_state_is_rejected(tmp_path: Path) -> None:
    config = _fake_checkout(tmp_path)
    owner = dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    dev_runtime._save_state(
        config,
        {
            "schema": dev_runtime.SCHEMA_VERSION,
            "instance_id": "different-runtime-instance",
            "status": "stopped",
            "processes": {},
        },
    )

    with pytest.raises(dev_runtime.RuntimeToolError, match="does not match ownership"):
        dev_runtime._load_state(config, owner)


def test_process_exit_during_startup_is_not_claimed_as_owned(
    tmp_path: Path,
) -> None:
    config = _fake_checkout(tmp_path)
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    process = dev_runtime._spawn(
        config,
        state,
        "redis",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        os.environ.copy(),
        ["time.sleep(60)"],
    )
    record = state["processes"]["redis"]
    try:
        dev_runtime._stop_one(record)
        process.wait(timeout=5)

        with pytest.raises(
            dev_runtime.RuntimeToolError,
            match="redis failed during startup: process is absent",
        ):
            dev_runtime._wait_until(config, "redis", lambda: False, timeout=0.1)
    finally:
        if process.poll() is None:
            dev_runtime._stop_one(record)
            process.wait(timeout=5)


def test_cleanup_refuses_changed_command_and_stops_only_verified_fixture(
    tmp_path: Path,
) -> None:
    config = _fake_checkout(tmp_path)
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    process = dev_runtime._spawn(
        config,
        state,
        "redis",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        os.environ.copy(),
        ["time.sleep(60)"],
    )
    record = state["processes"]["redis"]
    changed = {**record, "expected_cmdline": ["redis-server"]}
    try:
        with pytest.raises(
            dev_runtime.RuntimeToolError,
            match="refusing to stop redis: process command identity changed",
        ):
            dev_runtime._stop_one(changed)
        assert process.poll() is None
        dev_runtime._stop_one(record)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            dev_runtime._stop_one(record)
            process.wait(timeout=5)
