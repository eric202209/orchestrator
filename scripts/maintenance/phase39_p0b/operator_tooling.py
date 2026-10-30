#!/usr/bin/env python3
"""Offline-testable operator tooling for the Phase 39 P0B activation gate.

The module lives in the tracked maintenance package. Live operations are
available only through the protected release entry point; tests inject process,
launcher, health, and loader observations and never contact a running service.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .protected_refresh_authority import (
    DEFAULT_AUTHORITY_ROOT,
    REFRESH_RECEIPT_SCHEMA_VERSION as AUTHORITY_RECEIPT_SCHEMA_VERSION,
    RefreshAuthority,
    RefreshAuthorityError,
)
from .worker_pidfd import PidfdError, PidfdOperations, UnsupportedPidfdError


APPROVED_SOURCE_ROOT = "/nonexistent/phase39-p0b-source"
# Runtime approval is populated only by the protected signed-release entry
# point. Source-tree defaults are deliberately unusable and carry no historical
# approval identity.
APPROVED_HEAD = "0" * 40
APPROVED_TREE = "0" * 40
APPROVED_SOURCE_MANIFEST_DIGEST = "0" * 64
APPROVED_CONFIGURATION_FINGERPRINT = "0" * 64
APPROVED_CONFIGURATION_SCHEMA_VERSION = 2
APPROVED_CONFIGURATION_POLICY_ID = "UNAPPROVED"
APPROVED_WORKER_NODE = "UNAPPROVED"
APPROVED_OPERATOR = "UNAPPROVED"
APPROVED_APPROVAL_ID = "UNAPPROVED"
PREVIOUS_APPROVAL_ID = "P39-P0B-GATEB-001"
APPROVED_WORKSHEET_SHA256 = "0" * 64
APPROVED_MODULE_COUNT = 158
INSTALL_RECEIPT_SCHEMA_VERSION = 1
REFRESH_RECEIPT_SCHEMA_VERSION = AUTHORITY_RECEIPT_SCHEMA_VERSION
DEFAULT_PIN_PATH = "/root/.orchestrator/worker-pin/candidate-pin.json"
DEFAULT_RUNTIME_ROOT = "/root/.orchestrator"
DEFAULT_REFRESH_AUTHORITY_ROOT = DEFAULT_AUTHORITY_ROOT
PIN_FILE_NAME = "candidate-pin.json"
HISTORY_FILE_NAME = "candidate-pin.P39-P0B-GATEB-001.json"
INSTALL_TEMP_NAME = ".candidate-pin.install.tmp"
MAX_WORKSHEET_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
FRESH_EVIDENCE_SECONDS = 60
MAX_REFRESH_RECEIPT_AGE_SECONDS = 3600
SHUTDOWN_TIMEOUT_SECONDS = 90
WORKER_MATCH_ARGS = ("-A", "app.celery_app", "worker")
BEAT_MATCH_ARGS = ("-A", "app.celery_app", "beat")
BEAT_ROLE = "celery-beat"


class ToolingError(RuntimeError):
    """A closed, user-safe tooling failure; messages never include pin bytes."""


def _pin_owner() -> tuple[int, int]:
    """Return the production pin owner. Tests replace this at the fixture seam."""
    return 0, 0


class RecoveryRequiredError(ToolingError):
    """A launch was attempted or became ambiguous; never retry automatically."""

    def __init__(self, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.result = dict(result)


@dataclass(frozen=True)
class ApprovedIdentity:
    source_root: str
    candidate_head: str = APPROVED_HEAD
    candidate_tree: str = APPROVED_TREE
    source_manifest_digest: str = APPROVED_SOURCE_MANIFEST_DIGEST
    configuration_fingerprint: str = APPROVED_CONFIGURATION_FINGERPRINT
    configuration_schema_version: int = APPROVED_CONFIGURATION_SCHEMA_VERSION
    configuration_policy_id: str = APPROVED_CONFIGURATION_POLICY_ID
    worker_node_identity: str = APPROVED_WORKER_NODE
    operator_identity: str = APPROVED_OPERATOR
    approval_id: str = APPROVED_APPROVAL_ID
    worksheet_sha256: str = APPROVED_WORKSHEET_SHA256
    affected_module_count: int = APPROVED_MODULE_COUNT
    affected_module_names: tuple[str, ...] = ()

    @property
    def launcher_identity(self) -> dict[str, Any]:
        root = str(Path(self.source_root))
        return {
            "launcher": "start.sh:start_worker_only",
            "executable": "/usr/bin/python3.12",
            "cwd": root,
            "argv": [
                f"{root}/venv/bin/python3",
                f"{root}/venv/bin/celery",
                "-A",
                "app.celery_app",
                "worker",
                "--concurrency=4",
                "--loglevel=info",
            ],
        }


APPROVED = ApprovedIdentity(source_root=APPROVED_SOURCE_ROOT)


@dataclass(frozen=True)
class DeploymentProgress:
    installation: str = "NOT_RUN"
    launch: str = "NOT_RUN"
    process_observation: str = "NOT_RUN"
    identity_verification: str = "NOT_RUN"
    readiness: str = "NOT_RUN"
    gate_a: str = "NOT_RUN"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: str
    linux_boot_id: str
    executable: str
    cwd: str
    argv: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProcessIdentity":
        required = {
            "pid",
            "start_ticks",
            "linux_boot_id",
            "executable",
            "cwd",
            "argv",
        }
        if not isinstance(value, Mapping) or not required <= set(value):
            raise ToolingError("process identity evidence is incomplete")
        argv = value["argv"]
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
            raise ToolingError("process argv evidence is invalid")
        try:
            identity = cls(
                pid=int(value["pid"]),
                start_ticks=str(value["start_ticks"]),
                linux_boot_id=str(value["linux_boot_id"]),
                executable=str(value["executable"]),
                cwd=str(value["cwd"]),
                argv=tuple(str(item) for item in argv),
            )
        except (TypeError, ValueError) as exc:
            raise ToolingError("process identity evidence is invalid") from exc
        if (
            identity.pid <= 1
            or not identity.start_ticks.isdigit()
            or int(identity.start_ticks) <= 0
            or not _valid_linux_boot_id(identity.linux_boot_id)
            or not Path(identity.executable).is_absolute()
            or not Path(identity.cwd).is_absolute()
            or not identity.argv
            or any(not item for item in identity.argv)
        ):
            raise ToolingError("process identity evidence is incomplete")
        return identity

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["argv"] = list(self.argv)
        return result


class ProcfsObserver:
    """Read-only Linux process observer used by the future operator CLI."""

    def __init__(self, proc_root: str | Path = "/proc") -> None:
        self.proc_root = Path(proc_root)

    def _stat_fields(self, pid: int) -> list[str] | None:
        try:
            raw = (self.proc_root / str(pid) / "stat").read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return None
        # comm is parenthesized but can itself contain spaces or parentheses.
        close = raw.rfind(")")
        if close < 0:
            return None
        return raw[close + 1 :].strip().split()

    def identity(self, pid: int) -> ProcessIdentity | None:
        fields = self._stat_fields(pid)
        if fields is None or len(fields) <= 19:
            return None
        base = self.proc_root / str(pid)
        try:
            raw_argv = (base / "cmdline").read_bytes()
            argv = tuple(
                item.decode("utf-8", "surrogateescape")
                for item in raw_argv.split(b"\0")
                if item
            )
            executable = os.readlink(base / "exe")
            cwd = os.readlink(base / "cwd")
            boot_id = (self.proc_root / "sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
            return ProcessIdentity(
                pid=pid,
                start_ticks=fields[19],
                linux_boot_id=boot_id,
                executable=executable,
                cwd=cwd,
                argv=argv,
            )
        except (OSError, UnicodeError):
            return None

    def parent_pid(self, pid: int) -> int | None:
        fields = self._stat_fields(pid)
        if fields is None or len(fields) < 2:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None

    def children(self, pid: int) -> list[ProcessIdentity]:
        result: list[ProcessIdentity] = []
        try:
            pids = [int(item.name) for item in self.proc_root.iterdir() if item.name.isdigit()]
        except OSError as exc:
            raise ToolingError("cannot enumerate process identities") from exc
        for child_pid in pids:
            if child_pid == pid or self.parent_pid(child_pid) != pid:
                continue
            identity = self.identity(child_pid)
            if identity is not None:
                result.append(identity)
        return sorted(result, key=lambda item: item.pid)

    def _matching_pids(self, required_args: tuple[str, ...]) -> list[int]:
        matches: list[int] = []
        try:
            pids = [int(item.name) for item in self.proc_root.iterdir() if item.name.isdigit()]
        except OSError as exc:
            raise ToolingError("cannot enumerate worker processes") from exc
        for pid in pids:
            identity = self.identity(pid)
            if identity is None:
                continue
            argv = identity.argv
            if any(
                tuple(argv[index : index + len(required_args)]) == required_args
                for index in range(max(0, len(argv) - len(required_args) + 1))
            ):
                matches.append(pid)
        return sorted(matches)

    def worker_match_pids(self) -> list[int]:
        return self._matching_pids(WORKER_MATCH_ARGS)

    def beat_match_pids(self) -> list[int]:
        return self._matching_pids(BEAT_MATCH_ARGS)

    def environment_value(self, pid: int, name: str) -> str | None:
        try:
            raw = (self.proc_root / str(pid) / "environ").read_bytes()
        except OSError:
            return None
        prefix = name.encode("ascii") + b"="
        for entry in raw.split(b"\0"):
            if entry.startswith(prefix):
                return entry[len(prefix) :].decode("utf-8", "surrogateescape")
        return None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ToolingError("timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _timestamp_precise(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ToolingError("timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _timestamp_ns(value: dt.datetime) -> int:
    if value.tzinfo is None:
        raise ToolingError("timestamps must be timezone-aware")
    instant = value.astimezone(dt.timezone.utc)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = instant - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _valid_linux_boot_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (TypeError, ValueError, AttributeError):
        return False


def _parse_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ToolingError("timestamp evidence is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolingError("timestamp evidence is invalid") from exc
    if parsed.tzinfo is None:
        raise ToolingError("timestamp evidence must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _read_regular_file(
    path: str | Path,
    *,
    max_bytes: int,
    required_uid: int | None = None,
    required_gid: int | None = None,
    required_mode: int | None = None,
) -> tuple[bytes, os.stat_result]:
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ToolingError("required file is missing, symlinked, or unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ToolingError("required file is not a regular file")
        if required_uid is not None and info.st_uid != required_uid:
            raise ToolingError("required file has incorrect owner")
        if required_gid is not None and info.st_gid != required_gid:
            raise ToolingError("required file has incorrect group")
        if required_mode is not None and stat.S_IMODE(info.st_mode) != required_mode:
            raise ToolingError("required file has incorrect permissions")
        if info.st_size > max_bytes:
            raise ToolingError("required file exceeds the size limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ToolingError("required file exceeds the size limit")
        return raw, info
    finally:
        os.close(fd)


def _open_directory_no_symlink(path: str | Path) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise ToolingError("pin directory path must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(absolute.anchor, flags)
    except OSError as exc:
        raise ToolingError("cannot open pin directory root") from exc
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                raise ToolingError("pin directory contains a symlink or is unreadable") from exc
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ToolingError("pin path is not a directory")
        path_info = os.lstat(absolute)
        if stat.S_ISLNK(path_info.st_mode) or (path_info.st_dev, path_info.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise ToolingError("pin directory changed during no-follow validation")
        if (info.st_uid, info.st_gid) != _pin_owner():
            raise ToolingError("pin directory must be root:root")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ToolingError("pin directory must have mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _lock_pin_directory(directory_fd: int, *, exclusive: bool) -> None:
    """Fail closed when another installer or refresh owns the pin directory."""
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(directory_fd, operation | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ToolingError("pin directory is locked by a concurrent operation") from exc
    except OSError as exc:
        raise ToolingError("pin directory lock could not be acquired") from exc


def _read_at_regular(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
    uid: int | None = None,
    gid: int | None = None,
    mode: int = 0o600,
) -> tuple[bytes, os.stat_result]:
    expected_uid, expected_gid = _pin_owner()
    uid = expected_uid if uid is None else uid
    gid = expected_gid if gid is None else gid
    try:
        path_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ToolingError("pin evidence file is missing, symlinked, or unreadable") from exc
    if stat.S_ISLNK(path_info.st_mode):
        raise ToolingError("pin evidence is a symlink")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    except OSError as exc:
        raise ToolingError("pin evidence file is missing, symlinked, or unreadable") from exc
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise ToolingError("pin evidence changed during no-follow validation")
        if not stat.S_ISREG(info.st_mode):
            raise ToolingError("pin evidence is not a regular file")
        if info.st_uid != uid or info.st_gid != gid:
            raise ToolingError("pin evidence must be root:root")
        if stat.S_IMODE(info.st_mode) != mode:
            raise ToolingError("pin evidence must have mode 0600")
        if info.st_size > max_bytes:
            raise ToolingError("pin evidence exceeds the size limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ToolingError("pin evidence exceeds the size limit")
        return raw, info
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ToolingError("short write while preparing pin")
        view = view[written:]


def _write_new_at(directory_fd: int, name: str, payload: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError as exc:
        raise ToolingError("unexpected temporary or history file already exists") from exc
    created: tuple[int, int] | None = None
    try:
        info = os.fstat(fd)
        created = (info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != _pin_owner():
            raise ToolingError("new pin evidence has incorrect type or owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ToolingError("new pin evidence must have mode 0600")
        _write_all(fd, payload)
        os.fsync(fd)
        return info.st_dev, info.st_ino
    except BaseException:
        os.close(fd)
        if created is not None:
            _unlink_own_at(directory_fd, name, *created)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _unlink_own_at(directory_fd: int, name: str, device: int, inode: int) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if current.st_dev == device and current.st_ino == inode:
        os.unlink(name, dir_fd=directory_fd)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, separators=(",", ": ")) + "\n").encode(
        "utf-8"
    )


def _decode_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolingError(f"{label} is malformed JSON") from exc
    if not isinstance(value, Mapping):
        raise ToolingError(f"{label} must be a JSON object")
    return value


def _approved_worksheet_body(
    worksheet_path: str | Path,
    policy: ApprovedIdentity,
    *,
    expected_worksheet_sha256: str,
) -> dict[str, Any]:
    raw, _ = _read_regular_file(worksheet_path, max_bytes=MAX_WORKSHEET_BYTES)
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_worksheet_sha256:
        raise ToolingError("worksheet SHA-256 does not match the approved worksheet")
    worksheet = _decode_json(raw, "worksheet")
    if (
        worksheet.get("worksheet_kind") != "orchestrator-worker-candidate-pin-worksheet"
        or worksheet.get("worksheet_schema_version") != 1
        or worksheet.get("approval_state") != "NOT_APPROVED"
        or worksheet.get("OPERATOR_APPROVED_SOURCE") is not None
    ):
        raise ToolingError("worksheet approval state or schema is unexpected")
    source = worksheet.get("OBSERVED_SOURCE")
    configuration = worksheet.get("configuration_identity")
    proposed = worksheet.get("proposed_pin_body")
    if not isinstance(source, Mapping) or not isinstance(configuration, Mapping):
        raise ToolingError("worksheet identity sections are missing")
    if not isinstance(proposed, Mapping):
        raise ToolingError("worksheet proposed pin body is missing")
    expected = {
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
    }
    for key, value in expected.items():
        if source.get(key) != value or proposed.get(key) != value:
            raise ToolingError(f"worksheet approved identity mismatch: {key}")
    if (
        configuration.get("configuration_fingerprint")
        != policy.configuration_fingerprint
        or configuration.get("configuration_schema_version")
        != policy.configuration_schema_version
        or configuration.get("configuration_policy_id")
        != policy.configuration_policy_id
        or configuration.get("worker_concurrency") != 4
    ):
        raise ToolingError("worksheet configuration identity mismatch")
    launcher = policy.launcher_identity
    if proposed.get("launcher_identity") != launcher:
        raise ToolingError("worksheet approved identity mismatch: launcher_identity")
    if proposed.get("worker_node_identity") != policy.worker_node_identity:
        raise ToolingError("worksheet approved identity mismatch: worker_node_identity")
    if (
        proposed.get("configuration_fingerprint") != policy.configuration_fingerprint
        or proposed.get("configuration_schema_version")
        != policy.configuration_schema_version
        or proposed.get("configuration_policy_id") != policy.configuration_policy_id
    ):
        raise ToolingError("worksheet approved configuration mismatch")
    if proposed.get("approval") is not None:
        raise ToolingError("worksheet must not contain a pre-existing approval")
    required = {
        "pin_schema_version",
        "candidate_head",
        "candidate_tree",
        "source_root",
        "source_manifest_digest",
        "launcher_identity",
        "worker_node_identity",
        "configuration_schema_version",
        "configuration_fingerprint",
        "configuration_policy_id",
        "approval",
    }
    if set(proposed) != required or proposed.get("pin_schema_version") != 1:
        raise ToolingError("worksheet pin schema is unexpected")
    if worksheet.get("derivation", {}).get("affected_module_count") != policy.affected_module_count:
        raise ToolingError("worksheet affected-module count mismatch")
    return dict(proposed)


def production_pin_loader(
    pin_path: str | Path, *, policy: ApprovedIdentity, runtime_root: str | Path
) -> Mapping[str, Any]:
    """Invoke the production boot loader without exposing pin contents."""
    source_root = str(Path(policy.source_root).resolve())
    added_source_root = source_root not in sys.path
    if added_source_root:
        sys.path.insert(0, source_root)
    try:
        from app.services.orchestration.worker_identity import (
            PIN_LOADED,
            load_candidate_identity_pin,
        )
    finally:
        if added_source_root:
            try:
                sys.path.remove(source_root)
            except ValueError:
                pass
    module = sys.modules.get("app.services.orchestration.worker_identity")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ToolingError("production pin loader module origin is unavailable")
    try:
        Path(module_file).resolve(strict=True).relative_to(Path(source_root))
    except (OSError, ValueError) as exc:
        raise ToolingError("production pin loader came from outside the approved source root") from exc

    loaded = load_candidate_identity_pin(
        pin_path,
        source_root=policy.source_root,
        runtime_root=runtime_root,
    )
    if loaded.status != PIN_LOADED or loaded.pin is None or loaded.identity is None:
        raise ToolingError("production pin loader rejected the candidate pin")
    pin = loaded.pin
    expected_launcher = policy.launcher_identity
    if (
        pin.candidate_head != policy.candidate_head
        or pin.candidate_tree != policy.candidate_tree
        or pin.source_root != policy.source_root
        or pin.source_manifest_digest != policy.source_manifest_digest
        or pin.configuration_fingerprint != policy.configuration_fingerprint
        or pin.configuration_schema_version != policy.configuration_schema_version
        or pin.configuration_policy_id != policy.configuration_policy_id
        or pin.worker_node_identity != policy.worker_node_identity
        or dict(pin.launcher_identity) != expected_launcher
        or loaded.identity.get("approval_id") != policy.approval_id
    ):
        raise ToolingError("production loader identity does not match approval")
    return dict(loaded.identity)


def _approval_record(
    policy: ApprovedIdentity, now: dt.datetime
) -> dict[str, str]:
    approved_at = now.astimezone(dt.timezone.utc).replace(microsecond=0)
    expires_at = approved_at + dt.timedelta(hours=24)
    return {
        "operator_identity": policy.operator_identity,
        "approval_id": policy.approval_id,
        "approved_at": _timestamp(approved_at),
        "expires_at": _timestamp(expires_at),
    }


def _metadata_for_pin(
    pin_path: Path,
    payload: bytes,
    approval: Mapping[str, str],
    *,
    directory_mode: str,
) -> dict[str, Any]:
    return {
        "pin_sha256": hashlib.sha256(payload).hexdigest(),
        "owner": "0:0",
        "mode": "0o600",
        "dir_mode": directory_mode,
        "size": len(payload),
        "approval": dict(approval),
        "path": str(Path(pin_path).absolute()),
        "loader_status": "PIN_LOADED",
    }


def install_candidate_pin(
    worksheet_path: str | Path,
    *,
    pin_dir: str | Path = Path(DEFAULT_PIN_PATH).parent,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    dry_run: bool = True,
    policy: ApprovedIdentity = APPROVED,
    expected_worksheet_sha256: str = APPROVED_WORKSHEET_SHA256,
    now: dt.datetime | None = None,
    euid: Callable[[], int] = os.geteuid,
    loader: Callable[..., Mapping[str, Any]] = production_pin_loader,
    temp_root: str | Path | None = None,
    checkout_verifier: Callable[[str | Path, ApprovedIdentity], None] | None = None,
) -> dict[str, Any]:
    """Validate or install the approved pin. Dry run never opens pin_dir."""
    if not dry_run and euid() != 0:
        raise ToolingError("real pin installation requires root")
    proposed = _approved_worksheet_body(
        worksheet_path,
        policy,
        expected_worksheet_sha256=expected_worksheet_sha256,
    )
    verify_source = checkout_verifier or verify_current_checkout_identity
    if not dry_run:
        verify_source(policy.source_root, policy)
    instant = now or _utc_now()
    if instant.tzinfo is None:
        raise ToolingError("install time must be timezone-aware")
    approval = _approval_record(policy, instant)
    pin = dict(proposed)
    pin["approval"] = approval
    payload = _json_bytes(pin)
    if len(payload) > 16 * 1024:
        raise ToolingError("candidate pin exceeds the production loader size limit")

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="p39-pin-dry-run-", dir=temp_root) as temp:
            test_path = Path(temp) / PIN_FILE_NAME
            fd = os.open(test_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            loaded = loader(test_path, policy=policy, runtime_root=runtime_root)
            if loaded.get("approval_id") != policy.approval_id:
                raise ToolingError("production loader approval result mismatch")
            result = _metadata_for_pin(
                test_path, payload, approval, directory_mode="0o700"
            )
            result.update(
                DeploymentProgress(installation="NOT_INSTALLED").to_dict()
            )
            result["approval_id"] = policy.approval_id
            result["dry_run"] = "PIN_LOADED"
            return result

    directory_fd = _open_directory_no_symlink(pin_dir)
    temp_device: int | None = None
    temp_inode: int | None = None
    try:
        _lock_pin_directory(directory_fd, exclusive=True)
        # Revalidate while holding the install lock, before preserving history
        # or creating any replacement bytes.
        verify_source(policy.source_root, policy)
        proposed = _approved_worksheet_body(
            worksheet_path,
            policy,
            expected_worksheet_sha256=expected_worksheet_sha256,
        )
        pin = dict(proposed)
        pin["approval"] = approval
        payload = _json_bytes(pin)
        if len(payload) > 16 * 1024:
            raise ToolingError("candidate pin exceeds the production loader size limit")

        names = os.listdir(directory_fd)
        unexpected_temps = [
            name
            for name in names
            if name.endswith(".tmp")
            and ("candidate-pin" in name or name.startswith(".candidate-pin"))
        ]
        if unexpected_temps:
            raise ToolingError("unexpected temporary pin file exists; nothing was changed")
        old_bytes, _ = _read_at_regular(directory_fd, PIN_FILE_NAME, max_bytes=16 * 1024)
        old_pin = _decode_json(old_bytes, "installed pin")
        old_approval = old_pin.get("approval")
        if not isinstance(old_approval, Mapping) or old_approval.get("approval_id") != PREVIOUS_APPROVAL_ID:
            raise ToolingError("installed pin is not the expected historical approval")
        old_sha = hashlib.sha256(old_bytes).hexdigest()

        try:
            history_bytes, _ = _read_at_regular(
                directory_fd, HISTORY_FILE_NAME, max_bytes=16 * 1024
            )
        except ToolingError as exc:
            if "missing, symlinked" not in str(exc):
                raise
            history_bytes = b""
        if history_bytes and history_bytes != old_bytes:
            raise ToolingError("existing historical pin copy differs from installed pin")
        if not history_bytes:
            temp_device, temp_inode = _write_new_at(
                directory_fd, HISTORY_FILE_NAME, old_bytes
            )
            os.fsync(directory_fd)
            history_info = os.stat(HISTORY_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False)
            if (history_info.st_dev, history_info.st_ino) != (temp_device, temp_inode):
                raise ToolingError("historical pin copy changed during creation")
            temp_device = temp_inode = None

        # Fixed name makes stale/interrupted attempts visible. Never unlink it
        # unless this invocation created and still owns the same inode.
        temp_device, temp_inode = _write_new_at(directory_fd, INSTALL_TEMP_NAME, payload)
        temp_path = Path(pin_dir) / INSTALL_TEMP_NAME
        loaded = loader(temp_path, policy=policy, runtime_root=runtime_root)
        if loaded.get("approval_id") != policy.approval_id:
            raise ToolingError("production loader approval result mismatch")
        temp_info = os.stat(INSTALL_TEMP_NAME, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(temp_info.st_mode)
            or (temp_info.st_uid, temp_info.st_gid) != _pin_owner()
            or stat.S_IMODE(temp_info.st_mode) != 0o600
            or (temp_info.st_dev, temp_info.st_ino) != (temp_device, temp_inode)
        ):
            raise ToolingError("temporary pin metadata changed before replacement")

        # The approved checkout and worksheet must still be the inputs being
        # installed; the old pin must still be the exact version whose history
        # copy was preserved.
        verify_source(policy.source_root, policy)
        _approved_worksheet_body(
            worksheet_path,
            policy,
            expected_worksheet_sha256=expected_worksheet_sha256,
        )
        current_old_bytes, _ = _read_at_regular(
            directory_fd, PIN_FILE_NAME, max_bytes=16 * 1024
        )
        if current_old_bytes != old_bytes:
            raise ToolingError("installed pin changed during installation preflight")

        try:
            os.replace(
                INSTALL_TEMP_NAME,
                PIN_FILE_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ToolingError("atomic pin replacement failed; installed pin is unchanged") from exc
        temp_device = temp_inode = None
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise ToolingError("directory sync failed after replacement; pin is suspect") from exc
        installed_bytes, installed_info = _read_at_regular(
            directory_fd, PIN_FILE_NAME, max_bytes=16 * 1024
        )
        if installed_bytes != payload or stat.S_IMODE(installed_info.st_mode) != 0o600:
            raise ToolingError("post-replacement pin verification failed; pin is suspect")
        post_loaded = loader(
            Path(pin_dir) / PIN_FILE_NAME, policy=policy, runtime_root=runtime_root
        )
        if post_loaded.get("approval_id") != policy.approval_id:
            raise ToolingError("post-replacement loader check failed; pin is suspect")
        result = _metadata_for_pin(
            Path(pin_dir) / PIN_FILE_NAME,
            installed_bytes,
            approval,
            directory_mode="0o700",
        )
        result["previous_pin_sha256"] = old_sha
        result.update(DeploymentProgress(installation="INSTALLED").to_dict())
        result["approval_id"] = policy.approval_id
        result["dry_run"] = None
        result["receipt_schema_version"] = INSTALL_RECEIPT_SCHEMA_VERSION
        result["installation_receipt_id"] = str(uuid.uuid4())
        result["worksheet_sha256"] = expected_worksheet_sha256
        result["source_identity"] = {
            "candidate_head": policy.candidate_head,
            "candidate_tree": policy.candidate_tree,
            "source_root": policy.source_root,
            "source_manifest_digest": policy.source_manifest_digest,
            "configuration_fingerprint": policy.configuration_fingerprint,
            "configuration_schema_version": policy.configuration_schema_version,
            "configuration_policy_id": policy.configuration_policy_id,
            "launcher_identity": policy.launcher_identity,
            "worker_node_identity": policy.worker_node_identity,
        }
        return result
    except BaseException:
        if temp_device is not None and temp_inode is not None:
            _unlink_own_at(directory_fd, INSTALL_TEMP_NAME, temp_device, temp_inode)
            os.fsync(directory_fd)
        raise
    finally:
        os.close(directory_fd)


def _identity_dict(value: Mapping[str, Any] | ProcessIdentity) -> dict[str, Any]:
    if value is None:
        raise ToolingError("process identity is unavailable")
    identity = value if isinstance(value, ProcessIdentity) else ProcessIdentity.from_mapping(value)
    return identity.to_dict()


def _assert_identity_equal(
    actual: Mapping[str, Any] | ProcessIdentity | None,
    expected: Mapping[str, Any] | ProcessIdentity,
    label: str,
) -> None:
    if actual is None:
        raise ToolingError(f"{label} process identity is unavailable")
    if _identity_dict(actual) != _identity_dict(expected):
        raise ToolingError(f"{label} process identity changed")


def _policy_source_identity(policy: ApprovedIdentity) -> dict[str, Any]:
    return {
        "source_root": policy.source_root,
        "head": policy.candidate_head,
        "tree": policy.candidate_tree,
        "source_manifest_digest": policy.source_manifest_digest,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_policy_id": policy.configuration_policy_id,
    }


@contextlib.contextmanager
def _opened_pidfd(pidfd_ops: Any, pid: int):
    pidfd = pidfd_ops.open(pid)
    try:
        if type(pidfd) is not int or pidfd < 0:
            raise ToolingError("pidfd_open returned an invalid descriptor")
        yield pidfd
    finally:
        pidfd_ops.close(pidfd)


def _validate_pidfd_target(
    pidfd_ops: Any,
    pidfd: int,
    expected: ProcessIdentity,
    observer: Any,
    label: str,
) -> None:
    if pidfd_ops.is_exited(pidfd):
        raise ToolingError(f"{label} exited before signaling")
    target_pid = pidfd_ops.target_pid(pidfd)
    if target_pid != expected.pid:
        raise ToolingError(f"{label} pidfd no longer identifies the expected PID")
    _assert_identity_equal(observer.identity(expected.pid), expected, label)


def _assert_approved_launcher(
    identity: ProcessIdentity, policy: ApprovedIdentity, *, pid_expected: int | None = None
) -> None:
    launcher = policy.launcher_identity
    if (
        identity.pid <= 1
        or not identity.start_ticks.isdigit()
        or int(identity.start_ticks) <= 0
        or not _valid_linux_boot_id(identity.linux_boot_id)
    ):
        raise ToolingError("worker process identity is incomplete")
    if pid_expected is not None and identity.pid != pid_expected:
        raise ToolingError("worker PID does not match the requested master")
    if (
        identity.executable != launcher["executable"]
        or identity.cwd != launcher["cwd"]
        or list(identity.argv) != launcher["argv"]
    ):
        raise ToolingError("worker executable, cwd, or argv differs from the approved launcher")


def _expected_beat_argv(
    policy: ApprovedIdentity, runtime_root: str | Path
) -> tuple[str, ...]:
    root = Path(policy.source_root)
    schedule = root / "run/celerybeat-schedule"
    return (
        str(root / "venv/bin/celery"),
        "-A",
        "app.celery_app",
        "beat",
        "--loglevel=info",
        f"--schedule={schedule}",
    )


def _assert_approved_beat(
    identity: ProcessIdentity,
    policy: ApprovedIdentity,
    runtime_root: str | Path,
) -> None:
    if (
        identity.pid <= 1
        or not identity.start_ticks.isdigit()
        or int(identity.start_ticks) <= 0
        or not _valid_linux_boot_id(identity.linux_boot_id)
        or identity.executable != policy.launcher_identity["executable"]
        or identity.cwd != policy.source_root
        or identity.argv != _expected_beat_argv(policy, runtime_root)
    ):
        raise ToolingError("Beat process identity does not match the approved Celery Beat")


def _beat_receipt_identity(identity: ProcessIdentity) -> dict[str, Any]:
    return {"role": BEAT_ROLE, **identity.to_dict()}


def _beat_identity_from_receipt(
    value: Mapping[str, Any], policy: ApprovedIdentity, runtime_root: str | Path
) -> ProcessIdentity:
    expected_fields = {
        "role",
        "pid",
        "start_ticks",
        "linux_boot_id",
        "executable",
        "cwd",
        "argv",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ToolingError("refresh receipt Beat identity is missing or has unexpected fields")
    if value.get("role") != BEAT_ROLE:
        raise ToolingError("refresh receipt Beat role is invalid")
    identity = ProcessIdentity.from_mapping(value)
    _assert_approved_beat(identity, policy, runtime_root)
    return identity


def _observe_approved_beat(
    observer: Any, policy: ApprovedIdentity, runtime_root: str | Path
) -> ProcessIdentity:
    try:
        pids = observer.beat_match_pids()
    except (AttributeError, OSError, ToolingError) as exc:
        raise ToolingError("Celery Beat process list is unavailable") from exc
    if len(pids) != 1:
        raise ToolingError("exactly one approved Celery Beat process must be observable")
    identity = observer.identity(int(pids[0]))
    if identity is None:
        raise ToolingError("approved Celery Beat process identity is unavailable")
    _assert_approved_beat(identity, policy, runtime_root)
    if observer.beat_match_pids() != [identity.pid]:
        raise ToolingError("Celery Beat process set changed during independent observation")
    return identity


def _manifest_path_for_identity(
    runtime_root: str | Path, identity: ProcessIdentity
) -> Path:
    record_id = f"{identity.linux_boot_id}-{identity.pid}-{identity.start_ticks}"
    return Path(runtime_root) / "worker-identities" / f"worker-{record_id}.json"


def _fresh(timestamp: Any, now: dt.datetime, *, after: dt.datetime | None = None) -> None:
    value = _parse_timestamp(timestamp)
    if after is not None and value < after:
        raise ToolingError("runtime evidence predates the current worker boot")
    age = (now.astimezone(dt.timezone.utc) - value).total_seconds()
    if age < -5 or age > FRESH_EVIDENCE_SECONDS:
        raise ToolingError("runtime evidence is stale or from the future")


def _assert_idle_snapshot(
    snapshot: Mapping[str, Any], policy: ApprovedIdentity = APPROVED
) -> None:
    required = {
        "active",
        "reserved",
        "scheduled",
        "queue_depth",
        "slot_keys",
        "leases",
        "active_sessions",
        "environment_configuration_overrides",
        "configuration_fingerprint",
    }
    if not required <= set(snapshot):
        raise ToolingError("worker idleness snapshot is incomplete")
    if any(snapshot.get(key) != [] for key in ("active", "reserved", "scheduled", "slot_keys", "leases")):
        raise ToolingError("worker is not idle: active work, slots, or leases are present")
    if type(snapshot.get("queue_depth")) is not int or snapshot["queue_depth"] != 0:
        raise ToolingError("worker is not idle: queue is nonempty or unknown")
    if type(snapshot.get("active_sessions")) is not int or snapshot["active_sessions"] != 0:
        raise ToolingError("worker is not idle: active sessions are present or unknown")
    if snapshot.get("environment_configuration_overrides") != []:
        raise ToolingError("worker configuration has unapproved environment overrides")
    if snapshot.get("configuration_fingerprint") != policy.configuration_fingerprint:
        raise ToolingError("worker configuration fingerprint differs from approval")


def _same_pid_set(actual: Sequence[int], expected: Sequence[int], label: str) -> None:
    if sorted(set(int(item) for item in actual)) != sorted(set(int(item) for item in expected)):
        raise ToolingError(f"unexpected process match set {label}")


_APPROVED_SYSTEM_PYTHON = Path("/usr/bin/python3.12")
_APPROVED_SYSTEM_PYTHON_UID = 0
_WORKER_ONLY_FIXED_EXECUTABLES = (
    "/usr/bin/bash",
    "/usr/bin/env",
    "/usr/bin/nohup",
    "/usr/bin/cmp",
    "/usr/bin/stat",
    "/usr/bin/readlink",
)


def _verify_trusted_directory_chain(path: Path, owner_uid: int) -> bool:
    current = path
    while True:
        try:
            info = os.lstat(current)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_mode & 0o022
        ):
            return False
        if current == current.parent:
            return True
        current = current.parent


def _verify_fixed_worker_executable(path_text: str) -> None:
    path = Path(path_text)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ToolingError(
            f"required fixed worker launcher executable is unavailable: {path}"
        ) from exc
    if stat.S_ISREG(info.st_mode):
        if not os.access(path, os.X_OK):
            raise ToolingError(
                f"required fixed worker launcher executable is unexpected: {path}"
            )
        return
    # Permit package-managed links only when both ends stay in root-owned paths.
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise ToolingError(
            f"required fixed worker launcher executable is unexpected: {path}"
        )
    try:
        resolved = path.resolve(strict=True)
        target_info = os.stat(resolved)
        parent_info = os.lstat(path.parent)
    except (OSError, RuntimeError) as exc:
        raise ToolingError(
            f"required fixed worker launcher executable is unavailable: {path}"
        ) from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != 0
        or parent_info.st_mode & 0o022
        or not _verify_trusted_directory_chain(path.parent, 0)
        or not stat.S_ISREG(target_info.st_mode)
        or target_info.st_uid != 0
        or target_info.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
        or not _verify_trusted_directory_chain(resolved.parent, 0)
    ):
        raise ToolingError(
            f"required fixed worker launcher executable is unexpected: {path}"
        )


def _verify_worker_only_executables(source_root: str | Path) -> None:
    root = Path(source_root)
    for path_text in _WORKER_ONLY_FIXED_EXECUTABLES:
        _verify_fixed_worker_executable(path_text)

    python_bin = root / "venv/bin/python3"
    celery_bin = root / "venv/bin/celery"
    try:
        python_info = os.lstat(python_bin)
        base_python_info = os.lstat(_APPROVED_SYSTEM_PYTHON)
        if (
            not stat.S_ISREG(python_info.st_mode)
            or python_info.st_nlink != 1
            or not os.access(python_bin, os.X_OK)
            or not stat.S_ISREG(base_python_info.st_mode)
            or base_python_info.st_uid != _APPROVED_SYSTEM_PYTHON_UID
            or base_python_info.st_mode & 0o022
        ):
            raise ToolingError("worker interpreter is unavailable")
        with (
            python_bin.open("rb") as candidate,
            _APPROVED_SYSTEM_PYTHON.open("rb") as approved,
        ):
            if hashlib.file_digest(candidate, "sha256").digest() != hashlib.file_digest(approved, "sha256").digest():
                raise ToolingError("worker interpreter differs from the approved executable bytes")
        celery_info = os.lstat(celery_bin)
        if not stat.S_ISREG(celery_info.st_mode) or celery_info.st_nlink != 1 or not os.access(celery_bin, os.X_OK):
            raise ToolingError("worker executable is unavailable or unexpected")
        with celery_bin.open("rb") as handle:
            shebang = handle.readline(4096).decode("utf-8", "strict").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolingError("worker executable paths cannot be verified") from exc
    if shebang != f"#!{python_bin}":
        raise ToolingError("worker executable interpreter differs from the approved interpreter")


def run_start_worker_only_launcher(
    source_root: str | Path, pin_path: str | Path, *, timeout_seconds: float = 120
) -> dict[str, Any]:
    """Invoke the dedicated worker-only entry point once with a closed environment."""
    root = Path(source_root)
    start_script = root / "start.sh"
    if not start_script.is_file() or start_script.is_symlink():
        raise ToolingError("worker-only start.sh launcher is missing or symlinked")
    pin_path = Path(pin_path)
    if not pin_path.is_absolute():
        raise ToolingError("worker pin path must be absolute")
    verify_launcher_environment(root)
    verify_launcher_paths(root)
    _verify_worker_only_executables(root)
    env = {
        "PATH": f"{root}/venv/bin:/usr/bin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            ["/usr/bin/bash", str(start_script), "start_worker_only", str(pin_path)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryRequiredError(
            "worker-only launcher outcome is ambiguous; manual recovery is required",
            {"launch": "LAUNCH_ATTEMPTED", "process_observation": "NOT_OBSERVED"},
        ) from exc

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    match = re.fullmatch(r"LAUNCH_ATTEMPTED pid=([1-9][0-9]*)", lines[0]) if len(lines) == 1 else None
    if completed.returncode != 0 and match is None:
        raise ToolingError("worker-only launcher blocked before process creation")
    if completed.returncode != 0 or match is None:
        raise RecoveryRequiredError(
            "worker-only launch attempt did not return an unambiguous PID; manual recovery is required",
            {
                "launch": "LAUNCH_ATTEMPTED",
                "process_observation": "NOT_OBSERVED",
                "launcher_returncode": completed.returncode,
            },
        )
    return {
        "returncode": completed.returncode,
        "launch_attempts": 1,
        "launch": "LAUNCH_ATTEMPTED",
        "worker_pid": int(match.group(1)),
    }


def find_fresh_manifest(
    runtime_root: str | Path,
    *,
    pid: int,
    start_ticks: str,
    linux_boot_id: str,
    launched_at: dt.datetime,
) -> tuple[Mapping[str, Any], Path]:
    directory = Path(runtime_root) / "worker-identities"
    if directory.is_symlink() or not directory.is_dir():
        raise ToolingError("worker identity manifest directory is missing or symlinked")
    found: list[tuple[Mapping[str, Any], Path]] = []
    for path in directory.glob("worker-*.json"):
        if path.is_symlink():
            continue
        try:
            raw, info = _read_regular_file(path, max_bytes=MAX_MANIFEST_BYTES)
            manifest = _decode_json(raw, "worker manifest")
            booted = _parse_timestamp(manifest.get("boot_timestamp"))
        except ToolingError:
            continue
        record_id = f"{linux_boot_id}-{pid}-{start_ticks}"
        expected_path = (
            Path(runtime_root)
            / "worker-identities"
            / f"worker-{record_id}.json"
        )
        launched_second = launched_at.astimezone(dt.timezone.utc).replace(
            microsecond=0
        )
        if (
            manifest.get("record_id") == record_id
            and manifest.get("worker_pid") == pid
            and str(manifest.get("worker_start_ticks")) == start_ticks
            and manifest.get("linux_boot_id") == linux_boot_id
            and booted >= launched_second
            and info.st_mtime_ns >= _timestamp_ns(launched_at)
        ):
            found.append((manifest, path))
    if len(found) != 1:
        raise ToolingError("new worker does not have exactly one fresh boot manifest")
    if found[0][1] != expected_path:
        raise ToolingError("fresh worker manifest path does not match its process identity")
    return found[0]


def celery_ping(
    source_root: str | Path,
    node: str,
    *,
    timeout_seconds: int = 10,
) -> bool:
    command = [
        str(Path(source_root) / "venv/bin/celery"),
        "-A",
        "app.celery_app",
        "inspect",
        "ping",
        "--destination",
        node,
        "--timeout",
        str(timeout_seconds),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and f"{node}: OK" in (completed.stdout + completed.stderr)


def _verify_install_receipt(
    value: Mapping[str, Any],
    *,
    pin_path: str | Path,
    runtime_root: str | Path,
    policy: ApprovedIdentity,
    now: dt.datetime | None = None,
    loader: Callable[..., Mapping[str, Any]] = production_pin_loader,
    lock_held: bool = False,
) -> str:
    """Bind a receipt to the safely opened, currently installed production pin."""
    required = {
        "installation",
        "approval_id",
        "pin_sha256",
        "approval",
        "path",
        "owner",
        "mode",
        "dir_mode",
        "size",
        "loader_status",
        "receipt_schema_version",
        "installation_receipt_id",
        "worksheet_sha256",
        "source_identity",
    }
    if not isinstance(value, Mapping) or not required <= set(value):
        raise ToolingError("installation receipt is incomplete")
    if value.get("installation") != "INSTALLED":
        raise ToolingError("worker refresh requires an INSTALLED receipt")
    if value.get("receipt_schema_version") != INSTALL_RECEIPT_SCHEMA_VERSION:
        raise ToolingError("installation receipt schema is invalid")
    try:
        uuid.UUID(str(value.get("installation_receipt_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ToolingError("installation receipt identity is invalid") from exc
    if value.get("approval_id") != policy.approval_id:
        raise ToolingError("installation receipt has the wrong approval ID")
    expected_path = Path(os.path.abspath(pin_path))
    if value.get("path") != str(expected_path):
        raise ToolingError("installation receipt pin path does not match the requested pin")
    if (
        value.get("owner") != "0:0"
        or value.get("mode") != "0o600"
        or value.get("dir_mode") != "0o700"
        or value.get("loader_status") != "PIN_LOADED"
        or value.get("worksheet_sha256") != policy.worksheet_sha256
    ):
        raise ToolingError("installation receipt metadata does not match approval")
    expected_source_identity = {
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_policy_id": policy.configuration_policy_id,
        "launcher_identity": policy.launcher_identity,
        "worker_node_identity": policy.worker_node_identity,
    }
    if value.get("source_identity") != expected_source_identity:
        raise ToolingError("installation receipt source identity does not match approval")

    approval = value.get("approval")
    approval_fields = {
        "operator_identity",
        "approval_id",
        "approved_at",
        "expires_at",
    }
    if not isinstance(approval, Mapping) or set(approval) != approval_fields:
        raise ToolingError("installation receipt approval record is incomplete")
    if (
        approval.get("operator_identity") != policy.operator_identity
        or approval.get("approval_id") != policy.approval_id
    ):
        raise ToolingError("installation receipt approval identity does not match")
    instant = now or _utc_now()
    if instant.tzinfo is None:
        raise ToolingError("receipt verification time must be timezone-aware")
    try:
        approved_at = _parse_timestamp(approval.get("approved_at"))
        expires_at = _parse_timestamp(approval.get("expires_at"))
    except ToolingError as exc:
        raise ToolingError("installation receipt approval timestamps are invalid") from exc
    if not approved_at < expires_at or not approved_at <= instant.astimezone(
        dt.timezone.utc
    ) < expires_at:
        raise ToolingError("installation receipt is stale or approval is expired")

    pin_sha = value.get("pin_sha256")
    if not isinstance(pin_sha, str) or len(pin_sha) != 64 or any(
        char not in "0123456789abcdef" for char in pin_sha
    ):
        raise ToolingError("installation receipt pin SHA-256 is invalid")
    directory_fd = _open_directory_no_symlink(expected_path.parent)
    try:
        if not lock_held:
            _lock_pin_directory(directory_fd, exclusive=False)
        raw, info = _read_at_regular(
            directory_fd,
            expected_path.name,
            max_bytes=16 * 1024,
        )
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != pin_sha:
            raise ToolingError("installed pin SHA-256 differs from its receipt")
        if value.get("size") != len(raw):
            raise ToolingError("installed pin size differs from its receipt")
        loaded = loader(expected_path, policy=policy, runtime_root=runtime_root)
        if loaded.get("pin_sha256") != actual_sha:
            raise ToolingError("production loader pin hash differs from the opened pin")
        if (
            loaded.get("approval_id") != policy.approval_id
            or loaded.get("operator_identity") != policy.operator_identity
            or loaded.get("approved_at") != _timestamp(approved_at)
            or loaded.get("expires_at") != _timestamp(expires_at)
            or loaded.get("candidate_head") != policy.candidate_head
            or loaded.get("candidate_tree") != policy.candidate_tree
            or loaded.get("source_manifest_digest") != policy.source_manifest_digest
        ):
            raise ToolingError("production loader identity does not match the receipt")
        if not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != _pin_owner():
            raise ToolingError("installed pin owner or file type is unsafe")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ToolingError("installed pin mode is not 0600")
    finally:
        os.close(directory_fd)
    return pin_sha


def verify_current_checkout_identity(
    source_root: str | Path, policy: ApprovedIdentity = APPROVED
) -> None:
    """Require the exact clean source baseline and reproducible worksheet."""
    root = Path(source_root).resolve()
    if str(root) != policy.source_root:
        raise ToolingError("source root differs from the approved source identity")
    if os.path.lexists(root / ".orchestrator-release-identity.json"):
        try:
            from app.services.orchestration import worker_identity as wi

            dirty, head, tree = wi._source_identity(root)
        except Exception as exc:
            raise ToolingError("protected release identity cannot be verified") from exc
        if dirty or head != policy.candidate_head or tree != policy.candidate_tree:
            raise ToolingError("protected release source identity differs from approval")
        return
    commands = [
        (["git", "-C", str(root), "rev-parse", "HEAD"], policy.candidate_head),
        (["git", "-C", str(root), "write-tree"], policy.candidate_tree),
    ]
    for command, expected in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolingError("cannot verify approved source identity") from exc
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise ToolingError("source HEAD/tree differs from the approved identity")
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolingError("cannot verify source cleanliness") from exc
    if status.returncode != 0 or status.stdout.strip():
        raise ToolingError("source checkout is not clean")

def verify_launcher_environment(source_root: str | Path) -> None:
    """Reject .env values that conflict with the closed worker launch policy."""
    env_path = Path(source_root) / ".env"
    if not env_path.exists() and not env_path.is_symlink():
        return
    if env_path.is_symlink() or not env_path.is_file():
        raise ToolingError(".env is not a regular file")
    try:
        raw, _ = _read_regular_file(env_path, max_bytes=1024 * 1024)
        lines = raw.decode("utf-8").splitlines()
    except (ToolingError, UnicodeDecodeError) as exc:
        raise ToolingError(".env cannot be safely inspected") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        key_parts = key.split(None, 1)
        if key_parts and key_parts[0] == "export":
            key = key_parts[1].strip() if len(key_parts) == 2 else ""
        if key == "PATH":
            raise ToolingError(".env must not set PATH for the worker-only launcher")
        if key == "WORKER_CANDIDATE_PIN_PATH":
            raise ToolingError(".env must not override the approved pin path")
        if key == "CELERY_WORKER_CONCURRENCY" and value.strip("\"'") != "4":
            raise ToolingError(".env worker concurrency differs from approval")


def verify_launcher_paths(source_root: str | Path) -> None:
    """Refuse launcher log paths that could redirect writes through symlinks."""
    root = Path(source_root)
    for directory_name in ("run", "logs"):
        directory = root / directory_name
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise ToolingError("approved launcher runtime directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ToolingError("approved launcher runtime directory is not a real directory")
    log_file = root / "logs/worker.log"
    for path in (log_file,):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ToolingError("approved launcher output path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ToolingError("approved launcher output path must be a regular file")


def refresh_worker(**kwargs: Any) -> dict[str, Any]:
    """Hold both authority and pin locks for exactly one worker refresh."""
    if "authority" in kwargs or "pidfd_ops" in kwargs:
        raise ToolingError("refresh authority and pidfd implementation are not caller-selectable")
    authority = RefreshAuthority(DEFAULT_REFRESH_AUTHORITY_ROOT)
    pin_path = kwargs.get("pin_path", DEFAULT_PIN_PATH)
    try:
        with authority.locked(exclusive=True, create=True) as authority_directory_fd:
            authority.assert_no_unresolved_history(authority_directory_fd)
            directory_fd = _open_directory_no_symlink(Path(pin_path).parent)
            try:
                _lock_pin_directory(directory_fd, exclusive=True)
                return _refresh_worker_locked(
                    **kwargs,
                    authority=authority,
                    authority_directory_fd=authority_directory_fd,
                    pidfd_ops=PidfdOperations(),
                )
            finally:
                os.close(directory_fd)
    except RefreshAuthorityError as exc:
        raise ToolingError("protected refresh authority is unavailable or unresolved: " + str(exc)) from exc


def _refresh_worker_locked(
    *,
    master_pid: int,
    expected_start_ticks: str,
    preflight: Mapping[str, Any],
    installation_receipt: Mapping[str, Any],
    observer: Any,
    launcher: Callable[..., Mapping[str, Any]],
    manifest_reader: Callable[..., tuple[Mapping[str, Any], Path]],
    ping: Callable[..., bool],
    source_root: str | Path = APPROVED_SOURCE_ROOT,
    pin_path: str | Path = DEFAULT_PIN_PATH,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    policy: ApprovedIdentity = APPROVED,
    now: Callable[[], dt.datetime] = _utc_now,
    timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
    checkout_verifier: Callable[[str | Path, ApprovedIdentity], None] = verify_current_checkout_identity,
    launcher_path_verifier: Callable[[str | Path], None] = verify_launcher_paths,
    receipt_validator: Callable[..., str] = _verify_install_receipt,
    pin_loader: Callable[..., Mapping[str, Any]] = production_pin_loader,
    authority: RefreshAuthority,
    authority_directory_fd: int,
    pidfd_ops: Any | None = None,
) -> dict[str, Any]:
    """Stop one verified idle worker, make one worker-only attempt, verify READY."""
    pidfd_ops = pidfd_ops or PidfdOperations()
    if master_pid <= 1 or not str(expected_start_ticks).isdigit():
        raise ToolingError("requested worker PID/start ticks are invalid")
    instant = now()
    pin_sha = receipt_validator(
        installation_receipt,
        pin_path=pin_path,
        runtime_root=runtime_root,
        policy=policy,
        now=instant,
        loader=pin_loader,
        lock_held=True,
    )
    _fresh(preflight.get("observed_at"), instant)
    _assert_idle_snapshot(preflight, policy)
    if preflight.get("master_pid") != master_pid or str(preflight.get("master_start_ticks")) != str(expected_start_ticks):
        raise ToolingError("worker idleness snapshot does not identify the requested master")
    if preflight.get("approval_id") != policy.approval_id:
        raise ToolingError("worker preflight approval ID mismatch")
    checkout_verifier(source_root, policy)
    verify_launcher_environment(source_root)
    launcher_path_verifier(source_root)

    current = observer.identity(master_pid)
    if current is None or current.start_ticks != str(expected_start_ticks):
        raise ToolingError("worker PID/start ticks changed before shutdown")
    _assert_approved_launcher(current, policy, pid_expected=master_pid)
    recorded_children = [ProcessIdentity.from_mapping(item) for item in preflight.get("children", [])]
    current_children = observer.children(master_pid)
    if [item.to_dict() for item in current_children] != [item.to_dict() for item in recorded_children]:
        raise ToolingError("worker children changed before shutdown")
    expected_matches = [master_pid, *(item.pid for item in current_children)]
    _same_pid_set(observer.worker_match_pids(), expected_matches, "before SIGTERM")

    # The observer selects Beat from its live argv scan; preflight JSON can only
    # contradict that identity and can never establish the historical value.
    beat_before = _observe_approved_beat(observer, policy, runtime_root)
    preflight_beat = ProcessIdentity.from_mapping(preflight.get("beat", {}))
    _assert_identity_equal(preflight_beat, beat_before, "Preflight health Beat")
    if current.linux_boot_id != beat_before.linux_boot_id:
        raise ToolingError("worker and Beat Linux boot identities conflict")

    refresh_id = str(uuid.uuid4())
    stages = {
        "launch": "NOT_ATTEMPTED",
        "process_observation": "NOT_OBSERVED",
        "identity_verification": "NOT_VERIFIED",
        "readiness": "NOT_READY",
    }
    new_pid: int | None = None
    new_process: ProcessIdentity | None = None
    prepared = False
    signal_sent = False

    def recovery_error(exc: Exception) -> RecoveryRequiredError:
        result: dict[str, Any] = {
            "decision": "RECOVERY_REQUIRED",
            "refresh_id": refresh_id,
            **stages,
            "beat_process_identity": _beat_receipt_identity(beat_before),
            "reason": str(exc),
        }
        if new_pid is not None:
            result["worker_pid"] = new_pid
        if new_process is not None:
            result["worker_process_identity"] = new_process.to_dict()
        if prepared:
            try:
                current_record = authority.load_locked(authority_directory_fd, refresh_id)
                if current_record.last_stage != "RECOVERY_REQUIRED":
                    authority.append_stage(
                        authority_directory_fd,
                        refresh_id,
                        "RECOVERY_REQUIRED",
                        {"reason": str(exc)[:1000] or "unspecified post-signal failure"},
                    )
            except Exception as authority_exc:
                result["authority_recovery_record"] = "UNAVAILABLE"
                result["authority_recovery_error"] = str(authority_exc)
        return RecoveryRequiredError(
            "worker refresh requires manual recovery: " + str(exc), result
        )

    def recheck_beat(label: str) -> ProcessIdentity:
        observed = _observe_approved_beat(observer, policy, runtime_root)
        _assert_identity_equal(observed, beat_before, label)
        return observed

    # Bind the observed process tuple to one pidfd before receipt revalidation.
    immediately_before = observer.identity(master_pid)
    if immediately_before is None or immediately_before.start_ticks != str(expected_start_ticks):
        raise ToolingError("worker PID/start ticks changed immediately before SIGTERM")
    _assert_approved_launcher(immediately_before, policy, pid_expected=master_pid)
    _assert_identity_equal(immediately_before, current, "worker before pidfd acquisition")
    _same_pid_set(observer.worker_match_pids(), expected_matches, "immediately before SIGTERM")
    recheck_beat("Beat immediately before SIGTERM")

    pre_signal_observed_at = now()
    if pre_signal_observed_at.tzinfo is None:
        raise ToolingError("pre-signal observation time must be timezone-aware")
    authority_receipt = {
        "receipt_schema_version": REFRESH_RECEIPT_SCHEMA_VERSION,
        "refresh_id": refresh_id,
        "source_identity": _policy_source_identity(policy),
        "launcher_identity": policy.launcher_identity,
        "installation": "INSTALLED",
        "approval_id": policy.approval_id,
        "pin_sha256": pin_sha,
        "pin_path": str(Path(pin_path).absolute()),
        "installation_receipt_sha256": hashlib.sha256(
            _json_bytes(installation_receipt)
        ).hexdigest(),
        "worker_node_identity": policy.worker_node_identity,
        "pre_signal_worker_identity": current.to_dict(),
        "beat_process_identity": _beat_receipt_identity(beat_before),
        "pre_signal_observed_at": _timestamp_precise(pre_signal_observed_at),
    }

    try:
        with _opened_pidfd(pidfd_ops, master_pid) as pidfd:
            _validate_pidfd_target(
                pidfd_ops, pidfd, current, observer, "worker pidfd target after acquisition"
            )
            # Revalidate every mutable authorization condition before the
            # durable PREPARED record and the final pidfd target check.
            pin_sha = receipt_validator(
                installation_receipt,
                pin_path=pin_path,
                runtime_root=runtime_root,
                policy=policy,
                now=now(),
                loader=pin_loader,
                lock_held=True,
            )
            authority_receipt["pin_sha256"] = pin_sha
            recheck_beat("Beat during final receipt validation")
            authority.begin(authority_directory_fd, refresh_id, authority_receipt)
            prepared = True

            confirmed_pin_sha = receipt_validator(
                installation_receipt,
                pin_path=pin_path,
                runtime_root=runtime_root,
                policy=policy,
                now=now(),
                loader=pin_loader,
                lock_held=True,
            )
            if confirmed_pin_sha != pin_sha:
                raise ToolingError("installed pin changed after the durable PREPARED record")
            recheck_beat("Beat after final protected receipt validation")

            _assert_identity_equal(
                observer.identity(master_pid), current, "worker after durable PREPARED record"
            )
            children_after_receipt = observer.children(master_pid)
            if [item.to_dict() for item in children_after_receipt] != [
                item.to_dict() for item in recorded_children
            ]:
                raise ToolingError("worker child identities changed after the durable PREPARED record")
            _same_pid_set(observer.worker_match_pids(), expected_matches, "before pidfd SIGTERM")
            recheck_beat("Beat immediately before pidfd SIGTERM")
            _validate_pidfd_target(
                pidfd_ops, pidfd, current, observer, "worker pidfd target before SIGTERM"
            )
            pidfd_ops.send_signal(pidfd, signal.SIGTERM)
            signal_sent = True
            authority.append_stage(
                authority_directory_fd,
                refresh_id,
                "SIGTERM_SENT",
                {"signum": signal.SIGTERM, "pidfd_bound": True},
            )
            if not pidfd_ops.wait_for_exit(pidfd, timeout_seconds):
                raise ToolingError(
                    "original worker did not exit within the bounded grace period; no escalation sent"
                )
            authority.append_stage(
                authority_directory_fd,
                refresh_id,
                "WORKER_EXITED",
                {"pid": current.pid, "start_ticks": current.start_ticks},
            )
    except RecoveryRequiredError:
        raise
    except Exception as exc:
        if signal_sent:
            raise recovery_error(exc) from exc
        if isinstance(exc, ToolingError):
            raise
        if isinstance(exc, (PidfdError, RefreshAuthorityError, OSError)):
            raise ToolingError("pidfd or protected authority validation failed closed: " + str(exc)) from exc
        raise

    try:
        if observer.worker_match_pids():
            raise ToolingError("worker processes remain after graceful shutdown; no launcher called")
        recheck_beat("Beat after shutdown")

        # No existing or unexpected worker may coexist with the one authorized
        # attempt. This check is repeated by start_worker_only at launch time.
        if observer.worker_match_pids():
            raise ToolingError("unexpected worker process appeared before launch")
        recheck_beat("Beat immediately before launch")
        receipt_validator(
            installation_receipt,
            pin_path=pin_path,
            runtime_root=runtime_root,
            policy=policy,
            now=now(),
            loader=pin_loader,
            lock_held=True,
        )
        checkout_verifier(source_root, policy)
        verify_launcher_environment(source_root)
        launcher_path_verifier(source_root)
    except Exception as exc:
        raise recovery_error(exc) from exc

    launch_started = now()
    if launch_started.tzinfo is None:
        raise recovery_error(ToolingError("launch time must be timezone-aware"))
    launch_started_text = _timestamp_precise(launch_started)
    try:
        authority.append_stage(
            authority_directory_fd,
            refresh_id,
            "LAUNCH_STARTED",
            {"launch_started_at": launch_started_text},
        )
    except Exception as exc:
        raise recovery_error(exc) from exc

    try:
        if observer.worker_match_pids():
            raise ToolingError("unexpected worker process appeared immediately before launch")
        recheck_beat("Beat immediately before launch")
    except Exception as exc:
        raise recovery_error(exc) from exc

    try:
        try:
            launcher_result = launcher(source_root, pin_path)
        except RecoveryRequiredError as exc:
            stages["launch"] = "LAUNCH_ATTEMPTED"
            try:
                if exc.result.get("worker_pid") is not None:
                    new_pid = int(exc.result["worker_pid"])
                if isinstance(exc.result.get("worker_process_identity"), Mapping):
                    new_process = ProcessIdentity.from_mapping(
                        exc.result["worker_process_identity"]
                    )
            except (TypeError, ValueError, ToolingError):
                pass
            authority.append_stage(
                authority_directory_fd,
                refresh_id,
                "LAUNCH_ATTEMPTED",
                {
                    "worker_pid": new_pid,
                    "returncode": exc.result.get("returncode")
                    if type(exc.result.get("returncode")) is int
                    else None,
                    "launch_attempts": exc.result.get("launch_attempts")
                    if exc.result.get("launch_attempts") == 1
                    else None,
                },
            )
            raise recovery_error(exc) from exc
        except ToolingError as exc:
            # The concrete launcher raises ToolingError only during its
            # pre-spawn path checks; the old worker is already stopped, so
            # the durable history still requires manual recovery.
            raise recovery_error(exc) from exc
        except Exception as exc:
            stages["launch"] = "LAUNCH_ATTEMPTED"
            authority.append_stage(
                authority_directory_fd,
                refresh_id,
                "LAUNCH_ATTEMPTED",
                {"worker_pid": None, "returncode": None, "launch_attempts": None},
            )
            raise recovery_error(exc) from exc

        stages["launch"] = "LAUNCH_ATTEMPTED"
        try:
            new_pid = int(launcher_result["worker_pid"])
        except (KeyError, TypeError, ValueError):
            new_pid = None
        authority.append_stage(
            authority_directory_fd,
            refresh_id,
            "LAUNCH_ATTEMPTED",
            {
                "worker_pid": new_pid,
                "returncode": launcher_result.get("returncode")
                if type(launcher_result.get("returncode")) is int
                else None,
                "launch_attempts": launcher_result.get("launch_attempts")
                if launcher_result.get("launch_attempts") == 1
                else None,
            },
        )
        if new_pid is None:
            raise ToolingError("worker launch attempt returned no valid PID")
        if launcher_result.get("launch_attempts") != 1:
            raise ToolingError("launcher did not report exactly one worker launch attempt")
        if new_pid <= 1 or new_pid == master_pid:
            raise ToolingError("worker launch attempt returned an invalid or reused PID")

        try:
            recheck_beat("Beat immediately after launch")
            new_process = observer.identity(new_pid)
            if new_process is None:
                raise ToolingError("worker launch was attempted but no process was observed")
            stages["process_observation"] = "PROCESS_OBSERVED"
            authority.append_stage(
                authority_directory_fd,
                refresh_id,
                "PROCESS_OBSERVED",
                {"worker_process_identity": new_process.to_dict()},
            )
        except Exception as exc:
            raise recovery_error(exc) from exc
        _assert_approved_launcher(new_process, policy, pid_expected=new_pid)
        if new_process.linux_boot_id != beat_before.linux_boot_id:
            raise ToolingError("new worker Linux boot identity differs from preserved Beat")
        if observer.environment_value(new_pid, "WORKER_CANDIDATE_PIN_PATH") != str(pin_path):
            raise ToolingError("new worker does not have the approved pin path in its environment")
        current_children_after_launch = observer.children(new_pid)
        launch_matches = [new_pid, *(item.pid for item in current_children_after_launch)]
        _same_pid_set(observer.worker_match_pids(), launch_matches, "after worker launch")

        manifest, manifest_path = manifest_reader(
            runtime_root,
            pid=new_pid,
            start_ticks=new_process.start_ticks,
            linux_boot_id=new_process.linux_boot_id,
            launched_at=launch_started,
        )
        _validate_ready_manifest(
            manifest,
            manifest_path,
            new_process,
            runtime_root=runtime_root,
            launch_started=launch_started,
            pin_sha256=pin_sha,
            installation_receipt=installation_receipt,
            policy=policy,
        )
        raw_manifest, manifest_info = _read_regular_file(
            manifest_path, max_bytes=MAX_MANIFEST_BYTES
        )
        if _decode_json(raw_manifest, "worker manifest") != manifest:
            raise ToolingError("manifest file content changed after selection")
        recorded_new_children = [
            ProcessIdentity.from_mapping(item) for item in manifest.get("worker_children", [])
        ]
        current_new_children = observer.children(new_pid)
        if [item.to_dict() for item in current_new_children] != [item.to_dict() for item in recorded_new_children]:
            raise ToolingError("new worker child identities do not match the fresh manifest")
        new_matches = [new_pid, *(item.pid for item in current_new_children)]
        _same_pid_set(observer.worker_match_pids(), new_matches, "after launch")
        stages["identity_verification"] = "IDENTITY_VERIFIED"
        authority.append_stage(
            authority_directory_fd,
            refresh_id,
            "IDENTITY_VERIFIED",
            {"worker_process_identity": new_process.to_dict()},
        )

        if not ping(source_root, policy.worker_node_identity):
            raise ToolingError("new worker did not answer the independent Celery ping")
        current_after_ping = observer.identity(new_pid)
        _assert_identity_equal(current_after_ping, new_process, "worker after readiness ping")
        recheck_beat("Beat after readiness ping")
        if observer.environment_value(new_pid, "WORKER_CANDIDATE_PIN_PATH") != str(pin_path):
            raise ToolingError("new worker pin path changed during readiness verification")
        if [item.to_dict() for item in observer.children(new_pid)] != [
            item.to_dict() for item in recorded_new_children
        ]:
            raise ToolingError("worker children changed during readiness verification")
        _same_pid_set(observer.worker_match_pids(), new_matches, "after readiness ping")
        raw_manifest_after, manifest_info_after = _read_regular_file(
            manifest_path, max_bytes=MAX_MANIFEST_BYTES
        )
        if (
            raw_manifest_after != raw_manifest
            or manifest_info_after.st_ino != manifest_info.st_ino
            or manifest_info_after.st_dev != manifest_info.st_dev
        ):
            raise ToolingError("worker manifest changed during readiness verification")
        ready_at = now()
        if ready_at.tzinfo is None:
            raise ToolingError("readiness time must be timezone-aware")
        stages["readiness"] = "READY"
    except RecoveryRequiredError:
        raise
    except Exception as exc:
        raise recovery_error(exc) from exc

    manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
    ready_payload = {
        "worker_process_identity": new_process.to_dict(),
        "master_pid": new_pid,
        "worker_start_ticks": new_process.start_ticks,
        "linux_boot_id": new_process.linux_boot_id,
        "manifest_path": str(manifest_path),
        "manifest_record_id": manifest.get("record_id"),
        "manifest_sha256": manifest_sha,
        "manifest_mtime_ns": manifest_info.st_mtime_ns,
        "launch_started_at": launch_started_text,
        "ready_at": _timestamp_precise(ready_at),
        "ping": "OK",
        "launcher_returncode": launcher_result.get("returncode")
        if type(launcher_result.get("returncode")) is int
        else None,
    }
    try:
        record = authority.append_stage(
            authority_directory_fd, refresh_id, "READY", ready_payload
        )
        return authority.final_receipt(record)
    except Exception as exc:
        raise recovery_error(exc) from exc

def _validate_ready_manifest(
    manifest: Mapping[str, Any],
    manifest_path: str | Path,
    process: ProcessIdentity,
    *,
    runtime_root: str | Path,
    launch_started: dt.datetime,
    pin_sha256: str,
    installation_receipt: Mapping[str, Any],
    policy: ApprovedIdentity,
) -> None:
    required = {
        "candidate_head",
        "candidate_tree",
        "candidate_source_root",
        "source_manifest_digest",
        "boot_timestamp",
        "linux_boot_id",
        "worker_pid",
        "worker_start_ticks",
        "worker_executable",
        "worker_cwd",
        "worker_argv",
        "worker_node_identity",
        "configuration_fingerprint",
        "configuration_schema_version",
        "configuration_policy_id",
        "identity_status",
        "candidate_pin_status",
        "identity_status_reasons",
        "candidate_pin_identity",
        "record_id",
        "worker_children",
    }
    if not required <= set(manifest):
        raise ToolingError("fresh worker manifest is missing identity fields")
    try:
        recorded = _manifest_process(manifest)
        booted = _parse_timestamp(manifest.get("boot_timestamp"))
    except ToolingError as exc:
        raise ToolingError("fresh worker manifest identity is incomplete") from exc
    if recorded.to_dict() != process.to_dict():
        raise ToolingError("fresh manifest process identity differs from the current worker")
    expected_record_id = f"{process.linux_boot_id}-{process.pid}-{process.start_ticks}"
    expected_path = _manifest_path_for_identity(runtime_root, process)
    if (
        manifest.get("record_id") != expected_record_id
        or Path(manifest_path) != expected_path
    ):
        raise ToolingError("fresh worker manifest is not bound to this process identity")
    launched_second = launch_started.astimezone(dt.timezone.utc).replace(microsecond=0)
    if booted < launched_second:
        raise ToolingError("new worker boot manifest predates the launch second")
    required_identity = {
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "candidate_source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_policy_id": policy.configuration_policy_id,
        "worker_node_identity": policy.worker_node_identity,
    }
    for field, expected in required_identity.items():
        if manifest.get(field) != expected:
            raise ToolingError(f"fresh worker manifest identity mismatch: {field}")
    if (
        manifest.get("identity_status") != "IDENTITY_VERIFIED"
        or manifest.get("candidate_pin_status") != "PIN_MATCHED"
        or manifest.get("identity_status_reasons") != []
    ):
        raise ToolingError("fresh worker manifest identity status is not verified")
    pin_identity = manifest.get("candidate_pin_identity")
    receipt_approval = installation_receipt.get("approval")
    if not isinstance(pin_identity, Mapping) or not isinstance(receipt_approval, Mapping):
        raise ToolingError("fresh worker manifest pin identity is incomplete")
    expected_pin_identity = {
        "pin_schema_version": 1,
        "pin_sha256": pin_sha256,
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "source_manifest_digest": policy.source_manifest_digest,
        "operator_identity": policy.operator_identity,
        "approval_id": policy.approval_id,
        "approved_at": receipt_approval.get("approved_at"),
        "expires_at": receipt_approval.get("expires_at"),
    }
    if dict(pin_identity) != expected_pin_identity:
        raise ToolingError("fresh worker manifest pin identity differs from the receipt")
    approved_at = _parse_timestamp(pin_identity.get("approved_at"))
    expires_at = _parse_timestamp(pin_identity.get("expires_at"))
    if not approved_at < booted < expires_at:
        raise ToolingError("fresh worker manifest boot falls outside approval validity")
    raw, info = _read_regular_file(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    if info.st_mtime_ns < _timestamp_ns(launch_started):
        raise ToolingError("fresh worker manifest file predates this launch")
    if _decode_json(raw, "worker manifest") != manifest:
        raise ToolingError("selected worker manifest differs from its file")


def _manifest_process(manifest: Mapping[str, Any]) -> ProcessIdentity:
    try:
        return ProcessIdentity.from_mapping(
            {
                "pid": manifest["worker_pid"],
                "start_ticks": manifest["worker_start_ticks"],
                "linux_boot_id": manifest["linux_boot_id"],
                "executable": manifest["worker_executable"],
                "cwd": manifest["worker_cwd"],
                "argv": manifest["worker_argv"],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolingError("worker manifest process identity is incomplete") from exc


def _module_source_path(root: Path, module_name: str) -> Path:
    if not module_name or any(not part.isidentifier() for part in module_name.split(".")):
        raise ToolingError("manifest contains an invalid module name")
    package_path = root.joinpath(*module_name.split("."))
    module_path = package_path.with_suffix(".py")
    package_init_path = package_path / "__init__.py"
    module_exists = module_path.exists()
    package_exists = package_init_path.exists()
    if module_exists and package_exists:
        raise ToolingError("manifest module/package source is ambiguous")
    return package_init_path if package_exists else module_path


def _canonical_module_names(policy: ApprovedIdentity) -> tuple[str, ...]:
    if policy.affected_module_names:
        names = tuple(policy.affected_module_names)
    else:
        source_root = str(Path(policy.source_root).resolve())
        inserted = source_root not in sys.path
        if inserted:
            sys.path.insert(0, source_root)
        try:
            from app.services.orchestration.worker_identity import DEFAULT_AFFECTED_MODULES
        except (ImportError, OSError) as exc:
            raise ToolingError("canonical affected-module set is unavailable") from exc
        finally:
            if inserted:
                try:
                    sys.path.remove(source_root)
                except ValueError:
                    pass
        module = sys.modules.get("app.services.orchestration.worker_identity")
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise ToolingError("canonical affected-module source origin is unavailable")
        try:
            Path(module_file).resolve(strict=True).relative_to(Path(source_root))
        except (OSError, ValueError) as exc:
            raise ToolingError("canonical affected-module set came from another checkout") from exc
        names = tuple(DEFAULT_AFFECTED_MODULES)
    if len(names) != policy.affected_module_count or len(set(names)) != len(names):
        raise ToolingError("canonical affected-module set is not exact")
    return names


def _verify_module_provenance(
    records: Any,
    policy: ApprovedIdentity,
    *,
    hash_file: Callable[[Path], str],
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(records, list) or len(records) != policy.affected_module_count:
        return ["affected_module_count_mismatch"]
    try:
        expected_names = set(_canonical_module_names(policy))
    except ToolingError:
        return ["canonical_affected_module_set_unavailable"]
    root = Path(policy.source_root).resolve(strict=False)
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            reasons.append("module_record_invalid")
            continue
        name = str(record.get("module_name") or "")
        if name in seen:
            reasons.append(f"duplicate_module:{name}")
            continue
        seen.add(name)
        try:
            expected_path = _module_source_path(root, name)
            resolved_path = Path(str(record.get("resolved_source_path") or ""))
            origin = Path(str(record.get("spec_origin") or ""))
            resolved_expected = expected_path.resolve(strict=True)
            try:
                resolved_expected.relative_to(root)
            except ValueError:
                reasons.append(f"module_path_escape:{name}")
            if not record.get("loaded") or record.get("load_timing") != "boot":
                reasons.append(f"module_not_loaded:{name}")
            if resolved_expected != resolved_path.resolve(strict=True):
                reasons.append(f"module_path_mismatch:{name}")
            if resolved_expected != origin.resolve(strict=True):
                reasons.append(f"module_origin_mismatch:{name}")
            actual_hash = hash_file(expected_path)
            if (
                record.get("expected_source_path") != str(expected_path)
                or record.get("expected_source_sha256") != actual_hash
                or record.get("source_sha256") != actual_hash
            ):
                reasons.append(f"module_hash_mismatch:{name}")
        except (OSError, ValueError, ToolingError):
            reasons.append(f"module_source_unavailable:{name}")
    if seen != expected_names:
        reasons.append("affected_module_set_mismatch")
    return reasons


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_gate_a_loaded(
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    expected_pin_sha256: str,
    installation_receipt: Mapping[str, Any],
    refresh_receipt: Mapping[str, Any],
    manifest_path: str | Path,
    pin_path: str | Path = DEFAULT_PIN_PATH,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    policy: ApprovedIdentity = APPROVED,
    now: dt.datetime | None = None,
    hash_file: Callable[[Path], str] = sha256_file,
    pin_loader: Callable[..., Mapping[str, Any]] = production_pin_loader,
    observer: Any | None = None,
) -> dict[str, Any]:
    """Verify Gate A against the installed pin and one exact refresh receipt."""
    reasons: list[str] = []
    if not isinstance(refresh_receipt, Mapping):
        refresh_receipt = {}
    observed_now = now or _utc_now()
    required_evidence = {
        "observed_at",
        "refresh_id",
        "manifest_path",
        "manifest_sha256",
        "manifest_file_sha256",
        "installation",
        "launch",
        "readiness",
        "current_source_head",
        "current_source_tree",
        "current_source_manifest_digest",
        "current_configuration_fingerprint",
        "current_configuration_schema_version",
        "current_configuration_policy_id",
        "source_clean",
        "pin_sha256",
        "worker_pin_path",
        "master",
        "children",
        "worker_match_pids",
        "process_observation",
        "identity_verification",
        "beat_before",
        "beat_after",
        "ping_ok",
        "ping_node",
        "active",
        "reserved",
        "scheduled",
        "queue_depth",
        "slot_keys",
        "leases",
        "active_sessions",
    }
    missing_evidence = required_evidence - set(evidence)
    if missing_evidence:
        reasons.append("missing_required_evidence_fields")

    try:
        installed_pin_sha = _verify_install_receipt(
            installation_receipt,
            pin_path=pin_path,
            runtime_root=runtime_root,
            policy=policy,
            now=observed_now,
            loader=pin_loader,
        )
    except ToolingError as exc:
        installed_pin_sha = None
        reasons.append(f"installation_receipt_invalid:{exc}")

    refresh_required = {
        "receipt_schema_version",
        "refresh_id",
        "installation",
        "launch",
        "process_observation",
        "identity_verification",
        "readiness",
        "gate_a",
        "approval_id",
        "pin_sha256",
        "pin_path",
        "installation_receipt_sha256",
        "master_pid",
        "worker_start_ticks",
        "linux_boot_id",
        "worker_node_identity",
        "source_identity",
        "launcher_identity",
        "pre_signal_worker_identity",
        "pre_signal_observed_at",
        "worker_process_identity",
        "beat_process_identity",
        "manifest_path",
        "manifest_record_id",
        "manifest_sha256",
        "manifest_mtime_ns",
        "launch_started_at",
        "ready_at",
    }
    if not isinstance(refresh_receipt, Mapping) or not refresh_required <= set(
        refresh_receipt
    ):
        reasons.append("refresh_receipt_incomplete")
    else:
        try:
            uuid.UUID(str(refresh_receipt.get("refresh_id")))
        except (TypeError, ValueError, AttributeError):
            reasons.append("refresh_receipt_identity_invalid")
        if refresh_receipt.get("receipt_schema_version") != REFRESH_RECEIPT_SCHEMA_VERSION:
            reasons.append("refresh_receipt_schema_invalid")
        if refresh_receipt.get("source_identity") != _policy_source_identity(policy):
            reasons.append("refresh_receipt_source_identity_mismatch")
        if refresh_receipt.get("launcher_identity") != policy.launcher_identity:
            reasons.append("refresh_receipt_launcher_identity_mismatch")
        try:
            pre_signal_worker = ProcessIdentity.from_mapping(
                refresh_receipt.get("pre_signal_worker_identity", {})
            )
            _assert_approved_launcher(
                pre_signal_worker, policy, pid_expected=pre_signal_worker.pid
            )
            if pre_signal_worker.linux_boot_id != refresh_receipt.get(
                "beat_process_identity", {}
            ).get("linux_boot_id"):
                reasons.append("pre_signal_worker_beat_boot_id_mismatch")
        except ToolingError:
            reasons.append("refresh_receipt_pre_signal_worker_identity_invalid")
        if (
            refresh_receipt.get("installation") != "INSTALLED"
            or refresh_receipt.get("launch") != "LAUNCH_ATTEMPTED"
            or refresh_receipt.get("process_observation") != "PROCESS_OBSERVED"
            or refresh_receipt.get("identity_verification") != "IDENTITY_VERIFIED"
            or refresh_receipt.get("readiness") != "READY"
            or refresh_receipt.get("gate_a") != "NOT_RUN"
        ):
            reasons.append("refresh_receipt_stages_incomplete")
        if refresh_receipt.get("approval_id") != policy.approval_id:
            reasons.append("refresh_receipt_approval_mismatch")
        if refresh_receipt.get("pin_sha256") != expected_pin_sha256:
            reasons.append("refresh_receipt_pin_hash_mismatch")
        if refresh_receipt.get("pin_path") != str(Path(pin_path).absolute()):
            reasons.append("refresh_receipt_pin_path_mismatch")
        if refresh_receipt.get("installation_receipt_sha256") != hashlib.sha256(
            _json_bytes(installation_receipt)
        ).hexdigest():
            reasons.append("refresh_receipt_installation_binding_mismatch")

    if installed_pin_sha != expected_pin_sha256:
        reasons.append("installed_pin_sha256_mismatch")
    if evidence.get("refresh_id") != refresh_receipt.get("refresh_id"):
        reasons.append("health_evidence_refresh_identity_mismatch")
    if evidence.get("manifest_path") != refresh_receipt.get("manifest_path"):
        reasons.append("health_evidence_manifest_path_mismatch")

    try:
        recorded_master = _manifest_process(manifest)
        launched_at = _parse_timestamp(refresh_receipt.get("launch_started_at"))
        ready_at = _parse_timestamp(refresh_receipt.get("ready_at"))
        booted_at = _parse_timestamp(manifest.get("boot_timestamp"))
        if launched_at > ready_at:
            reasons.append("refresh_receipt_time_order_invalid")
        if booted_at < launched_at.replace(microsecond=0):
            reasons.append("manifest_predates_refresh_launch")
        if ready_at < booted_at:
            reasons.append("refresh_receipt_ready_before_manifest_boot")
        _fresh(
            evidence.get("observed_at"),
            observed_now,
            after=booted_at,
        )
    except ToolingError as exc:
        recorded_master = None
        reasons.append(str(exc))

    if recorded_master is not None:
        expected_manifest_path = _manifest_path_for_identity(runtime_root, recorded_master)
        selected_path = Path(manifest_path)
        if (
            selected_path != expected_manifest_path
            or refresh_receipt.get("manifest_path") != str(expected_manifest_path)
            or evidence.get("manifest_path") != str(expected_manifest_path)
        ):
            reasons.append("manifest_path_not_canonical_or_not_refresh_bound")
        if manifest.get("record_id") != (
            f"{recorded_master.linux_boot_id}-{recorded_master.pid}-{recorded_master.start_ticks}"
        ):
            reasons.append("manifest_record_id_mismatch")
        try:
            raw_manifest, manifest_info = _read_regular_file(
                selected_path, max_bytes=MAX_MANIFEST_BYTES
            )
            actual_manifest_sha = hashlib.sha256(raw_manifest).hexdigest()
            if _decode_json(raw_manifest, "worker manifest") != manifest:
                reasons.append("manifest_file_content_mismatch")
            if actual_manifest_sha != refresh_receipt.get("manifest_sha256"):
                reasons.append("refresh_receipt_manifest_hash_mismatch")
            if actual_manifest_sha != evidence.get("manifest_sha256"):
                reasons.append("manifest_sha256_evidence_mismatch")
            if actual_manifest_sha != evidence.get("manifest_file_sha256"):
                reasons.append("manifest_file_sha256_evidence_mismatch")
            if manifest_info.st_mtime_ns != refresh_receipt.get("manifest_mtime_ns"):
                reasons.append("manifest_file_mtime_receipt_mismatch")
            if manifest_info.st_mtime_ns < _timestamp_ns(launched_at):
                reasons.append("manifest_file_stale_for_refresh")
        except ToolingError:
            reasons.append("refresh_manifest_file_unavailable")
        if refresh_receipt.get("manifest_record_id") != manifest.get("record_id"):
            reasons.append("refresh_receipt_manifest_identity_mismatch")
        try:
            receipt_process = ProcessIdentity.from_mapping(
                refresh_receipt.get("worker_process_identity", {})
            )
            if receipt_process.to_dict() != recorded_master.to_dict():
                reasons.append("refresh_receipt_worker_identity_mismatch")
        except ToolingError:
            reasons.append("refresh_receipt_worker_identity_incomplete")
        if (
            refresh_receipt.get("master_pid") != recorded_master.pid
            or str(refresh_receipt.get("worker_start_ticks"))
            != recorded_master.start_ticks
            or refresh_receipt.get("linux_boot_id") != recorded_master.linux_boot_id
            or refresh_receipt.get("worker_node_identity")
            != policy.worker_node_identity
        ):
            reasons.append("refresh_receipt_worker_fields_mismatch")

    exact_fields = {
        "candidate_head": policy.candidate_head,
        "candidate_tree": policy.candidate_tree,
        "candidate_source_root": policy.source_root,
        "source_manifest_digest": policy.source_manifest_digest,
        "configuration_fingerprint": policy.configuration_fingerprint,
        "configuration_schema_version": policy.configuration_schema_version,
        "configuration_policy_id": policy.configuration_policy_id,
        "worker_node_identity": policy.worker_node_identity,
    }
    for field, expected in exact_fields.items():
        if manifest.get(field) != expected:
            reasons.append(f"identity_mismatch:{field}")
    current_fields = {
        "current_source_head": policy.candidate_head,
        "current_source_tree": policy.candidate_tree,
        "current_source_manifest_digest": policy.source_manifest_digest,
        "current_configuration_fingerprint": policy.configuration_fingerprint,
        "current_configuration_schema_version": policy.configuration_schema_version,
        "current_configuration_policy_id": policy.configuration_policy_id,
    }
    for field, expected in current_fields.items():
        if evidence.get(field) != expected:
            reasons.append(f"current_identity_mismatch:{field}")
    if evidence.get("source_clean") is not True:
        reasons.append("current_source_not_clean")
    if evidence.get("installation") != "INSTALLED":
        reasons.append("installation_not_installed")
    if evidence.get("launch") != "LAUNCH_ATTEMPTED":
        reasons.append("worker_not_launched")
    if evidence.get("process_observation") != "PROCESS_OBSERVED":
        reasons.append("worker_process_not_observed")
    if evidence.get("identity_verification") != "IDENTITY_VERIFIED":
        reasons.append("worker_identity_not_verified")
    if evidence.get("readiness") != "READY":
        reasons.append("worker_not_ready")
    if manifest.get("identity_status") != "IDENTITY_VERIFIED":
        reasons.append("identity_status_not_verified")
    if manifest.get("candidate_pin_status") != "PIN_MATCHED":
        reasons.append("candidate_pin_status_not_matched")
    if manifest.get("identity_status_reasons") != []:
        reasons.append("identity_status_reasons_not_empty")
    if list(manifest.get("worker_argv") or []) != policy.launcher_identity["argv"]:
        reasons.append("launcher_argv_mismatch")
    if manifest.get("worker_cwd") != policy.launcher_identity["cwd"]:
        reasons.append("launcher_cwd_mismatch")
    if manifest.get("worker_executable") != policy.launcher_identity["executable"]:
        reasons.append("launcher_executable_mismatch")

    pin_identity = manifest.get("candidate_pin_identity")
    if not isinstance(pin_identity, Mapping):
        reasons.append("candidate_pin_identity_missing")
    else:
        if pin_identity.get("pin_schema_version") != 1:
            reasons.append("pin_schema_version_mismatch")
        if pin_identity.get("approval_id") != policy.approval_id:
            reasons.append("approval_id_mismatch")
        if pin_identity.get("operator_identity") != policy.operator_identity:
            reasons.append("operator_identity_mismatch")
        if pin_identity.get("pin_sha256") != expected_pin_sha256:
            reasons.append("pin_sha256_mismatch")
        try:
            approved = _parse_timestamp(pin_identity.get("approved_at"))
            expires = _parse_timestamp(pin_identity.get("expires_at"))
            booted = _parse_timestamp(manifest.get("boot_timestamp"))
            if not approved < booted < expires:
                reasons.append("approval_window_invalid")
        except ToolingError:
            reasons.append("approval_timestamps_invalid")

    reasons.extend(
        _verify_module_provenance(
            manifest.get("affected_module_provenance"), policy, hash_file=hash_file
        )
    )

    try:
        recorded_master = _manifest_process(manifest)
        current_master = ProcessIdentity.from_mapping(evidence.get("master", {}))
        if current_master.to_dict() != recorded_master.to_dict():
            reasons.append("worker_master_process_identity_mismatch")
        if refresh_receipt.get("worker_process_identity") != current_master.to_dict():
            reasons.append("process_evidence_conflicts_with_refresh_receipt")
        recorded_children = [
            ProcessIdentity.from_mapping(item) for item in manifest.get("worker_children", [])
        ]
        current_children = [
            ProcessIdentity.from_mapping(item) for item in evidence.get("children", [])
        ]
        if [item.to_dict() for item in current_children] != [item.to_dict() for item in recorded_children]:
            reasons.append("worker_child_process_identity_mismatch")
        expected_matches = sorted([recorded_master.pid, *(item.pid for item in recorded_children)])
        try:
            current_matches = sorted(set(int(item) for item in evidence.get("worker_match_pids", [])))
        except (TypeError, ValueError):
            reasons.append("worker_process_match_set_invalid")
        else:
            if current_matches != expected_matches:
                reasons.append("worker_process_match_set_mismatch")
    except ToolingError:
        reasons.append("worker_process_identity_evidence_invalid")

    try:
        refresh_beat = _beat_identity_from_receipt(
            refresh_receipt.get("beat_process_identity", {}), policy, runtime_root
        )
        beat_before = ProcessIdentity.from_mapping(evidence.get("beat_before", {}))
        beat_after = ProcessIdentity.from_mapping(evidence.get("beat_after", {}))
        if beat_before.to_dict() != refresh_beat.to_dict():
            reasons.append("health_beat_before_conflicts_with_refresh_receipt")
        if beat_after.to_dict() != refresh_beat.to_dict():
            reasons.append("health_beat_after_conflicts_with_refresh_receipt")
        current_observer = observer or ProcfsObserver()
        current_beat = _observe_approved_beat(current_observer, policy, runtime_root)
        if current_beat.to_dict() != refresh_beat.to_dict():
            reasons.append("current_beat_identity_differs_from_refresh_receipt")
        if recorded_master is not None and refresh_beat.linux_boot_id != recorded_master.linux_boot_id:
            reasons.append("beat_worker_linux_boot_id_mismatch")
    except ToolingError:
        reasons.append("beat_identity_evidence_invalid_or_not_celery_beat")

    if evidence.get("pin_sha256") != expected_pin_sha256:
        reasons.append("installed_pin_sha256_mismatch")
    if (
        not isinstance(expected_pin_sha256, str)
        or len(expected_pin_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_pin_sha256)
    ):
        reasons.append("expected_pin_sha256_invalid")
    if evidence.get("worker_pin_path") != str(pin_path):
        reasons.append("worker_pin_path_mismatch")
    if evidence.get("ping_ok") is not True:
        reasons.append("worker_ping_failed")
    if evidence.get("ping_node") != policy.worker_node_identity:
        reasons.append("worker_ping_node_mismatch")
    for field in ("active", "reserved", "scheduled", "slot_keys", "leases"):
        if evidence.get(field) != []:
            reasons.append(f"worker_not_idle:{field}")
    if type(evidence.get("queue_depth")) is not int or evidence.get("queue_depth") != 0:
        reasons.append("queue_not_empty")
    if type(evidence.get("active_sessions")) is not int or evidence.get("active_sessions") != 0:
        reasons.append("active_sessions_present_or_unknown")

    if reasons:
        raise ToolingError("Gate A not verified: " + ", ".join(dict.fromkeys(reasons)))
    return {
        "installation": evidence.get("installation", "INSTALLED"),
        "launch": evidence.get("launch", "LAUNCH_ATTEMPTED"),
        "process_observation": evidence.get("process_observation", "PROCESS_OBSERVED"),
        "identity_verification": evidence.get("identity_verification", "IDENTITY_VERIFIED"),
        "readiness": evidence.get("readiness", "READY"),
        "gate_a": "GATE_A_VERIFIED",
        "approval_id": policy.approval_id,
        "pin_sha256": expected_pin_sha256,
        "manifest_sha256": refresh_receipt.get("manifest_sha256"),
        "refresh_id": refresh_receipt.get("refresh_id"),
        "verified_at": _timestamp(observed_now),
    }


def verify_gate_a(
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    refresh_id: str,
    expected_pin_sha256: str,
    installation_receipt: Mapping[str, Any],
    refresh_receipt: Mapping[str, Any] | None,
    manifest_path: str | Path,
    pin_path: str | Path = DEFAULT_PIN_PATH,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    policy: ApprovedIdentity = APPROVED,
    now: dt.datetime | None = None,
    hash_file: Callable[[Path], str] = sha256_file,
    pin_loader: Callable[..., Mapping[str, Any]] = production_pin_loader,
    observer: Any | None = None,
) -> dict[str, Any]:
    """Load historical truth from protected storage before validating Gate A."""
    authority = RefreshAuthority(DEFAULT_REFRESH_AUTHORITY_ROOT)
    observed_now = now or _utc_now()
    if observed_now.tzinfo is None:
        raise ToolingError("Gate A clock must be timezone-aware")
    try:
        with authority.locked(exclusive=False, create=False) as directory_fd:
            authority.assert_no_unresolved_history(directory_fd)
            record = authority.load_locked(directory_fd, refresh_id)
            if record.last_stage != "READY":
                raise ToolingError("protected refresh history is incomplete or requires recovery")
            authoritative_receipt = authority.final_receipt(record)
            if record.receipt.get("source_identity") != _policy_source_identity(policy):
                raise ToolingError("protected refresh source identity differs from Gate A policy")
            if record.receipt.get("launcher_identity") != policy.launcher_identity:
                raise ToolingError("protected refresh launcher identity differs from Gate A policy")
            if refresh_receipt is not None and dict(refresh_receipt) != authoritative_receipt:
                raise ToolingError("caller refresh receipt conflicts with protected authority")
            age = (
                observed_now.astimezone(dt.timezone.utc) - authority.ready_at(record)
            ).total_seconds()
            if age < 0 or age > MAX_REFRESH_RECEIPT_AGE_SECONDS:
                raise ToolingError("protected refresh receipt is stale or from the future")
            return _verify_gate_a_loaded(
                manifest,
                evidence,
                expected_pin_sha256=expected_pin_sha256,
                installation_receipt=installation_receipt,
                refresh_receipt=authoritative_receipt,
                manifest_path=manifest_path,
                pin_path=pin_path,
                runtime_root=runtime_root,
                policy=policy,
                now=observed_now,
                hash_file=hash_file,
                pin_loader=pin_loader,
                observer=observer,
            )
    except RefreshAuthorityError as exc:
        raise ToolingError(
            "protected refresh authority is unavailable or invalid: " + str(exc)
        ) from exc


def load_json_file(path: str | Path, *, max_bytes: int = MAX_MANIFEST_BYTES) -> Mapping[str, Any]:
    raw, _ = _read_regular_file(path, max_bytes=max_bytes)
    return _decode_json(raw, Path(path).name)


def _policy_from_root(root: str | Path) -> ApprovedIdentity:
    root_text = str(Path(root).resolve())
    if root_text != APPROVED_SOURCE_ROOT:
        raise ToolingError("source root differs from the approved source identity")
    return APPROVED


def _write_receipt(path: str | Path, result: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise ToolingError("receipt target already exists")
    payload = _json_bytes(result)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="validate or install the approved candidate pin", allow_abbrev=False
    )
    install_parser.add_argument("--worksheet", required=True)
    install_parser.add_argument("--source-root", default=APPROVED_SOURCE_ROOT)
    install_parser.add_argument("--pin-dir", default=str(Path(DEFAULT_PIN_PATH).parent))
    install_parser.add_argument("--runtime-root", default=DEFAULT_RUNTIME_ROOT)
    install_parser.add_argument("--dry-run", action="store_true", default=True)
    install_parser.add_argument("--install-approved-pin", action="store_true")
    install_parser.add_argument("--receipt-out")

    refresh_parser = subparsers.add_parser(
        "refresh", help="perform one guarded worker refresh", allow_abbrev=False
    )
    refresh_parser.add_argument("--source-root", default=APPROVED_SOURCE_ROOT)
    refresh_parser.add_argument("--runtime-root", default=DEFAULT_RUNTIME_ROOT)
    refresh_parser.add_argument("--pin-path", default=DEFAULT_PIN_PATH)
    refresh_parser.add_argument("--master-pid", required=True, type=int)
    refresh_parser.add_argument("--start-ticks", required=True)
    refresh_parser.add_argument("--preflight-json", required=True)
    refresh_parser.add_argument("--install-receipt", required=True)
    refresh_parser.add_argument("--execute-one-refresh", action="store_true")
    refresh_parser.add_argument("--receipt-out")

    gate_parser = subparsers.add_parser(
        "gate-a", help="verify an offline/read-only Gate A evidence bundle", allow_abbrev=False
    )
    gate_parser.add_argument("--manifest", required=True)
    gate_parser.add_argument("--evidence-json", required=True)
    gate_parser.add_argument("--source-root", default=APPROVED_SOURCE_ROOT)
    gate_parser.add_argument("--runtime-root", default=DEFAULT_RUNTIME_ROOT)
    gate_parser.add_argument("--pin-sha256", required=True)
    gate_parser.add_argument("--pin-path", default=DEFAULT_PIN_PATH)
    gate_parser.add_argument("--install-receipt", required=True)
    gate_parser.add_argument("--refresh-id", required=True)
    gate_parser.add_argument("--refresh-receipt")

    args = parser.parse_args()
    try:
        policy = _policy_from_root(getattr(args, "source_root", APPROVED_SOURCE_ROOT))
        if args.command == "install":
            real_install = bool(args.install_approved_pin)
            result = install_candidate_pin(
                args.worksheet,
                pin_dir=args.pin_dir,
                runtime_root=args.runtime_root,
                dry_run=not real_install,
                policy=policy,
            )
            if real_install:
                result["approval_id"] = policy.approval_id
                if args.receipt_out:
                    _write_receipt(args.receipt_out, result)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "gate-a":
            manifest_raw, _ = _read_regular_file(args.manifest, max_bytes=MAX_MANIFEST_BYTES)
            manifest = _decode_json(manifest_raw, "worker manifest")
            evidence = dict(load_json_file(args.evidence_json))
            actual_manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
            if evidence.get("manifest_sha256") != actual_manifest_sha:
                raise ToolingError("captured manifest SHA-256 differs from the manifest file")
            if evidence.get("manifest_file_sha256") != actual_manifest_sha:
                raise ToolingError("captured manifest file SHA-256 differs from the manifest file")
            installation_receipt = load_json_file(args.install_receipt)
            refresh_receipt = (
                load_json_file(args.refresh_receipt) if args.refresh_receipt else None
            )
            receipt_pin_sha = _verify_install_receipt(
                installation_receipt,
                pin_path=args.pin_path,
                runtime_root=args.runtime_root,
                policy=policy,
            )
            if receipt_pin_sha != args.pin_sha256:
                raise ToolingError("installed pin hash differs from the requested Gate A hash")
            actual_pin_sha = receipt_pin_sha
            observer = ProcfsObserver()
            manifest_process = _manifest_process(manifest)
            current_master = observer.identity(manifest_process.pid)
            if current_master is None:
                raise ToolingError("current worker master is unavailable")
            current_pin_path = observer.environment_value(
                current_master.pid, "WORKER_CANDIDATE_PIN_PATH"
            )
            if evidence.get("worker_pin_path") != observer.environment_value(
                current_master.pid, "WORKER_CANDIDATE_PIN_PATH"
            ):
                raise ToolingError("operator pin-path evidence contradicts the current worker")
            evidence.update(
                {
                    "master": current_master.to_dict(),
                    "children": [item.to_dict() for item in observer.children(current_master.pid)],
                    "worker_match_pids": observer.worker_match_pids(),
                    "manifest_sha256": actual_manifest_sha,
                    "manifest_file_sha256": actual_manifest_sha,
                    "current_source_head": policy.candidate_head,
                    "current_source_tree": policy.candidate_tree,
                    "current_source_manifest_digest": policy.source_manifest_digest,
                    "current_configuration_fingerprint": policy.configuration_fingerprint,
                    "current_configuration_schema_version": policy.configuration_schema_version,
                    "current_configuration_policy_id": policy.configuration_policy_id,
                    "source_clean": True,
                }
            )
            verify_current_checkout_identity(args.source_root, policy)
            result = verify_gate_a(
                manifest,
                evidence,
                refresh_id=args.refresh_id,
                expected_pin_sha256=args.pin_sha256,
                installation_receipt=installation_receipt,
                refresh_receipt=refresh_receipt,
                manifest_path=args.manifest,
                pin_path=args.pin_path,
                runtime_root=args.runtime_root,
                policy=policy,
                observer=observer,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "refresh":
            if not args.execute_one_refresh:
                raise ToolingError("refresh requires the explicit --execute-one-refresh flag")
            preflight = load_json_file(args.preflight_json)
            receipt = load_json_file(args.install_receipt)
            observer = ProcfsObserver()
            result = refresh_worker(
                master_pid=args.master_pid,
                expected_start_ticks=args.start_ticks,
                preflight=preflight,
                installation_receipt=receipt,
                observer=observer,
                launcher=run_start_worker_only_launcher,
                manifest_reader=find_fresh_manifest,
                ping=celery_ping,
                source_root=args.source_root,
                pin_path=args.pin_path,
                runtime_root=args.runtime_root,
                policy=policy,
            )
            if args.receipt_out:
                _write_receipt(args.receipt_out, result)
            print(json.dumps(result, sort_keys=True))
            return 0
    except RecoveryRequiredError as exc:
        print(json.dumps({**exc.result, "error": str(exc)}, sort_keys=True))
        return 3
    except ToolingError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
