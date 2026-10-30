"""PR20-A3: explicit task-acceptance-verification opt-in for the dev runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import dev_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
OPT_IN = dev_runtime.ACCEPTANCE_VERIFICATION_OPT_IN_ENV
APP_FLAG = dev_runtime.ACCEPTANCE_VERIFICATION_APP_ENV


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


def _offline_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dev_runtime, "_which_required", lambda name: "/usr/bin/redis-server"
    )
    monkeypatch.setattr(dev_runtime, "_qdrant_binary", lambda _config: None)
    monkeypatch.setattr(dev_runtime, "_port_available", lambda _port: True)


@pytest.mark.parametrize(
    ("opt_in", "expected"), [(None, "false"), ("0", "false"), ("1", "true")]
)
def test_opt_in_maps_to_application_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: str | None,
    expected: str,
) -> None:
    config = _fake_checkout(tmp_path)
    if opt_in is None:
        monkeypatch.delenv(OPT_IN, raising=False)
    else:
        monkeypatch.setenv(OPT_IN, opt_in)

    env = dev_runtime.build_environment(config)
    dev_runtime.validate_isolation(config, env)

    assert env[APP_FLAG] == expected


@pytest.mark.parametrize(
    "opt_in", ["", "true", "TRUE", "yes", "on", "2", " 1", "1 ", "false"]
)
def test_unsupported_opt_in_values_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: str
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.setenv(OPT_IN, opt_in)

    with pytest.raises(dev_runtime.RuntimeToolError, match="unset, 0, or 1"):
        dev_runtime.build_environment(config)
    _offline_preflight(monkeypatch)
    with pytest.raises(dev_runtime.RuntimeToolError, match="unset, 0, or 1"):
        dev_runtime.preflight(config)


def test_closed_environment_does_not_inherit_application_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.delenv(OPT_IN, raising=False)
    monkeypatch.setenv(APP_FLAG, "true")
    monkeypatch.setenv("UNRELATED_APP_SETTING", "inherited-marker")

    env = dev_runtime.build_environment(config)

    assert env[APP_FLAG] == "false"
    assert "UNRELATED_APP_SETTING" not in env
    assert OPT_IN not in env
    assert all("inherited-marker" not in value for value in env.values())


@pytest.mark.parametrize("value", [None, "", "True", "1", "yes", "false "])
def test_isolation_rejects_malformed_application_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.delenv(OPT_IN, raising=False)
    env = dev_runtime.build_environment(config)
    if value is None:
        del env[APP_FLAG]
    else:
        env[APP_FLAG] = value

    with pytest.raises(dev_runtime.RuntimeToolError, match="exactly true or false"):
        dev_runtime.validate_isolation(config, env)


@pytest.mark.parametrize(("opt_in", "expected"), [(None, "false"), ("1", "true")])
def test_preflight_reports_planned_value_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: str | None,
    expected: str,
) -> None:
    config = _fake_checkout(tmp_path)
    _offline_preflight(monkeypatch)
    if opt_in is None:
        monkeypatch.delenv(OPT_IN, raising=False)
    else:
        monkeypatch.setenv(OPT_IN, opt_in)

    result = dev_runtime.preflight(config)

    assert result["task_acceptance_verification"] == {
        "opt_in_env": OPT_IN,
        "opt_in_value": opt_in,
        "planned_task_acceptance_verification": expected,
        "isolation_validated": True,
        "basis": "planned_configuration_no_process_started",
    }
    assert result["queue"] == "phase40b-r1-dev"
    assert result["worker"] == config.worker_name


def test_preflight_still_refuses_unexpected_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    _offline_preflight(monkeypatch)
    monkeypatch.setenv(OPT_IN, "1")
    (config.process_cwd / ".env").write_text(f"{APP_FLAG}=true\n")

    with pytest.raises(dev_runtime.RuntimeToolError, match="unexpected .env"):
        dev_runtime.preflight(config)


def test_api_and_worker_receive_same_derived_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    _offline_preflight(monkeypatch)
    monkeypatch.setenv(OPT_IN, "1")
    spawned: dict[str, dict[str, str]] = {}

    def fake_spawn(
        _config: Any,
        state: dict[str, Any],
        kind: str,
        _command: list[str],
        process_env: dict[str, str],
        _expected: list[str],
    ) -> None:
        spawned[kind] = dict(process_env)
        state.setdefault("processes", {})

    monkeypatch.setattr(dev_runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(dev_runtime, "_wait_until", lambda *_a, **_k: None)
    monkeypatch.setattr(dev_runtime, "initialize_database", lambda *_a: None)
    monkeypatch.setattr(dev_runtime, "_ensure_evaluator_and_project", lambda *_a: None)
    monkeypatch.setattr(dev_runtime, "status_report", lambda _config: {})

    dev_runtime.start_runtime(config)

    assert spawned["api"] == spawned["worker"]
    assert spawned["api"][APP_FLAG] == "true"
    assert APP_FLAG not in spawned["redis"]
    state = dev_runtime._load_state(config, dev_runtime._owner_record(config))
    assert state["task_acceptance_verification"] == {
        "opt_in_value": "1",
        "configured": "true",
    }
    assert (
        state["preflight"]["task_acceptance_verification"][
            "planned_task_acceptance_verification"
        ]
        == "true"
    )


def test_status_reports_configured_and_owned_process_start_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)
    owner = dev_runtime._owner_record(config)
    state = dev_runtime._load_state(config, owner)
    state["task_acceptance_verification"] = {
        "opt_in_value": "1",
        "configured": "true",
    }
    process = dev_runtime._spawn(
        config,
        state,
        "worker",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), APP_FLAG: "true"},
        ["time.sleep(60)"],
    )
    record = state["processes"]["worker"]
    state["processes"]["api"] = {
        "kind": "api",
        "pid": 99_999_999,
        "process_group": 99_999_999,
        "start_ticks": 1,
        "expected_cmdline": [],
    }
    dev_runtime._save_state(config, state)
    monkeypatch.setattr(dev_runtime, "_worker_ping", lambda *_a: False)
    monkeypatch.setattr(dev_runtime, "_api_ready", lambda *_a: False)
    monkeypatch.delenv(OPT_IN, raising=False)
    try:
        result = dev_runtime.status_report(config)
    finally:
        dev_runtime._stop_one(record)
        process.wait(timeout=5)

    reported = result["task_acceptance_verification"]
    assert reported["configured"] == "true"
    assert reported["opt_in_value"] == "1"
    assert reported["process_start"] == {"worker": "true", "api": None}
    assert "persisted completion verdict" in reported["effective_per_task"]

    # After the processes stop, the configured value persists but no
    # process-start value is claimed.
    stopped = dev_runtime.status_report(config)["task_acceptance_verification"]
    assert stopped["configured"] == "true"
    assert stopped["process_start"] == {"worker": None, "api": None}


def test_status_without_recorded_configuration_claims_nothing(
    tmp_path: Path,
) -> None:
    config = _fake_checkout(tmp_path)
    _prepare(config)

    reported = dev_runtime.status_report(config)["task_acceptance_verification"]

    assert reported["configured"] is None
    assert reported["process_start"] == {}


def test_main_stack_configuration_and_shell_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings

    config = _fake_checkout(tmp_path)
    monkeypatch.setenv(OPT_IN, "1")
    before = dict(os.environ)

    dev_runtime.build_environment(config)

    assert dict(os.environ) == before
    assert Settings.model_fields[APP_FLAG].default is False


def test_absent_opt_in_leaves_existing_environment_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _fake_checkout(tmp_path)
    monkeypatch.delenv(OPT_IN, raising=False)
    monkeypatch.delenv(dev_runtime.INFERENCE_MODEL_ENV, raising=False)

    env = dev_runtime.build_environment(config, secret_key="k" * 48)
    dev_runtime.validate_isolation(config, env)

    # "false" equals the application default, so the off profile behaves as
    # it did before the flag was written into the closed environment.
    assert env[APP_FLAG] == "false"
    assert env["AGENT_BACKEND"] == "stub_success"
    assert env["PLANNING_PROVIDER"] == "disabled"
    assert env["CELERY_QUEUE_NAME"] == "phase40b-r1-dev"
    assert env["ONE_SHOT_EXECUTION_ENABLED"] == "false"
