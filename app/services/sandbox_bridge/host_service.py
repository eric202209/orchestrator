"""Host Launcher service entrypoint (C1 bootstrap); not imported by production code.

Run from an installed root-owned copy: ``python3 -B -E -s -m sandbox_bridge.host_service serve``.
The HMAC key is read from a file (systemd credential), never argv/env/logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import secrets

from .contract import PROFILE, ExecutionRequest, ExecutionResult
from .docker_executor import IMAGE, DockerExecutor
from .transport import HostSocketLauncher, SandboxClient, _mac, _receive, _send

STATE_DIR = Path("/var/lib/p43-sandbox-bridge")
SOCKET_NAME = "launcher.sock"
CLIENT_SOCKET = Path("/run/p43-sandbox-bridge") / SOCKET_NAME
CLIENT_KEY = Path("/run/secrets/p43-sandbox-bridge.key")
MAX_ACTIVE = 4
STOP_GRACE_SECONDS = 20
_CONTAINER = re.compile(r"^p43-sbx-[0-9a-f]{32}$")
_STAGING_ENTRY = re.compile(r"^request-[A-Za-z0-9_]{8}$")
log = logging.getLogger("p43-sandbox-bridge")


class BootstrapError(RuntimeError):
    """Fail-closed service precondition failure; message never contains secrets."""


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path
    allowed_uid: int
    key_path: Path
    docker: str = "docker"
    max_active: int = MAX_ACTIVE

    @property
    def socket_dir(self) -> Path:
        return self.state_dir / "socket"

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / SOCKET_NAME

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger" / "ledger.sqlite3"

    @property
    def staging_root(self) -> Path:
        return self.state_dir / "staging"


def load_key(path: str | Path) -> bytes:
    """Read a hex key from an owner-only regular file; never echo its contents."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise BootstrapError("missing_secret") from None
    except OSError:
        raise BootstrapError("unreadable_secret") from None
    try:
        st = os.fstat(fd)
        credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        systemd_credential = bool(
            credentials_dir
            and os.path.abspath(os.fspath(Path(path).parent))
            == os.path.abspath(credentials_dir)
            and st.st_uid == 0
            and st.st_gid == 0
            and stat.S_IMODE(st.st_mode) == 0o440
        )
        if not stat.S_ISREG(st.st_mode) or (
            (st.st_mode & 0o077 or st.st_uid != os.geteuid()) and not systemd_credential
        ):
            raise BootstrapError("unsafe_secret_permissions")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        key = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        raise BootstrapError("invalid_secret_format") from None
    if len(key) < 32:
        raise BootstrapError("secret_too_short")
    return key


def _private_dir(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise BootstrapError(f"missing_directory:{path.name}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise BootstrapError(f"unsafe_directory:{path.name}")


def check_directories(config: ServiceConfig) -> None:
    """Socket, ledger and staging must be separate, owner-only, non-symlink directories."""
    _private_dir(config.state_dir)
    for path in (config.socket_dir, config.ledger_path.parent, config.staging_root):
        _private_dir(path)


def remove_stale_socket(path: Path) -> bool:
    """Unlink a dead socket left by a crash; refuse anything else."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
        raise BootstrapError("refusing_unsafe_socket_path")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        pass
    except OSError:
        raise BootstrapError("socket_state_unknown") from None
    else:
        raise BootstrapError("launcher_already_listening")
    finally:
        probe.close()
    after = os.lstat(path)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise BootstrapError("socket_path_replaced")
    os.unlink(path)
    return True


def check_socket_dir_contents(config: ServiceConfig) -> None:
    # This directory is the only Host path exposed to ai-agent: it must hold nothing else.
    extra = [name for name in os.listdir(config.socket_dir) if name != SOCKET_NAME]
    if extra:
        raise BootstrapError("unexpected_socket_directory_entry")


def recover_orphans(config: ServiceConfig) -> dict[str, int]:
    """Scoped crash recovery: only exact bridge container names on the pinned image,
    and only request-* staging directories. Never broad Docker cleanup."""
    listed = subprocess.run(
        [
            config.docker,
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            "name=^/p43-sbx-",
            "--format",
            "{{.Names}}\t{{.Image}}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    if listed.returncode != 0:
        raise BootstrapError("docker_unavailable")
    removed = skipped = 0
    for line in listed.stdout.splitlines():
        name, _, image = line.partition("\t")
        if not _CONTAINER.fullmatch(name) or image != IMAGE:
            skipped += 1
            continue
        rm = subprocess.run(
            [config.docker, "rm", "-f", name],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        if rm.returncode != 0:
            raise BootstrapError("orphan_container_removal_failed")
        removed += 1
    staging_removed = staging_failed = 0
    for name in os.listdir(config.staging_root):
        path = config.staging_root / name
        st = os.lstat(path)
        if (
            not _STAGING_ENTRY.fullmatch(name)
            or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
        ):
            staging_failed += 1
            continue
        try:
            shutil.rmtree(path)
            staging_removed += 1
        except OSError:
            # Sandbox-owned subdirectories are not removable by the Launcher identity.
            staging_failed += 1
    return {
        "containers_removed": removed,
        "containers_skipped": skipped,
        "staging_removed": staging_removed,
        "staging_left": staging_failed,
    }


class BoundedExecutor:
    """Caps concurrent sandboxes; lets shutdown wait for request-owned cleanup."""

    def __init__(self, execute, max_active: int):
        self._execute, self._max = execute, max_active
        self._active = 0
        self._cond = threading.Condition()

    def __call__(
        self, request: ExecutionRequest, cancel: threading.Event
    ) -> ExecutionResult:
        with self._cond:
            if self._active >= self._max:
                log.info(
                    "request=%s status=REJECTED error=launcher_busy", request.request_id
                )
                return ExecutionResult(
                    request.request_id,
                    "REJECTED",
                    None,
                    "",
                    "",
                    error_code="launcher_busy",
                )
            self._active += 1
        try:
            result = self._execute(request, cancel)
            log.info(
                "request=%s status=%s exit=%s error=%s duration=%.2f",
                request.request_id,
                result.execution_status,
                result.exit_code,
                result.error_code,
                result.duration,
            )
            return result
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._active == 0, timeout)


def serve(config: ServiceConfig, stop: threading.Event, *, executor=None) -> int:
    key = load_key(config.key_path)
    check_directories(config)
    stale = remove_stale_socket(config.socket_path)
    check_socket_dir_contents(config)
    recovered = recover_orphans(config) if executor is None else {}
    log.info(
        "startup stale_socket_removed=%s recovery=%s allowed_uid=%d max_active=%d",
        stale,
        json.dumps(recovered, sort_keys=True),
        config.allowed_uid,
        config.max_active,
    )
    bounded = BoundedExecutor(
        executor or DockerExecutor(config.staging_root, docker=config.docker).execute,
        config.max_active,
    )
    launcher = HostSocketLauncher(
        config.socket_path, key, config.allowed_uid, bounded, config.ledger_path
    )
    try:
        launcher.serve_forever(stop)
    finally:
        # serve_forever has signalled cancellation; wait for container/staging cleanup.
        idle = bounded.wait_idle(STOP_GRACE_SECONDS)
        log.info("stopped idle=%s", idle)
    return 0 if idle else 1


def status(config: ServiceConfig) -> dict[str, object]:
    """Bounded, secret-free status. Run as the service user (read-only ledger access)."""
    out: dict[str, object] = {}
    try:
        st = os.lstat(config.socket_path)
        out["socket"] = {
            "present": stat.S_ISSOCK(st.st_mode),
            "mode": oct(stat.S_IMODE(st.st_mode)),
            "uid": st.st_uid,
            "gid": st.st_gid,
        }
    except FileNotFoundError:
        out["socket"] = {"present": False}
    try:
        db = sqlite3.connect(f"file:{config.ledger_path}?mode=ro", uri=True, timeout=5)
        try:
            out["ledger"] = dict(
                db.execute(
                    "SELECT status, COUNT(*) FROM sandbox_requests GROUP BY status"
                )
            )
        finally:
            db.close()
    except sqlite3.Error as exc:
        out["ledger"] = {"error": type(exc).__name__}
    try:
        out["staging_entries"] = len(os.listdir(config.staging_root))
    except OSError as exc:
        out["staging_entries"] = {"error": type(exc).__name__}
    return out


def raw_execute(
    socket_path: Path, key: bytes, request: ExecutionRequest
) -> dict[str, object]:
    """Send one request and return the raw result frame. Pre-parse rejections carry
    request_id 'invalid', which SandboxClient (correctly) refuses as uncorrelated."""
    wire = request.to_wire()
    payload = json.dumps(wire, separators=(",", ":"), sort_keys=True).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(socket_path))
        _send(
            sock, {"operation": "execute", "request": wire, "mac": _mac(key, payload)}
        )
        return _receive(sock)


def client_check(socket_path: Path, key_path: Path) -> dict[str, object]:
    """Trusted synthetic connectivity check from ai-agent. Fixed code; no Task/Session state."""
    key = load_key(key_path)

    def req() -> ExecutionRequest:
        return ExecutionRequest(
            str(uuid.uuid4()),
            "c1-connectivity",
            "c1-connectivity",
            str(uuid.uuid4()),
            PROFILE,
            "print(1 + 1)\n",
            30,
            int(time.time()),
            secrets.token_hex(32),
        )

    good = req()
    result = SandboxClient(socket_path, key, connect_timeout=5).execute(good)
    bad = raw_execute(socket_path, secrets.token_bytes(32), req())
    probe = (
        "import socket,sys\ns=socket.socket(socket.AF_UNIX)\n"
        "try:\n s.connect(sys.argv[1]); print('connected')\nexcept PermissionError: print('EACCES')\n"
        "except OSError as e: print(type(e).__name__)\n"
    )
    nonroot = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", probe, str(socket_path)],
        user=65534,
        group=65534,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    checks = {
        "authenticated_success": result.execution_status == "SUCCESS"
        and result.exit_code == 0,
        "output_is_2": result.stdout == "2\n",
        "request_id_correlated": result.request_id == good.request_id,
        "no_artifacts": not result.artifacts,
        "bad_hmac_rejected": (bad.get("execution_status"), bad.get("error_code"))
        == ("REJECTED", "invalid_authentication"),
        "nonroot_peer_denied": nonroot.stdout.strip() == "EACCES",
    }
    return {
        "request_id": good.request_id,
        "status": result.execution_status,
        "exit_code": result.exit_code,
        "stdout": result.stdout[:64],
        "error_code": result.error_code,
        "duration": round(result.duration, 3),
        "bad_hmac": [bad.get("execution_status"), bad.get("error_code")],
        "nonroot_probe": nonroot.stdout.strip()[:32],
        "checks": checks,
        "verdict": "PASS" if all(checks.values()) else "FAIL",
    }


def _config_from_env() -> ServiceConfig:
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds:
        raise BootstrapError("missing_credentials_directory")
    try:
        uid = int(os.environ["P43_SANDBOX_BRIDGE_ALLOWED_UID"])
    except (KeyError, ValueError):
        raise BootstrapError("missing_allowed_uid") from None
    return ServiceConfig(STATE_DIR, uid, Path(creds) / "bridge.key")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    command = args[0] if args else ""
    try:
        if command == "serve":
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            return serve(_config_from_env(), stop)
        if command == "status":
            print(
                json.dumps(
                    status(ServiceConfig(STATE_DIR, -1, Path("/nonexistent"))),
                    sort_keys=True,
                )
            )
            return 0
        if command == "client-check":
            report = client_check(CLIENT_SOCKET, CLIENT_KEY)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["verdict"] == "PASS" else 1
    except BootstrapError as exc:
        log.error("bootstrap_error=%s", exc)
        return 2
    print("usage: host_service {serve|status|client-check}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
