"""P43-PRODUCT-22-C1: Host Launcher bootstrap (service entrypoint, unit, Compose fragment, Host script)."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import textwrap
import threading
import time
import uuid

import pytest
import yaml

from app.services.sandbox_bridge.contract import (
    PROFILE,
    ExecutionRequest,
    ExecutionResult,
    RequestRejected,
)
from app.services.sandbox_bridge.docker_executor import IMAGE
from app.services.sandbox_bridge.host_service import (
    BootstrapError,
    BoundedExecutor,
    ServiceConfig,
    check_directories,
    client_check,
    load_key,
    raw_execute,
    recover_orphans,
    remove_stale_socket,
    serve,
)
from app.services.sandbox_bridge.transport import SandboxClient

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / "scripts" / "maintenance"
UNIT = MAINT / "p43-sandbox-bridge.service"
COMPOSE = MAINT / "p43-sandbox-bridge.compose-ai-agent.yml"
HOST_SCRIPT = MAINT / "p43_sandbox_bridge_host.sh"
root_only = pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to change identities"
)


def _state(tmp_path, *, allowed_uid=None) -> ServiceConfig:
    state = tmp_path / "state"
    for d in (state, state / "socket", state / "ledger", state / "staging"):
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
    key = tmp_path / "bridge.key"
    key.write_text(secrets.token_hex(32) + "\n")
    key.chmod(0o600)
    uid = os.geteuid() if allowed_uid is None else allowed_uid
    return ServiceConfig(state, uid, key)


def _request(code="print(1 + 1)\n", request_id=None) -> ExecutionRequest:
    return ExecutionRequest(
        request_id or str(uuid.uuid4()),
        "c1-test",
        "c1-test",
        str(uuid.uuid4()),
        PROFILE,
        code,
        30,
        int(time.time()),
        secrets.token_hex(32),
    )


def _ok_executor(calls=None):
    def run(req, cancel):
        if calls is not None:
            calls.append(req.request_id)
        return ExecutionResult(req.request_id, "SUCCESS", 0, "2\n", "")

    return run


class Running:
    def __init__(self, config, executor):
        self.config, self.stop, self.rc = config, threading.Event(), None
        self.thread = threading.Thread(target=self._run, args=(executor,), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.thread.is_alive():
            with socket.socket(
                socket.AF_UNIX
            ) as probe:  # a leftover dead socket file is not "ready"
                try:
                    probe.connect(str(config.socket_path))
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            raise AssertionError("launcher did not start")

    def _run(self, executor):
        self.rc = serve(self.config, self.stop, executor=executor)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive()


def _client(config):
    return SandboxClient(
        config.socket_path, load_key(config.key_path), connect_timeout=2
    )


def _raw_execute(config, key, req):
    return raw_execute(config.socket_path, key, req)


# 1. Service configuration validity
def test_unit_file_is_hardened_and_least_privilege():
    unit = configparser.ConfigParser(strict=True, interpolation=None)
    unit.optionxform = str
    unit.read(UNIT)
    svc = unit["Service"]
    assert svc["User"] == "p43-sandbox" and svc["Group"] == "p43-sandbox"
    assert svc["SupplementaryGroups"] == "docker"
    assert (
        svc["ExecStart"]
        == "/usr/bin/python3 -B -E -s -m sandbox_bridge.host_service serve"
    )
    assert svc["WorkingDirectory"] == "/opt/p43-sandbox-bridge/lib"
    assert svc["LoadCredential"] == "bridge.key:/etc/p43-sandbox-bridge/bridge.key"
    assert svc["Environment"] == "P43_SANDBOX_BRIDGE_ALLOWED_UID=0"
    assert svc["StateDirectoryMode"] == "0700" and svc["UMask"] == "0077"
    assert svc["Restart"] == "on-failure"
    assert (
        svc["RestrictAddressFamilies"] == "AF_UNIX" and svc["PrivateNetwork"] == "yes"
    )
    for key in (
        "NoNewPrivileges",
        "ProtectHome",
        "PrivateTmp",
        "PrivateDevices",
        "RestrictSUIDSGID",
    ):
        assert svc[key] == "yes"
    assert svc["ProtectSystem"] == "strict" and svc["CapabilityBoundingSet"] == ""
    assert (
        unit["Unit"]["Requires"] == "docker.service"
        and unit["Unit"]["After"] == "docker.service"
    )
    text = UNIT.read_text()
    assert (
        "ListenStream" not in text and "Socket" not in unit and "/workspace" not in text
    )
    assert not re.search(r"[0-9a-f]{64}", text)


def test_unit_passes_systemd_analyze_when_available(tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze unavailable")
    # docker.service exists only on the Host; a stub satisfies Requires= for offline verification.
    (tmp_path / "docker.service").write_text(
        "[Unit]\nDescription=stub\n[Service]\nExecStart=/bin/true\n"
    )
    shutil.copy(UNIT, tmp_path / UNIT.name)
    out = subprocess.run(
        ["systemd-analyze", "verify", "./docker.service", f"./{UNIT.name}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The runner's own installed units may emit unrelated diagnostics (e.g. xfs_scrub
    # CPUAccounting= deprecation on GitHub runners); only lines about the units under test count.
    ours = [
        line
        for line in (out.stdout + out.stderr).splitlines()
        if UNIT.name in line or str(tmp_path) in line
    ]
    assert (out.returncode, ours) == (0, [])


# 2. Socket directory permissions
@pytest.mark.parametrize("target", ["state", "socket", "ledger", "staging"])
def test_directories_must_be_private_owned_and_not_symlinks(tmp_path, target):
    config = _state(tmp_path)
    check_directories(config)
    path = {
        "state": config.state_dir,
        "socket": config.socket_dir,
        "ledger": config.ledger_path.parent,
        "staging": config.staging_root,
    }[target]
    os.chmod(path, 0o750)
    with pytest.raises(BootstrapError, match="unsafe_directory"):
        check_directories(config)
    os.chmod(path, 0o700)
    if target != "state":
        moved = tmp_path / f"real-{target}"
        path.rename(moved)
        path.symlink_to(moved)
        with pytest.raises(BootstrapError, match="unsafe_directory"):
            check_directories(config)


@root_only
def test_directory_owned_by_other_uid_is_refused(tmp_path):
    config = _state(tmp_path)
    os.chown(config.socket_dir, 12345, 12345)
    with pytest.raises(BootstrapError, match="unsafe_directory:socket"):
        check_directories(config)


# 3. UID/GID mismatch and 4. unauthorized peer rejection
def test_peer_uid_mismatch_is_rejected_without_execution(tmp_path):
    calls = []
    config = _state(tmp_path, allowed_uid=os.geteuid() + 4242)
    running = Running(config, _ok_executor(calls))
    try:
        result = _raw_execute(config, load_key(config.key_path), _request())
        # The server rejects before reading the request, so the client either reads
        # the anonymous rejection (request_id mismatch) or, if the server already
        # closed, sees EPIPE/ECONNRESET and reports UNAVAILABLE. Both fail closed.
        try:
            client_result = _client(config).execute(_request())
        except RequestRejected as exc:
            assert str(exc) == "result_request_mismatch"
        else:
            assert (client_result.execution_status, client_result.error_code) == (
                "UNAVAILABLE",
                "launcher_unavailable",
            )
    finally:
        running.close()
    assert (result["execution_status"], result["error_code"], calls) == (
        "REJECTED",
        "unauthorized_peer",
        [],
    )


def test_wrong_hmac_is_rejected_without_execution(tmp_path):
    calls = []
    config = _state(tmp_path)
    running = Running(config, _ok_executor(calls))
    try:
        bad = _raw_execute(config, secrets.token_bytes(32), _request())
        good = _client(config).execute(_request())
    finally:
        running.close()
    assert (bad["execution_status"], bad["error_code"]) == (
        "REJECTED",
        "invalid_authentication",
    )
    assert good.execution_status == "SUCCESS" and len(calls) == 1


@root_only
def test_nonroot_peer_cannot_traverse_private_socket_directory(tmp_path):
    config = _state(tmp_path)
    os.chmod(tmp_path, 0o755)
    running = Running(config, _ok_executor())
    try:
        probe = (
            "import socket,sys\ns=socket.socket(socket.AF_UNIX)\n"
            "try:\n s.connect(sys.argv[1]); print('connected')\nexcept PermissionError: print('EACCES')\n"
        )
        out = subprocess.run(
            ["python3", "-I", "-c", probe, str(config.socket_path)],
            user=65534,
            group=65534,
            extra_groups=[],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        running.close()
    assert out.stdout.strip() == "EACCES"


# 5. Missing secret and 6. invalid secret permissions
def test_missing_secret_fails_closed(tmp_path):
    config = _state(tmp_path)
    config.key_path.unlink()
    with pytest.raises(BootstrapError, match="missing_secret"):
        serve(config, threading.Event(), executor=_ok_executor())
    assert not config.socket_path.exists()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_group_or_world_readable_secret_is_refused(tmp_path, mode):
    config = _state(tmp_path)
    config.key_path.chmod(mode)
    with pytest.raises(BootstrapError, match="unsafe_secret_permissions"):
        load_key(config.key_path)


def test_systemd_credential_root_0440_is_accepted_only_in_credentials_dir(
    tmp_path, monkeypatch
):
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir(mode=0o700)
    key_path = credentials_dir / "bridge.key"
    key_path.write_text(secrets.token_hex(32) + "\n")
    key_path.chmod(0o400)
    real_fstat = os.fstat

    class CredentialStat:
        def __init__(self, fd):
            actual = real_fstat(fd)
            self.st_mode = (actual.st_mode & ~0o777) | 0o440
            self.st_uid = 0
            self.st_gid = 0
            self.st_size = actual.st_size

    monkeypatch.setattr(os, "fstat", CredentialStat)
    monkeypatch.setattr(os, "geteuid", lambda: 995)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials_dir))
    assert len(load_key(key_path)) == 32

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(BootstrapError, match="unsafe_secret_permissions"):
        load_key(key_path)


@pytest.mark.parametrize(
    "content,error",
    [
        ("ab" * 31, "secret_too_short"),
        ("zz" * 32, "invalid_secret_format"),
        ("", "secret_too_short"),
    ],
)
def test_short_or_malformed_secret_is_refused(tmp_path, content, error):
    config = _state(tmp_path)
    config.key_path.write_text(content)
    with pytest.raises(BootstrapError, match=error):
        load_key(config.key_path)


def test_symlinked_secret_is_refused(tmp_path):
    config = _state(tmp_path)
    link = tmp_path / "link.key"
    link.symlink_to(config.key_path)
    with pytest.raises(BootstrapError, match="unreadable_secret"):
        load_key(link)


@root_only
def test_secret_owned_by_other_uid_is_refused(tmp_path):
    config = _state(tmp_path)
    os.chown(config.key_path, 12345, 12345)
    with pytest.raises(BootstrapError, match="unsafe_secret_permissions"):
        load_key(config.key_path)


def test_secret_never_appears_in_logs_or_errors(tmp_path, caplog):
    config = _state(tmp_path)
    secret = config.key_path.read_text().strip()
    caplog.set_level("DEBUG")
    running = Running(config, _ok_executor())
    try:
        _client(config).execute(_request())
    finally:
        running.close()
    config.key_path.write_text(secret[:40])
    with pytest.raises(BootstrapError) as exc:
        load_key(config.key_path)
    assert secret not in caplog.text and secret[:40] not in str(exc.value)


# 7. Launcher restart and 8. ledger persistence
def test_restart_reuses_socket_path_and_never_replays_ledger_ids(tmp_path):
    calls = []
    config = _state(tmp_path)
    running = Running(config, _ok_executor(calls))
    first = (
        _request()
    )  # issued after start: earlier requests are rejected as predating the Launcher
    try:
        assert sorted(os.listdir(config.socket_dir)) == ["launcher.sock"]
        assert _client(config).execute(first).execution_status == "SUCCESS"
    finally:
        running.close()
    assert running.rc == 0 and not config.socket_path.exists()
    assert (
        config.ledger_path.exists() and config.ledger_path.parent != config.socket_dir
    )
    running = Running(config, _ok_executor(calls))
    try:
        replay = _client(config).execute(_request(request_id=first.request_id))
        fresh = _client(config).execute(_request())
    finally:
        running.close()
    assert (replay.execution_status, replay.error_code) == (
        "REJECTED",
        "duplicate_request",
    )
    assert fresh.execution_status == "SUCCESS" and calls == [
        first.request_id,
        fresh.request_id,
    ]


def test_stop_cancels_and_waits_for_active_request_cleanup(tmp_path):
    config = _state(tmp_path)
    started, cleaned = threading.Event(), threading.Event()

    def slow(req, cancel):
        started.set()
        cancel.wait(20)
        time.sleep(0.2)  # simulated container stop + staging cleanup
        cleaned.set()
        return ExecutionResult(req.request_id, "CANCELLED", None, "", "")

    running = Running(config, slow)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(_client(config).execute(_request()))
    )
    worker.start()
    assert started.wait(5)
    running.close()
    worker.join(5)
    assert cleaned.is_set() and running.rc == 0
    assert results and results[0].execution_status == "CANCELLED"


def test_concurrency_cap_rejects_excess_work():
    gate = threading.Event()
    bounded = BoundedExecutor(
        lambda req, cancel: (
            gate.wait(5),
            ExecutionResult(req.request_id, "SUCCESS", 0, "", ""),
        )[1],
        1,
    )
    first = threading.Thread(target=bounded, args=(_request(), threading.Event()))
    first.start()
    time.sleep(0.1)
    busy = bounded(_request(), threading.Event())
    gate.set()
    first.join(5)
    assert (busy.execution_status, busy.error_code) == ("REJECTED", "launcher_busy")
    assert bounded.wait_idle(1)


# 9. Stale socket handling
def test_dead_socket_from_crash_is_removed_and_service_starts(tmp_path):
    config = _state(tmp_path)
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(config.socket_path))
    dead.close()
    assert remove_stale_socket(config.socket_path) is True
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(config.socket_path))
    dead.close()
    running = Running(config, _ok_executor())
    try:
        assert _client(config).execute(_request()).execution_status == "SUCCESS"
    finally:
        running.close()


def test_live_listener_is_not_replaced(tmp_path):
    config = _state(tmp_path)
    running = Running(config, _ok_executor())
    try:
        with pytest.raises(BootstrapError, match="launcher_already_listening"):
            remove_stale_socket(config.socket_path)
        assert config.socket_path.exists()
    finally:
        running.close()


@pytest.mark.parametrize("kind", ["file", "symlink", "dir"])
def test_non_socket_path_is_refused_not_deleted(tmp_path, kind):
    config = _state(tmp_path)
    target = tmp_path / "victim"
    target.write_text("keep")
    if kind == "file":
        config.socket_path.write_text("x")
    elif kind == "symlink":
        config.socket_path.symlink_to(target)
    else:
        config.socket_path.mkdir()
    with pytest.raises(BootstrapError, match="refusing_unsafe_socket_path"):
        serve(config, threading.Event(), executor=_ok_executor())
    assert os.path.lexists(config.socket_path) and target.read_text() == "keep"


@root_only
def test_dead_socket_owned_by_other_uid_is_refused(tmp_path):
    config = _state(tmp_path)
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(config.socket_path))
    dead.close()
    os.chown(config.socket_path, 12345, 12345)
    with pytest.raises(BootstrapError, match="refusing_unsafe_socket_path"):
        remove_stale_socket(config.socket_path)


def test_exposed_socket_directory_must_hold_only_the_socket(tmp_path):
    config = _state(tmp_path)
    (config.socket_dir / "ledger.sqlite3").write_text("")
    with pytest.raises(BootstrapError, match="unexpected_socket_directory_entry"):
        serve(config, threading.Event(), executor=_ok_executor())


# 10. Scoped cleanup
def _fake_docker(tmp_path, listing: str, rm_rc=0) -> Path:
    log = tmp_path / "docker.log"
    script = tmp_path / "fake-docker"
    script.write_text(
        textwrap.dedent(
            f"""\
        #!/usr/bin/env python3
        import sys
        open({str(log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
        if sys.argv[1] == "ps":
            sys.stdout.write({listing!r})
        sys.exit({rm_rc} if sys.argv[1] == "rm" else 0)
    """
        )
    )
    script.chmod(0o755)
    return script


def test_orphan_recovery_removes_only_exact_bridge_containers_and_staging(tmp_path):
    config = _state(tmp_path)
    own = "p43-sbx-" + uuid.uuid4().hex
    listing = (
        "\n".join(
            [
                f"{own}\t{IMAGE}",
                f"p43-sbx-{uuid.uuid4().hex}\tpython:3.12-slim",
                f"p43-sbx-{uuid.uuid4().hex}-x\t{IMAGE}",
                f"p43-sbx-unrelated\t{IMAGE}",
            ]
        )
        + "\n"
    )
    docker = _fake_docker(tmp_path, listing)
    leftover = config.staging_root / "request-abc_1234"
    (leftover / "output").mkdir(parents=True)
    (config.staging_root / "not-a-request").mkdir()
    (config.staging_root / "request-link1234").symlink_to(tmp_path)
    config = ServiceConfig(
        config.state_dir, config.allowed_uid, config.key_path, docker=str(docker)
    )
    summary = recover_orphans(config)
    log = (tmp_path / "docker.log").read_text().splitlines()
    assert [line for line in log if line.startswith("rm")] == [f"rm -f {own}"]
    assert log[0].startswith("ps -a --no-trunc --filter name=^/p43-sbx-")
    assert not any("prune" in line or "kill" in line for line in log)
    assert summary == {
        "containers_removed": 1,
        "containers_skipped": 3,
        "staging_removed": 1,
        "staging_left": 2,
    }
    assert (
        not leftover.exists()
        and (config.staging_root / "not-a-request").exists()
        and tmp_path.exists()
    )


def test_orphan_recovery_failure_blocks_startup(tmp_path):
    config = _state(tmp_path)
    docker = _fake_docker(tmp_path, f"p43-sbx-{uuid.uuid4().hex}\t{IMAGE}\n", rm_rc=1)
    with pytest.raises(BootstrapError, match="orphan_container_removal_failed"):
        recover_orphans(
            ServiceConfig(
                config.state_dir,
                config.allowed_uid,
                config.key_path,
                docker=str(docker),
            )
        )


# 11. Bootstrap idempotency and 12. rollback safety (Host script, without touching the Host)
def _script_function(name: str) -> str:
    text = HOST_SCRIPT.read_text()
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", text, re.S | re.M)
    assert match, name
    return match.group(1)


def test_host_script_syntax_and_approval_gates():
    assert subprocess.run(["bash", "-n", str(HOST_SCRIPT)]).returncode == 0
    for args in (["install"], ["rollback"], ["install", "--yes"], []):
        out = subprocess.run(
            ["bash", str(HOST_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 64, args


def test_install_is_idempotent_and_never_rotates_or_prints_key():
    body = _script_function("install_host")
    assert 'if [[ ! -e "$KEY" ]]' in body and 'mv -n "$tmp" "$KEY"' in body
    assert 'id "$SVC_USER" >/dev/null 2>&1' in body  # user created only when absent
    readers = [
        line for line in body.splitlines() if '< "$KEY"' in line or 'cat "$KEY"' in line
    ]
    assert readers == [
        """  [[ "$(tr -d '\\n' < "$KEY" | wc -c)" -ge 64 ]] || { echo "key too short" >&2; exit 1; }"""
    ]
    assert (
        "usermod" not in body and "gpasswd" not in body
    )  # no permanent docker group membership
    assert (
        "sha256sum -c" in body and "/opt/p43-sandbox-bridge" in HOST_SCRIPT.read_text()
    )


def test_rollback_is_scoped_and_preserves_ledger():
    body = _script_function("rollback")
    assert 'systemctl disable --now "$SERVICE"' in body
    removed = re.findall(r"rm -f -- (\S+)", body)
    assert removed == ['"$SOCK"', '"$UNIT_DST"'] and "rm -rf" not in body
    assert "docker " not in body
    assert "$STATE" in body and "preserved" in body


def test_no_broad_docker_cleanup_anywhere():
    for path in (HOST_SCRIPT, REPO / "app/services/sandbox_bridge/host_service.py"):
        text = path.read_text()
        for forbidden in (
            "prune",
            "docker rm -f $(",
            "xargs docker",
            "docker kill $(",
            "rm -rf /",
            "docker stop $(",
        ):
            assert forbidden not in text, (path.name, forbidden)


# 13. No Docker socket exposure and 14. no unintended Host mounts
def test_compose_fragment_exposes_only_socket_dir_and_key_read_only():
    volumes = yaml.safe_load(COMPOSE.read_text())["services"]["ai-agent"]["volumes"]
    assert set(yaml.safe_load(COMPOSE.read_text())["services"]) == {"ai-agent"}
    assert [(v["source"], v["target"]) for v in volumes] == [
        ("/var/lib/p43-sandbox-bridge/socket", "/run/p43-sandbox-bridge"),
        ("/etc/p43-sandbox-bridge/bridge.key", "/run/secrets/p43-sandbox-bridge.key"),
    ]
    for v in volumes:
        assert (
            v["type"] == "bind"
            and v["read_only"] is True
            and v["bind"] == {"create_host_path": False}
        )
    text = COMPOSE.read_text()
    for forbidden in (
        "docker.sock",
        "privileged",
        "/ledger",
        "/staging",
        "group_add",
        "cap_add",
        "user:",
    ):
        assert forbidden not in "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )


def test_client_paths_match_compose_and_unit():
    from app.services.sandbox_bridge import host_service

    volumes = yaml.safe_load(COMPOSE.read_text())["services"]["ai-agent"]["volumes"]
    assert str(host_service.CLIENT_SOCKET.parent) == volumes[0]["target"]
    assert str(host_service.CLIENT_KEY) == volumes[1]["target"]
    assert str(host_service.STATE_DIR / "socket") == volumes[0]["source"]
    assert (
        "p43-sandbox-bridge" in UNIT.read_text()
        and volumes[1]["source"] in UNIT.read_text()
    )


# 15. No production execution routing
def test_bridge_is_not_imported_by_production_code():
    hits = subprocess.run(
        [
            "grep",
            "-rlE",
            r"sandbox_bridge|host_service",
            "--include=*.py",
            str(REPO / "app"),
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    production = [
        h
        for h in hits
        if "/app/tests/" not in h and "/app/services/sandbox_bridge/" not in h
    ]
    # C2-C2B: the only permitted production caller is the disposable-mode
    # Gateway (inert unless WORKER_BRIDGE_MODE=c2b-sandbox).
    assert production == [
        str(REPO / "app/services/orchestration/execution/sandbox_gateway.py")
    ]


# 16. Stage A contract compatibility: the injected executor sees the unchanged wire contract.
def test_service_preserves_stage_a_contract(tmp_path):
    seen = []
    config = _state(tmp_path)

    def capture(req, cancel):
        seen.append(req)
        return ExecutionResult(req.request_id, "SUCCESS", 0, "2\n", "")

    running = Running(config, capture)
    try:
        req = _request()
        result = _client(config).execute(req)
    finally:
        running.close()
    assert (
        seen == [req] and result.request_id == req.request_id and result.stdout == "2\n"
    )
    assert seen[0].execution_profile == PROFILE


# Section 7 client check, end to end against an injected executor (Host run uses real Docker).
@root_only
def test_client_check_reports_pass_without_leaking_key(tmp_path):
    config = _state(tmp_path)
    secret = config.key_path.read_text().strip()

    def echo(req, cancel):
        assert req.code == "print(1 + 1)\n" and req.task_id == "c1-connectivity"
        return ExecutionResult(req.request_id, "SUCCESS", 0, "2\n", "")

    running = Running(config, echo)
    try:
        report = client_check(config.socket_path, config.key_path)
    finally:
        running.close()
    assert report["verdict"] == "PASS" and all(report["checks"].values()), report
    assert (
        report["bad_hmac"] == ["REJECTED", "invalid_authentication"]
        and report["nonroot_probe"] == "EACCES"
    )
    assert secret not in json.dumps(report)


def test_client_check_fails_when_launcher_unavailable(tmp_path):
    config = _state(tmp_path)
    with pytest.raises(OSError):
        client_check(config.socket_path, config.key_path)
