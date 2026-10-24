#!/usr/bin/env python3
"""Project-scoped process identity and graceful shutdown helpers.

Each managed process is recorded under its own checkout's ``run`` directory.
Signals are sent only after the saved process start time, executable, argv,
working directory, and canonical source root still match. Linux pidfds close
the PID-reuse race between validation and signal delivery.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import select
import signal
import socket
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
SERVICES = (
    "backend",
    "worker",
    "worker-candidate",
    "beat",
    "frontend",
    "qdrant",
    "redis",
)
LEGACY_PID_FILES = {
    "backend": "backend.pid",
    "worker": "worker.pid",
    "worker-candidate": "worker-candidate.pid",
    "beat": "beat.pid",
    "frontend": "frontend.pid",
    "qdrant": "qdrant.pid",
    "redis": "redis.pid",
}


class ProcessIdentityError(RuntimeError):
    """The saved identity is missing, ambiguous, stale, or does not match."""


class PortConflict(RuntimeError):
    """The requested TCP port is already occupied."""


@dataclass(frozen=True)
class ProcessIdentity:
    schema_version: int
    service: str
    pid: int
    start_ticks: int
    executable: str
    argv: tuple[str, ...]
    cwd: str
    source_root: str


@dataclass(frozen=True)
class ProcSnapshot:
    pid: int
    start_ticks: int
    executable: str
    argv: tuple[str, ...]
    cwd: str


def canonical_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ProcessIdentityError(f"project root cannot be resolved: {root}") from exc
    if not resolved.is_dir():
        raise ProcessIdentityError(f"project root is not a directory: {resolved}")
    return resolved


def identity_path(root: Path, service: str) -> Path:
    if service not in SERVICES:
        raise ProcessIdentityError(f"unknown managed service: {service}")
    return root / "run" / f"{service}.process.json"


def expected_cwd(root: Path, service: str) -> Path:
    if service == "frontend":
        return root / "frontend"
    if service == "qdrant":
        return root / "qdrant"
    return root


def _parse_proc_stat(raw: str) -> int:
    # comm is parenthesized and can itself contain spaces or closing parens.
    close = raw.rfind(")")
    if close < 0:
        raise ProcessIdentityError("process start identity is malformed")
    fields = raw[close + 1 :].split()  # begins with field 3 (state)
    if len(fields) <= 19:
        raise ProcessIdentityError("process start identity is incomplete")
    try:
        start_ticks = int(fields[19])  # field 22
    except ValueError as exc:
        raise ProcessIdentityError("process start identity is malformed") from exc
    return start_ticks


def read_process(pid: int, proc_root: Path = Path("/proc")) -> ProcSnapshot:
    proc_dir = proc_root / str(pid)
    try:
        stat = (proc_dir / "stat").read_text()
        start_ticks = _parse_proc_stat(stat)
        raw_argv = (proc_dir / "cmdline").read_bytes()
        argv = tuple(os.fsdecode(arg) for arg in raw_argv.split(b"\0") if arg)
        executable = str(Path(os.readlink(proc_dir / "exe")).resolve(strict=True))
        cwd = str(Path(os.readlink(proc_dir / "cwd")).resolve(strict=True))
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise ProcessIdentityError(f"process {pid} is not running") from exc
    except OSError as exc:
        raise ProcessIdentityError(f"process {pid} identity cannot be read: {exc}") from exc
    if not argv:
        raise ProcessIdentityError(f"process {pid} has no observable argv")
    return ProcSnapshot(
        pid=pid,
        start_ticks=start_ticks,
        executable=executable,
        argv=argv,
        cwd=cwd,
    )


def _service_argv_matches(service: str, snapshot: ProcSnapshot, root: Path) -> bool:
    args = snapshot.argv
    if service == "backend":
        return "app.main:app" in args and any("uvicorn" in arg for arg in args)
    if service in {"worker", "worker-candidate"}:
        return (
            "app.celery_app" in args
            and "worker" in args
            and "-A" in args
            and any(str(root) in arg or arg == "app.celery_app" for arg in args)
        )
    if service == "beat":
        return "app.celery_app" in args and "beat" in args and "-A" in args
    if service == "frontend":
        return "dev" in args and any("pnpm" in arg or "vite" in arg for arg in args)
    if service == "qdrant":
        qdrant_bin = (root / "qdrant" / "bin" / "qdrant").resolve()
        return snapshot.executable == str(qdrant_bin) or str(qdrant_bin) in args
    if service == "redis":
        return Path(snapshot.executable).name.startswith("redis-server") or any(
            Path(arg).name == "redis-server" for arg in args
        )
    return False


def _identity_from_snapshot(
    *, service: str, root: Path, snapshot: ProcSnapshot
) -> ProcessIdentity:
    target_cwd = expected_cwd(root, service).resolve(strict=True)
    actual_cwd = Path(snapshot.cwd).resolve(strict=True)
    try:
        actual_cwd.relative_to(root)
    except ValueError as exc:
        raise ProcessIdentityError(
            f"process {snapshot.pid} CWD is outside project root {root}"
        ) from exc
    if actual_cwd != target_cwd:
        raise ProcessIdentityError(
            f"process {snapshot.pid} CWD does not match {service} project directory"
        )
    if not _service_argv_matches(service, snapshot, root):
        raise ProcessIdentityError(
            f"process {snapshot.pid} executable/argv does not match service {service}"
        )
    return ProcessIdentity(
        schema_version=SCHEMA_VERSION,
        service=service,
        pid=snapshot.pid,
        start_ticks=snapshot.start_ticks,
        executable=snapshot.executable,
        argv=snapshot.argv,
        cwd=snapshot.cwd,
        source_root=str(root),
    )


def _record_from_json(path: Path) -> ProcessIdentity:
    if path.is_symlink() or not path.is_file():
        raise ProcessIdentityError(f"process identity record is missing or unsafe: {path}")
    try:
        data = json.loads(path.read_text())
        identity = ProcessIdentity(
            schema_version=data["schema_version"],
            service=data["service"],
            pid=data["pid"],
            start_ticks=data["start_ticks"],
            executable=data["executable"],
            argv=tuple(data["argv"]),
            cwd=data["cwd"],
            source_root=data["source_root"],
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessIdentityError(f"process identity record is invalid: {path}") from exc
    if identity.schema_version != SCHEMA_VERSION or identity.service not in SERVICES:
        raise ProcessIdentityError(f"process identity record has unsupported schema: {path}")
    if (
        not isinstance(identity.pid, int)
        or isinstance(identity.pid, bool)
        or not isinstance(identity.start_ticks, int)
        or isinstance(identity.start_ticks, bool)
        or identity.pid <= 0
        or identity.start_ticks <= 0
        or not isinstance(identity.executable, str)
        or not identity.executable
        or not isinstance(identity.cwd, str)
        or not isinstance(identity.source_root, str)
    ):
        raise ProcessIdentityError(f"process identity record is incomplete: {path}")
    if not identity.argv or any(not isinstance(arg, str) for arg in identity.argv):
        raise ProcessIdentityError(f"process identity argv is invalid: {path}")
    return identity


def _read_verified_identity(
    root: Path, service: str, proc_root: Path = Path("/proc")
) -> tuple[Path, ProcessIdentity, ProcSnapshot]:
    root = canonical_root(root)
    path = identity_path(root, service)
    identity = _record_from_json(path)
    if identity.service != service:
        raise ProcessIdentityError("process identity record belongs to another service")
    if identity.source_root != str(root):
        raise ProcessIdentityError(
            f"process identity root mismatch: {identity.source_root} != {root}"
        )
    snapshot = read_process(identity.pid, proc_root)
    if snapshot.start_ticks != identity.start_ticks:
        raise ProcessIdentityError(f"PID {identity.pid} was reused; refusing to signal")
    if snapshot.executable != identity.executable:
        raise ProcessIdentityError(f"process {identity.pid} executable changed")
    if snapshot.argv != identity.argv:
        raise ProcessIdentityError(f"process {identity.pid} argv changed")
    if snapshot.cwd != identity.cwd:
        raise ProcessIdentityError(f"process {identity.pid} CWD changed")
    _identity_from_snapshot(service=service, root=root, snapshot=snapshot)
    return path, identity, snapshot


def record_process(
    root: Path,
    service: str,
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    timeout: float = 3.0,
) -> ProcessIdentity:
    root = canonical_root(root)
    path = identity_path(root, service)
    if path.exists() or path.is_symlink():
        raise ProcessIdentityError(f"identity record already exists: {path}")
    deadline = time.monotonic() + timeout
    last_error: ProcessIdentityError | None = None
    while True:
        try:
            snapshot = read_process(pid, proc_root)
            identity = _identity_from_snapshot(
                service=service, root=root, snapshot=snapshot
            )
            break
        except ProcessIdentityError as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(0.05)

    try:
        path.parent.mkdir(mode=0o777)
    except FileExistsError:
        pass
    else:
        os.chmod(path.parent, 0o777)
    temp = path.with_name(f".{path.name}.{pid}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o666)
    try:
        os.fchmod(fd, 0o666)
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(identity) | {"argv": list(identity.argv)}, stream, indent=2)
            stream.write("\n")
        os.link(temp, path, follow_symlinks=False)
        temp.unlink()
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return identity


def process_status(root: Path, service: str, *, proc_root: Path = Path("/proc")) -> bool:
    path = identity_path(canonical_root(root), service)
    if not path.exists() and not path.is_symlink():
        legacy = LEGACY_PID_FILES.get(service)
        if legacy and (path.parent / legacy).exists():
            raise ProcessIdentityError(
                f"legacy PID file has no identity record; no process adopted: {path.parent / legacy}"
            )
        return False
    _read_verified_identity(root, service, proc_root)
    return True


def _send_sigterm(pidfd: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise ProcessIdentityError("pidfd signaling is unavailable; refusing to signal")
    sender(pidfd, signal.SIGTERM, None, 0)


def _wait_pidfd(pidfd: int, timeout: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(max(0, int(timeout * 1000))))


def _remove_identity(path: Path, expected: ProcessIdentity) -> None:
    identity = _record_from_json(path)
    if identity != expected:
        raise ProcessIdentityError("process identity record changed during shutdown")
    path.unlink()
    legacy = LEGACY_PID_FILES.get(identity.service)
    if legacy:
        legacy_path = path.parent / legacy
        try:
            if legacy_path.is_file() and not legacy_path.is_symlink():
                legacy_path.unlink()
        except FileNotFoundError:
            pass


def stop_process(
    root: Path,
    service: str,
    *,
    timeout: float = 30.0,
    proc_root: Path = Path("/proc"),
    pidfd_open=None,
    signal_sender=None,
    waiter=None,
) -> bool:
    root = canonical_root(root)
    path = identity_path(root, service)
    if not path.exists() and not path.is_symlink():
        legacy = LEGACY_PID_FILES.get(service)
        if legacy and (root / "run" / legacy).exists():
            raise ProcessIdentityError(
                f"legacy PID file has no identity record; no signal sent: {root / 'run' / legacy}"
            )
        raise ProcessIdentityError(f"process identity is missing for {service}; no signal sent")

    identity = _record_from_json(path)
    if identity.service != service:
        raise ProcessIdentityError("process identity service mismatch; no signal sent")
    if identity.source_root != str(root):
        raise ProcessIdentityError("process identity root mismatch; no signal sent")
    try:
        _read_verified_identity(root, service, proc_root)
    except ProcessIdentityError as exc:
        if str(exc).endswith("is not running"):
            _remove_identity(path, identity)
            return False
        raise
    if pidfd_open is None:
        pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None:
        raise ProcessIdentityError("pidfds are unavailable; refusing to signal")
    if signal_sender is None:
        signal_sender = _send_sigterm
    if waiter is None:
        waiter = _wait_pidfd
    try:
        pidfd = pidfd_open(identity.pid, 0)
    except ProcessLookupError:
        # A confirmed absent PID cannot refer to a reused process. Retire its
        # dead identity record without sending any signal.
        _remove_identity(path, identity)
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            _remove_identity(path, identity)
            return False
        raise ProcessIdentityError(f"cannot open pidfd for {identity.pid}: {exc}") from exc

    try:
        # Re-read after opening the pidfd. A reused numeric PID fails the
        # start-time comparison and the pidfd still pins the process we opened.
        _read_verified_identity(root, service, proc_root)
        signal_sender(pidfd)
        if not waiter(pidfd, timeout):
            raise ProcessIdentityError(
                f"process {identity.pid} did not exit after SIGTERM; no SIGKILL sent"
            )
    finally:
        os.close(pidfd)
    _remove_identity(path, identity)
    return True


def stop_all(root: Path, services: Iterable[str] = SERVICES, timeout: float = 30.0) -> list[str]:
    root = canonical_root(root)
    stopped: list[str] = []
    for service in services:
        if service not in SERVICES:
            raise ProcessIdentityError(f"unknown managed service: {service}")
        record = identity_path(root, service)
        legacy = LEGACY_PID_FILES.get(service)
        legacy_path = root / "run" / legacy if legacy else None
        if not record.exists() and not record.is_symlink():
            if legacy_path is not None and legacy_path.exists():
                raise ProcessIdentityError(
                    f"legacy PID file has no identity record; no signal sent: {legacy_path}"
                )
            continue
        if stop_process(root, service, timeout=timeout):
            stopped.append(service)
    return stopped


def require_port_available(host: str, port: int) -> None:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as exc:
        raise PortConflict(f"cannot resolve bind address {host}:{port}: {exc}") from exc
    last_error: OSError | None = None
    bound_any = False
    in_use = False
    for family, socktype, proto, _, address in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.bind(address)
            bound_any = True
        except OSError as exc:
            last_error = exc
            if exc.errno == errno.EADDRINUSE:
                in_use = True
        finally:
            sock.close()
    if in_use:
        raise PortConflict(f"port {host}:{port} is occupied; no process was signalled")
    if bound_any:
        return
    if last_error is not None:
        raise PortConflict(f"cannot bind {host}:{port}: {last_error}") from last_error
    raise PortConflict(f"no bindable address resolved for {host}:{port}")


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("record", "status", "stop"):
        child = sub.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--service", choices=SERVICES, required=True)
        if command == "record":
            child.add_argument("--pid", type=int, required=True)
        if command == "stop":
            child.add_argument("--timeout", type=float, default=30.0)
    stop_parser = sub.add_parser("stop-all")
    stop_parser.add_argument("--root", type=Path, required=True)
    stop_parser.add_argument("--timeout", type=float, default=30.0)
    port_parser = sub.add_parser("port-check")
    port_parser.add_argument("--host", required=True)
    port_parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "record":
            identity = record_process(args.root, args.service, args.pid)
            print(f"recorded {identity.service} PID {identity.pid} for {identity.source_root}")
        elif args.command == "status":
            if not process_status(args.root, args.service):
                return 1
            print(f"verified {args.service} process identity")
        elif args.command == "stop":
            stop_process(args.root, args.service, timeout=args.timeout)
            print(f"stopped verified {args.service} process")
        elif args.command == "stop-all":
            stopped = stop_all(args.root, timeout=args.timeout)
            print("stopped verified project processes" + (f": {', '.join(stopped)}" if stopped else ": none"))
        elif args.command == "port-check":
            require_port_available(args.host, args.port)
    except (ProcessIdentityError, PortConflict, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
