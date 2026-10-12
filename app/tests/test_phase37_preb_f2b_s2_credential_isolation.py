"""Phase 37 Pre-B-F2B-S2: provider-free credential isolation verification.

Containment contract for this module (enforced by ``_containment``, autouse):

* ``os.killpg``, ``os.kill`` and ``signal.signal`` are replaced by recorders
  for every test, before the test body runs. No signal reaches the OS.
* Every process-group number used here is above Linux ``pid_max`` (2**22),
  so it can never name a real group even if a recorder were bypassed.
* ``asyncio.create_subprocess_exec`` and ``subprocess.Popen`` raise, so no
  OpenClaw CLI (and therefore no provider) can be started. Teardown asserts
  both were never reached.
* Credentials are synthetic and live under ``tmp_path``; the binding root is
  the conftest-isolated one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.services.agents import subprocess_lifecycle as sl
from app.services.agents.openclaw_service import (
    OpenClawAgentSelectionError,
    OpenClawSessionError,
)
from app.services.orchestration.execution import binding_reconciliation as recon
from app.services.orchestration.execution import executor_workspace_binding as ewb
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext
from app.tests.test_phase37_preb_f2b_binding_lifecycle import (
    _persistent_state,
    _planning_service,
    _root_entries,
    _service,
)

# Above Linux pid_max (4194304): can never be a live PID or PGID.
FAKE_PGID = 2**22 + 101
FAKE_PGID_2 = 2**22 + 202
FAKE_PGIDS = {FAKE_PGID, FAKE_PGID_2}
SECRET_A = "s2-synthetic-secret-alpha-not-real"
SECRET_B = "s2-synthetic-secret-bravo-not-real"


# --------------------------------------------------------------------------
# containment
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _containment(monkeypatch):
    record = {"killpg": [], "kill": [], "signal": [], "spawn": []}

    def fake_killpg(pgid, sig):
        record["killpg"].append((pgid, sig))

    def fake_kill(pid, sig):
        record["kill"].append((pid, sig))

    def fake_signal(signum, handler):
        record["signal"].append((signum, handler))

    def blocked_spawn(*args, **kwargs):
        record["spawn"].append(args[:1])
        raise AssertionError("S2 is provider-free: subprocess spawn blocked")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_spawn)
    monkeypatch.setattr(subprocess, "Popen", blocked_spawn)
    monkeypatch.setattr(sl.time, "sleep", lambda _s: None)
    sl._reset_for_tests()
    yield record
    sl._reset_for_tests()
    assert record["spawn"] == [], "provider/subprocess spawn attempted"
    assert all(pgid in FAKE_PGIDS for pgid, _sig in record["killpg"])
    assert all(
        pid == os.getpid() and sig == signal.SIGTERM for pid, sig in record["kill"]
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _auth_source(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "home"
        / ".openclaw"
        / "agents"
        / "main"
        / "agent"
        / ("auth-profiles.json")
    )


def _set_secret(tmp_path: Path, secret: str) -> None:
    config = _auth_source(tmp_path).parents[3] / "openclaw.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["models"]["providers"]["openai"]["apiKey"] = secret
    config.write_text(json.dumps(payload), encoding="utf-8")


def _state(tmp_path: Path, secret: str = SECRET_A) -> Path:
    _state_dir, config = _persistent_state(tmp_path)
    _set_secret(tmp_path, secret)
    return config


def _ctx(tmp_path: Path, name: str, task_execution_id: int) -> RuntimeExecutorContext:
    project = tmp_path / "product"
    project.mkdir(exist_ok=True)
    runtime_root = tmp_path / "runtime-root"
    runtime = runtime_root / "tasks" / name
    runtime.mkdir(parents=True, exist_ok=True)
    return RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime,
        project_workspace=project,
        project_id=None,
        task_execution_id=task_execution_id,
        runtime_root=runtime_root,
        sandbox=object(),
    )


def _bind(tmp_path, config, name, task_execution_id):
    return ewb.bind_openclaw_workspace(
        _ctx(tmp_path, name, task_execution_id),
        real_config_path=config,
        model_ref="openai/qwen-local",
    )


def _copied_secret(binding) -> str:
    config = json.loads(binding.config_path.read_text())
    return config["models"]["providers"]["openai"]["apiKey"]


def _assert_self_contained(binding, runtime_workspace: Path) -> None:
    """Every credential/state path a binding exposes lives in its own dir."""

    root = binding._tmp_dir
    config = json.loads(binding.config_path.read_text())
    agent = next(a for a in config["agents"]["list"] if a["id"] == binding.agent_id)
    assert Path(agent["agentDir"]).parent == root
    assert Path(agent["workspace"]) == runtime_workspace
    assert Path(config["session"]["store"]).parent.parent == root
    for value in binding.environment.values():
        assert Path(value).parent == root
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700
    copied = root / "agent" / "auth-profiles.json"
    assert not copied.exists()
    assert stat.S_IMODE(os.lstat(binding.config_path).st_mode) == 0o600


def _stub_planning_run(service, monkeypatch, run):
    monkeypatch.setattr(service, "_run_cli_prompt_with_diagnostics", run)
    monkeypatch.setattr(service, "parse_cli_response", lambda *a, **k: {"output": "ok"})


# --------------------------------------------------------------------------
# S2-01 concurrent isolation
# --------------------------------------------------------------------------


def test_s2_01_concurrent_bindings_are_disjoint_and_independently_owned(
    tmp_path, isolated_openclaw_binding_root
):
    config = _state(tmp_path)
    workers = 4  # matches deployed worker concurrency
    barrier = threading.Barrier(workers)
    bindings, errors = {}, []

    def bind(index):
        try:
            barrier.wait(timeout=10)
            bindings[index] = _bind(tmp_path, config, f"exec-{index}", 9000 + index)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bind, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and len(bindings) == workers

    dirs = {b._tmp_dir for b in bindings.values()}
    assert len(dirs) == workers
    assert len({b.binding_id for b in bindings.values()}) == workers
    assert len({b.config_path for b in bindings.values()}) == workers
    assert len({b._lock_fd for b in bindings.values()}) == workers
    for index, binding in bindings.items():
        runtime = tmp_path / "runtime-root" / "tasks" / f"exec-{index}"
        _assert_self_contained(binding, runtime)
        metadata = json.loads(
            (binding._tmp_dir / ewb.BINDING_METADATA_NAME).read_text()
        )
        assert metadata["task_execution_id"] == 9000 + index
        # No binding's config or env references another binding's directory.
        own_text = binding.config_path.read_text() + json.dumps(binding.environment)
        for other in dirs - {binding._tmp_dir}:
            assert str(other) not in own_text

    # Each lock is independently held: none can be taken by a reconciler.
    payload = recon.reconcile_binding_artifacts(apply=True)
    assert {a["classification"] for a in payload["artifacts"]} == {recon.ACTIVE}
    assert payload["summary"]["by_action"] == {recon.REPORTED: workers}

    # Releasing one owner leaves every other owner's credential untouched.
    victim = bindings.pop(0)
    victim.release()
    assert not victim._tmp_dir.exists()
    for binding in bindings.values():
        assert _copied_secret(binding) == SECRET_A
        assert binding._lock_fd is not None
    assert len(sl._active_cleanup_callbacks) == workers - 1
    for binding in bindings.values():
        binding.release()
    assert _root_entries(isolated_openclaw_binding_root) == []
    assert sl._active_cleanup_callbacks == []


def test_s2_01_concurrent_services_expose_only_their_own_binding(
    tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    config = _state(tmp_path)
    first, second = _service(config, monkeypatch), _service(config, monkeypatch)
    first.bind_runtime_workspace(_ctx(tmp_path, "svc-1", 1))
    second.bind_runtime_workspace(_ctx(tmp_path, "svc-2", 2))
    env_1 = first._apply_workspace_binding_env({})
    env_2 = second._apply_workspace_binding_env({})
    assert (
        Path(env_1["OPENCLAW_CONFIG_PATH"]).parent == first._workspace_binding._tmp_dir
    )
    assert (
        Path(env_2["OPENCLAW_CONFIG_PATH"]).parent == second._workspace_binding._tmp_dir
    )
    assert first._openclaw_config_path() != second._openclaw_config_path()
    assert first._workspace_binding_spawn_kwargs() != (
        second._workspace_binding_spawn_kwargs()
    )
    second_dir = second._workspace_binding._tmp_dir
    first.release_runtime_workspace_binding()
    assert second._workspace_binding is not None and second_dir.exists()
    assert second._apply_workspace_binding_env({}) == env_2
    second.release_runtime_workspace_binding()
    assert _root_entries(isolated_openclaw_binding_root) == []


def test_s2_01_credential_snapshots_are_per_binding(
    tmp_path, isolated_openclaw_binding_root
):
    config = _state(tmp_path, SECRET_A)
    first = _bind(tmp_path, config, "snap-a", 1)
    _set_secret(tmp_path, SECRET_B)  # operator rotates the credential
    second = _bind(tmp_path, config, "snap-b", 2)
    assert _copied_secret(first) == SECRET_A
    assert _copied_secret(second) == SECRET_B
    assert not (first._tmp_dir / "agent" / "auth-profiles.json").exists()
    assert not (second._tmp_dir / "agent" / "auth-profiles.json").exists()
    first.release()
    assert _copied_secret(second) == SECRET_B
    second.release()


# --------------------------------------------------------------------------
# S2-02 sequential: no stale reuse
# --------------------------------------------------------------------------


def test_s2_02_sequential_rebind_never_reuses_previous_binding(
    tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    config = _state(tmp_path, SECRET_A)
    service = _service(config, monkeypatch)
    service.bind_runtime_workspace(_ctx(tmp_path, "seq-1", 1))
    old = service._workspace_binding
    old_dir, old_id, old_env = old._tmp_dir, old.binding_id, dict(old.environment)
    service.release_runtime_workspace_binding()
    assert not old_dir.exists()
    assert service._apply_workspace_binding_env({}) == {}
    assert service._workspace_binding_spawn_kwargs() == {}
    assert service._openclaw_config_path() == config  # operator config, unbound

    _set_secret(tmp_path, SECRET_B)
    service.bind_runtime_workspace(_ctx(tmp_path, "seq-2", 2))
    new = service._workspace_binding
    assert new is not old
    assert new._tmp_dir != old_dir and new.binding_id != old_id
    assert _copied_secret(new) == SECRET_B
    new_text = new.config_path.read_text() + json.dumps(new.environment)
    assert str(old_dir) not in new_text
    assert set(new.environment.values()).isdisjoint(old_env.values())
    assert service.execution_cwd_override == str(
        tmp_path / "runtime-root" / "tasks" / "seq-2"
    )
    assert len(sl._active_cleanup_callbacks) == 1
    service.release_runtime_workspace_binding()
    assert _root_entries(isolated_openclaw_binding_root) == []
    assert sl._active_cleanup_callbacks == []


# --------------------------------------------------------------------------
# S2-03..S2-06 lifecycle exits through invoke_prompt
# --------------------------------------------------------------------------


def _assert_fully_released(service, observed, root):
    assert observed["dir"] is not None
    assert not observed["dir"].exists()
    assert service._workspace_binding is None
    assert service._openclaw_config_path_override is None
    assert service._workspace_binding_spawn_kwargs() == {}
    assert sl._active_cleanup_callbacks == []
    assert _root_entries(root) == []


def _observing_run(service, observed, behaviour):
    async def run(full_cmd, *, cwd, **kwargs):
        binding = service._workspace_binding
        observed["dir"] = binding._tmp_dir
        env = service._apply_workspace_binding_env({})
        observed["env_in_own_dir"] = (
            Path(env["OPENCLAW_CONFIG_PATH"]).parent == binding._tmp_dir
        )
        observed["cwd"] = cwd
        return await behaviour()

    return run


@pytest.mark.asyncio
async def test_s2_03_successful_execution_releases_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}

    async def succeed():
        return object(), {}

    _stub_planning_run(service, monkeypatch, _observing_run(service, observed, succeed))
    result = await service.invoke_prompt("plan", session_prefix="planning")
    assert result["output"] == "ok"
    assert observed["env_in_own_dir"] is True
    _assert_fully_released(service, observed, isolated_openclaw_binding_root)


@pytest.mark.parametrize("stage", ["provider", "parse"])
@pytest.mark.asyncio
async def test_s2_04_failed_execution_releases_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, stage
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}

    async def provider_fails():
        raise RuntimeError("synthetic provider failure")

    async def succeed():
        return object(), {}

    _stub_planning_run(
        service,
        monkeypatch,
        _observing_run(
            service, observed, provider_fails if stage == "provider" else succeed
        ),
    )
    if stage == "parse":

        def parse_fails(*a, **k):
            raise ValueError("synthetic parse failure")

        monkeypatch.setattr(service, "parse_cli_response", parse_fails)
    with pytest.raises((RuntimeError, ValueError)):
        await service.invoke_prompt("plan", session_prefix="planning")
    _assert_fully_released(service, observed, isolated_openclaw_binding_root)


@pytest.mark.asyncio
async def test_s2_05_external_cancellation_releases_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.Event().wait()

    _stub_planning_run(service, monkeypatch, _observing_run(service, observed, hang))
    task = asyncio.ensure_future(
        service.invoke_prompt("plan", session_prefix="planning")
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert observed["dir"].exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_fully_released(service, observed, isolated_openclaw_binding_root)


@pytest.mark.parametrize("kind", ["caller_deadline", "provider_timeout"])
@pytest.mark.asyncio
async def test_s2_06_timeout_releases_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, kind
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {"dir": None}

    async def hang():
        await asyncio.Event().wait()

    async def provider_timeout():
        raise asyncio.TimeoutError()

    behaviour = hang if kind == "caller_deadline" else provider_timeout
    _stub_planning_run(
        service, monkeypatch, _observing_run(service, observed, behaviour)
    )
    if kind == "caller_deadline":
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                service.invoke_prompt("plan", session_prefix="planning"), timeout=0.2
            )
    else:
        with pytest.raises(OpenClawSessionError) as exc_info:
            await service.invoke_prompt("plan", session_prefix="planning")
        assert exc_info.value.provider_failure_classification == "provider_timeout"
    _assert_fully_released(service, observed, isolated_openclaw_binding_root)


class _FakeProcess:
    """Stand-in for an asyncio subprocess; never backed by a real PID."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.killed = False

    async def wait(self):
        self.returncode = -signal.SIGKILL
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_s2_06_timeout_termination_goes_through_pgid_guard(_containment):
    from app.services.agents.openclaw_service import OpenClawSessionService

    # Unverified registration (not our child): timeout cleanup must refuse.
    sl.register_process_group(FAKE_PGID)
    assert sl._active_process_groups == {FAKE_PGID: None}
    await OpenClawSessionService._terminate_process_with_bounded_reap(
        _FakeProcess(FAKE_PGID), {}
    )
    assert _containment["killpg"] == []
    assert FAKE_PGID not in sl._active_process_groups

    # Verified ownership (simulated /proc view): SIGTERM then SIGKILL, both
    # recorded by the mock and never delivered.
    sl._active_process_groups[FAKE_PGID_2] = 777
    real_stat = sl._read_proc_stat
    sl._read_proc_stat = lambda pid: (
        (os.getpid(), FAKE_PGID_2, 777) if pid == FAKE_PGID_2 else real_stat(pid)
    )
    try:
        await OpenClawSessionService._terminate_process_with_bounded_reap(
            _FakeProcess(FAKE_PGID_2), {}
        )
    finally:
        sl._read_proc_stat = real_stat
    assert _containment["killpg"] == [
        (FAKE_PGID_2, signal.SIGTERM),
        (FAKE_PGID_2, signal.SIGKILL),
    ]


# --------------------------------------------------------------------------
# S2-07 idempotent cleanup
# --------------------------------------------------------------------------


def test_s2_07_repeated_cleanup_is_idempotent_on_every_entry_point(
    tmp_path, monkeypatch, isolated_openclaw_binding_root, _containment
):
    config = _state(tmp_path)
    binding = _bind(tmp_path, config, "idem", 1)
    unregister = binding._unregister_forced_cleanup
    for _ in range(3):
        binding.release()
    unregister()
    unregister()
    assert binding._lock_fd is None and not binding._tmp_dir.exists()

    service = _service(config, monkeypatch)
    service.bind_runtime_workspace(_ctx(tmp_path, "idem-svc", 2))
    service.release_runtime_workspace_binding()
    service.release_runtime_workspace_binding()
    assert service._workspace_binding is None

    # A forced-termination drain after normal release finds nothing to do.
    sl._handle_forced_termination(signal.SIGTERM, None)
    assert _containment["killpg"] == []
    assert _containment["kill"] == [(os.getpid(), signal.SIGTERM)]

    first = recon.reconcile_binding_artifacts(apply=True)
    second = recon.reconcile_binding_artifacts(apply=True)
    assert first["artifacts"] == second["artifacts"] == []
    assert _root_entries(isolated_openclaw_binding_root) == []


# --------------------------------------------------------------------------
# S2-08 stale cleanup vs newer owner
# --------------------------------------------------------------------------


def test_s2_08_stale_release_and_callback_cannot_touch_newer_binding(
    tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    config = _state(tmp_path)
    service = _service(config, monkeypatch)
    service.bind_runtime_workspace(_ctx(tmp_path, "old", 1))
    stale = service._workspace_binding
    stale_release, stale_unregister = stale.release, stale._unregister_forced_cleanup
    service.release_runtime_workspace_binding()

    service.bind_runtime_workspace(_ctx(tmp_path, "new", 2))
    newer = service._workspace_binding
    stale_release()
    stale_unregister()
    assert newer._tmp_dir.exists() and newer._lock_fd is not None
    assert sl._active_cleanup_callbacks == [newer.release]

    # A reconciler running with --apply never removes the live newer binding.
    payload = recon.reconcile_binding_artifacts(apply=True)
    assert [a["classification"] for a in payload["artifacts"]] == [recon.ACTIVE]
    assert newer._tmp_dir.exists()
    service.release_runtime_workspace_binding()


@pytest.mark.asyncio
async def test_s2_08_nested_planning_invocation_cannot_release_outer_binding(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    """A planning-scoped invocation must not release a binding it did not create.

    Before S2, the nested bind failed closed but the invocation's ``finally``
    released the *outer* (dispatch-owned) binding, leaving the service with a
    Runtime Workspace cwd but no binding, config override or context.
    """

    service = _planning_service(db_session, tmp_path, monkeypatch)
    outer_context = _ctx(tmp_path, "outer", 77)
    service.bind_runtime_workspace(outer_context)
    outer = service._workspace_binding
    provider_calls = []

    async def run(*a, **k):
        provider_calls.append(True)
        return object(), {}

    _stub_planning_run(service, monkeypatch, run)
    with pytest.raises(OpenClawAgentSelectionError):
        await service.invoke_prompt("plan", session_prefix="planning")
    assert provider_calls == []
    assert service._workspace_binding is outer
    assert outer._tmp_dir.exists() and outer._lock_fd is not None
    assert service._openclaw_config_path_override == outer.config_path
    assert service._runtime_executor_context is outer_context
    assert service.execution_cwd_override == str(outer_context.runtime_workspace)
    assert sl._active_cleanup_callbacks == [outer.release]
    service.release_runtime_workspace_binding()
    assert _root_entries(isolated_openclaw_binding_root) == []


# --------------------------------------------------------------------------
# S2-09 malformed / missing process identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [None, "4242", 4242.0, True, False, -1, 0, 1, [FAKE_PGID]]
)
def test_s2_09_malformed_pgid_is_never_registered_or_signaled(bad, _containment):
    sl.register_process_group(bad)
    assert sl._active_process_groups == {}
    sl.kill_process_group(bad)
    assert _containment["killpg"] == []


def test_s2_09_missing_or_unreadable_proc_identity_is_unverified(
    monkeypatch, _containment
):
    for proc_view in (None, (os.getpid(), FAKE_PGID + 1, 5), (1, FAKE_PGID, 5)):
        monkeypatch.setattr(sl, "_read_proc_stat", lambda pid, v=proc_view: v)
        sl.register_process_group(FAKE_PGID)
        assert sl._active_process_groups[FAKE_PGID] is None
        sl.kill_process_group(FAKE_PGID)
    assert _containment["killpg"] == []


@pytest.mark.parametrize("owner_pid", ["abc", None, -1, 2**22 + 9, 1.5])
def test_s2_09_malformed_owner_identity_never_blocks_lock_authority(
    tmp_path, isolated_openclaw_binding_root, owner_pid
):
    config = _state(tmp_path)
    binding = _bind(tmp_path, config, "meta", 1)
    path = binding._tmp_dir / ewb.BINDING_METADATA_NAME
    metadata = json.loads(path.read_text())
    metadata.update(owner_pid=owner_pid, owner_pid_start_ticks="garbage")
    path.write_text(json.dumps(metadata))
    # Lock held: never removed regardless of malformed identity.
    payload = recon.reconcile_binding_artifacts(apply=True)
    assert payload["artifacts"][0]["classification"] == recon.ACTIVE
    assert binding._tmp_dir.exists()
    binding.release()
    assert ewb.read_process_start_ticks("abc") is None


# --------------------------------------------------------------------------
# S2-10 credential values never leak
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s2_10_credential_value_absent_from_logs_errors_and_evidence(
    db_session,
    tmp_path,
    monkeypatch,
    isolated_openclaw_binding_root,
    caplog,
    capsys,
):
    caplog.set_level(logging.DEBUG)
    service = _planning_service(db_session, tmp_path, monkeypatch)
    _set_secret(tmp_path, SECRET_A)
    surfaces = []

    # 1. Provider failure through invoke_prompt.
    async def fail(*a, **k):
        raise RuntimeError("provider failed")

    _stub_planning_run(service, monkeypatch, fail)
    with pytest.raises(RuntimeError) as failure:
        await service.invoke_prompt("plan", session_prefix="planning")
    surfaces += [str(failure.value), repr(failure.value)]

    # 2. Binding failure diagnostics (collision -> OpenClawAgentSelectionError).
    config = _auth_source(tmp_path).parents[3] / "openclaw.json"
    unbound = _service(config, monkeypatch)
    unbound.bind_runtime_workspace(_ctx(tmp_path, "diag", 3))
    with pytest.raises(OpenClawAgentSelectionError) as collision:
        unbound.bind_runtime_workspace(_ctx(tmp_path, "diag", 3))
    surfaces += [
        str(collision.value),
        json.dumps(getattr(collision.value, "runtime_diagnostics", {}), default=str),
    ]

    # 3. Release failure warning + metadata + reconciliation evidence.
    binding = unbound._workspace_binding
    surfaces.append((binding._tmp_dir / ewb.BINDING_METADATA_NAME).read_text())
    active_report = recon.reconcile_binding_artifacts()
    assert active_report["summary"]["credential_bearing"] == 0
    surfaces.append(json.dumps(active_report, default=str))
    surfaces.append(
        json.dumps(
            recon.inventory_legacy_tmp_bindings(isolated_openclaw_binding_root),
            default=str,
        )
    )
    real_rmtree = ewb.shutil.rmtree

    def failing_rmtree(path, onerror=None, **kwargs):
        onerror(os.unlink, str(path), (PermissionError, PermissionError(), None))

    monkeypatch.setattr(ewb.shutil, "rmtree", failing_rmtree)
    binding.release()
    monkeypatch.setattr(ewb.shutil, "rmtree", real_rmtree)
    # The owner is "gone" (as after a crash) so the residue is removable.
    metadata_path = binding._tmp_dir / ewb.BINDING_METADATA_NAME
    metadata = json.loads(metadata_path.read_text())
    metadata["owner_pid"] = 2**22 + 9
    metadata_path.write_text(json.dumps(metadata))
    payload = recon.reconcile_binding_artifacts(apply=True)
    surfaces.append(json.dumps(payload, default=str))

    # 4. Maintenance script output (in-process; no subprocess).
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[2] / "scripts" / "maintenance")
    )
    try:
        import openclaw_binding_reconcile as script

        monkeypatch.setattr(sys, "argv", ["reconcile"])
        assert script.main() == 0
    finally:
        sys.path.pop(0)
    surfaces.append(capsys.readouterr().out)
    surfaces.append(caplog.text)

    assert payload["summary"]["by_action"] == {recon.REMOVED: 1}
    for text in surfaces:
        assert SECRET_A not in text
    assert "binding_release_failed" in caplog.text
    assert _root_entries(isolated_openclaw_binding_root) == []


# --------------------------------------------------------------------------
# S2-11 shutdown cleanup under simulated reentrancy
# --------------------------------------------------------------------------


def test_s2_11_forced_shutdown_cleanup_does_not_deadlock_on_reentry(
    tmp_path, monkeypatch, isolated_openclaw_binding_root, _containment
):
    # Bounded probe first: a non-reentrant lock would otherwise strand a
    # daemon thread holding it and hang fixture teardown.
    with sl._lock:
        assert sl._lock.acquire(timeout=1), "registry lock is not reentrant"
        sl._lock.release()

    config = _state(tmp_path)
    service = _service(config, monkeypatch)
    service.bind_runtime_workspace(_ctx(tmp_path, "shutdown", 1))
    binding = service._workspace_binding
    sl._active_process_groups[FAKE_PGID] = None  # unverified: must be refused
    trace = []

    def worker_closure():
        # Mirrors worker._forced_termination_cleanup: releases via the
        # service, which re-enters the registry through unregister().
        trace.append("worker_closure")
        sl._handle_forced_termination(signal.SIGTERM, None)  # repeated SIGTERM
        service.release_runtime_workspace_binding()
        sl.register_forced_termination_cleanup(lambda: trace.append("late"))

    sl.register_forced_termination_cleanup(worker_closure)

    contender_holding = threading.Event()
    contender_release = threading.Event()

    def contender():
        with sl._lock:
            contender_holding.set()
            contender_release.wait(timeout=5)

    def interrupted_main():
        # Handler runs while this thread already holds the RLock, as when a
        # signal lands inside a registry critical section.
        with sl._lock:
            sl._handle_forced_termination(signal.SIGTERM, None)
        trace.append("handler_done")

    other = threading.Thread(target=contender, daemon=True)
    other.start()
    assert contender_holding.wait(timeout=5)
    runner = threading.Thread(target=interrupted_main, daemon=True)
    runner.start()
    threading.Timer(0.2, contender_release.set).start()
    runner.join(timeout=15)
    other.join(timeout=15)
    assert not runner.is_alive(), "forced-termination cleanup deadlocked"
    assert trace == ["worker_closure", "late", "handler_done"]
    assert not binding._tmp_dir.exists() and binding._lock_fd is None
    assert service._workspace_binding is None
    assert sl._active_cleanup_callbacks == [] and sl._active_process_groups == {}
    assert _containment["killpg"] == []
    assert _containment["signal"] == [(signal.SIGTERM, signal.SIG_DFL)]
    assert _containment["kill"] == [(os.getpid(), signal.SIGTERM)]
    assert _root_entries(isolated_openclaw_binding_root) == []


# --------------------------------------------------------------------------
# S2-12 PGID safety
# --------------------------------------------------------------------------


def test_s2_12_reserved_own_and_unregistered_groups_are_refused(caplog, _containment):
    caplog.set_level(logging.WARNING)
    own_group, own_pid = os.getpgrp(), os.getpid()

    sl.register_process_group(1)
    assert 1 not in sl._active_process_groups
    for pgid in (1, own_group, own_pid, FAKE_PGID):
        sl.kill_process_group(pgid)  # none of these are registered

    # Even a corrupted registry that claims verified ownership is refused.
    for pgid in (1, own_group, own_pid):
        assert sl._termination_refusal(pgid, 12345) is not None
    with sl._lock:
        sl._active_process_groups.update({1: 1, own_group: 1, own_pid: 1})
    sl._handle_forced_termination(signal.SIGTERM, None)

    assert _containment["killpg"] == []
    reasons = [
        json.loads(r.getMessage().split(" ", 2)[2])["reason"]
        for r in caplog.records
        if "process_group_signal_refused" in r.getMessage()
    ]
    assert {"invalid_pgid", "own_process_group", "unregistered_pgid"} <= set(reasons)
    assert sl._termination_refusal(FAKE_PGID, None) == "unverified_ownership"
