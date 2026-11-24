"""P43-PRODUCT-22-C2-C3A: disposable live-slice readiness (offline).

Covers the repository-side preparation only: authenticated Launcher epoch
binding, the strict isolated C2 Launcher configuration, the disposable-only
publication admission, the C2 Worker entrypoint gate, and a reproduction of
the SQLite writer lock held across the Sandbox call by the real execution
loop. No Host, Docker, Worker, broker, or provider is touched.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import DEFAULT_DATABASE_URL, settings
from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.orchestration.error_handler import error_handler
from app.services.orchestration.execution import sandbox_gateway
from app.services.orchestration.execution.sandbox_gateway import (
    C2_SANDBOX_TIMEOUT_CAP_SECONDS,
    SandboxExecutionGateway,
)
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.run_state.transitions import (
    TaskCompletionRejected,
    mark_task_attempt_done,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.worker_boundary import (
    C2_DISPOSABLE_EXECUTION_ADMISSION,
    DISPOSABLE_WORKER_TASK_ALLOWLIST,
    WorkerTaskAdmissionError,
    disposable_c2_publication_admitted,
    model_environment_authority_violations,
)
from app.services.sandbox_bridge import host_service
from app.services.sandbox_bridge.contract import RequestRejected
from app.services.sandbox_bridge.transport import (
    HostSocketLauncher,
    SandboxClient,
    _mac,
    _receive,
    _send,
)
from app.tests.test_p43_product22_c2_c1_worker_foundation import (
    _make_contract_fixture,
    _run_entrypoint,
    _skip_if_host_exposes_privileged_socket,
)
from app.tests import test_p43_product22_c2_c2b_sandbox_slice as c2b
from app.tests.test_phase33c4_execution_mutation_gate import (
    _authority,
    _persist_authority,
)

# Reuse the C2-C2B lifecycle, real-Launcher, and no-fallback fixtures/helpers.
lifecycle, host, no_local_execution = c2b.lifecycle, c2b.host, c2b.no_local_execution
KEY, PASS, FakeDocker = c2b.KEY, c2b.PASS, c2b.FakeDocker
_authorized_request, _claimed_attempt = c2b._authorized_request, c2b._claimed_attempt
_context, _raw = c2b._context, c2b._raw

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / "scripts" / "maintenance"
C2_UNIT = MAINT / "p43-sandbox-bridge-c2.service"
C1_UNIT = MAINT / "p43-sandbox-bridge.service"
HOST_SCRIPT = MAINT / "p43_sandbox_bridge_host.sh"


# --- 5. Launcher epoch binding ---------------------------------------------


def test_worker_binds_current_launcher_epoch_over_authenticated_socket(lifecycle, host):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    path = host(docker)
    epoch = SandboxClient(path, KEY).launcher_epoch()
    assert type(epoch) is int and epoch > 0
    good = _authorized_request(db, session, task, execution, path=path)
    assert good.authorization["launcher_epoch"] == epoch
    assert _raw(path, good.to_wire())["execution_status"] == "SUCCESS"
    assert len(docker.calls) == 1


def test_wrong_missing_and_previous_instance_epoch_are_rejected(lifecycle, host):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    first = host(docker)
    previous_epoch = SandboxClient(first, KEY).launcher_epoch()
    second = host(docker)  # a new Launcher instance (restart) has a new epoch
    current_epoch = SandboxClient(second, KEY).launcher_epoch()
    assert current_epoch != previous_epoch

    previous = _authorized_request(
        db, session, task, execution, epoch=previous_epoch
    ).to_wire()
    assert _raw(second, previous)["error_code"] == "launcher_epoch_mismatch"
    wrong = _authorized_request(
        db, session, task, execution, epoch=current_epoch + 1
    ).to_wire()
    assert _raw(second, wrong)["error_code"] == "launcher_epoch_mismatch"

    # Missing epoch: re-MAC with the real key so only strict mode can catch it.
    missing = _authorized_request(db, session, task, execution, path=second).to_wire()
    missing["authorization"]["launcher_epoch"] = None
    assert _raw(second, missing)["error_code"] == "launcher_epoch_required"
    assert docker.calls == []


def test_non_strict_launcher_keeps_c1b_null_epoch_behavior(lifecycle, host):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    docker = FakeDocker()
    path = host(docker, require_authorization=False)
    wire = _authorized_request(db, session, task, execution, path=path).to_wire()
    wire["authorization"]["launcher_epoch"] = None
    assert _raw(path, wire)["execution_status"] == "SUCCESS"


def test_epoch_reply_is_authenticated_and_nonce_bound(tmp_path):
    socket_dir = tmp_path / "s"
    socket_dir.mkdir(mode=0o700)
    path = socket_dir / "launcher.sock"
    launcher = HostSocketLauncher(
        path, KEY, os.geteuid(), FakeDocker(), tmp_path / "l.sqlite3"
    )
    stop = threading.Event()
    thread = threading.Thread(target=launcher.serve_forever, args=(stop,))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert SandboxClient(path, KEY).launcher_epoch() == launcher.epoch
        with pytest.raises(RequestRejected, match="epoch_reply_invalid"):
            SandboxClient(path, b"w" * 32).launcher_epoch()
        # A recorded reply for another nonce is not accepted as this call's reply.
        import socket as socket_module

        body = {"operation": "epoch", "nonce": "a" * 64}
        with socket_module.socket(socket_module.AF_UNIX) as sock:
            sock.connect(str(path))
            payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
            _send(sock, {**body, "mac": _mac(KEY, payload.encode())})
            recorded = _receive(sock)
        assert recorded["launcher_epoch"] == launcher.epoch

        client = SandboxClient(path, KEY)

        class Replay:
            """Feeds the recorded reply without contacting the Launcher."""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def settimeout(self, value):
                pass

            def sendall(self, data):
                pass

            def recv(self, n):
                if not hasattr(self, "buffer"):
                    data = json.dumps(
                        recorded, separators=(",", ":"), sort_keys=True
                    ).encode()
                    self.buffer = len(data).to_bytes(4, "big") + data
                chunk, self.buffer = self.buffer[:n], self.buffer[n:]
                return chunk

        client._connect_before_submission = Replay
        with pytest.raises(RequestRejected, match="epoch_reply_unauthenticated"):
            client.launcher_epoch()
    finally:
        stop.set()
        thread.join(5)


def test_gateway_refuses_without_epoch_and_never_executes(
    lifecycle, host, tmp_path, monkeypatch
):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    host(FakeDocker())

    class NoEpoch:
        calls = 0

        def launcher_epoch(self):
            raise OSError("unavailable")

        def execute(self, request):
            NoEpoch.calls += 1

    outcome = SandboxExecutionGateway(NoEpoch()).execute_python(
        _context(db, session, task, execution), "import sys; sys.exit(0)", 300
    )
    assert (outcome.accepted, outcome.reason, NoEpoch.calls) == (
        False,
        "host_unavailable",
        0,
    )


def test_c2_sandbox_timeout_is_capped_below_sqlite_busy_timeout(lifecycle, host):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    path = host(FakeDocker())
    request = _authorized_request(db, session, task, execution, path=path)
    # _authorized_request asks for 30 s; LOCAL_VERIFICATION_TIMEOUT_SECONDS is 300.
    assert request.timeout_seconds == C2_SANDBOX_TIMEOUT_CAP_SECONDS == 10
    # connect (2) + epoch (5) + timeout + response wait (10) < busy_timeout (30)
    assert 2 + 5 + C2_SANDBOX_TIMEOUT_CAP_SECONDS + 10 < 30


# --- 4. Strict isolated C2 Launcher ----------------------------------------


def _c2_env(monkeypatch, tmp_path, **overrides):
    values = {
        "CREDENTIALS_DIRECTORY": str(tmp_path),
        "P43_SANDBOX_BRIDGE_ALLOWED_UID": "10001",
        "P43_SANDBOX_BRIDGE_INSTANCE": "c2",
        "RUNTIME_DIRECTORY": "/run/p43-sandbox-bridge-c2",
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_c2_instance_config_is_strict_and_isolated(monkeypatch, tmp_path):
    _c2_env(monkeypatch, tmp_path)
    config = host_service._config_from_env()
    assert config.require_authorization is True
    assert config.max_active == 1 and config.allowed_uid == 10001
    assert config.state_dir == Path("/var/lib/p43-sandbox-bridge-c2")
    assert config.socket_path == Path("/run/p43-sandbox-bridge-c2/launcher.sock")
    assert config.container_prefix == "p43-sbx-c2-" and config.socket_group_traverse
    for path in (config.socket_dir, config.ledger_path, config.staging_root):
        assert "/p43-sandbox-bridge/" not in f"{path}/"

    _c2_env(monkeypatch, tmp_path, P43_SANDBOX_BRIDGE_INSTANCE=None)
    default = host_service._config_from_env()
    assert default.require_authorization is False  # deployed C1B unchanged
    assert default.socket_path == Path(
        "/var/lib/p43-sandbox-bridge/socket/launcher.sock"
    )
    assert default.container_prefix == "p43-sbx-" and not default.socket_group_traverse


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"P43_SANDBOX_BRIDGE_INSTANCE": "c3"}, "unknown_instance"),
        ({"P43_SANDBOX_BRIDGE_ALLOWED_UID": "0"}, "c2_requires_non_root_peer"),
        ({"RUNTIME_DIRECTORY": "/run/other"}, "c2_runtime_directory_mismatch"),
        ({"RUNTIME_DIRECTORY": None}, "c2_runtime_directory_mismatch"),
    ],
)
def test_c2_instance_config_fails_closed(monkeypatch, tmp_path, overrides, error):
    _c2_env(monkeypatch, tmp_path, **overrides)
    with pytest.raises(host_service.BootstrapError, match=error):
        host_service._config_from_env()


def _c2_service_config(tmp_path, socket_mode=0o710):
    state = tmp_path / "state"
    for directory in (state, state / "ledger", state / "staging"):
        directory.mkdir(mode=0o700)
    socket_dir = tmp_path / "run"
    socket_dir.mkdir()
    socket_dir.chmod(socket_mode)
    key = tmp_path / "bridge.key"
    key.write_text(KEY.hex())
    key.chmod(0o600)
    return host_service.ServiceConfig(
        state,
        os.geteuid(),
        key,
        max_active=1,
        require_authorization=True,
        socket_dir_override=socket_dir,
        socket_group_traverse=True,
        container_prefix=host_service.C2_CONTAINER_PREFIX,
    )


def test_c2_serve_is_strict_end_to_end(lifecycle, tmp_path, monkeypatch):
    _, db = lifecycle
    session, task, _, execution = _claimed_attempt(db)
    config = _c2_service_config(tmp_path)
    docker = FakeDocker()
    stop = threading.Event()
    thread = threading.Thread(
        target=host_service.serve, args=(config, stop), kwargs={"executor": docker}
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not config.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert oct(config.socket_dir.stat().st_mode & 0o777) == "0o710"
        clock = lambda: time.time() + 2  # noqa: E731 - after Launcher start
        good = _authorized_request(
            db, session, task, execution, path=config.socket_path, clock=clock
        )
        assert _raw(config.socket_path, good.to_wire())["execution_status"] == (
            "SUCCESS"
        )
        stale = _authorized_request(
            db, session, task, execution, path=config.socket_path, clock=clock
        ).to_wire()
        stale["authorization"]["launcher_epoch"] = None
        assert _raw(config.socket_path, stale)["error_code"] == (
            "launcher_epoch_required"
        )
        assert len(docker.calls) == 1
    finally:
        stop.set()
        thread.join(30)


@pytest.mark.parametrize("mode", [0o750, 0o711, 0o770])
def test_c2_socket_dir_allows_only_group_search(tmp_path, mode):
    config = _c2_service_config(tmp_path, socket_mode=mode)
    with pytest.raises(host_service.BootstrapError, match="unsafe_directory:run"):
        host_service.check_directories(config)


def test_c1b_socket_dir_still_refuses_group_search(tmp_path):
    config = _c2_service_config(tmp_path)
    c1b = host_service.ServiceConfig(
        config.state_dir,
        os.geteuid(),
        config.key_path,
        socket_dir_override=config.socket_dir,
    )
    with pytest.raises(host_service.BootstrapError, match="unsafe_directory:run"):
        host_service.check_directories(c1b)


def test_c2_orphan_recovery_never_touches_c1b_containers(tmp_path):
    from app.services.sandbox_bridge.docker_executor import IMAGE

    config = _c2_service_config(tmp_path)
    log = tmp_path / "docker.log"
    c1b = "p43-sbx-" + "a" * 32
    c2 = "p43-sbx-c2-" + "b" * 32
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'if [ "$1" = ps ]; then\n'
        f'  printf "{c1b}\\t{IMAGE}\\n{c2}\\t{IMAGE}\\n"\n'
        "fi\n"
    )
    fake.chmod(0o755)
    config = host_service.ServiceConfig(**{**config.__dict__, "docker": str(fake)})
    result = host_service.recover_orphans(config)
    calls = log.read_text().splitlines()
    assert "name=^/p43-sbx-c2-" in calls[0]
    assert [line for line in calls if line.startswith("rm")] == [f"rm -f {c2}"]
    assert result["containers_removed"] == 1 and result["containers_skipped"] == 1


def test_c2_unit_is_strict_isolated_and_not_enabled_at_boot():
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(C2_UNIT)
    svc = unit["Service"]
    assert svc["User"] == svc["Group"] == "p43-sandbox-c2"
    assert svc["ExecStart"] == (
        "/usr/bin/python3 -B -E -s -m sandbox_bridge.host_service serve"
    )
    assert svc["WorkingDirectory"] == "/opt/p43-sandbox-bridge-c2/lib"
    assert svc["LoadCredential"] == "bridge.key:/etc/p43-sandbox-bridge-c2/bridge.key"
    text = C2_UNIT.read_text()
    assert "Environment=P43_SANDBOX_BRIDGE_INSTANCE=c2" in text
    assert "Environment=P43_SANDBOX_BRIDGE_ALLOWED_UID=10001" in text
    assert svc["RuntimeDirectory"] == "p43-sandbox-bridge-c2"
    assert svc["RuntimeDirectoryMode"] == "0710"
    assert svc["StateDirectoryMode"] == "0700" and svc["Restart"] == "no"
    assert int(svc["RuntimeMaxSec"]) <= 1800
    assert "Install" not in unit  # never enabled at boot
    for key in ("NoNewPrivileges", "PrivateNetwork", "ProtectHome", "PrivateTmp"):
        assert svc[key] == "yes"
    assert svc["RestrictAddressFamilies"] == "AF_UNIX"
    assert svc["CapabilityBoundingSet"] == "" and svc["ProtectSystem"] == "strict"
    # Shares no user, key, code, state, or socket path with the deployed C1B unit.
    c1 = C1_UNIT.read_text()
    for shared in (
        "User=p43-sandbox\n",
        "/etc/p43-sandbox-bridge/",
        "/opt/p43-sandbox-bridge/",
        "StateDirectory=p43-sandbox-bridge ",
    ):
        assert shared in c1 and shared not in text, shared


def test_c2_unit_passes_systemd_analyze_when_available(tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze unavailable")
    (tmp_path / "docker.service").write_text(
        "[Unit]\nDescription=stub\n[Service]\nExecStart=/bin/true\n"
    )
    shutil.copy(C2_UNIT, tmp_path / C2_UNIT.name)
    out = subprocess.run(
        ["systemd-analyze", "verify", "./docker.service", f"./{C2_UNIT.name}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    ours = [
        line
        for line in (out.stdout + out.stderr).splitlines()
        if (C2_UNIT.name in line or str(tmp_path) in line)
        and "p43-sandbox-c2" not in line  # service user exists only after c2-install
    ]
    assert (out.returncode, ours) == (0, [])


def test_host_script_c2_commands_require_approval_and_are_scoped():
    for args in (["c2-install"], ["c2-rollback"], ["c2-install", "--yes"]):
        out = subprocess.run(
            ["bash", str(HOST_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 64, args
    text = HOST_SCRIPT.read_text()
    assert "authorization.py" in re.search(r"^MODULES=\((.*)\)$", text, re.M).group(1)
    install = re.search(r"^c2_install\(\) \{\n(.*?)^\}", text, re.M | re.S).group(1)
    rollback = re.search(r"^c2_rollback\(\) \{\n(.*?)^\}", text, re.M | re.S).group(1)
    assert "systemctl enable" not in install and "systemctl start" not in install
    assert "systemctl restart" not in install
    assert '"$SERVICE"' not in install + rollback  # never the deployed C1B unit
    assert 'cmp -s "$C2_KEY" "$KEY"' in install  # C2 key never equals the C1B key
    assert re.findall(r"rm -f -- (\S+)", rollback) == [
        '"$C2_UNIT_DST"',
        '"$C2_WORKER_KEY"',
    ]
    assert "rm -rf" not in rollback and "docker " not in rollback


# --- 2/3. Disposable publication admission ---------------------------------


def _admission_env(tmp_path, **overrides):
    db_dir = tmp_path / "db"
    env = {
        "WORKER_ISOLATION_MODE": "disposable",
        "WORKER_ENVIRONMENT": "c2-c1-test",
        "WORKER_BRIDGE_MODE": "c2b-sandbox",
        "C2_DISPOSABLE_EXECUTION_ADMISSION": C2_DISPOSABLE_EXECUTION_ADMISSION,
        "WORKER_DATABASE_DIR": str(db_dir),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}, (
        f"sqlite:///{db_dir}/orchestrator.db"
    )


def test_disposable_publication_admission_is_exact(tmp_path):
    env, url = _admission_env(tmp_path)

    def admitted(env=env, queue="orchestrator-c2-c1-test", url=url):
        return disposable_c2_publication_admitted(
            env,
            queue_name=queue,
            database_url=url,
            production_database_url=DEFAULT_DATABASE_URL,
        )

    assert admitted()
    for name in (
        "WORKER_ISOLATION_MODE",
        "WORKER_ENVIRONMENT",
        "WORKER_BRIDGE_MODE",
        "C2_DISPOSABLE_EXECUTION_ADMISSION",
        "WORKER_DATABASE_DIR",
    ):
        assert not admitted(env={**env, name: "other"}), name
        assert not admitted(env={k: v for k, v in env.items() if k != name}), name
    assert not admitted(queue="celery")
    assert not admitted(url=DEFAULT_DATABASE_URL)  # production authority database
    production_dir = DEFAULT_DATABASE_URL.removeprefix("sqlite:///").rsplit("/", 1)[0]
    assert not admitted(
        env={**env, "WORKER_DATABASE_DIR": production_dir}, url=DEFAULT_DATABASE_URL
    )
    assert (
        "C2_DISPOSABLE_EXECUTION_ADMISSION"
        in model_environment_authority_violations(env)
    )


def test_publication_admission_never_enables_apply_or_widens_tasks(
    tmp_path, monkeypatch
):
    from app import celery_app as celery_module
    from app.tasks.worker import execute_orchestration_task

    env, url = _admission_env(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CELERY_QUEUE_NAME", "orchestrator-c2-c1-test")
    monkeypatch.setenv(
        "WORKER_TASK_ALLOWLIST",
        ",".join(sorted(DISPOSABLE_WORKER_TASK_ALLOWLIST)),
    )
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", False)
    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    celery_module._require_execution_enabled(disposable_publication=True)
    with pytest.raises(RuntimeError, match="EXECUTION_DISABLED"):
        celery_module._require_execution_enabled()
    with pytest.raises(RuntimeError, match="EXECUTION_DISABLED"):
        execute_orchestration_task.apply(kwargs={})
    # The existing allowlist and queue guard still apply after admission.
    with pytest.raises(WorkerTaskAdmissionError):
        celery_module.celery_app.send_task(
            "app.tasks.maintenance.cleanup_old_logs", queue="orchestrator-c2-c1-test"
        )
    with pytest.raises(WorkerTaskAdmissionError):
        celery_module.celery_app.send_task(
            "app.tasks.worker.execute_orchestration_task", queue="celery"
        )
    monkeypatch.setattr(settings, "DEV_API_ONLY", True)
    with pytest.raises(RuntimeError, match="DEVELOPMENT_EXECUTION_DISABLED"):
        celery_module._require_execution_enabled(disposable_publication=True)
    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "disabled")
    with pytest.raises(RuntimeError, match="EXECUTION_DISABLED"):
        celery_module._require_execution_enabled(disposable_publication=True)


# --- 2. Worker entrypoint C2 gate ------------------------------------------


def _c2_entrypoint_env(tmp_path, **overrides):
    env = _make_contract_fixture(tmp_path)
    env.update(
        {
            "WORKER_BRIDGE_MODE": "c2b-sandbox",
            "C2_DISPOSABLE_EXECUTION_ADMISSION": C2_DISPOSABLE_EXECUTION_ADMISSION,
            "SANDBOX_BRIDGE_SOCKET": "/run/p43-sandbox-bridge-c2/launcher.sock",
            "SANDBOX_BRIDGE_KEY_FILE": "/run/secrets/p43-sandbox-bridge-c2.key",
            "WORKER_CONCURRENCY": "1",
        }
    )
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.mark.parametrize(
    "overrides,message",
    [
        (
            {"C2_DISPOSABLE_EXECUTION_ADMISSION": None},
            "C2_DISPOSABLE_EXECUTION_ADMISSION must be",
        ),
        (
            {"SANDBOX_BRIDGE_SOCKET": "/run/p43-sandbox-bridge/launcher.sock"},
            "SANDBOX_BRIDGE_SOCKET must be",
        ),
        (
            {"SANDBOX_BRIDGE_KEY_FILE": "/run/secrets/p43-sandbox-bridge.key"},
            "SANDBOX_BRIDGE_KEY_FILE must be",
        ),
        ({"WORKER_CONCURRENCY": "2"}, "WORKER_CONCURRENCY must be"),
        ({"WORKER_BRIDGE_MODE": "c2b"}, "WORKER_BRIDGE_MODE must be"),
        ({"WORKER_ISOLATION_MODE": "production"}, "WORKER_ISOLATION_MODE must be"),
        ({"EXECUTION_ENABLED": "true"}, "EXECUTION_ENABLED must be"),
        ({"WORKER_QUEUE_NAME": "celery"}, "WORKER_QUEUE_NAME must be"),
        ({"WORKER_CANDIDATE_PIN_PATH": None}, "WORKER_CANDIDATE_PIN_PATH is required"),
        ({"WORKER_INSTANCE_ID": None}, "WORKER_INSTANCE_ID is required"),
        (
            {"AGENT_BACKEND": "local_openclaw"},
            "not an approved structured HTTP backend",
        ),
        (
            {"OPENCLAW_CLI_PATH": "/usr/bin/openclaw"},
            "not part of the Worker credential",
        ),
        ({"WORKER_ENV_FILE": "/app/.env"}, "WORKER_ENV_FILE must be"),
    ],
)
def test_c2_entrypoint_rejects_unsafe_bridge_configuration(
    tmp_path, overrides, message
):
    out = _run_entrypoint(
        _c2_entrypoint_env(tmp_path, **overrides), user=pwd.getpwnam("nobody")
    )
    assert out.returncode == 78, out.stdout + out.stderr
    assert message in out.stderr


def test_c2_entrypoint_admits_exact_opt_in_then_requires_mounted_launcher(tmp_path):
    # Every configuration check passes; the run stops only because the C2
    # Launcher socket is (correctly) not mounted on this test host.
    out = _run_entrypoint(_c2_entrypoint_env(tmp_path), user=pwd.getpwnam("nobody"))
    assert out.returncode == 78
    assert "C2 Launcher socket is not mounted" in out.stderr


@pytest.mark.parametrize(
    "name",
    [
        "SANDBOX_BRIDGE_SOCKET",
        "SANDBOX_BRIDGE_KEY_FILE",
        "C2_DISPOSABLE_EXECUTION_ADMISSION",
    ],
)
def test_disabled_bridge_mode_rejects_bridge_credentials(tmp_path, name):
    env = _make_contract_fixture(tmp_path)
    env[name] = "x"
    out = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert out.returncode == 78
    assert f"{name} is not part of the Worker credential allowlist" in out.stderr


def test_disabled_bridge_mode_still_accepts_c2c1_contract(tmp_path):
    _skip_if_host_exposes_privileged_socket()
    out = _run_entrypoint(_make_contract_fixture(tmp_path), user=pwd.getpwnam("nobody"))
    assert out.returncode == 0, out.stderr


# --- 6. SQLite writer lock through the real execution loop -----------------


class _StructuredRuntime:
    """Structured HTTP topology: no command tools; must never be called here."""

    backend_descriptor = get_backend_descriptor("direct_ollama")

    async def execute_task(self, *args, **kwargs):
        raise AssertionError("runtime must not be dispatched for a sandbox step")

    def reports_context_overflow(self, result):
        return False

    def get_backend_metadata(self):
        return {"backend": "provider-free-test", "model_family": "test"}


def _loop_context(db, session, task, link, execution, project_dir):
    project = db.query(Project).filter_by(id=session.project_id).one()
    project.workspace_path = str(project_dir)
    db.commit()
    step = {
        "step_number": 1,
        "description": "Run the bounded sandbox verification",
        "commands": [PASS],
        "verification": "",
        "rollback": None,
        "expected_files": [],
        "ops": [],
    }
    state = OrchestrationState(
        session_id=str(session.id),
        task_description=task.description,
        project_name=project.name,
        project_context="",
        task_id=task.id,
        plan=[step],
        reasoning_artifact={
            "intent": "bounded sandbox verification",
            "workspace_facts": [f"project_dir={project_dir}"],
            "planned_actions": ["run sys.exit(0) in the Host sandbox"],
            "verification_plan": ["sandbox exit code is 0"],
        },
    )
    state._project_dir_override = str(project_dir)
    _persist_authority(
        db,
        session_id=session.id,
        task_id=task.id,
        authority=_authority(project_dir, state.plan),
    )
    return OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=30,
        execution_profile="test_only",
        validation_profile="test_only",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=_StructuredRuntime(),
        task_service=SimpleNamespace(),
        logger=logging.getLogger("c2-c3a-lock"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=error_handler,
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )


def _run_loop(ctx):
    return execute_step_loop(
        ctx=ctx,
        extract_structured_text=lambda value: (
            str(value.get("output", value)) if isinstance(value, dict) else str(value)
        ),
        normalize_step=lambda raw_step, project_dir, logger_obj, step_number: dict(
            raw_step
        ),
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )


SANDBOX_DELAY_SECONDS = 1.5


def test_execution_loop_holds_writer_lock_across_sandbox_call(
    lifecycle, host, tmp_path, no_local_execution
):
    """Reproduce, measure, and bound the C2-C2B writer-lock finding.

    1. The loop reserves mutation authority (unchanged Task UPDATE, no commit)
       and then calls the Gateway on the same transaction.
    2. A concurrent cancellation transaction from another connection is
       blocked for the whole delayed Sandbox response.
    3. With a busy timeout shorter than the hold, cancellation FAILS with
       "database is locked" — the defect if hold > the API's 30 s busy_timeout.
    4. Once the Worker commits, the cancellation commits and the late result
       cannot complete the attempt (existing CAS).
    """

    factory, db = lifecycle
    session, task, link, execution = _claimed_attempt(db)
    db_path = str(db.get_bind().url.database)
    project_dir = tmp_path / "loop-workspace"
    project_dir.mkdir()
    ctx = _loop_context(db, session, task, link, execution, project_dir)
    observations = {}

    def cancel_with_busy_timeout(busy_timeout):
        connection = sqlite3.connect(db_path, timeout=busy_timeout)
        started = time.monotonic()
        try:
            connection.execute(
                "UPDATE tasks SET status='CANCELLED' WHERE id=?", (task.id,)
            )
            connection.execute(
                "UPDATE sessions SET status='stopped', is_active=0 WHERE id=?",
                (session.id,),
            )
            connection.commit()
            return "committed", time.monotonic() - started
        except sqlite3.OperationalError as exc:
            return str(exc), time.monotonic() - started
        finally:
            connection.close()

    waiter = {}

    def delayed_host(request):
        # Runs inside the Launcher while the Worker thread awaits the reply.
        observations["short"] = cancel_with_busy_timeout(0.2)

        def long_wait():
            waiter["result"] = cancel_with_busy_timeout(30)
            waiter["committed_at"] = time.monotonic()

        thread = threading.Thread(target=long_wait)
        thread.start()
        waiter["thread"] = thread
        time.sleep(SANDBOX_DELAY_SECONDS)
        observations["reply_at"] = time.monotonic()

    docker = FakeDocker(on_execute=delayed_host)
    host(docker)
    started = time.monotonic()
    result = _run_loop(ctx)
    loop_returned = time.monotonic()
    waiter["thread"].join(35)

    assert len(docker.calls) == 1
    # (2)/(3) Blocked: a short busy timeout fails outright while the lock is held.
    assert observations["short"][0] == "database is locked"
    # The long waiter could commit only after the delayed Sandbox reply.
    status, waited = waiter["result"]
    assert status == "committed"
    assert waiter["committed_at"] >= observations["reply_at"]
    assert waited >= SANDBOX_DELAY_SECONDS - 0.3
    hold = waiter["committed_at"] - started
    # (4) Cancellation is authoritative afterward: the existing completion CAS
    # refuses to mark the attempt DONE from the loop's stale objects.
    with pytest.raises(TaskCompletionRejected):
        mark_task_attempt_done(
            task=ctx.task, session_task_link=link, task_execution=execution
        )
    db.rollback()
    db.expire_all()
    final = db.query(TaskExecution.status).filter_by(id=execution.id).scalar()
    task_status = db.query(Task.status).filter_by(id=task.id).scalar()
    assert task_status == TaskStatus.CANCELLED
    assert final != TaskStatus.DONE
    print(
        json.dumps(
            {
                "loop_status": result.get("status"),
                "loop_reason": result.get("reason"),
                "sandbox_delay_s": SANDBOX_DELAY_SECONDS,
                "cancel_wait_s": round(waited, 3),
                "lock_hold_from_loop_start_s": round(hold, 3),
                "loop_return_s": round(loop_returned - started, 3),
                "short_busy_timeout_result": observations["short"][0],
                "task_execution_status": str(final),
            },
            sort_keys=True,
        )
    )


def test_c2_live_env_template_matches_admission_and_entrypoint():
    lines = (REPO / "config" / "worker-c2-live.env.example").read_text().splitlines()
    env = dict(
        line.split("=", 1) for line in lines if line and not line.startswith("#")
    )
    assert disposable_c2_publication_admitted(
        env,
        queue_name=env["CELERY_QUEUE_NAME"],
        database_url=env["DATABASE_URL"],
        production_database_url=DEFAULT_DATABASE_URL,
    )
    assert env["EXECUTION_ENABLED"] == "false" and env["WORKER_CONCURRENCY"] == "1"
    entrypoint = (REPO / "scripts" / "worker-entrypoint.sh").read_text()
    assert f"c2_socket={env['SANDBOX_BRIDGE_SOCKET']}" in entrypoint
    assert f"c2_key={env['SANDBOX_BRIDGE_KEY_FILE']}" in entrypoint
    assert "CELERY_BROKER_URL" not in env and "SECRET_KEY" not in env
    assert not model_environment_authority_violations(
        {k: v for k, v in env.items() if k.startswith(("AGENT_", "PLANNING_D"))}
    )
