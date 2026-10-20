"""Root-owned append-only authority for Phase 39 worker refresh receipts.

The SHA-256 chain detects accidental damage and binds caller evidence to one
durable history. It is not a signature and does not protect against a
compromised root account.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


AUTHORITY_SCHEMA_VERSION = 1
REFRESH_RECEIPT_SCHEMA_VERSION = 3
DEFAULT_AUTHORITY_ROOT = "/root/.orchestrator/worker-refresh-authority"
MAX_RECORD_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_STAGES = (
    "PREPARED",
    "SIGTERM_SENT",
    "WORKER_EXITED",
    "LAUNCH_STARTED",
    "LAUNCH_ATTEMPTED",
    "PROCESS_OBSERVED",
    "IDENTITY_VERIFIED",
    "READY",
)
_NEXT_STAGE = {current: following for current, following in zip(_STAGES, _STAGES[1:])}
_PROCESS_KEYS = frozenset(
    {"pid", "start_ticks", "linux_boot_id", "executable", "cwd", "argv"}
)
_SOURCE_KEYS = frozenset(
    {
        "source_root",
        "head",
        "tree",
        "source_manifest_digest",
        "configuration_fingerprint",
        "configuration_schema_version",
        "configuration_policy_id",
    }
)
_LAUNCHER_KEYS = frozenset({"launcher", "executable", "cwd", "argv"})
_BASE_RECEIPT_KEYS = frozenset(
    {
        "receipt_schema_version",
        "refresh_id",
        "source_identity",
        "launcher_identity",
        "installation",
        "approval_id",
        "pin_sha256",
        "pin_path",
        "installation_receipt_sha256",
        "worker_node_identity",
        "pre_signal_worker_identity",
        "beat_process_identity",
        "pre_signal_observed_at",
    }
)
_EVENT_KEYS = frozenset(
    {
        "authority_schema_version",
        "refresh_id",
        "sequence",
        "stage",
        "recorded_at",
        "receipt_sha256",
        "previous_event_sha256",
        "payload",
        "event_sha256",
    }
)


class RefreshAuthorityError(RuntimeError):
    """The protected receipt authority is unavailable or inconsistent."""


@dataclass(frozen=True)
class AuthorityRecord:
    refresh_id: str
    receipt: dict[str, Any]
    receipt_sha256: str
    events: tuple[dict[str, Any], ...]

    @property
    def last_stage(self) -> str:
        return str(self.events[-1]["stage"])


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RefreshAuthorityError("authority JSON is not canonicalizable") from exc


def _parse_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RefreshAuthorityError("refresh ID is invalid") from exc
    canonical = str(parsed)
    if value != canonical:
        raise RefreshAuthorityError("refresh ID must use canonical lowercase UUID form")
    return canonical


def _parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RefreshAuthorityError("authority timestamp is malformed")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RefreshAuthorityError("authority timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise RefreshAuthorityError("authority timestamp has no timezone")
    return parsed.astimezone(dt.timezone.utc)


def _valid_process_identity(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != _PROCESS_KEYS:
        return False
    if type(value.get("pid")) is not int or value["pid"] <= 1:
        return False
    if not isinstance(value.get("start_ticks"), str) or not value["start_ticks"].isdigit():
        return False
    if int(value["start_ticks"]) <= 0:
        return False
    if not isinstance(value.get("linux_boot_id"), str):
        return False
    try:
        if str(uuid.UUID(value["linux_boot_id"])) != value["linux_boot_id"]:
            return False
    except ValueError:
        return False
    if not isinstance(value.get("executable"), str) or not Path(value["executable"]).is_absolute():
        return False
    if not isinstance(value.get("cwd"), str) or not Path(value["cwd"]).is_absolute():
        return False
    argv = value.get("argv")
    return isinstance(argv, list) and bool(argv) and all(
        isinstance(item, str) and item for item in argv
    )


def _validate_base_receipt(refresh_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BASE_RECEIPT_KEYS:
        raise RefreshAuthorityError("pre-signal receipt schema is incomplete or unknown")
    if value.get("receipt_schema_version") != REFRESH_RECEIPT_SCHEMA_VERSION:
        raise RefreshAuthorityError("pre-signal receipt schema version is unsupported")
    if value.get("installation") != "INSTALLED":
        raise RefreshAuthorityError("pre-signal installation state is incomplete")
    if value.get("refresh_id") != refresh_id:
        raise RefreshAuthorityError("pre-signal receipt refresh ID mismatch")
    source = value.get("source_identity")
    if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS:
        raise RefreshAuthorityError("pre-signal source identity is incomplete")
    if not isinstance(source.get("source_root"), str) or not Path(
        source["source_root"]
    ).is_absolute():
        raise RefreshAuthorityError("pre-signal source root is invalid")
    for field in ("head", "tree"):
        if not isinstance(source.get(field), str) or not re.fullmatch(
            r"[a-f0-9]{40}", source[field]
        ):
            raise RefreshAuthorityError(f"pre-signal source {field} is invalid")
    for field in ("source_manifest_digest", "configuration_fingerprint"):
        if not isinstance(source.get(field), str) or not _SHA256.fullmatch(source[field]):
            raise RefreshAuthorityError(f"pre-signal source {field} is invalid")
    if type(source.get("configuration_schema_version")) is not int:
        raise RefreshAuthorityError("pre-signal configuration schema version is invalid")
    if not isinstance(source.get("configuration_policy_id"), str) or not source[
        "configuration_policy_id"
    ]:
        raise RefreshAuthorityError("pre-signal configuration policy is invalid")
    launcher = value.get("launcher_identity")
    if not isinstance(launcher, Mapping) or set(launcher) != _LAUNCHER_KEYS:
        raise RefreshAuthorityError("pre-signal launcher identity is incomplete")
    if any(not isinstance(launcher.get(field), str) or not launcher[field] for field in ("launcher", "executable", "cwd")):
        raise RefreshAuthorityError("pre-signal launcher identity is invalid")
    if not Path(launcher["executable"]).is_absolute() or not Path(launcher["cwd"]).is_absolute():
        raise RefreshAuthorityError("pre-signal launcher paths are invalid")
    argv = launcher.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise RefreshAuthorityError("pre-signal launcher argv is invalid")
    for field in ("approval_id", "worker_node_identity"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RefreshAuthorityError(f"pre-signal {field} is invalid")
    for field in ("pin_sha256", "installation_receipt_sha256"):
        if not isinstance(value.get(field), str) or not _SHA256.fullmatch(value[field]):
            raise RefreshAuthorityError(f"pre-signal {field} is invalid")
    if not isinstance(value.get("pin_path"), str) or not Path(value["pin_path"]).is_absolute():
        raise RefreshAuthorityError("pre-signal pin path is invalid")
    if not _valid_process_identity(value.get("pre_signal_worker_identity")):
        raise RefreshAuthorityError("pre-signal worker identity is incomplete")
    beat = value.get("beat_process_identity")
    if not isinstance(beat, Mapping) or set(beat) != _PROCESS_KEYS | {"role"}:
        raise RefreshAuthorityError("historical Beat identity is incomplete")
    if beat.get("role") != "celery-beat" or not _valid_process_identity(
        {key: beat[key] for key in _PROCESS_KEYS} if set(beat) == _PROCESS_KEYS | {"role"} else None
    ):
        raise RefreshAuthorityError("historical Beat identity is invalid")
    _parse_time(value.get("pre_signal_observed_at"))
    return dict(value)


def _validate_stage_payload(stage: str, payload: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RefreshAuthorityError("authority event payload is not an object")
    if stage == "PREPARED":
        if set(payload) != {"receipt"}:
            raise RefreshAuthorityError("PREPARED payload schema is invalid")
        _validate_base_receipt(str(receipt["refresh_id"]), payload["receipt"])
        if dict(payload["receipt"]) != dict(receipt):
            raise RefreshAuthorityError("PREPARED receipt differs from the historical receipt")
    elif stage == "SIGTERM_SENT":
        if set(payload) != {"signum", "pidfd_bound"} or payload.get("signum") != 15 or payload.get("pidfd_bound") is not True:
            raise RefreshAuthorityError("SIGTERM_SENT payload is invalid")
    elif stage == "WORKER_EXITED":
        if set(payload) != {"pid", "start_ticks"}:
            raise RefreshAuthorityError("WORKER_EXITED payload schema is invalid")
        old = receipt["pre_signal_worker_identity"]
        if payload.get("pid") != old["pid"] or payload.get("start_ticks") != old["start_ticks"]:
            raise RefreshAuthorityError("WORKER_EXITED identity conflicts with PREPARED")
    elif stage == "LAUNCH_STARTED":
        if set(payload) != {"launch_started_at"}:
            raise RefreshAuthorityError("LAUNCH_STARTED payload schema is invalid")
        _parse_time(payload["launch_started_at"])
    elif stage == "LAUNCH_ATTEMPTED":
        if set(payload) != {"worker_pid", "returncode", "launch_attempts"}:
            raise RefreshAuthorityError("LAUNCH_ATTEMPTED payload schema is invalid")
        if payload["worker_pid"] is not None and (
            type(payload["worker_pid"]) is not int or payload["worker_pid"] <= 1
        ):
            raise RefreshAuthorityError("LAUNCH_ATTEMPTED worker PID is invalid")
        if payload["returncode"] is not None and type(payload["returncode"]) is not int:
            raise RefreshAuthorityError("LAUNCH_ATTEMPTED return code is invalid")
        if payload["launch_attempts"] is not None and (
            type(payload["launch_attempts"]) is not int or payload["launch_attempts"] != 1
        ):
            raise RefreshAuthorityError("LAUNCH_ATTEMPTED attempt count is invalid")
    elif stage in {"PROCESS_OBSERVED", "IDENTITY_VERIFIED"}:
        if set(payload) != {"worker_process_identity"} or not _valid_process_identity(
            payload.get("worker_process_identity")
        ):
            raise RefreshAuthorityError(f"{stage} worker identity is invalid")
    elif stage == "READY":
        required = {
            "worker_process_identity",
            "master_pid",
            "worker_start_ticks",
            "linux_boot_id",
            "manifest_path",
            "manifest_record_id",
            "manifest_sha256",
            "manifest_mtime_ns",
            "launch_started_at",
            "ready_at",
            "ping",
            "launcher_returncode",
        }
        if set(payload) != required:
            raise RefreshAuthorityError("READY payload schema is incomplete or unknown")
        process = payload["worker_process_identity"]
        if not _valid_process_identity(process):
            raise RefreshAuthorityError("READY worker identity is invalid")
        if (
            type(payload["master_pid"]) is not int
            or payload["master_pid"] != process["pid"]
            or not isinstance(payload["worker_start_ticks"], str)
            or payload["worker_start_ticks"] != process["start_ticks"]
            or payload["linux_boot_id"] != process["linux_boot_id"]
        ):
            raise RefreshAuthorityError("READY worker fields contradict process identity")
        for field in ("manifest_path", "manifest_record_id"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise RefreshAuthorityError(f"READY {field} is invalid")
        if not Path(payload["manifest_path"]).is_absolute():
            raise RefreshAuthorityError("READY manifest path is invalid")
        if not isinstance(payload["manifest_sha256"], str) or not _SHA256.fullmatch(payload["manifest_sha256"]):
            raise RefreshAuthorityError("READY manifest digest is invalid")
        if type(payload["manifest_mtime_ns"]) is not int or payload["manifest_mtime_ns"] <= 0:
            raise RefreshAuthorityError("READY manifest mtime is invalid")
        launch_at = _parse_time(payload["launch_started_at"])
        ready_at = _parse_time(payload["ready_at"])
        if ready_at < launch_at:
            raise RefreshAuthorityError("READY timestamp precedes launch")
        if payload["ping"] != "OK":
            raise RefreshAuthorityError("READY ping state is invalid")
        if payload["launcher_returncode"] is not None and type(payload["launcher_returncode"]) is not int:
            raise RefreshAuthorityError("READY launcher return code is invalid")
    elif stage == "RECOVERY_REQUIRED":
        if set(payload) != {"reason"} or not isinstance(payload.get("reason"), str) or not payload["reason"]:
            raise RefreshAuthorityError("RECOVERY_REQUIRED payload is invalid")
    else:
        raise RefreshAuthorityError("authority stage is unsupported")
    return dict(payload)


class RefreshAuthority:
    """Secure directory and append-only per-refresh event journal."""

    def __init__(
        self,
        root: str | Path = DEFAULT_AUTHORITY_ROOT,
        *,
        required_uid: int = 0,
        euid: Callable[[], int] = os.geteuid,
        clock: Callable[[], dt.datetime] | None = None,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.root = Path(root)
        self.required_uid = required_uid
        self.euid = euid
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.fsync = fsync

    def _check_directory(self, fd: int, *, final: bool) -> None:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise RefreshAuthorityError("authority directory type or owner is invalid")
        if final:
            if info.st_uid != self.required_uid:
                raise RefreshAuthorityError("authority directory type or owner is invalid")
        elif info.st_uid not in {0, self.required_uid}:
            raise RefreshAuthorityError("authority directory type or owner is invalid")
        mode = stat.S_IMODE(info.st_mode)
        if final:
            if mode != 0o700:
                raise RefreshAuthorityError("authority directory mode must be 0700")
        elif mode & 0o022 and not (mode & stat.S_ISVTX and info.st_uid == 0):
            raise RefreshAuthorityError("authority path ancestor is writable by other users")

    def _open_directory(self, *, create: bool) -> int:
        if self.euid() != self.required_uid:
            raise RefreshAuthorityError("root privileges are required for refresh authority")
        if not self.root.is_absolute():
            raise RefreshAuthorityError("authority path must be absolute")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open("/", flags)
        try:
            self._check_directory(current_fd, final=False)
            parts = self.root.parts[1:]
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise RefreshAuthorityError("authority path components are invalid")
            for index, component in enumerate(parts):
                final = index == len(parts) - 1
                if final and create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                        self.fsync(current_fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                self._check_directory(current_fd, final=final)
            return current_fd
        except OSError as exc:
            os.close(current_fd)
            raise RefreshAuthorityError("authority path is unavailable or traverses a symlink") from exc
        except Exception:
            os.close(current_fd)
            raise

    @contextlib.contextmanager
    def locked(self, *, exclusive: bool, create: bool = False) -> Iterator[int]:
        directory_fd = self._open_directory(create=create)
        lock_fd = -1
        try:
            lock_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if create:
                lock_flags |= os.O_CREAT
            try:
                lock_fd = os.open(".lock", lock_flags, 0o600, dir_fd=directory_fd)
            except OSError as exc:
                raise RefreshAuthorityError("authority lock is unavailable") from exc
            self._check_regular_file(lock_fd, expected_mode=0o600, label="authority lock")
            try:
                fcntl.flock(
                    lock_fd,
                    (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise RefreshAuthorityError("another refresh or Gate A owns the authority lock") from exc
            yield directory_fd
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(directory_fd)

    def _check_regular_file(self, fd: int, *, expected_mode: int, label: str) -> os.stat_result:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.required_uid
            or stat.S_IMODE(info.st_mode) != expected_mode
            or info.st_nlink != 1
        ):
            raise RefreshAuthorityError(f"{label} ownership, type, link count, or mode is invalid")
        return info

    def _timestamp(self) -> str:
        current = self.clock()
        if current.tzinfo is None:
            raise RefreshAuthorityError("authority clock must be timezone-aware")
        return current.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    def _build_event(
        self,
        *,
        refresh_id: str,
        sequence: int,
        stage: str,
        receipt_sha256: str,
        previous_event_sha256: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
            "refresh_id": refresh_id,
            "sequence": sequence,
            "stage": stage,
            "recorded_at": self._timestamp(),
            "receipt_sha256": receipt_sha256,
            "previous_event_sha256": previous_event_sha256,
            "payload": dict(payload),
        }
        event["event_sha256"] = hashlib.sha256(canonical_json_bytes(event)).hexdigest()
        return event

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RefreshAuthorityError("authority append made no progress")
            view = view[written:]

    def begin(self, directory_fd: int, refresh_id: str, receipt: Mapping[str, Any]) -> AuthorityRecord:
        canonical_id = _parse_uuid(refresh_id)
        validated = _validate_base_receipt(canonical_id, receipt)
        receipt_sha = hashlib.sha256(canonical_json_bytes(validated)).hexdigest()
        event = self._build_event(
            refresh_id=canonical_id,
            sequence=0,
            stage="PREPARED",
            receipt_sha256=receipt_sha,
            previous_event_sha256=None,
            payload={"receipt": validated},
        )
        name = f"{canonical_id}.jsonl"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            raise RefreshAuthorityError("refresh ID already has an immutable history") from exc
        try:
            self._check_regular_file(fd, expected_mode=0o600, label="refresh journal")
            self._write_all(fd, canonical_json_bytes(event) + b"\n")
            self.fsync(fd)
            self.fsync(directory_fd)
        finally:
            os.close(fd)
        loaded = self.load_locked(directory_fd, canonical_id)
        if loaded.receipt != validated or loaded.last_stage != "PREPARED":
            raise RefreshAuthorityError("durable PREPARED record failed read-back verification")
        return loaded

    def append_stage(
        self,
        directory_fd: int,
        refresh_id: str,
        stage: str,
        payload: Mapping[str, Any],
    ) -> AuthorityRecord:
        canonical_id = _parse_uuid(refresh_id)
        current = self.load_locked(directory_fd, canonical_id)
        last_stage = current.last_stage
        if last_stage == "RECOVERY_REQUIRED":
            raise RefreshAuthorityError("refresh history is already terminal")
        if last_stage == "READY" and stage != "RECOVERY_REQUIRED":
            raise RefreshAuthorityError("READY history can only be invalidated by recovery")
        expected_next = _NEXT_STAGE.get(last_stage)
        if stage != "RECOVERY_REQUIRED" and stage != expected_next:
            raise RefreshAuthorityError("refresh stage transition is conflicting or out of order")
        validated_payload = _validate_stage_payload(stage, payload, current.receipt)
        previous = current.events[-1]["event_sha256"]
        event = self._build_event(
            refresh_id=canonical_id,
            sequence=len(current.events),
            stage=stage,
            receipt_sha256=current.receipt_sha256,
            previous_event_sha256=previous,
            payload=validated_payload,
        )
        name = f"{canonical_id}.jsonl"
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise RefreshAuthorityError("refresh journal cannot be appended") from exc
        try:
            self._check_regular_file(fd, expected_mode=0o600, label="refresh journal")
            self._write_all(fd, canonical_json_bytes(event) + b"\n")
            self.fsync(fd)
        finally:
            os.close(fd)
        return self.load_locked(directory_fd, canonical_id)

    def _matching_history_names(self, directory_fd: int, refresh_id: str) -> list[str]:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise RefreshAuthorityError("authority directory cannot be listed") from exc
        matching: list[str] = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            stem = name[:-6]
            try:
                candidate = str(uuid.UUID(stem))
            except (ValueError, AttributeError):
                continue
            if candidate == refresh_id:
                matching.append(name)
        return matching

    def load(self, refresh_id: str) -> AuthorityRecord:
        with self.locked(exclusive=False, create=False) as directory_fd:
            return self.load_locked(directory_fd, refresh_id)

    def load_locked(self, directory_fd: int, refresh_id: str) -> AuthorityRecord:
        canonical_id = _parse_uuid(refresh_id)
        names = self._matching_history_names(directory_fd, canonical_id)
        expected_name = f"{canonical_id}.jsonl"
        if names != [expected_name]:
            if names:
                raise RefreshAuthorityError("conflicting refresh history filenames exist")
            raise RefreshAuthorityError("protected refresh record is missing")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(expected_name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise RefreshAuthorityError("protected refresh record is unreadable") from exc
        try:
            info = self._check_regular_file(fd, expected_mode=0o600, label="refresh journal")
            if info.st_size <= 0 or info.st_size > MAX_RECORD_BYTES:
                raise RefreshAuthorityError("protected refresh record size is invalid")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, MAX_RECORD_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RECORD_BYTES:
                    raise RefreshAuthorityError("protected refresh record exceeds the size limit")
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        if not raw.endswith(b"\n") or b"\n\n" in raw:
            raise RefreshAuthorityError("protected refresh record is truncated or malformed")
        lines = raw[:-1].split(b"\n")
        if not lines:
            raise RefreshAuthorityError("protected refresh record is empty")
        events: list[dict[str, Any]] = []
        receipt: dict[str, Any] | None = None
        receipt_sha: str | None = None
        previous_hash: str | None = None
        previous_stage: str | None = None
        previous_recorded_at: dt.datetime | None = None
        for sequence, line in enumerate(lines):
            try:
                event_value = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RefreshAuthorityError("protected refresh event is malformed JSON") from exc
            if not isinstance(event_value, Mapping) or set(event_value) != _EVENT_KEYS:
                raise RefreshAuthorityError("protected refresh event schema is unknown")
            event = dict(event_value)
            if canonical_json_bytes(event) != line:
                raise RefreshAuthorityError("protected refresh event serialization is not canonical")
            if (
                type(event.get("authority_schema_version")) is not int
                or event.get("authority_schema_version") != AUTHORITY_SCHEMA_VERSION
            ):
                raise RefreshAuthorityError("protected refresh authority schema is unsupported")
            if (
                event.get("refresh_id") != canonical_id
                or type(event.get("sequence")) is not int
                or event.get("sequence") != sequence
            ):
                raise RefreshAuthorityError("protected refresh sequence or ID conflicts")
            if not isinstance(event.get("recorded_at"), str):
                raise RefreshAuthorityError("protected refresh timestamp is missing")
            recorded_at = _parse_time(event["recorded_at"])
            if previous_recorded_at is not None and recorded_at < previous_recorded_at:
                raise RefreshAuthorityError("protected refresh event timestamps go backwards")
            stage = event.get("stage")
            if not isinstance(stage, str):
                raise RefreshAuthorityError("protected refresh stage is malformed")
            try:
                supplied_hash = event["event_sha256"]
                unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
                calculated_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
                if supplied_hash != calculated_hash:
                    raise RefreshAuthorityError("protected refresh event integrity check failed")
            except (TypeError, ValueError) as exc:
                raise RefreshAuthorityError("protected refresh event hash is malformed") from exc
            if sequence == 0:
                if stage != "PREPARED" or event.get("previous_event_sha256") is not None:
                    raise RefreshAuthorityError("protected refresh history has no unique PREPARED record")
                if not isinstance(event.get("payload"), Mapping) or set(event["payload"]) != {"receipt"}:
                    raise RefreshAuthorityError("protected PREPARED event schema is invalid")
                receipt = _validate_base_receipt(canonical_id, event["payload"]["receipt"])
                if recorded_at < _parse_time(receipt["pre_signal_observed_at"]):
                    raise RefreshAuthorityError("PREPARED event predates its process observation")
                receipt_sha = hashlib.sha256(canonical_json_bytes(receipt)).hexdigest()
                if event.get("receipt_sha256") != receipt_sha:
                    raise RefreshAuthorityError("protected canonical receipt SHA-256 mismatch")
            else:
                if receipt is None or receipt_sha is None:
                    raise RefreshAuthorityError("protected refresh receipt is unavailable")
                if event.get("previous_event_sha256") != previous_hash:
                    raise RefreshAuthorityError("protected refresh event chain conflicts")
                if event.get("receipt_sha256") != receipt_sha:
                    raise RefreshAuthorityError("protected refresh receipt hash conflicts")
                if previous_stage == "RECOVERY_REQUIRED":
                    raise RefreshAuthorityError("protected refresh has events after recovery")
                if previous_stage == "READY" and stage != "RECOVERY_REQUIRED":
                    raise RefreshAuthorityError("protected READY history has a conflicting transition")
                expected_next = _NEXT_STAGE.get(previous_stage or "")
                if stage not in {expected_next, "RECOVERY_REQUIRED"}:
                    raise RefreshAuthorityError("protected refresh stages are incomplete or conflicting")
            if receipt is None:
                raise RefreshAuthorityError("protected refresh PREPARED receipt is missing")
            event["payload"] = _validate_stage_payload(stage, event.get("payload"), receipt)
            if stage == "READY" and recorded_at < _parse_time(event["payload"]["ready_at"]):
                raise RefreshAuthorityError("READY event predates its readiness observation")
            events.append(event)
            previous_hash = str(event["event_sha256"])
            previous_stage = stage
            previous_recorded_at = recorded_at
        if receipt is None or receipt_sha is None:
            raise RefreshAuthorityError("protected refresh record is incomplete")
        return AuthorityRecord(canonical_id, receipt, receipt_sha, tuple(events))

    def assert_no_unresolved_history(self, directory_fd: int) -> None:
        """Reject malformed, conflicting, incomplete, or recovery-pending history."""
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise RefreshAuthorityError("authority directory cannot be listed") from exc
        seen: set[str] = set()
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            stem = name[:-6]
            try:
                canonical_id = _parse_uuid(stem)
            except RefreshAuthorityError as exc:
                raise RefreshAuthorityError("authority contains an invalid history filename") from exc
            if canonical_id in seen:
                raise RefreshAuthorityError("authority contains conflicting refresh histories")
            seen.add(canonical_id)
            if name != f"{canonical_id}.jsonl":
                raise RefreshAuthorityError("authority history filename is not canonical")
            record = self.load_locked(directory_fd, canonical_id)
            if record.last_stage != "READY":
                raise RefreshAuthorityError(
                    "an incomplete or recovery-required refresh history needs manual reconciliation"
                )

    def final_receipt(self, record: AuthorityRecord) -> dict[str, Any]:
        stages = [str(event["stage"]) for event in record.events]
        if stages != list(_STAGES):
            raise RefreshAuthorityError("refresh history does not contain the complete READY stage sequence")
        receipt = dict(record.receipt)
        receipt.update(
            {
                "installation": "INSTALLED",
                "launch": "LAUNCH_ATTEMPTED",
                "process_observation": "PROCESS_OBSERVED",
                "identity_verification": "IDENTITY_VERIFIED",
                "readiness": "READY",
                "gate_a": "NOT_RUN",
            }
        )
        launch = record.events[3]["payload"]
        attempt = record.events[4]["payload"]
        ready = record.events[7]["payload"]
        receipt.update(
            {
                "master_pid": ready["master_pid"],
                "worker_start_ticks": ready["worker_start_ticks"],
                "linux_boot_id": ready["linux_boot_id"],
                "worker_process_identity": ready["worker_process_identity"],
                "manifest_path": ready["manifest_path"],
                "manifest_record_id": ready["manifest_record_id"],
                "manifest_sha256": ready["manifest_sha256"],
                "manifest_mtime_ns": ready["manifest_mtime_ns"],
                "launch_started_at": launch["launch_started_at"],
                "ready_at": ready["ready_at"],
                "ping": ready["ping"],
                "launcher_returncode": attempt["returncode"],
            }
        )
        return receipt

    def ready_at(self, record: AuthorityRecord) -> dt.datetime:
        if record.last_stage != "READY":
            raise RefreshAuthorityError("refresh is not durably READY")
        return _parse_time(record.events[-1]["recorded_at"])
