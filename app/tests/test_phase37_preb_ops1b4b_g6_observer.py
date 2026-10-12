"""Phase 37 Pre-B OPS1-B4B-PREP: provider-free tests for the G6 observer.

Every test uses tmp_path fixtures: a synthetic managed binding root, a
synthetic procfs tree (or this test's own child processes), decoy credential
values and decoy prompts.  No real OpenClaw config, binding root, database,
Redis, network or model endpoint is touched, and no signal is ever sent.
"""

from __future__ import annotations

import ast
import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "maintenance"
    / "phase37_ops1b4b_g6_observer.py"
)
_spec = importlib.util.spec_from_file_location("phase37_g6_observer", SCRIPT)
obs = importlib.util.module_from_spec(_spec)
sys.modules["phase37_g6_observer"] = obs
_spec.loader.exec_module(obs)

DECOYS = (
    "DECOY-PROVIDER-APIKEY-7f3a",
    "DECOY-GATEWAY-TOKEN-91bc",
    "DECOY-AUTH-PROFILE-KEY-55de",
    "DECOY-CODEX-OAUTH-REFRESH-0a1b",
    "DECOY-WORKER-ENV-OPENAI-KEY-c0ffee",
    "DECOY-GITHUB-TOKEN-beef",
)
PROMPT = "DECOY-PROMPT create hello.txt containing ok PRIVATE-TASK-TEXT"
EXEC_ID = 386
AGENT = "orchestrator-runtime"
MODEL = "openai/qwen-local"


# --------------------------------------------------------------------------
# Synthetic world
# --------------------------------------------------------------------------


def _own_ticks() -> int:
    return obs.ProcReader().stat(os.getpid()).start_ticks


class World:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "bindings"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        self.workspace = tmp_path / "runtime" / "tasks" / "170" / str(EXEC_ID)
        self.workspace.mkdir(parents=True)
        self.out = tmp_path / "evidence" / "g6"
        (tmp_path / "evidence").mkdir()
        self.lock_fds: dict = {}

    def binding(
        self,
        *,
        exec_id=EXEC_ID,
        agent=AGENT,
        model=MODEL,
        fallbacks=(),
        workspace=None,
        owner_pid=None,
        owner_ticks=None,
        hold_lock=True,
        duplicate_agent=False,
        metadata_override=None,
    ) -> Path:
        name = obs.BINDING_DIR_PREFIX + uuid.uuid4().hex[:8]
        d = self.root / name
        d.mkdir(mode=0o700)
        lock = d / obs.BINDING_LOCK_NAME
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        if hold_lock:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fds[name] = fd
        else:
            os.close(fd)
        metadata = {
            "schema": obs.BINDING_METADATA_SCHEMA,
            "artifact_type": obs.BINDING_ARTIFACT_TYPE,
            "binding_id": uuid.uuid4().hex,
            "artifact_dir_name": name,
            "created_at_epoch": 1.0,
            "owner_service": "orchestrator.executor_workspace_binding",
            "owner_hostname": "synthetic-host",
            "owner_uid": os.getuid(),
            "owner_pid": owner_pid if owner_pid is not None else os.getpid(),
            "owner_pid_start_ticks": (
                owner_ticks if owner_ticks is not None else _own_ticks()
            ),
            "owner_pid_namespace": os.readlink("/proc/self/ns/pid"),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "task_execution_id": exec_id,
            "liveness": "exclusive_flock:" + obs.BINDING_LOCK_NAME,
            "cleanup_eligibility": "operator --apply required",
        }
        metadata.update(metadata_override or {})
        (d / obs.BINDING_METADATA_NAME).write_text(json.dumps(metadata))
        (d / "state").mkdir(mode=0o700)
        (d / "agent").mkdir(mode=0o700)
        (d / "agent" / "auth-profiles.json").write_text(
            json.dumps(
                {
                    "profiles": {
                        "openai:default": {"type": "api_key", "key": DECOYS[2]},
                        "openai-codex:default": {
                            "type": "oauth",
                            "refresh": DECOYS[3],
                        },
                    }
                }
            )
        )
        entry = {
            "id": agent,
            "workspace": str(workspace or self.workspace),
            "agentDir": str(d / "agent"),
            "model": {"primary": model, "fallbacks": list(fallbacks)},
        }
        agents = [{"id": "main", "workspace": "/elsewhere"}, entry]
        if duplicate_agent:
            agents.append(dict(entry))
        config = {
            "models": {
                "providers": {
                    "openai": {"baseUrl": "http://gw:8000/v1", "apiKey": DECOYS[0]}
                }
            },
            "gateway": {"auth": {"mode": "token", "token": DECOYS[1]}},
            "agents": {"defaults": {}, "list": agents},
        }
        (d / "openclaw.json").write_text(json.dumps(config))
        os.chmod(d / "openclaw.json", 0o600)
        return d

    def release(self, d: Path) -> None:
        """Mimic ExecutorWorkspaceBinding.release(): rmtree, then drop lock."""
        import shutil

        shutil.rmtree(d)
        fd = self.lock_fds.pop(d.name, None)
        if fd is not None:
            os.close(fd)

    def close(self):
        for fd in self.lock_fds.values():
            os.close(fd)
        self.lock_fds.clear()

    def args(self, *extra):
        return obs.build_parser().parse_args(
            [
                "--mode",
                "synthetic",
                "--binding-root",
                str(self.root),
                "--expected-task-execution-id",
                str(EXEC_ID),
                "--expected-runtime-workspace",
                str(self.workspace),
                "--output-dir",
                str(self.out),
                "--interval",
                "0.05",
                "--duration",
                "5",
                "--settle",
                "0",
                *extra,
            ]
        )


class FakeProc(obs.ProcReader):
    """Synthetic procfs tree under tmp; fd entries are symlinks to real files."""

    def __init__(self, root: Path, locks_from: World):
        root.mkdir(exist_ok=True)
        super().__init__(str(root))
        self.world = locks_from
        (root / "sys/kernel/random").mkdir(parents=True)
        (root / "sys/kernel/random/boot_id").write_text(
            Path("/proc/sys/kernel/random/boot_id").read_text()
        )
        (root / "self/ns").mkdir(parents=True)
        os.symlink(os.readlink("/proc/self/ns/pid"), root / "self/ns/pid")
        self.denied_fd: set = set()

    def add(
        self,
        pid,
        *,
        ppid,
        ticks,
        comm="node",
        argv=(),
        env=None,
        fds=(),
        pgid=None,
        sid=None,
    ):
        d = self.root / str(pid)
        if d.exists():
            import shutil

            shutil.rmtree(d)
        (d / "fd").mkdir(parents=True)
        rest = ["S", str(ppid), str(pgid or pid), str(sid or pid)] + ["0"] * 15
        rest.append(str(ticks))
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        env = env or {}
        (d / "environ").write_bytes(
            b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0"
        )
        os.symlink("/usr/bin/node", d / "exe")
        for i, target in enumerate(fds):
            os.symlink(str(target), d / "fd" / str(i + 3))

    def remove(self, pid):
        import shutil

        shutil.rmtree(self.root / str(pid))

    def holds_inode(self, pid, dev, ino):
        if pid in self.denied_fd:
            raise PermissionError(13, "denied")
        return super().holds_inode(pid, dev, ino)

    def locked_inodes(self):
        inodes = set()
        for name in self.world.lock_fds:
            p = self.world.root / name / obs.BINDING_LOCK_NAME
            if p.exists():
                inodes.add(p.stat().st_ino)
        return inodes


def cli_env(binding: Path, *, config=None, state=None):
    return {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": DECOYS[4],
        "GITHUB_TOKEN": DECOYS[5],
        "OPENCLAW_CONFIG_PATH": str(config or binding / "openclaw.json"),
        "OPENCLAW_STATE_DIR": str(state or binding / "state"),
    }


def cli_argv(agent=AGENT, local=True):
    argv = ["node", "/usr/bin/openclaw", "agent", "--agent", agent]
    if local:
        argv.append("--local")
    return argv + [
        "--session-id",
        "sess-1",
        "--message",
        PROMPT,
        "--json",
        "--timeout",
        "120",
    ]


class Script:
    """Fake clock + sleep that runs scripted world mutations per poll."""

    def __init__(self, steps):
        self.t = 0.0
        self.n = 0
        self.steps = steps

    def clock(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        self.n += 1
        action = self.steps.get(self.n)
        if action:
            action()


OWNER = 7000
OWNER_TICKS = 5000


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def fake(world, tmp_path):
    p = FakeProc(tmp_path / "proc", world)
    p.add(1, ppid=0, ticks=1, comm="bash")
    p.add(OWNER, ppid=1, ticks=OWNER_TICKS, comm="celery", argv=["celery"])
    return p


def synthetic_binding(world, fake, **kw):
    return world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS, **kw)


def add_cli(fake, binding, pid=7100, ticks=6000, *, argv=None, env=None, **kw):
    fake.add(
        pid,
        ppid=OWNER,
        ticks=ticks,
        comm="node",
        argv=argv if argv is not None else cli_argv(),
        env=env if env is not None else cli_env(binding),
        fds=[binding / obs.BINDING_LOCK_NAME],
        **kw,
    )


def run(world, fake, steps=None, extra=(), setup=None):
    steps = dict(steps or {})
    if setup is not None:
        steps[1] = setup
    script = Script(steps)
    observer = obs.Observer(
        world.args(*extra), proc=fake, clock=script.clock, sleep=script.sleep
    )
    observer.open_output()
    return observer, observer.run()


def full_lifecycle_steps(world, fake, holder):
    """poll1: nothing; poll2: binding+CLI; poll4: CLI exits; poll5: release."""

    def create():
        holder["b"] = synthetic_binding(world, fake)
        add_cli(fake, holder["b"])
        (holder["b"] / "agent" / "models.json").write_text("{}")

    return {
        1: create,
        3: lambda: fake.remove(7100),
        4: lambda: world.release(holder["b"]),
    }


def all_output_text(world, capsys=None) -> str:
    parts = []
    if world.out.exists():
        for p in world.out.iterdir():
            parts.append(p.read_text())
    if capsys is not None:
        captured = capsys.readouterr()
        parts += [captured.out, captured.err]
    return "\n".join(parts)


def events(observer, name):
    return [e for e in observer.events if e["event"] == name]


# --------------------------------------------------------------------------
# 1-11, 30: discovery, correlation, config checks, full lifecycle
# --------------------------------------------------------------------------


def test_01_02_30_full_synthetic_lifecycle_produces_expected_g6_sequence(world, fake):
    holder = {}
    observer, summary = run(world, fake, full_lifecycle_steps(world, fake, holder))
    sequence = [e["event"] for e in observer.events]
    order = [
        "observer_started",
        "binding_discovered",
        "lock_state",
        "config_checked",
        "agent_dir_activity",
        "process_correlated",
        "process_exited",
        "binding_removed",
        "observer_finished",
    ]
    positions = [sequence.index(name) for name in order]
    assert positions == sorted(positions)
    assert summary["verdict"] == obs.PASS, summary["checks"]
    assert summary["classification"] == obs.RELEASE_OBSERVED
    assert set(summary["checks"].values()) == {obs.PASS}
    discovered = events(observer, "binding_discovered")[0]
    assert discovered["task_execution_id"] == EXEC_ID
    assert discovered["owner_pid"] == OWNER and discovered["owner_alive"] is True
    assert discovered["boot_id_matches"] is True
    assert discovered["credential_copy_present"] is True


def test_03_wrong_task_execution_id_is_not_accepted_as_target(world, fake):
    def create():
        b = world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS, exec_id=999)
        add_cli(fake, b)

    observer, summary = run(world, fake, {1: create}, extra=("--duration", "0.3"))
    assert events(observer, "foreign_binding")[0]["task_execution_id"] == 999
    assert events(observer, "binding_discovered") == []
    assert events(observer, "process_correlated") == []
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert "target_binding_never_observed" in summary["notes"]


def test_04_05_cli_agent_identity_pass_and_wrong_agent_detected(world, fake):
    def setup():
        b = synthetic_binding(world, fake)
        add_cli(fake, b, argv=cli_argv(agent="main"))

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    rec = events(observer, "process_correlated")[0]
    assert rec["argv_form"] == "original_argv" and rec["argv_agent"] == "main"
    assert summary["checks"]["cli_agent_identity"] == obs.FAIL
    assert summary["verdict"] == obs.FAIL


def test_04_correct_agent_from_original_argv(world, fake):
    def setup():
        add_cli(fake, synthetic_binding(world, fake))

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    rec = events(observer, "process_correlated")[0]
    assert rec["argv_agent"] == AGENT and rec["argv_local"] is True
    assert rec["argv_message_present"] is True
    assert summary["checks"]["cli_agent_identity"] == obs.PASS
    assert summary["checks"]["cli_local_flag"] == obs.PASS


def test_title_rewritten_cli_leaves_agent_identity_inconclusive(world, fake):
    def setup():
        add_cli(fake, synthetic_binding(world, fake), argv=["openclaw-agent"])

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    rec = events(observer, "process_correlated")[0]
    assert rec["argv_form"] == "process_title"
    assert rec["process_title"] == "openclaw-agent" and rec["argv_agent"] is None
    assert summary["checks"]["cli_agent_identity"] == obs.INCONCLUSIVE
    assert "cli_argv_agent_not_captured_title_rewritten" in summary["notes"]


def test_06_07_ephemeral_config_ok_and_persistent_config_rejected(world, fake):
    persistent = world.tmp / "persistent" / "openclaw.json"

    def setup():
        b = synthetic_binding(world, fake)
        add_cli(fake, b, env=cli_env(b, config=persistent))

    observer, summary = run(
        world,
        fake,
        setup=setup,
        extra=("--duration", "0.2", "--persistent-config-path", str(persistent)),
    )
    rec = events(observer, "process_correlated")[0]
    assert rec["config_path_ok"] is False and rec["config_is_persistent"] is True
    assert summary["checks"]["ephemeral_config_path"] == obs.FAIL
    assert summary["verdict"] == obs.FAIL
    assert not persistent.exists()  # compared as a path, never opened/created


def test_06_state_dir_outside_binding_fails(world, fake):
    def setup():
        b = synthetic_binding(world, fake)
        add_cli(fake, b, env=cli_env(b, state=world.tmp / "elsewhere"))

    _, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert summary["checks"]["ephemeral_config_path"] == obs.PASS
    assert summary["checks"]["ephemeral_state_dir"] == obs.FAIL


def test_08_09_runtime_workspace_pass_and_wrong_workspace_rejected(world, fake):
    def setup():
        synthetic_binding(world, fake, workspace=world.tmp / "wrong-ws")

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert events(observer, "config_checked")[0]["workspace_matches"] is False
    assert summary["checks"]["runtime_workspace"] == obs.FAIL


def test_10_11_primary_model_and_unexpected_fallback(world, fake):
    def setup():
        synthetic_binding(world, fake, fallbacks=("openai-codex/gpt-5.4",))

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    rec = events(observer, "config_checked")[0]
    assert rec["model_primary"] == MODEL and rec["model_matches"] is True
    assert rec["fallbacks"] == ["openai-codex/gpt-5.4"]
    assert summary["checks"]["model_primary"] == obs.PASS
    assert summary["checks"]["fallbacks_empty"] == obs.FAIL


def test_wrong_primary_model_detected(world, fake):
    def setup():
        synthetic_binding(world, fake, model="openai-codex/gpt-5.4")

    _, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert summary["checks"]["model_primary"] == obs.FAIL


# --------------------------------------------------------------------------
# 12-17: lock, release, never-observed, vanished CLI
# --------------------------------------------------------------------------


def test_12_active_lock_observed_by_nonblocking_probe(world, fake):
    holder = {}

    def setup():
        holder["b"] = synthetic_binding(world, fake)

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert events(observer, "lock_state")[0]["lock_held"] is True
    assert events(observer, "lock_state")[0]["method"] == "flock_probe"
    assert summary["checks"]["lock_held"] == obs.PASS
    # The owner's exclusive lock is untouched by the probe.
    fd = os.open(holder["b"] / obs.BINDING_LOCK_NAME, os.O_RDONLY)
    with pytest.raises(BlockingIOError):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(fd)


def test_13_lock_released_is_reported_free_and_lockfile_existence_is_not_held(
    world, fake
):
    def setup():
        b = synthetic_binding(world, fake, hold_lock=False)
        assert (b / obs.BINDING_LOCK_NAME).exists()
        assert obs.probe_lock(b / obs.BINDING_LOCK_NAME) == "free"

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert events(observer, "lock_state")[0]["lock_held"] is False
    assert summary["checks"]["lock_held"] == obs.INCONCLUSIVE
    # Lock-free binding left at window end is residue, not "active".
    assert summary["checks"]["no_unexpected_residue"] == obs.FAIL
    assert summary["classification"] == obs.RESIDUE_DETECTED


def test_14_binding_directory_removed_and_release_observed(world, fake):
    holder = {}
    observer, summary = run(world, fake, full_lifecycle_steps(world, fake, holder))
    removed = events(observer, "binding_removed")[0]
    assert removed["cli_alive"] is False and removed["lock_still_held"] is False
    assert summary["checks"]["release_observed"] == obs.PASS
    assert summary["checks"]["no_cli_remaining"] == obs.PASS


def test_15_release_only_after_prior_discovery(world, fake):
    def create_and_remove_between_polls():
        b = synthetic_binding(world, fake)
        world.release(b)

    steps = {1: create_and_remove_between_polls}
    observer, summary = run(world, fake, steps, extra=("--duration", "0.3"))
    assert events(observer, "binding_removed") == []
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert "release_observed" not in summary["checks"]


def test_15b_preexisting_binding_removal_is_not_a_release(world, fake):
    b = synthetic_binding(world, fake)  # present before the observer starts
    observer, summary = run(world, fake, {2: lambda: world.release(b)})
    assert events(observer, "observer_started")[0]["preexisting_entries"] == 1
    assert events(observer, "binding_discovered") == []
    assert events(observer, "binding_removed") == []
    assert summary["verdict"] == obs.INCONCLUSIVE


def test_16_binding_never_observed_is_inconclusive_not_fail(world, fake):
    observer, summary = run(world, fake, extra=("--duration", "0.2"))
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert summary["classification"] == obs.INCONCLUSIVE
    assert summary["checks"] == {"binding_identity": obs.INCONCLUSIVE}


def test_17_cli_disappears_before_correlation(world, fake):
    holder = {}

    def create():
        holder["b"] = synthetic_binding(world, fake)

    steps = {1: create, 3: lambda: world.release(holder["b"])}
    observer, summary = run(world, fake, steps)
    assert events(observer, "process_correlated") == []
    assert summary["checks"]["lock_inherited_by_cli"] == obs.INCONCLUSIVE
    assert summary["checks"]["release_observed"] == obs.PASS
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert summary["classification"] == obs.RELEASE_OBSERVED


# --------------------------------------------------------------------------
# 18-22: PID reuse, malformed metadata, symlinks, permissions, concurrency
# --------------------------------------------------------------------------


def test_18_pid_reuse_protection(world, fake):
    holder = {}

    def create():
        holder["b"] = synthetic_binding(world, fake)
        add_cli(fake, holder["b"], pid=7100, ticks=6000)

    def reuse_pid():
        # Original CLI exits; an unrelated process reuses PID 7100.
        fake.add(7100, ppid=1, ticks=9999, comm="bash", argv=["bash"])

    steps = {1: create, 3: reuse_pid, 4: lambda: world.release(holder["b"])}
    observer, summary = run(
        world,
        fake,
        steps,
        extra=("--expected-cli-pid", "7100", "--expected-cli-start-ticks", "6000"),
    )
    exited = events(observer, "process_exited")
    assert [(e["pid"], e["start_ticks"]) for e in exited] == [(7100, 6000)]
    assert len(events(observer, "process_correlated")) == 1
    assert summary["checks"]["expected_cli_identity"] == obs.PASS
    assert summary["checks"]["release_observed"] == obs.PASS


def test_18c_owner_ticks_mismatch_reported(world, fake):
    def create():
        world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS + 1)

    observer, _ = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    assert events(observer, "binding_discovered")[0]["owner_alive"] is False


def test_19_malformed_metadata(world, fake):
    def create():
        d = world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS)
        (d / obs.BINDING_METADATA_NAME).write_text("{not json" + DECOYS[0])

    observer, summary = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    assert events(observer, "binding_metadata_invalid")[0]["reason"] == (
        "malformed_json"
    )
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert DECOYS[0] not in all_output_text(world)


def test_19b_metadata_schema_and_dirname_mismatch(world, fake):
    def create():
        world.binding(
            owner_pid=OWNER,
            owner_ticks=OWNER_TICKS,
            metadata_override={"artifact_dir_name": "orchestrator-openclaw-binding-x"},
        )

    observer, _ = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    assert events(observer, "binding_metadata_invalid")[0]["reason"] == (
        "artifact_dir_name_mismatch"
    )


def test_20_unsafe_symlink_entry_is_not_followed(world, fake, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / obs.BINDING_METADATA_NAME).write_text(DECOYS[0])

    def create():
        os.symlink(outside, world.root / (obs.BINDING_DIR_PREFIX + "evil0001"))

    observer, _ = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    assert events(observer, "unsafe_entry")[0]["reason"].startswith("symlink")
    assert DECOYS[0] not in all_output_text(world)


def test_20b_symlinked_metadata_file_is_not_followed(world, fake, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"x": DECOYS[1]}))

    def create():
        d = world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS)
        (d / obs.BINDING_METADATA_NAME).unlink()
        os.symlink(secret, d / obs.BINDING_METADATA_NAME)

    observer, _ = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    assert events(observer, "binding_metadata_invalid")[0]["reason"].startswith(
        "unreadable"
    )
    assert DECOYS[1] not in all_output_text(world)


def test_20c_symlinked_binding_root_fails_closed(world, fake, tmp_path):
    real = tmp_path / "real-root"
    real.mkdir(mode=0o700)
    link = tmp_path / "link-root"
    os.symlink(real, link)
    args = world.args("--binding-root", str(link))
    observer = obs.Observer(args, proc=fake)
    observer.open_output()
    summary = observer.run()
    assert summary["verdict"] == obs.OBSERVATION_ERROR


def test_21_missing_proc_permission_is_inconclusive(world, fake):
    def setup():
        add_cli(fake, synthetic_binding(world, fake))
        fake.denied_fd.add(7100)

    observer, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert events(observer, "process_access_denied")[0]["what"] == "fd"
    assert summary["checks"]["lock_inherited_by_cli"] == obs.INCONCLUSIVE
    assert "proc_access_denied" in summary["notes"]
    assert summary["verdict"] == obs.INCONCLUSIVE


def test_22_multiple_concurrent_bindings(world, fake):
    holder = {}

    def create():
        holder["a"] = synthetic_binding(world, fake)
        add_cli(fake, holder["a"], pid=7100)
        holder["nested"] = synthetic_binding(world, fake)
        holder["foreign"] = world.binding(
            owner_pid=OWNER, owner_ticks=OWNER_TICKS, exec_id=12
        )

    observer, summary = run(world, fake, {1: create}, extra=("--duration", "0.3"))
    assert len(events(observer, "binding_discovered")) == 2
    assert len(events(observer, "foreign_binding")) == 1
    assert len(summary["targets"]) == 2
    # Still locked at window end: active, not residue, and not a PASS.
    assert summary["checks"]["no_unexpected_residue"] == obs.INCONCLUSIVE
    assert summary["checks"]["release_observed"] == obs.INCONCLUSIVE
    assert "bindings_still_active_at_window_end" in summary["notes"]


def test_duplicate_agent_entry_fails_closed(world, fake):
    def setup():
        synthetic_binding(world, fake, duplicate_agent=True)

    _, summary = run(world, fake, setup=setup, extra=("--duration", "0.2"))
    assert summary["checks"]["bound_agent_entry"] == obs.FAIL
    assert summary["checks"]["runtime_workspace"] == obs.INCONCLUSIVE


# --------------------------------------------------------------------------
# 23-25: credential/prompt minimization and evidence permissions
# --------------------------------------------------------------------------


def test_23_24_25_decoys_and_prompt_never_emitted_and_files_private(
    world, fake, capsys
):
    holder = {}
    script = Script(full_lifecycle_steps(world, fake, holder))
    observer = obs.Observer(
        world.args(), proc=fake, clock=script.clock, sleep=script.sleep
    )
    observer.open_output()
    summary = observer.run()
    print(json.dumps(summary))  # what main() would print
    text = all_output_text(world, capsys)
    for secret in DECOYS + (PROMPT, "PRIVATE-TASK-TEXT", "sess-1"):
        assert secret not in text
    assert "apiKey" not in text and "auth-profiles.json" not in text
    assert (os.stat(world.out).st_mode & 0o777) == 0o700
    for p in world.out.iterdir():
        assert (os.stat(p).st_mode & 0o777) == 0o600
    assert sorted(p.name for p in world.out.iterdir()) == [
        "g6-observe.jsonl",
        "g6-summary.json",
    ]


def test_23b_environ_select_decodes_only_requested_keys(world, fake):
    b = synthetic_binding(world, fake)
    add_cli(fake, b)
    env = fake.environ_select(7100, obs.ENV_KEYS)
    assert set(env) == set(obs.ENV_KEYS)
    assert DECOYS[4] not in json.dumps(env)


def test_23c_event_schema_rejects_unlisted_fields_and_unsafe_text():
    with pytest.raises(obs.ObserverError):
        obs.build_event("lock_state", entry="x", api_key=DECOYS[0])
    with pytest.raises(obs.ObserverError) as exc:
        obs.build_event("lock_state", entry="bad\nvalue" + DECOYS[0])
    assert DECOYS[0] not in str(exc.value)
    with pytest.raises(obs.ObserverError):
        obs.build_event("not_an_event")


def test_24b_argv_parser_discards_prompt_and_unknown_flag_values():
    tokens = [t.encode() for t in cli_argv()] + [b"--api-key", DECOYS[0].encode()]
    parsed = obs.parse_argv_structure(tokens)
    blob = json.dumps(parsed)
    assert PROMPT not in blob and DECOYS[0] not in blob and "sess-1" not in blob
    assert parsed["argv_agent"] == AGENT
    assert parsed["argv_unknown_flag_count"] == 1


def test_bound_config_extraction_is_allowlisted(world, fake):
    b = synthetic_binding(world, fake)
    info = obs.extract_bound_agent(b / "openclaw.json", AGENT)
    assert set(info) == {
        "agent_entries",
        "reason",
        "workspace",
        "model_primary",
        "fallbacks",
    }
    assert DECOYS[0] not in json.dumps(info) and DECOYS[1] not in json.dumps(info)


# --------------------------------------------------------------------------
# 26-29: no signals, no cleanup, no inference, fail-closed layouts
# --------------------------------------------------------------------------


def _forbid(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("forbidden side effect")

    for name in ("kill", "killpg", "remove", "unlink", "rmdir", "rename"):
        monkeypatch.setattr(os, name, boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    import shutil
    import signal

    monkeypatch.setattr(shutil, "rmtree", boom)
    monkeypatch.setattr(signal, "signal", boom)


def test_26_timeout_with_live_real_cli_child_does_not_signal(world, monkeypatch):
    """A real child holding the inherited lock outlives the observer window."""
    b = world.binding()
    fd = world.lock_fds[b.name]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "OPENCLAW_CONFIG_PATH": str(b / "openclaw.json"),
        "OPENCLAW_STATE_DIR": str(b / "state"),
        "OPENAI_API_KEY": DECOYS[4],
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()", *cli_argv()[2:]],
        stdin=subprocess.PIPE,
        env=env,
        pass_fds=(fd,),
        start_new_session=True,
    )
    try:
        # Real /proc: the test process is the owner, the child the CLI.  One
        # poll then window end, exactly what a timed-out observation does.
        observer = obs.Observer(world.args("--duration", "0.4"))
        observer.open_output()
        _forbid(monkeypatch)
        observer.poll()
        summary = observer.finish(observer.clock(), False)
        monkeypatch.undo()
        rec = events(observer, "process_correlated")[0]
        assert rec["pid"] == child.pid and rec["argv_agent"] == AGENT
        assert rec["holds_lock"] is True and rec["own_session"] is True
        assert rec["config_path_ok"] is True and rec["state_dir_ok"] is True
        assert summary["checks"]["lock_inherited_by_cli"] == obs.PASS
        assert summary["checks"]["release_observed"] == obs.INCONCLUSIVE
        assert child.poll() is None  # still running: never signalled
        assert DECOYS[4] not in all_output_text(world)
    finally:
        monkeypatch.undo()
        child.stdin.close()  # natural exit, no signal
        assert child.wait(timeout=10) == 0


def test_27_28_observer_never_cleans_up_signals_or_connects(world, fake, monkeypatch):
    def create():
        b = synthetic_binding(world, fake, hold_lock=False)
        add_cli(fake, b)

    script = Script({1: create})
    observer = obs.Observer(
        world.args("--duration", "0.3"),
        proc=fake,
        clock=script.clock,
        sleep=script.sleep,
    )
    observer.open_output()
    _forbid(monkeypatch)
    summary = observer.run()
    monkeypatch.undo()
    residue = [p.name for p in world.root.iterdir()]
    assert len(residue) == 1
    assert summary["classification"] == obs.RESIDUE_DETECTED
    assert (world.root / residue[0] / "agent" / "auth-profiles.json").exists()


def test_28b_static_source_has_no_network_signal_subprocess_or_app_imports():
    tree = ast.parse(SCRIPT.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden_modules = {
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "subprocess",
        "asyncio",
        "signal",
        "shutil",
        "sqlite3",
        "redis",
        "app",
        "celery",
    }
    assert imported.isdisjoint(forbidden_modules), imported & forbidden_modules
    forbidden_attrs = {
        "kill",
        "killpg",
        "remove",
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
        "system",
        "popen",
        "execv",
        "execve",
        "fork",
        "truncate",
    }
    used = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "os"
    }
    assert used.isdisjoint(forbidden_attrs), used & forbidden_attrs
    lock_ops = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in {"LOCK_EX", "LOCK_SH"}
    ]
    assert {n.attr for n in lock_ops} == {"LOCK_SH"}  # never exclusive


def test_29_unsafe_root_mode_and_startup_guards_fail_closed(world, tmp_path, capsys):
    os.chmod(world.root, 0o755)
    observer = obs.Observer(world.args(), proc=obs.ProcReader())
    observer.open_output()
    assert observer.run()["verdict"] == obs.OBSERVATION_ERROR
    os.chmod(world.root, 0o700)

    base = [
        "--binding-root",
        str(world.root),
        "--expected-task-execution-id",
        "1",
        "--expected-runtime-workspace",
        str(world.workspace),
    ]
    out = str(tmp_path / "o")
    # synthetic mode refuses the production root
    assert (
        obs.main(
            [
                "--mode",
                "synthetic",
                *base[2:],
                "--binding-root",
                obs.default_production_root(),
                "--output-dir",
                out,
            ]
        )
        == 3
    )
    # production mode requires explicit confirmation
    assert obs.main(["--mode", "production", *base, "--output-dir", out]) == 3
    # output dir inside binding root / pre-existing output dir are refused
    assert (
        obs.main(["--mode", "synthetic", *base, "--output-dir", str(world.root / "ev")])
        == 3
    )
    existing = tmp_path / "exists"
    existing.mkdir()
    assert obs.main(["--mode", "synthetic", *base, "--output-dir", str(existing)]) == 3
    assert not (world.root / "ev").exists() and not Path(out).exists()
    err = capsys.readouterr().err
    assert "ObserverError" in err


def test_constants_match_production_binding_contract():
    from app.services.orchestration.execution import executor_workspace_binding as ewb

    assert obs.BINDING_DIR_PREFIX == ewb.BINDING_DIR_PREFIX
    assert obs.BINDING_METADATA_NAME == ewb.BINDING_METADATA_NAME
    assert obs.BINDING_LOCK_NAME == ewb.BINDING_LOCK_NAME
    assert obs.BINDING_METADATA_SCHEMA == ewb.BINDING_METADATA_SCHEMA
    assert obs.BINDING_ARTIFACT_TYPE == ewb.BINDING_ARTIFACT_TYPE
    assert obs.EPHEMERAL_AGENT_ID == ewb.EPHEMERAL_AGENT_ID
    meta = ewb._binding_metadata(
        Path("/x/orchestrator-openclaw-binding-ab"), "f" * 32, 5
    )
    assert set(obs._METADATA_ALLOWLIST) <= set(meta)
    assert set(ewb.create_managed_binding.__code__.co_varnames) >= {"tmp_dir"}


def test_real_create_managed_binding_lifecycle_with_real_child(
    tmp_path, isolated_openclaw_binding_root, monkeypatch
):
    """End to end against the production binding primitive (tmp root only):
    create_managed_binding -> real child inheriting the lock with the binding
    env -> child exits -> release() -> observer adjudicates PASS (argv
    captured) without ever seeing a decoy value."""
    from app.services.orchestration.execution.executor_workspace_binding import (
        create_managed_binding,
    )

    workspace = tmp_path / "rt-ws"
    workspace.mkdir()

    def build_config(binding_dir):
        agent_dir = binding_dir / "agent"
        agent_dir.mkdir(mode=0o700)
        (agent_dir / "auth-profiles.json").write_text(DECOYS[2])
        (agent_dir / "models.json").write_text("{}")
        return {
            "models": {"providers": {"openai": {"apiKey": DECOYS[0]}}},
            "gateway": {"auth": {"token": DECOYS[1]}},
            "agents": {
                "list": [
                    {"id": "main"},
                    {
                        "id": AGENT,
                        "workspace": str(workspace),
                        "agentDir": str(agent_dir),
                        "model": {"primary": MODEL, "fallbacks": []},
                    },
                ]
            },
        }

    args = obs.build_parser().parse_args(
        [
            "--mode",
            "synthetic",
            "--binding-root",
            str(isolated_openclaw_binding_root),
            "--expected-task-execution-id",
            str(EXEC_ID),
            "--expected-runtime-workspace",
            str(workspace),
            "--output-dir",
            str(tmp_path / "g6-out"),
            "--settle",
            "0",
        ]
    )
    observer = obs.Observer(args)
    observer.open_output()
    observer.poll()  # root absent: nothing observed yet
    binding = create_managed_binding(
        agent_id=AGENT, task_execution_id=EXEC_ID, build_config=build_config
    )
    child = None
    try:
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "OPENAI_API_KEY": DECOYS[4]}
        env.update(binding.environment)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()", *cli_argv()[2:]],
            stdin=subprocess.PIPE,
            env=env,
            pass_fds=binding.subprocess_pass_fds(),
            start_new_session=True,
        )
        observer.poll()
        child.stdin.close()
        assert child.wait(timeout=10) == 0
        observer.poll()
    finally:
        if child is not None and child.poll() is None:
            child.stdin.close()
            child.wait(timeout=10)
        binding.release()
    observer.poll()
    summary = observer.finish(observer.clock(), False)
    assert summary["verdict"] == obs.PASS, summary["checks"]
    assert summary["classification"] == obs.RELEASE_OBSERVED
    disc = events(observer, "binding_discovered")[0]
    assert disc["binding_id"] == binding.binding_id
    assert disc["owner_pid"] == os.getpid() and disc["owner_alive"] is True
    rec = events(observer, "process_correlated")[0]
    assert rec["pid"] == child.pid and rec["role"] == "cli"
    assert rec["config_path"] == str(binding.config_path)
    text = "\n".join(f.read_text() for f in Path(args.output_dir).iterdir())
    for secret in DECOYS + (PROMPT,):
        assert secret not in text
    assert list(isolated_openclaw_binding_root.iterdir()) == []


def test_24c_hostile_prompt_cannot_spoof_agent_or_leak():
    hostile = "--agent main --local --api-key " + DECOYS[0]
    tokens = [
        b"node",
        b"/usr/bin/openclaw",
        b"agent",
        b"--agent",
        AGENT.encode(),
        b"--message",
        hostile.encode(),
        b"--json",
    ]
    parsed = obs.parse_argv_structure(tokens)
    assert parsed["argv_agent"] == AGENT
    assert parsed["argv_local"] is False
    assert DECOYS[0] not in json.dumps(parsed)


def test_19c_residue_with_unknown_lock_state_is_inconclusive_not_fail(world, fake):
    def create():
        d = world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS, hold_lock=False)
        (d / obs.BINDING_METADATA_NAME).write_text("{broken")
        (d / obs.BINDING_LOCK_NAME).unlink()  # no lock evidence at all

    observer, summary = run(world, fake, {1: create}, extra=("--duration", "0.2"))
    residue = events(observer, "residue")[0]
    assert residue["lock_held"] is None and residue["metadata_valid"] is False
    assert summary["checks"]["no_unexpected_residue"] == obs.INCONCLUSIVE
    assert summary["verdict"] == obs.INCONCLUSIVE
    assert "residue_lock_state_unknown" in summary["notes"]


@pytest.mark.parametrize("prompt", [b"--agent", b"--local", b"agent"])
def test_24d_prompt_equal_to_a_flag_is_never_parsed_as_structure(prompt):
    tokens = [b"node", b"/usr/bin/openclaw", b"agent", b"--agent", AGENT.encode()]
    tokens += [b"--message", prompt, b"--json", b"--timeout", b"60"]
    parsed = obs.parse_argv_structure(tokens)
    assert parsed["argv_agent"] == AGENT
    assert parsed["argv_local"] is False  # no --local outside the prompt


def test_24e_repeated_agent_flag_is_ambiguous_and_fails():
    tokens = [b"node", b"openclaw", b"agent", b"--agent", AGENT.encode()]
    tokens += [b"--agent", b"main", b"--local"]
    assert obs.parse_argv_structure(tokens)["argv_agent"] == "<ambiguous>"


def test_19d_unknown_lock_residue_beside_released_target_is_not_fail(world, fake):
    holder = {}
    steps = full_lifecycle_steps(world, fake, holder)
    create_target = steps[1]

    def create():
        create_target()
        d = world.binding(owner_pid=OWNER, owner_ticks=OWNER_TICKS, hold_lock=False)
        (d / obs.BINDING_METADATA_NAME).write_text("{broken")
        (d / obs.BINDING_LOCK_NAME).unlink()

    steps[1] = create
    _, summary = run(world, fake, steps)
    assert summary["checks"]["release_observed"] == obs.PASS
    assert summary["checks"]["no_unexpected_residue"] == obs.INCONCLUSIVE
    assert summary["verdict"] == obs.INCONCLUSIVE


def test_14b_dir_removed_but_lock_still_held_is_not_a_release(world, fake):
    """An unidentified holder (e.g. an orphan the observer never correlated)
    keeps the deleted lock: removal alone must not count as release."""
    holder = {}
    steps = full_lifecycle_steps(world, fake, holder)
    create = steps[1]
    kept = {}

    def create_and_remember():
        create()
        kept["ino"] = (holder["b"] / obs.BINDING_LOCK_NAME).stat().st_ino

    steps[1] = create_and_remember
    fake.locked_inodes = lambda: {kept["ino"]} if kept else set()
    observer, summary = run(world, fake, steps, extra=("--duration", "0.5"))
    assert events(observer, "binding_removed")[0]["lock_still_held"] is True
    assert summary["checks"]["release_observed"] == obs.INCONCLUSIVE
    assert summary["verdict"] == obs.INCONCLUSIVE
