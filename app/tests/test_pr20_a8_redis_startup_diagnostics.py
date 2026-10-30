"""PR20-A8 bounded diagnostics for the isolated Redis startup boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]


def _record(**overrides: object) -> dict[str, object]:
    return {
        "kind": "redis",
        "pid": 1234,
        "process_group": 1234,
        "start_ticks": 5678,
        "expected_cmdline": ["redis-server"],
        **overrides,
    }


def _config(tmp_path: Path, *, port: int = 16379) -> dev_runtime.RuntimeConfig:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    (repo_root / "app").symlink_to(REPO_ROOT / "app", target_is_directory=True)
    return dev_runtime.RuntimeConfig(
        repo_root=repo_root,
        root=repo_root / ".dev-runtime" / "a8-test",
        redis_port=port,
    )


def _observation(
    cmdline: str = "/usr/bin/redis-server --bind 127.0.0.1 --port 16379",
    *,
    process_group: int = 1234,
    start_ticks: int = 5678,
) -> dict[str, object]:
    raw = b"\0".join(part.encode() for part in cmdline.split()) + b"\0"
    return {
        "observed_monotonic_ns": 123456789,
        "observed_wall_clock_utc": "2026-10-30T04:00:00.000000Z",
        "proc_reads": {
            "stat": {
                "monotonic_start_ns": 123456700,
                "wall_clock_start_utc": "2026-10-30T04:00:00.000000Z",
                "monotonic_end_ns": 123456710,
            },
            "cmdline": {
                "monotonic_start_ns": 123456720,
                "wall_clock_start_utc": "2026-10-30T04:00:00.000000Z",
                "monotonic_end_ns": 123456730,
            },
            "exe": {
                "monotonic_start_ns": 123456740,
                "wall_clock_start_utc": "2026-10-30T04:00:00.000000Z",
                "monotonic_end_ns": 123456750,
            },
        },
        "proc_read_errors": {},
        "parent_pid": 4321,
        "process_state": "S",
        "process_group": process_group,
        "start_ticks": start_ticks,
        "cmdline": raw.replace(b"\0", b" ").decode(),
        "raw_cmdline": raw,
        "executable_target": "/usr/bin/redis-server",
        "snapshot": (
            process_group,
            start_ticks,
            raw.replace(b"\0", b" ").decode(),
        ),
    }


def _diagnostics(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _emit(
    path: Path,
    record: dict[str, object],
    observation: dict[str, object],
    identity: str = "owned",
    reason: str = "PID, start time, process group, and command identity match",
    *,
    readiness: bool | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dev_runtime._write_redis_diagnostic(
        path,
        record,
        observation,
        identity,
        reason,
        event="test_observation",
        child_process=None,
        readiness_result=readiness,
    )


def test_diagnostics_are_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    state: dict[str, object] = {}
    monkeypatch.delenv(dev_runtime.REDIS_STARTUP_DIAGNOSTICS_OPT_IN_ENV, raising=False)

    dev_runtime._configure_redis_startup_diagnostics(
        state,
        enabled=os.environ.get(dev_runtime.REDIS_STARTUP_DIAGNOSTICS_OPT_IN_ENV) == "1",
    )

    assert dev_runtime._redis_diagnostic_path(config, state) is None
    assert not (config.root / "logs" / "redis-startup-identity.jsonl").exists()


def test_explicit_opt_in_enables_capture(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    state: dict[str, object] = {}
    monkeypatch.setenv(dev_runtime.REDIS_STARTUP_DIAGNOSTICS_OPT_IN_ENV, "1")
    dev_runtime._configure_redis_startup_diagnostics(
        state,
        enabled=os.environ.get(dev_runtime.REDIS_STARTUP_DIAGNOSTICS_OPT_IN_ENV) == "1",
    )
    path = dev_runtime._redis_diagnostic_path(config, state)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        dev_runtime, "_read_proc_diagnostic_observation", lambda _pid: _observation()
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path)[0] == "owned"

    assert len(_diagnostics(path)) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_normal_redis_identity_is_recorded(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "logs" / "identity.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(
        dev_runtime, "_read_proc_diagnostic_observation", lambda _pid: _observation()
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path) == (
        "owned",
        "PID, start time, process group, and command identity match",
    )
    entry = _diagnostics(path)[0]
    assert entry["pid"] == 1234
    assert entry["parent_pid"] == 4321
    assert entry["process_group"] == 1234
    assert entry["process_start_ticks"] == 5678
    assert entry["process_state"] == "S"
    assert entry["expected_command_tokens"] == ["redis-server"]
    assert entry["identity_classification"] == "owned"
    assert entry["failure_reason"] is None
    assert entry["observed_monotonic_ns"] == 123456789
    assert entry["observed_wall_clock_utc"] == "2026-10-30T04:00:00.000000Z"
    assert entry["executable_symlink_target"] == "/usr/bin/redis-server"


def test_normal_redis_title_rewriting_is_recorded_without_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "identity.jsonl"
    title = "redis-server 127.0.0.1:16379"
    padded_title = "/usr/bin/redis-server 127.0.0.1:16379" + (" " * 80)
    observation = _observation(title)
    raw_title = padded_title.encode() + b"\0"
    observation["raw_cmdline"] = raw_title
    observation["cmdline"] = raw_title.replace(b"\0", b" ").decode()
    observation["snapshot"] = (
        1234,
        5678,
        raw_title.replace(b"\0", b" ").decode(),
    )
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: observation,
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path)[0] == "owned"
    assert _diagnostics(path)[0]["observed_cmdline"] == title


def test_missing_expected_token_records_observed_cmdline(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "identity.jsonl"
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: _observation("/usr/bin/redis-check-rdb --version"),
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path) == (
        "unknown",
        "process command identity changed",
    )
    entry = _diagnostics(path)[0]
    assert entry["observed_cmdline"] == "redis-check-rdb --version"
    assert entry["failure_reason"] == "process command identity changed"


@pytest.mark.parametrize(
    ("observation", "record", "reason"),
    [
        (_observation(start_ticks=5679), _record(), "PID start time changed"),
        (_observation(process_group=9999), _record(), "process group changed"),
    ],
)
def test_start_time_and_process_group_mismatches_remain_rejected(
    tmp_path: Path,
    monkeypatch,
    observation: dict[str, object],
    record: dict[str, object],
    reason: str,
) -> None:
    path = tmp_path / "identity.jsonl"
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: observation,
    )

    assert dev_runtime._process_identity(record, diagnostic_path=path) == (
        "unknown",
        reason,
    )
    assert _diagnostics(path)[0]["failure_reason"] == reason


def test_process_substitution_is_rejected_and_cleanup_remains_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "logs" / "identity.jsonl"
    path.parent.mkdir()
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    record = dev_runtime._record_for_process("redis", process, ["redis-server"])
    try:
        with pytest.raises(
            dev_runtime.RuntimeToolError,
            match="refusing to stop redis: process command identity changed",
        ):
            dev_runtime._stop_one(record, diagnostic_path=path)
        assert process.poll() is None
        assert _diagnostics(path)[0]["identity_classification"] == "unknown"

        verified = {**record, "expected_cmdline": ["sleep"]}
        dev_runtime._stop_one(verified, diagnostic_path=path)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            dev_runtime._stop_one(
                {**record, "expected_cmdline": ["sleep"]}, diagnostic_path=path
            )
            process.wait(timeout=5)


def test_startup_failure_and_rollback_retain_evidence_without_unsafe_cleanup(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    owner = dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    state = dev_runtime._load_state(config, owner)
    dev_runtime._configure_redis_startup_diagnostics(state, enabled=True)
    dev_runtime._save_state(config, state)
    process = dev_runtime._spawn(
        config,
        state,
        "redis",
        ["/bin/sleep", "30"],
        {
            "PATH": "/usr/bin:/bin",
            "HOME": str(config.root / "home"),
            "TMPDIR": str(config.root / "tmp"),
            "TZ": "UTC",
        },
        ["redis-server"],
    )
    path = dev_runtime._redis_diagnostic_path(config, state)
    assert path is not None
    try:
        with pytest.raises(
            dev_runtime.RuntimeToolError,
            match="redis failed during startup: process command identity changed",
        ) as startup_error:
            dev_runtime._wait_until(
                config,
                "redis",
                lambda: False,
                timeout=0.01,
                child_process=process,
            )

        cleanup_errors = dev_runtime._rollback_startup(
            config,
            state,
            startup_error.value,
            child_processes={"redis": process},
        )

        assert cleanup_errors == [
            "refusing to stop redis: process command identity changed"
        ]
        assert process.poll() is None
        entries = _diagnostics(path)
        startup_entry = next(
            item for item in entries if item["event"] == "startup_identity_poll"
        )
        rollback_entry = next(
            item for item in entries if item["event"] == "cleanup_identity"
        )
        assert startup_entry["identity_classification"] == "unknown"
        assert startup_entry["failure_reason"] == "process command identity changed"
        assert startup_entry["child_poll_status"]["available"] is True
        assert rollback_entry["identity_classification"] == "unknown"
        assert rollback_entry["failure_reason"] == "process command identity changed"
    finally:
        if process.poll() is None:
            dev_runtime._stop_one(
                {**state["processes"]["redis"], "expected_cmdline": ["sleep"]},
                diagnostic_path=path,
                child_process=process,
            )
            process.wait(timeout=5)


def test_process_exit_records_child_status(tmp_path: Path) -> None:
    path = tmp_path / "identity.jsonl"
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(7)"], start_new_session=True
    )
    assert process.wait(timeout=5) == 7
    record = _record(pid=process.pid, process_group=process.pid)

    identity, reason = dev_runtime._process_identity(
        record, diagnostic_path=path, child_process=process
    )

    assert (identity, reason) == ("stopped", "process is absent")
    entry = _diagnostics(path)[0]
    assert entry["child_poll_status"]["available"] is True
    assert entry["child_poll_status"]["returncode"] == 7
    assert isinstance(entry["child_poll_status"]["observed_monotonic_ns"], int)
    assert entry["identity_classification"] == "stopped"


def test_missing_proc_data_is_recorded_and_handled_safely(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "identity.jsonl"
    missing = _observation()
    missing.update(
        {
            "snapshot": None,
            "parent_pid": None,
            "process_group": None,
            "start_ticks": None,
            "process_state": None,
            "raw_cmdline": b"",
            "cmdline": "",
            "proc_read_errors": {
                "stat": "FileNotFoundError",
                "cmdline": "FileNotFoundError",
                "exe": "FileNotFoundError",
            },
        }
    )
    monkeypatch.setattr(
        dev_runtime, "_read_proc_diagnostic_observation", lambda _pid: missing
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path) == (
        "stopped",
        "process is absent",
    )
    entry = _diagnostics(path)[0]
    assert entry["proc_read_errors"]["stat"] == "FileNotFoundError"
    assert entry["observed_cmdline"] == ""


def test_diagnostic_collection_failure_does_not_grant_ownership(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "identity.jsonl"
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("sensitive exception value")),
    )
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (1234, 5678, "/usr/bin/redis-check-rdb --version"),
    )

    assert dev_runtime._process_identity(_record(), diagnostic_path=path) == (
        "unknown",
        "process command identity changed",
    )
    entry = _diagnostics(path)[0]
    assert entry["proc_read_errors"]["diagnostic_collection"] == "RuntimeError"
    assert "sensitive exception value" not in path.read_text()


@pytest.mark.parametrize("enabled", [False, True])
def test_diagnostics_do_not_change_readiness_decision(
    tmp_path: Path, monkeypatch, enabled: bool
) -> None:
    config = _config(tmp_path)
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    record = _record()
    state["processes"] = {"redis": record}
    dev_runtime._configure_redis_startup_diagnostics(state, enabled=enabled)
    dev_runtime._save_state(config, state)
    monkeypatch.setattr(
        dev_runtime,
        "_proc_snapshot",
        lambda _pid: (1234, 5678, "/usr/bin/redis-server --port 16379"),
    )
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: _observation(),
    )

    with pytest.raises(
        dev_runtime.RuntimeToolError,
        match=r"redis did not become ready within 0\.01 seconds",
    ):
        dev_runtime._wait_until(config, "redis", lambda: False, timeout=0.01)

    path = dev_runtime._redis_diagnostic_path(config, state)
    if enabled:
        assert path is not None
        assert _diagnostics(path)[0]["redis_readiness_result"] is False
    else:
        assert path is None


def test_record_count_and_record_size_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "identity.jsonl"
    observation = _observation("/usr/bin/redis-server " + "x" * 8000)
    for _ in range(dev_runtime.REDIS_DIAGNOSTICS_MAX_RECORDS + 10):
        _emit(path, _record(), observation)

    entries = _diagnostics(path)
    assert len(entries) == dev_runtime.REDIS_DIAGNOSTICS_MAX_RECORDS
    assert path.stat().st_size <= dev_runtime.REDIS_DIAGNOSTICS_MAX_FILE_BYTES
    assert all(
        len(line.encode()) + 1 <= dev_runtime.REDIS_DIAGNOSTICS_MAX_RECORD_BYTES
        for line in path.read_text().splitlines()
    )


def test_file_size_bound_prevents_append_near_limit(tmp_path: Path) -> None:
    path = tmp_path / "identity.jsonl"
    path.write_bytes(b"x" * (dev_runtime.REDIS_DIAGNOSTICS_MAX_FILE_BYTES - 1))
    before = path.stat().st_size

    _emit(path, _record(), _observation())

    assert path.stat().st_size == before


def test_sensitive_command_and_environment_values_are_not_retained(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "identity.jsonl"
    monkeypatch.setenv("APP_SECRET_FOR_A8_TEST", "environment-secret-value")
    command = (
        "/usr/bin/redis-server --requirepass command-line-secret "
        "--dir /tmp/private-runtime --api-key=inline-secret "
        "--custom-option 54321"
    )
    observation = _observation(command)
    monkeypatch.setattr(
        dev_runtime,
        "_read_proc_diagnostic_observation",
        lambda _pid: observation,
    )

    dev_runtime._process_identity(
        _record(expected_cmdline=["redis-server", "expected-secret-token"]),
        diagnostic_path=path,
    )

    retained = path.read_text()
    assert "command-line-secret" not in retained
    assert "inline-secret" not in retained
    assert "expected-secret-token" not in retained
    assert "54321" not in retained
    assert "/tmp/private-runtime" not in retained
    assert "environment-secret-value" not in retained
    assert "APP_SECRET_FOR_A8_TEST" not in retained
    assert "[REDACTED]" in retained


def test_redacted_cmdline_length_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "identity.jsonl"
    raw = b"\0".join([b"/usr/bin/redis-server", *([b"--safe"] * 200)])
    observation = _observation()
    observation["raw_cmdline"] = raw
    _emit(path, _record(), observation)

    entry = _diagnostics(path)[0]
    assert (
        len(entry["observed_cmdline"])
        <= dev_runtime.REDIS_DIAGNOSTICS_MAX_CMDLINE_CHARS
    )
    assert entry["observed_cmdline_truncated"] is True
