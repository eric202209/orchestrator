"""Phase 37 Pre-B-F2B: ephemeral OpenClaw binding lifecycle hardening.

Provider-free. Every OpenClaw config, credential file and binding root is a
synthetic copy under ``tmp_path``; the operator's real OpenClaw state and the
real temp directory are never touched. Signals are only ever sent to isolated
subprocesses started by these tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Project
from app.services.agents import subprocess_lifecycle
from app.services.agents.openclaw_service import (
    OpenClawAgentSelectionError,
    OpenClawSessionError,
    OpenClawSessionService,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.execution import binding_reconciliation as recon
from app.services.orchestration.execution import executor_workspace_binding as ewb
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_SECRET = "synthetic-f2b-token-not-a-real-credential"
SYNTHETIC_LOCAL_GATEWAY_TOKEN = "synthetic-local-gateway-token-not-real"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _persistent_state(tmp_path: Path) -> tuple[Path, Path]:
    """Synthetic persistent OpenClaw state with a synthetic credential."""

    state = tmp_path / "home" / ".openclaw"
    main_agent_dir = state / "agents" / "main" / "agent"
    main_agent_dir.mkdir(parents=True)
    (state / "agents" / "phase36-dogfood-reentry-x" / "agent").mkdir(parents=True)
    auth = main_agent_dir / "auth-profiles.json"
    auth.write_text(json.dumps({"token": SYNTHETIC_SECRET}), encoding="utf-8")
    os.chmod(auth, 0o600)
    config = state / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": SYNTHETIC_LOCAL_GATEWAY_TOKEN,
                            "models": [{"id": "qwen-local"}],
                        },
                        "openai-codex": {
                            "baseUrl": "https://synthetic-external.invalid/v1",
                            "api": "openai-completions",
                            "apiKey": "synthetic-external-key-not-real",
                            "models": [{"id": "external-model"}],
                        },
                    }
                },
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "workspace": str(state / "workspace"),
                            "agentDir": str(main_agent_dir),
                        },
                        {
                            "id": "phase36-dogfood-reentry-x",
                            "workspace": str(state / "reentry"),
                        },
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return state, config


def _snapshot(state: Path) -> dict:
    return {
        str(path.relative_to(state)): (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        )
        for path in sorted(state.rglob("*"))
    }


def _context(tmp_path: Path, name: str = "probe") -> RuntimeExecutorContext:
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
        task_execution_id=4242,
        runtime_root=runtime_root,
        sandbox=object(),
    )


def _bind(tmp_path: Path, config: Path, name: str = "probe"):
    return ewb.bind_openclaw_workspace(
        _context(tmp_path, name),
        real_config_path=config,
        model_ref="openai/qwen-local",
    )


def _service(config: Path, monkeypatch) -> OpenClawSessionService:
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config))
    service = object.__new__(OpenClawSessionService)
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._workspace_binding = None
    service._strict_planning_binding = None
    service._openclaw_config_path_override = None
    service._runtime_executor_context = None
    service._runtime_runner_agent_id = None
    service._runtime_workspace_previous_cwd_override = None
    service.execution_cwd_override = None
    service._last_selected_openclaw_agent_id = None
    return service


def _root_entries(root: Path) -> list[str]:
    return sorted(os.listdir(root)) if root.exists() else []


def _kill_binding_owner(binding, *, owner_alive: bool = False) -> None:
    """Simulate SIGKILL in-process: drop the lock without running release.

    Unless ``owner_alive``, the metadata is also pointed at a PID that no
    longer exists, as it would be after the owning worker really died.
    """

    binding._unregister_forced_cleanup()
    binding._unregister_forced_cleanup = None
    os.close(binding._lock_fd)
    binding._lock_fd = None
    if not owner_alive:
        _rewrite_metadata(binding, owner_pid=2**22 + 17)


def _rewrite_metadata(binding, **updates) -> None:
    path = binding._tmp_dir / ewb.BINDING_METADATA_NAME
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update(updates)
    path.write_text(json.dumps(metadata), encoding="utf-8")


def _classes(payload: dict) -> dict:
    return {
        Path(item["path"]).name: (item["classification"], item["action"])
        for item in payload["artifacts"]
    }


@pytest.fixture(autouse=True)
def _reset_lifecycle_registry():
    subprocess_lifecycle._reset_for_tests()
    yield
    subprocess_lifecycle._reset_for_tests()


# Child script for signal tests: binds in a separate process, optionally
# spawns a grandchild that inherits the lock fd exactly as OpenClaw does.
CHILD_SCRIPT = textwrap.dedent(
    """
    import json, subprocess, sys, time
    from pathlib import Path
    from app.services.agents import subprocess_lifecycle
    from app.services.orchestration.execution import executor_workspace_binding as ewb
    from app.services.orchestration.execution.runtime_context import (
        RuntimeExecutorContext,
    )

    tmp, config, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    subprocess_lifecycle.install_sigterm_handler()
    runtime_root = tmp / "runtime-root"
    runtime = runtime_root / "tasks" / ("child-" + mode)
    runtime.mkdir(parents=True, exist_ok=True)
    (tmp / "product").mkdir(exist_ok=True)
    binding = ewb.bind_openclaw_workspace(
        RuntimeExecutorContext(
            executor="openclaw", runtime_workspace=runtime,
            project_workspace=tmp / "product", project_id=None,
            task_execution_id=None, runtime_root=runtime_root, sandbox=object(),
        ),
        real_config_path=config, model_ref="openai/qwen-local",
    )
    grandchild = None
    if mode == "orphan":
        grandchild = subprocess.Popen(
            ["sleep", "60"], pass_fds=binding.subprocess_pass_fds(),
            start_new_session=True,
        )
    print(json.dumps({"dir": str(binding._tmp_dir),
                      "grandchild": grandchild.pid if grandchild else None}),
          flush=True)
    time.sleep(60)
    """
)


def _start_child(tmp_path: Path, config: Path, root: Path, mode: str):
    env = {
        **os.environ,
        "ORCHESTRATOR_OPENCLAW_BINDING_ROOT": str(root),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD_SCRIPT, str(tmp_path), str(config), mode],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    if not line:
        proc.kill()
        raise AssertionError(f"child failed: {proc.stderr.read()[-2000:]}")
    return proc, json.loads(line)


def _wait_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not exit")


# --------------------------------------------------------------------------
# A/L/M — normal completion, credential protection, persistent integrity
# --------------------------------------------------------------------------


def test_a_l_m_normal_binding_is_private_owned_and_fully_released(
    tmp_path, isolated_openclaw_binding_root, caplog
):
    caplog.set_level(logging.DEBUG)
    state, config = _persistent_state(tmp_path)
    before = _snapshot(state)

    binding = _bind(tmp_path, config)
    root = isolated_openclaw_binding_root
    assert binding._tmp_dir.parent == root
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(binding._tmp_dir).st_mode) == 0o700
    copied = binding._tmp_dir / "agent" / "auth-profiles.json"
    assert not copied.exists()
    assert stat.S_IMODE(os.stat(binding._tmp_dir / "agent").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(binding._tmp_dir / "state").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(binding.config_path).st_mode) == 0o600

    metadata_text = (binding._tmp_dir / ewb.BINDING_METADATA_NAME).read_text()
    metadata = json.loads(metadata_text)
    assert metadata["schema"] == ewb.BINDING_METADATA_SCHEMA
    assert metadata["binding_id"] == binding.binding_id
    assert metadata["artifact_dir_name"] == binding._tmp_dir.name
    assert metadata["task_execution_id"] == 4242
    assert SYNTHETIC_SECRET not in metadata_text
    assert _snapshot(state) == before

    binding.release()
    binding.release()  # idempotent
    assert _root_entries(root) == []
    assert _snapshot(state) == before
    assert SYNTHETIC_SECRET not in caplog.text
    assert subprocess_lifecycle._active_cleanup_callbacks == []


def test_m_service_bind_release_and_reconcile_leave_persistent_state_identical(
    tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    state, config = _persistent_state(tmp_path)
    before = _snapshot(state)
    service = _service(config, monkeypatch)
    service.bind_runtime_workspace(_context(tmp_path))
    assert service._workspace_binding is not None
    assert service._openclaw_config_path_override != config
    recon.reconcile_binding_artifacts(apply=True)
    assert _snapshot(state) == before
    service.release_runtime_workspace_binding()
    recon.reconcile_binding_artifacts(apply=True)
    assert _root_entries(isolated_openclaw_binding_root) == []
    assert _snapshot(state) == before


# --------------------------------------------------------------------------
# B/D — exception, timeout and cancellation through invoke_prompt
# --------------------------------------------------------------------------


def _planning_service(db_session, tmp_path, monkeypatch):
    _state, config = _persistent_state(tmp_path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    project = Project(name="F2B Lifecycle", workspace_path=str(canonical))
    db_session.add(project)
    db_session.commit()
    service = _service(config, monkeypatch)
    service.db = db_session
    service.session_id = None
    service.task_id = None
    service.task_execution_id = None
    service.session_model = None
    service.task_model = None
    service.project_id = project.id
    monkeypatch.setattr(
        service,
        "build_cli_agent_command",
        lambda *a, **k: ["openclaw", "agent", "--session-id", "planning-f2b"],
    )
    return service


@pytest.mark.parametrize(
    "raised, expected",
    [
        (RuntimeError("provider exploded"), RuntimeError),
        (asyncio.TimeoutError(), OpenClawSessionError),
        (subprocess.TimeoutExpired("openclaw", 1), OpenClawSessionError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
@pytest.mark.asyncio
async def test_b_d_invoke_prompt_releases_binding_and_preserves_error(
    db_session, tmp_path, monkeypatch, isolated_openclaw_binding_root, raised, expected
):
    service = _planning_service(db_session, tmp_path, monkeypatch)
    observed = {}

    async def failing_run(full_cmd, *, cwd, **kwargs):
        observed["binding_dir"] = service._workspace_binding._tmp_dir
        assert observed["binding_dir"].exists()
        raise raised

    monkeypatch.setattr(service, "_run_cli_prompt_with_diagnostics", failing_run)
    with pytest.raises(expected) as exc_info:
        await service.invoke_prompt("plan", session_prefix="planning")
    if expected is OpenClawSessionError:
        # Timeout semantics are unchanged by the lifecycle hardening.
        assert exc_info.value.provider_failure_classification == "provider_timeout"
    if expected is RuntimeError:
        assert "provider exploded" in str(exc_info.value)
    assert not observed["binding_dir"].exists()
    assert service._workspace_binding is None
    assert _root_entries(isolated_openclaw_binding_root) == []


@pytest.mark.parametrize(
    "raised", [TimeoutError("lock wait"), asyncio.CancelledError()]
)
@pytest.mark.asyncio
async def test_d_planner_lock_wait_failure_releases_bound_repair_runtime(
    monkeypatch, raised
):
    from contextlib import asynccontextmanager

    from app.services.orchestration.planning import planner as planner_module

    released = []
    bound = []

    class RepairRuntime:
        def bind_runtime_workspace(self, context):
            bound.append(context)

        def release_runtime_workspace_binding(self):
            released.append(True)

    monkeypatch.setattr(
        "app.services.agents.agent_runtime.create_agent_runtime",
        lambda *a, **k: RepairRuntime(),
    )

    @asynccontextmanager
    async def failing_lock():
        raise raised
        yield {}  # pragma: no cover

    monkeypatch.setattr(
        planner_module.PlannerService,
        "_openclaw_planning_lock_async",
        staticmethod(failing_lock),
    )
    parent = SimpleNamespace(
        db=object(),
        session_id=1,
        task_id=2,
        runtime_executor_context=object(),
    )
    with pytest.raises(type(raised)):
        await planner_module.PlannerService._invoke_repair_prompt(parent, "fix", 30)
    assert bound and released == [True]


def test_service_rebind_still_fails_closed_without_new_artifacts(
    tmp_path, monkeypatch, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    service = _service(config, monkeypatch)
    context = _context(tmp_path)
    service.bind_runtime_workspace(context)
    before = _root_entries(isolated_openclaw_binding_root)
    with pytest.raises(OpenClawAgentSelectionError):
        service.bind_runtime_workspace(context)
    assert _root_entries(isolated_openclaw_binding_root) == before
    service.release_runtime_workspace_binding()
    assert _root_entries(isolated_openclaw_binding_root) == []


def test_spawn_kwargs_pass_lock_fd_only_while_bound(tmp_path, monkeypatch):
    _state, config = _persistent_state(tmp_path)
    service = _service(config, monkeypatch)
    assert service._workspace_binding_spawn_kwargs() == {}
    service.bind_runtime_workspace(_context(tmp_path))
    fd = service._workspace_binding._lock_fd
    assert service._workspace_binding_spawn_kwargs() == {"pass_fds": (fd,)}
    service.release_runtime_workspace_binding()
    assert service._workspace_binding_spawn_kwargs() == {}


# --------------------------------------------------------------------------
# C — partial initialization
# --------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["agent_dir", "config_write", "metadata"])
def test_c_partial_initialization_leaves_no_artifact(
    tmp_path, monkeypatch, isolated_openclaw_binding_root, failing
):
    _state, config = _persistent_state(tmp_path)
    if failing == "agent_dir":
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path.name == "agent":
                raise OSError("disk full")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    elif failing == "config_write":
        original = Path.write_text

        def write_text(path, *args, **kwargs):
            if path.name == "openclaw.json":
                raise OSError("disk full")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
    else:
        monkeypatch.setattr(
            ewb,
            "_binding_metadata",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    with pytest.raises((OSError, KeyboardInterrupt)):
        _bind(tmp_path, config)
    assert _root_entries(isolated_openclaw_binding_root) == []
    assert subprocess_lifecycle._active_cleanup_callbacks == []


def test_unsafe_binding_root_fails_closed(tmp_path, monkeypatch):
    _state, config = _persistent_state(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(outside)
    monkeypatch.setenv(ewb.BINDING_ROOT_ENV, str(link))
    with pytest.raises(ewb.ExecutorWorkspaceBindingError):
        _bind(tmp_path, config)
    open_root = tmp_path / "open-root"
    open_root.mkdir(mode=0o777)
    os.chmod(open_root, 0o777)
    monkeypatch.setenv(ewb.BINDING_ROOT_ENV, str(open_root))
    with pytest.raises(ewb.ExecutorWorkspaceBindingError):
        _bind(tmp_path, config)
    assert list(outside.iterdir()) == [] and list(open_root.iterdir()) == []


# --------------------------------------------------------------------------
# E/F/G — SIGTERM, SIGKILL, active protection (isolated subprocesses)
# --------------------------------------------------------------------------


def test_e_sigterm_releases_binding_via_forced_termination_cleanup(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    proc, info = _start_child(tmp_path, config, isolated_openclaw_binding_root, "plain")
    assert Path(info["dir"]).exists()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == -signal.SIGTERM
    assert not Path(info["dir"]).exists()


def test_f_sigkill_residue_is_discoverable_and_classified_stale_confirmed(
    tmp_path, isolated_openclaw_binding_root
):
    state, config = _persistent_state(tmp_path)
    before = _snapshot(state)
    proc, info = _start_child(tmp_path, config, isolated_openclaw_binding_root, "plain")
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=15)
    residue = Path(info["dir"])
    assert residue.exists(), "SIGKILL handlers never run; residue must remain"
    assert not (residue / "agent" / "auth-profiles.json").exists()

    report = recon.reconcile_binding_artifacts(isolated_openclaw_binding_root)
    assert _classes(report) == {residue.name: (recon.STALE_CONFIRMED, recon.REPORTED)}
    assert report["artifacts"][0]["credential_bearing"] is False
    assert residue.exists(), "report-only mode must not delete"

    applied = recon.reconcile_binding_artifacts(
        isolated_openclaw_binding_root, apply=True
    )
    assert _classes(applied) == {residue.name: (recon.STALE_CONFIRMED, recon.REMOVED)}
    assert not residue.exists()
    assert _snapshot(state) == before


def test_g_active_binding_in_other_process_is_never_removed(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    proc, info = _start_child(tmp_path, config, isolated_openclaw_binding_root, "plain")
    try:
        applied = recon.reconcile_binding_artifacts(
            isolated_openclaw_binding_root, apply=True
        )
        assert _classes(applied) == {
            Path(info["dir"]).name: (recon.ACTIVE, recon.REPORTED)
        }
        assert Path(info["dir"]).exists()
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_g_orphaned_openclaw_child_keeps_binding_active_after_worker_sigkill(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    proc, info = _start_child(
        tmp_path, config, isolated_openclaw_binding_root, "orphan"
    )
    grandchild = info["grandchild"]
    residue = Path(info["dir"])
    try:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=15)
        applied = recon.reconcile_binding_artifacts(
            isolated_openclaw_binding_root, apply=True
        )
        assert _classes(applied) == {residue.name: (recon.ACTIVE, recon.REPORTED)}
        assert residue.exists()
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_gone(grandchild)
    applied = recon.reconcile_binding_artifacts(
        isolated_openclaw_binding_root, apply=True
    )
    assert _classes(applied) == {residue.name: (recon.STALE_CONFIRMED, recon.REMOVED)}


def test_g_in_process_active_binding_survives_apply(tmp_path):
    _state, config = _persistent_state(tmp_path)
    binding = _bind(tmp_path, config)
    try:
        applied = recon.reconcile_binding_artifacts(apply=True)
        assert _classes(applied) == {
            binding._tmp_dir.name: (recon.ACTIVE, recon.REPORTED)
        }
        assert binding.config_path.exists()
    finally:
        binding.release()


# --------------------------------------------------------------------------
# H — PID reuse / namespace ambiguity
# --------------------------------------------------------------------------


def test_h_pid_information_never_authorizes_or_blocks_on_its_own(tmp_path):
    _state, config = _persistent_state(tmp_path)

    # Lock held, metadata claims a PID that does not exist: still ACTIVE.
    live = _bind(tmp_path, config, "live")
    _rewrite_metadata(live, owner_pid=2**22 + 17, owner_pid_start_ticks=1)

    # Lock free, metadata matches this live process exactly (release failed
    # in a still-running owner): conservative STALE_SUSPECTED, not removed.
    same_process = _bind(tmp_path, config, "same")
    _kill_binding_owner(same_process, owner_alive=True)

    # Lock free, same PID but different start time (PID reuse): owner gone.
    reused = _bind(tmp_path, config, "reused")
    _kill_binding_owner(reused, owner_alive=True)
    _rewrite_metadata(reused, owner_pid_start_ticks=1)

    # Lock free, same PID recorded from another PID namespace: not this pid.
    foreign_ns = _bind(tmp_path, config, "foreign")
    _kill_binding_owner(foreign_ns, owner_alive=True)
    _rewrite_metadata(foreign_ns, owner_pid_namespace="pid:[1]")

    try:
        applied = recon.reconcile_binding_artifacts(apply=True)
        classes = _classes(applied)
        assert classes[live._tmp_dir.name] == (recon.ACTIVE, recon.REPORTED)
        assert classes[same_process._tmp_dir.name] == (
            recon.STALE_SUSPECTED,
            recon.REPORTED,
        )
        assert classes[reused._tmp_dir.name] == (recon.STALE_CONFIRMED, recon.REMOVED)
        assert classes[foreign_ns._tmp_dir.name] == (
            recon.STALE_CONFIRMED,
            recon.REMOVED,
        )
        assert live._tmp_dir.exists() and same_process._tmp_dir.exists()
    finally:
        live.release()
        same_process.release()


# --------------------------------------------------------------------------
# I/J/K — symlinks, invalid metadata, legacy directories
# --------------------------------------------------------------------------


def test_i_symlinks_and_substitution_cannot_escape_the_root(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep", encoding="utf-8")

    stale = _bind(tmp_path, config, "stale")
    _kill_binding_owner(stale)
    (stale._tmp_dir / "escape").symlink_to(outside)
    (stale._tmp_dir / "state" / "escape-file").symlink_to(outside / "precious.txt")

    root = isolated_openclaw_binding_root
    (root / (ewb.BINDING_DIR_PREFIX + "link")).symlink_to(outside)

    meta_link = root / (ewb.BINDING_DIR_PREFIX + "metalink")
    meta_link.mkdir()
    (meta_link / ewb.BINDING_METADATA_NAME).symlink_to(
        stale._tmp_dir / ewb.BINDING_METADATA_NAME
    )

    applied = recon.reconcile_binding_artifacts(apply=True)
    classes = _classes(applied)
    assert classes[ewb.BINDING_DIR_PREFIX + "link"][0] == recon.UNSAFE_PATH
    assert classes[ewb.BINDING_DIR_PREFIX + "metalink"][0] == recon.INVALID_METADATA
    assert classes[stale._tmp_dir.name] == (recon.STALE_CONFIRMED, recon.REMOVED)
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep"
    assert (root / (ewb.BINDING_DIR_PREFIX + "link")).is_symlink()
    assert meta_link.exists()

    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(root)
    refused = recon.reconcile_binding_artifacts(symlink_root, apply=True)
    assert refused["root_status"].startswith("refused")
    assert refused["artifacts"] == []


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ("{not json", "malformed_json"),
        (json.dumps({"schema": ewb.BINDING_METADATA_SCHEMA}), "missing_keys"),
        ("WRONG_SCHEMA", "unknown_schema"),
        ("COPIED_FROM_OTHER_DIR", "artifact_dir_name_mismatch"),
        ("[]", "not_an_object"),
    ],
)
def test_j_invalid_metadata_fails_closed(tmp_path, mutation, reason):
    _state, config = _persistent_state(tmp_path)
    binding = _bind(tmp_path, config)
    _kill_binding_owner(binding)
    path = binding._tmp_dir / ewb.BINDING_METADATA_NAME
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "WRONG_SCHEMA":
        metadata["schema"] = "someone-else.v9"
        mutation = json.dumps(metadata)
    elif mutation == "COPIED_FROM_OTHER_DIR":
        metadata["artifact_dir_name"] = ewb.BINDING_DIR_PREFIX + "elsewhere"
        mutation = json.dumps(metadata)
    path.write_text(mutation, encoding="utf-8")

    applied = recon.reconcile_binding_artifacts(apply=True)
    item = applied["artifacts"][0]
    assert item["classification"] == recon.INVALID_METADATA
    assert item["reason"].startswith(reason)
    assert item["action"] == recon.REPORTED
    assert binding._tmp_dir.exists()


def test_k_legacy_and_unowned_directories_are_never_removed(
    tmp_path, isolated_openclaw_binding_root
):
    root = isolated_openclaw_binding_root
    root.mkdir(mode=0o700)
    legacy_in_root = root / (ewb.BINDING_DIR_PREFIX + "legacy")
    (legacy_in_root / "agent").mkdir(parents=True)
    (legacy_in_root / "agent" / "auth-profiles.json").write_text(
        SYNTHETIC_SECRET, encoding="utf-8"
    )
    stray = root / "not-a-binding"
    stray.mkdir()

    applied = recon.reconcile_binding_artifacts(apply=True)
    classes = _classes(applied)
    assert classes[legacy_in_root.name][0] == recon.UNKNOWN_OWNERSHIP
    assert classes["not-a-binding"][0] == recon.UNKNOWN_OWNERSHIP
    assert all(action == recon.REPORTED for _c, action in classes.values())
    assert legacy_in_root.exists() and stray.exists()

    fake_tmp = tmp_path / "fake-tmp"
    legacy = fake_tmp / (ewb.BINDING_DIR_PREFIX + "abc123")
    (legacy / "agent").mkdir(parents=True)
    (legacy / "agent" / "auth-profiles.json").write_text(
        SYNTHETIC_SECRET, encoding="utf-8"
    )
    (fake_tmp / "unrelated-dir").mkdir()
    inventory = recon.inventory_legacy_tmp_bindings(fake_tmp)
    assert [Path(a["path"]).name for a in inventory["artifacts"]] == [legacy.name]
    assert inventory["artifacts"][0]["classification"] == recon.LEGACY_UNMANAGED
    assert inventory["artifacts"][0]["credential_bearing"] is True
    assert SYNTHETIC_SECRET not in json.dumps(inventory)
    assert legacy.exists()


# --------------------------------------------------------------------------
# N/O/P — idempotency, interrupted cleanup, concurrent reconcilers
# --------------------------------------------------------------------------


def test_n_reconciliation_is_idempotent(tmp_path, isolated_openclaw_binding_root):
    _state, config = _persistent_state(tmp_path)
    active = _bind(tmp_path, config, "active")
    stale = _bind(tmp_path, config, "stale")
    _kill_binding_owner(stale)
    try:
        first = recon.reconcile_binding_artifacts()
        second = recon.reconcile_binding_artifacts()
        assert _classes(first) == _classes(second)
        applied = recon.reconcile_binding_artifacts(apply=True)
        assert _classes(applied)[stale._tmp_dir.name][1] == recon.REMOVED
        again = recon.reconcile_binding_artifacts(apply=True)
        assert _classes(again) == {active._tmp_dir.name: (recon.ACTIVE, recon.REPORTED)}
    finally:
        active.release()
    assert recon.reconcile_binding_artifacts(apply=True)["artifacts"] == []


def test_o_interrupted_cleanup_stays_reconcilable(tmp_path, monkeypatch):
    _state, config = _persistent_state(tmp_path)
    stale = _bind(tmp_path, config)
    _kill_binding_owner(stale)
    original_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if str(path).endswith(ewb.BINDING_METADATA_NAME):
            raise PermissionError("interrupted")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(recon.os, "unlink", failing_unlink)
    interrupted = recon.reconcile_binding_artifacts(apply=True)
    assert interrupted["artifacts"][0]["action"] == recon.REMOVAL_FAILED
    assert (stale._tmp_dir / ewb.BINDING_METADATA_NAME).exists()
    monkeypatch.setattr(recon.os, "unlink", original_unlink)

    resumed = recon.reconcile_binding_artifacts(apply=True)
    assert _classes(resumed) == {
        stale._tmp_dir.name: (recon.STALE_CONFIRMED, recon.REMOVED)
    }

    # Interrupted after metadata removal: only the lock file remains. Not
    # provable, so it is reported and left for the operator.
    second = _bind(tmp_path, config, "second")
    _kill_binding_owner(second)
    for child in ("agent", "state"):
        import shutil

        shutil.rmtree(second._tmp_dir / child)
    for name in ("openclaw.json", ewb.BINDING_METADATA_NAME):
        os.unlink(second._tmp_dir / name)
    leftover = recon.reconcile_binding_artifacts(apply=True)
    assert _classes(leftover) == {
        second._tmp_dir.name: (recon.STALE_SUSPECTED, recon.REPORTED)
    }


def test_release_failure_is_observable_and_residue_reconcilable(
    tmp_path, monkeypatch, caplog
):
    _state, config = _persistent_state(tmp_path)
    binding = _bind(tmp_path, config)

    original_rmtree = ewb.shutil.rmtree

    def failing_rmtree(path, onerror=None, **kwargs):
        onerror(os.rmdir, str(path), (PermissionError, PermissionError(), None))

    monkeypatch.setattr(ewb.shutil, "rmtree", failing_rmtree)
    caplog.set_level(logging.WARNING)
    binding.release()  # never raises
    assert "binding_release_failed" in caplog.text
    assert binding.binding_id in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text
    monkeypatch.setattr(ewb.shutil, "rmtree", original_rmtree)
    report = recon.reconcile_binding_artifacts()
    # Owner (this process) is still alive, so stay conservative.
    assert _classes(report) == {
        binding._tmp_dir.name: (recon.STALE_SUSPECTED, recon.REPORTED)
    }
    original_rmtree(binding._tmp_dir)


RECONCILER_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from app.services.orchestration.execution import binding_reconciliation as r
    payload = r.reconcile_binding_artifacts(Path(sys.argv[1]), apply=True)
    print(json.dumps([[a["path"], a["classification"], a["action"]]
                      for a in payload["artifacts"]]))
    """
)


def test_p_concurrent_reconcilers_remove_each_stale_artifact_once(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    active = _bind(tmp_path, config, "active")
    stale = []
    for index in range(6):
        binding = _bind(tmp_path, config, f"stale-{index}")
        _kill_binding_owner(binding)
        stale.append(binding._tmp_dir.name)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                RECONCILER_SCRIPT,
                str(isolated_openclaw_binding_root),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    removed = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err[-2000:]
            for path, classification, action in json.loads(out):
                assert action in (recon.REPORTED, recon.REMOVED), (path, action)
                if Path(path).name == active._tmp_dir.name:
                    assert classification == recon.ACTIVE
                if action == recon.REMOVED:
                    removed.append(Path(path).name)
        assert sorted(removed) == sorted(stale)
        assert active.config_path.exists()
        assert _root_entries(isolated_openclaw_binding_root) == [active._tmp_dir.name]
    finally:
        active.release()


# --------------------------------------------------------------------------
# boot hook and maintenance script
# --------------------------------------------------------------------------


def test_boot_residue_scan_is_report_only(tmp_path):
    from app.tasks.worker import report_openclaw_binding_residue_on_boot

    _state, config = _persistent_state(tmp_path)
    stale = _bind(tmp_path, config)
    _kill_binding_owner(stale)
    summary = report_openclaw_binding_residue_on_boot()
    assert summary["by_classification"] == {recon.STALE_CONFIRMED: 1}
    assert summary["by_action"] == {recon.REPORTED: 1}
    assert stale._tmp_dir.exists()


def test_maintenance_script_defaults_to_report_only(
    tmp_path, isolated_openclaw_binding_root
):
    _state, config = _persistent_state(tmp_path)
    stale = _bind(tmp_path, config)
    _kill_binding_owner(stale)
    script = REPO_ROOT / "scripts" / "maintenance" / "openclaw_binding_reconcile.py"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    env["TMPDIR"] = str(fake_tmp)
    report = subprocess.run(
        [sys.executable, str(script), "--include-legacy-tmp"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert report.returncode == 0, report.stderr[-2000:]
    payload = json.loads(report.stdout)
    assert payload["managed"]["summary"]["by_action"] == {recon.REPORTED: 1}
    assert payload["legacy_tmp"]["tmp_root"] == str(fake_tmp)
    assert stale._tmp_dir.exists()
    applied = subprocess.run(
        [sys.executable, str(script), "--apply"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert applied.returncode == 0, applied.stderr[-2000:]
    assert json.loads(applied.stdout)["managed"]["summary"]["by_action"] == {
        recon.REMOVED: 1
    }
    assert not stale._tmp_dir.exists()
