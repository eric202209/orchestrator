"""Phase 37 Pre-B-F2C: dedicated Protocol v2 planning config lifecycle (D-37-15).

The dedicated strict-planning config now uses the F2B managed-binding
lifecycle. Provider-free and process-safe:

* ``os.killpg``, ``os.kill`` and ``signal.signal`` are recorders for every
  test; ``subprocess.Popen`` and ``asyncio.create_subprocess_exec`` raise.
  Teardown asserts no spawn and no signal outside the mocked handler path.
* Configs and credentials are synthetic (``_persistent_state``); the binding
  root is the conftest-isolated one, and the system temp dir is redirected
  into ``tmp_path`` so a regression to system-temp configs is observable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services.agents import subprocess_lifecycle as sl
from app.services.agents.openclaw_service import OpenClawAgentSelectionError
from app.services.orchestration.execution import binding_reconciliation as recon
from app.services.orchestration.execution import executor_workspace_binding as ewb
from app.tests.test_phase37_preb_f2b_binding_lifecycle import (
    SYNTHETIC_SECRET,
    _planning_service,
    _root_entries,
)
from app.tests.test_phase37_preb_f2b_s2_credential_isolation import _ctx

DEDICATED = "phase36-dogfood-reentry-x"  # present in the synthetic config
PROVIDER_SECRET = "f2c-synthetic-provider-key-not-real"
STRICT_PREFIX = "planning-brief"


# --------------------------------------------------------------------------
# containment
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _containment(monkeypatch, tmp_path):
    record = {"killpg": [], "kill": [], "signal": [], "spawn": []}

    def blocked_spawn(*args, **kwargs):
        record["spawn"].append(args[:1])
        raise AssertionError("F2C is provider-free: subprocess spawn blocked")

    monkeypatch.setattr(os, "killpg", lambda p, s: record["killpg"].append((p, s)))
    monkeypatch.setattr(os, "kill", lambda p, s: record["kill"].append((p, s)))
    monkeypatch.setattr(signal, "signal", lambda n, h: record["signal"].append((n, h)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_spawn)
    monkeypatch.setattr(subprocess, "Popen", blocked_spawn)
    monkeypatch.setattr(sl.time, "sleep", lambda _s: None)
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv("ORCHESTRATOR_OPENCLAW_PROTOCOL_V2_PLANNING_AGENT", DEDICATED)
    record["system_tmp"] = system_tmp
    sl._reset_for_tests()
    yield record
    sl._reset_for_tests()
    assert record["spawn"] == [], "provider/subprocess spawn attempted"
    assert record["killpg"] == []
    assert all(
        pid == os.getpid() and sig == signal.SIGTERM for pid, sig in record["kill"]
    )
    # Nothing is ever created in the (redirected) system temp dir any more.
    assert list(system_tmp.iterdir()) == []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _source_config(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".openclaw" / "openclaw.json"


def _add_provider_secret(tmp_path: Path) -> None:
    path = _source_config(tmp_path)
    config = json.loads(path.read_text())
    config["models"]["providers"]["openai"]["apiKey"] = PROVIDER_SECRET
    path.write_text(json.dumps(config, indent=2))


def _legacy_dedicated_config(source, *, agent_id, runtime_workspace, model_ref, root):
    """Verbatim pre-F2C rewrite rules (openclaw_service.py at ``2d00d51``)."""

    config = json.loads(json.dumps(source))
    agents = (config.get("agents") or {}).get("list") or []
    selected = next(a for a in agents if str(a.get("id") or "").strip() == agent_id)
    selected["workspace"] = str(runtime_workspace)
    selected["agentDir"] = str(Path(root) / "agent")
    if model_ref is not None:
        selected["model"] = {"primary": model_ref, "fallbacks": []}
    defaults = (config.setdefault("agents", {})).setdefault("defaults", {})
    defaults["workspace"] = str(runtime_workspace)
    memory_search = defaults.get("memorySearch")
    if isinstance(memory_search, dict):
        defaults["memorySearch"] = {**memory_search, "enabled": False}
    session_config = config.setdefault("session", {})
    session_config["store"] = str(Path(root) / "sessions.json")
    return config


def _normalized(config: dict, root: Path) -> str:
    return json.dumps(config, sort_keys=True).replace(str(root), "<BINDING_DIR>")


def _bind(service, tmp_path, name="direct"):
    runtime_workspace = _ctx(tmp_path, name, 1).runtime_workspace
    service._bind_dedicated_strict_planning_agent(runtime_workspace, DEDICATED)
    return service._strict_planning_binding, runtime_workspace


def _lock_is_held(directory: Path) -> bool:
    fd, problem = recon._try_lock(directory)
    if fd is not None:
        os.close(fd)
    return problem == "lock_held"


def _snapshot_files(directory: Path) -> dict:
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _observing_run(service, observed, behaviour):
    async def run(full_cmd, *, cwd, **kwargs):
        binding = service._strict_planning_binding
        observed["binding"] = binding
        observed["dir"] = binding._tmp_dir
        observed["cwd"] = cwd
        observed["lock_held"] = _lock_is_held(binding._tmp_dir)
        observed["env"] = service._apply_workspace_binding_env({})
        observed["spawn_kwargs"] = service._workspace_binding_spawn_kwargs()
        observed["lock_fd"] = binding._lock_fd
        observed["config"] = json.loads(binding.config_path.read_text())
        observed["callbacks"] = list(sl._active_cleanup_callbacks)
        return await behaviour()

    return run


def _stub(service, monkeypatch, run):
    monkeypatch.setattr(service, "_run_cli_prompt_with_diagnostics", run)
    monkeypatch.setattr(service, "parse_cli_response", lambda *a, **k: {"output": "ok"})


def _assert_released(service, observed, root):
    assert observed["dir"] is not None and not observed["dir"].exists()
    assert observed["binding"]._lock_fd is None
    assert service._strict_planning_binding is None
    assert service._openclaw_config_path_override is None
    assert service._workspace_binding_spawn_kwargs() == {}
    assert sl._active_cleanup_callbacks == []
    assert _root_entries(root) == []


async def _ok():
    return object(), {}


# --------------------------------------------------------------------------
# D1-D12: managed artifact and config compatibility
# --------------------------------------------------------------------------


def test_d1_d6_dedicated_binding_is_a_private_managed_artifact(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    service.task_execution_id = 5150
    binding, _workspace = _bind(service, tmp_path)
    try:
        root = binding._tmp_dir
        assert root.parent == isolated_openclaw_binding_root  # D1
        assert root.name.startswith(ewb.BINDING_DIR_PREFIX)
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700  # D2
        assert stat.S_IMODE(os.lstat(binding.config_path).st_mode) == 0o600  # D3
        assert binding.config_path.parent == root
        metadata = json.loads((root / ewb.BINDING_METADATA_NAME).read_text())  # D4
        assert metadata["schema"] == ewb.BINDING_METADATA_SCHEMA
        assert metadata["binding_id"] == binding.binding_id
        assert metadata["artifact_dir_name"] == root.name
        assert metadata["owner_pid"] == os.getpid()
        assert metadata["task_execution_id"] == 5150
        assert _lock_is_held(root)  # D5
        assert sorted(os.listdir(root)) == sorted(
            [
                ewb.BINDING_LOCK_NAME,
                ewb.BINDING_METADATA_NAME,
                "agent",
                "home",
                "openclaw.json",
                "state",
                "tmp",
            ]
        )
        assert (root / "agent").is_dir()
        assert not any(p.name == "auth-profiles.json" for p in root.rglob("*"))
        for content in _snapshot_files(root).values():
            assert SYNTHETIC_SECRET.encode() not in content
        assert sl._active_cleanup_callbacks == [binding.release]
    finally:
        service._release_dedicated_strict_planning_agent()
    assert _root_entries(isolated_openclaw_binding_root) == []


def test_d7_d10_config_is_equivalent_to_the_previous_builder(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    _add_provider_secret(tmp_path)
    source = json.loads(_source_config(tmp_path).read_text())
    model_ref = service._runtime_openclaw_model_ref_from_config(
        source, service.runtime_configuration.model_family
    )
    assert model_ref  # D9: a pinned model is actually exercised
    binding, workspace = _bind(service, tmp_path)
    try:
        root = binding._tmp_dir
        written = json.loads(binding.config_path.read_text())
        expected = ewb.project_local_openclaw_config(
            source,
            model_ref=model_ref,
            agent_id=DEDICATED,
            runtime_workspace=workspace,
            agent_dir=root / "agent",
            state_dir=root / "state",
        )
        assert _normalized(written, root) == _normalized(expected, root)  # D7
        selected = next(a for a in written["agents"]["list"] if a["id"] == DEDICATED)
        assert service._last_selected_openclaw_agent_id == DEDICATED  # D8
        assert selected["model"] == {"primary": model_ref, "fallbacks": []}  # D9
        assert list(written["models"]["providers"]) == ["openai"]
        assert "openai-codex" not in written["models"]["providers"]
        assert (
            written["models"]["providers"]["openai"]["apiKey"]
            == source["models"]["providers"]["openai"]["apiKey"]
        )
        # D10: the operator config is the only source and stays untouched;
        # the override points at the binding's own config.
        assert json.loads(_source_config(tmp_path).read_text()) == source
        assert service._openclaw_config_path_override == binding.config_path
        assert service._openclaw_config_path() == binding.config_path
        assert service._apply_workspace_binding_env({}) == {
            "OPENCLAW_CONFIG_PATH": str(binding.config_path),
            "OPENCLAW_STATE_DIR": str(root / "state"),
            "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"),
        }
        assert selected["workspace"] == str(workspace)  # D11 (config side)
    finally:
        service._release_dedicated_strict_planning_agent()


def test_d12_strict_provider_controls_accept_only_the_managed_config(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    binding, _workspace = _bind(service, tmp_path)
    try:
        service._configure_strict_provider_controls(DEDICATED)
        written = json.loads(binding.config_path.read_text())
        selected = next(a for a in written["agents"]["list"] if a["id"] == DEDICATED)
        assert selected["params"]["temperature"] == 0
        assert stat.S_IMODE(os.lstat(binding.config_path).st_mode) == 0o600
        # Pointing the override anywhere else still fails closed (F2A).
        service._openclaw_config_path_override = _source_config(tmp_path)
        from app.services.agents.openclaw_service import (
            OpenClawProviderControlError,
        )

        with pytest.raises(OpenClawProviderControlError):
            service._configure_strict_provider_controls(DEDICATED)
    finally:
        service._release_dedicated_strict_planning_agent()


# --------------------------------------------------------------------------
# D11, D13-D16, D18: lifecycle through the real invoke_prompt
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d11_d13_success_uses_managed_binding_and_releases_it(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}
    _stub(service, monkeypatch, _observing_run(service, observed, _ok))
    result = await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    assert result["output"] == "ok"
    binding = observed["binding"]
    assert observed["dir"].parent == isolated_openclaw_binding_root
    assert observed["lock_held"] is True
    assert observed["callbacks"] == [binding.release]
    assert observed["env"]["OPENCLAW_CONFIG_PATH"] == str(binding.config_path)
    # The OpenClaw child inherits the lock, keeping the artifact ACTIVE.
    assert observed["spawn_kwargs"] == {"pass_fds": (observed["lock_fd"],)}
    selected = next(
        a for a in observed["config"]["agents"]["list"] if a["id"] == DEDICATED
    )
    assert observed["cwd"] == selected["workspace"]  # D11: planning runtime cwd
    assert Path(observed["cwd"]).parent.parent.name == "planning"
    assert service._last_selected_openclaw_agent_id == DEDICATED
    _assert_released(service, observed, isolated_openclaw_binding_root)


@pytest.mark.parametrize("kind", ["provider_failure", "cancel", "timeout"])
@pytest.mark.asyncio
async def test_d14_d16_failure_cancel_timeout_release_the_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, kind
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}
    entered = asyncio.Event()

    async def fail():
        raise RuntimeError("synthetic provider failure")

    async def hang():
        entered.set()
        await asyncio.Event().wait()

    _stub(
        service,
        monkeypatch,
        _observing_run(service, observed, fail if kind == "provider_failure" else hang),
    )
    if kind == "provider_failure":
        with pytest.raises(RuntimeError):
            await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    elif kind == "cancel":
        task = asyncio.ensure_future(
            service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert observed["dir"].exists() and observed["lock_held"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                service.invoke_prompt("plan", session_prefix=STRICT_PREFIX),
                timeout=0.2,
            )
    _assert_released(service, observed, isolated_openclaw_binding_root)


@pytest.mark.asyncio
async def test_d18_mocked_forced_termination_drain_releases_the_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, _containment
):
    """Where the handler is installed (Celery worker), it releases the binding.

    This does not verify FastAPI: FastAPI installs no such handler (see the
    F2C report). It only proves the binding is registered and drainable.
    """

    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}

    async def sigterm_mid_run():
        sl._handle_forced_termination(signal.SIGTERM, None)  # all signals mocked
        observed["exists_after_drain"] = observed["dir"].exists()
        return object(), {}

    _stub(service, monkeypatch, _observing_run(service, observed, sigterm_mid_run))
    await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    assert observed["exists_after_drain"] is False
    # The asyncio runner may also (mock-)register SIGINT; only SIGTERM matters.
    sigterm = [entry for entry in _containment["signal"] if entry[0] == signal.SIGTERM]
    assert sigterm == [(signal.SIGTERM, signal.SIG_DFL)]
    assert _containment["kill"] == [(os.getpid(), signal.SIGTERM)]
    _assert_released(service, observed, isolated_openclaw_binding_root)


# --------------------------------------------------------------------------
# D17, D19-D22: ownership, idempotence, reconciliation, partial failure
# --------------------------------------------------------------------------


def test_d17_repeated_cleanup_is_idempotent(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    binding, _workspace = _bind(service, tmp_path)
    unregister = binding._unregister_forced_cleanup
    service._release_dedicated_strict_planning_agent()
    service._release_dedicated_strict_planning_agent()
    binding.release()
    unregister()
    assert binding._lock_fd is None and not binding._tmp_dir.exists()
    assert sl._active_cleanup_callbacks == []
    assert _root_entries(isolated_openclaw_binding_root) == []


@pytest.mark.parametrize("owner_alive", [False, True])
def test_d19_crash_residue_is_classified_by_the_existing_reconciler(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, owner_alive
):
    """Host (e.g. FastAPI) dies without running any cleanup callback."""

    service = _planning_service(db_session, tmp_path, monkeypatch)
    binding, _workspace = _bind(service, tmp_path)
    # Simulated death: no release() runs; the kernel drops the flock.
    binding._unregister_forced_cleanup()
    os.close(binding._lock_fd)
    binding._lock_fd = None
    if not owner_alive:
        path = binding._tmp_dir / ewb.BINDING_METADATA_NAME
        metadata = json.loads(path.read_text())
        metadata["owner_pid"] = 2**22 + 31  # above pid_max: never alive
        path.write_text(json.dumps(metadata))

    report = recon.reconcile_binding_artifacts()
    [entry] = report["artifacts"]
    assert entry["binding_id"] == binding.binding_id
    assert entry["credential_bearing"] is False
    expected = recon.STALE_SUSPECTED if owner_alive else recon.STALE_CONFIRMED
    assert entry["classification"] == expected
    assert binding._tmp_dir.exists()  # report-only never removes

    applied = recon.reconcile_binding_artifacts(apply=True)  # isolated root only
    if owner_alive:
        assert applied["summary"]["by_action"] == {recon.REPORTED: 1}
        assert binding._tmp_dir.exists()
    else:
        assert applied["summary"]["by_action"] == {recon.REMOVED: 1}
        assert _root_entries(isolated_openclaw_binding_root) == []
    service._strict_planning_binding = None
    if binding._tmp_dir.exists():
        binding.release()


def test_d20_reconciliation_never_removes_an_active_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    binding, _workspace = _bind(service, tmp_path)
    try:
        payload = recon.reconcile_binding_artifacts(apply=True)
        assert [a["classification"] for a in payload["artifacts"]] == [recon.ACTIVE]
        assert binding.config_path.exists() and _lock_is_held(binding._tmp_dir)
    finally:
        service._release_dedicated_strict_planning_agent()


def test_d21_stale_cleanup_cannot_remove_a_newer_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    stale, _workspace = _bind(service, tmp_path, "first")
    stale_release, stale_unregister = stale.release, stale._unregister_forced_cleanup
    service._release_dedicated_strict_planning_agent()
    newer, _workspace = _bind(service, tmp_path, "second")
    stale_release()
    stale_unregister()
    assert newer._tmp_dir != stale._tmp_dir
    assert newer.config_path.exists() and _lock_is_held(newer._tmp_dir)
    assert service._openclaw_config_path_override == newer.config_path
    assert sl._active_cleanup_callbacks == [newer.release]
    service._release_dedicated_strict_planning_agent()


def test_d21_second_dedicated_owner_is_refused_without_side_effects(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    first, workspace = _bind(service, tmp_path)
    before = _root_entries(isolated_openclaw_binding_root)
    with pytest.raises(OpenClawAgentSelectionError):
        service._bind_dedicated_strict_planning_agent(workspace, DEDICATED)
    assert service._strict_planning_binding is first
    assert _root_entries(isolated_openclaw_binding_root) == before
    assert sl._active_cleanup_callbacks == [first.release]
    service._release_dedicated_strict_planning_agent()


@pytest.mark.parametrize("failing", ["config_write", "metadata", "unsafe_root"])
def test_d22_partial_construction_leaves_nothing_registered(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, failing
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    root = isolated_openclaw_binding_root
    if failing == "config_write":
        original = Path.write_text

        def write_text(path, *args, **kwargs):
            if path.name == "openclaw.json" and path.parent.parent == root:
                raise OSError("disk full")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        expected = OSError
    elif failing == "metadata":
        monkeypatch.setattr(
            ewb,
            "_binding_metadata",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        expected = KeyboardInterrupt
    else:
        root = tmp_path / "open-root"
        root.mkdir(mode=0o777)
        os.chmod(root, 0o777)
        monkeypatch.setenv(ewb.BINDING_ROOT_ENV, str(root))
        expected = OpenClawAgentSelectionError
    workspace = _ctx(tmp_path, "partial", 1).runtime_workspace
    with pytest.raises(expected):
        service._bind_dedicated_strict_planning_agent(workspace, DEDICATED)
    assert _root_entries(root) == []
    assert service._strict_planning_binding is None
    assert service._openclaw_config_path_override is None
    assert sl._active_cleanup_callbacks == []


# --------------------------------------------------------------------------
# D23: credential values never surface
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d23_credential_values_absent_from_diagnostics(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, caplog
):
    caplog.set_level(logging.DEBUG)
    service = _planning_service(db_session, tmp_path, monkeypatch)
    _add_provider_secret(tmp_path)
    surfaces = []

    async def fail():
        raise RuntimeError("synthetic provider failure")

    observed = {"dir": None}
    _stub(service, monkeypatch, _observing_run(service, observed, fail))
    with pytest.raises(RuntimeError) as failure:
        await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    surfaces += [str(failure.value), repr(failure.value)]
    surfaces.append(
        json.dumps(getattr(failure.value, "runtime_diagnostics", {}), default=str)
    )

    binding, _workspace = _bind(service, tmp_path, "diag")
    surfaces.append((binding._tmp_dir / ewb.BINDING_METADATA_NAME).read_text())
    surfaces.append(json.dumps(recon.reconcile_binding_artifacts(), default=str))
    with pytest.raises(OpenClawAgentSelectionError) as refused:
        service._bind_dedicated_strict_planning_agent(_workspace, DEDICATED)
    surfaces.append(str(refused.value))
    service._release_dedicated_strict_planning_agent()
    surfaces.append(caplog.text)
    for text in surfaces:
        assert PROVIDER_SECRET not in text
        assert SYNTHETIC_SECRET not in text


# --------------------------------------------------------------------------
# D-S2-01 dedicated-branch regression (mandatory)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_s2_01_dedicated_branch_cannot_release_outer_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    """Before S2 this branch reached the provider and destroyed the outer binding.

    Pre-S2: ``_bind_dedicated_strict_planning_agent`` copied the *outer*
    ephemeral config, the provider call ran, and the ``finally`` released
    the outer worker-owned binding.
    """

    service = _planning_service(db_session, tmp_path, monkeypatch)
    outer_context = _ctx(tmp_path, "outer", 77)
    service.bind_runtime_workspace(outer_context)
    outer = service._workspace_binding
    outer_id, outer_dir, outer_fd = outer.binding_id, outer._tmp_dir, outer._lock_fd
    outer_files = _snapshot_files(outer_dir)
    provider_calls = []

    async def run(*a, **k):
        provider_calls.append(True)
        return object(), {}

    _stub(service, monkeypatch, run)
    with pytest.raises(OpenClawAgentSelectionError):
        await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    assert provider_calls == []
    assert service._workspace_binding is outer and outer.binding_id == outer_id
    assert outer._tmp_dir == outer_dir and outer_dir.exists()
    assert outer._lock_fd == outer_fd and _lock_is_held(outer_dir)
    assert _snapshot_files(outer_dir) == outer_files
    assert service._openclaw_config_path_override == outer.config_path
    assert service._runtime_executor_context is outer_context
    assert service.execution_cwd_override == str(outer_context.runtime_workspace)
    assert service._strict_planning_binding is None  # no dedicated leak
    assert _root_entries(isolated_openclaw_binding_root) == [outer_dir.name]
    assert sl._active_cleanup_callbacks == [outer.release]
    service.release_runtime_workspace_binding()
    assert _root_entries(isolated_openclaw_binding_root) == []


@pytest.mark.asyncio
async def test_planning_invocation_refuses_while_dedicated_binding_is_active(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    active, _workspace = _bind(service, tmp_path)
    provider_calls = []

    async def run(*a, **k):
        provider_calls.append(True)
        return object(), {}

    _stub(service, monkeypatch, run)
    with pytest.raises(OpenClawAgentSelectionError):
        await service.invoke_prompt("plan", session_prefix=STRICT_PREFIX)
    assert provider_calls == []
    assert service._strict_planning_binding is active
    assert active.config_path.exists() and _lock_is_held(active._tmp_dir)
    assert service._openclaw_config_path_override == active.config_path
    service._release_dedicated_strict_planning_agent()
