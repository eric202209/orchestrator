"""Podman-backed one-shot executor for the fixed contained Python profile.

The Supervisor supplies only immutable hashes and authorization metadata. This
adapter resolves candidate and source snapshot bytes from the configured
content-addressed store, verifies both hashes, and launches a fixed Python
entry point. It never accepts a command, image, mount, environment, or runtime
option from a Worker request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
from typing import Any

from app.supervisor.ledger import AttemptLedger, AttemptRecord, TERMINAL_STATUSES

SUPPORTED_PROFILE = ("executor-contained-planning", "v1")
SUPPORTED_SCOPE = ("restricted-python-workspace", "1")
SCOPE_POLICY = {
    "schema": "restricted-python-workspace/1",
    "candidate": "utf-8-python-source/v1",
    "source_snapshot": "tar-regular-files-and-directories/v1",
    "entrypoint": "fixed-python-isolated-runner/v1",
    "filesystem": "read-only-input-source-plus-bounded-tmpfs/v1",
    "network": "none",
}
SUPPORTED_SCOPE_SHA256 = hashlib.sha256(
    json.dumps(SCOPE_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
MAX_EXECUTION_TIMEOUT_SECONDS = 360
MAX_CONTROL_OUTPUT_BYTES = 65536
MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 10000
MAX_CAPTURE_BYTES = 1024 * 1024
WORKSPACE_TMPFS_BYTES = 64 * 1024 * 1024
RESULT_TMPFS_BYTES = 8 * 1024 * 1024
TEMP_TMPFS_BYTES = 8 * 1024 * 1024
CONTAINER_UID = 65534
CONTAINER_GID = 65534

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_OWNER_LABEL = "io.orchestrator.supervisor"
_ATTEMPT_LABEL = "io.orchestrator.attempt-key"
_REQUEST_LABEL = "io.orchestrator.request-sha256"
_AUTH_LABEL = "io.orchestrator.authorization-sha256"
_ATTEMPT_ID_LABEL = "io.orchestrator.supervisor-attempt-id"
_OWNER_VALUE = "phase39-f3/v1"


class OCIExecutionError(RuntimeError):
    """A fail-closed executor or runtime boundary error."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ResourceLimits:
    cpu_count: float = 1.0
    memory_bytes: int = 536870912
    pids: int = 64
    timeout_seconds: int = 120
    output_bytes_per_stream: int = MAX_CAPTURE_BYTES
    workspace_tmpfs_bytes: int = WORKSPACE_TMPFS_BYTES
    result_tmpfs_bytes: int = RESULT_TMPFS_BYTES

    def as_dict(self) -> dict[str, int | float]:
        return {
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "timeout_seconds": self.timeout_seconds,
            "output_bytes_per_stream": self.output_bytes_per_stream,
            "workspace_tmpfs_bytes": self.workspace_tmpfs_bytes,
            "result_tmpfs_bytes": self.result_tmpfs_bytes,
        }


def validate_image_reference(image: str) -> str:
    value = str(image or "").strip()
    if not _IMAGE_DIGEST.fullmatch(value):
        raise OCIExecutionError("image_must_be_pinned_by_sha256")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _clean_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            item = Path(root) / name
            if item.is_symlink():
                item.unlink(missing_ok=True)
            else:
                try:
                    item.chmod(0o600)
                except OSError:
                    pass
        for name in directories:
            item = Path(root) / name
            if item.is_symlink():
                item.unlink(missing_ok=True)
            else:
                try:
                    item.chmod(0o700)
                except OSError:
                    pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


_RUNNER = r"""import os, resource, runpy, sys
limit = int(os.environ["ORCHESTRATOR_OUTPUT_LIMIT"])
timeout = int(os.environ["ORCHESTRATOR_TIMEOUT"])
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
os.chdir("/workspace")
os.environ.clear()
os.environ.update({
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "ORCHESTRATOR_SOURCE": "/source",
    "ORCHESTRATOR_WORKSPACE": "/workspace",
    "ORCHESTRATOR_RESULT": "/result",
})
out = open("/result/stdout", "wb", buffering=0)
err = open("/result/stderr", "wb", buffering=0)
os.dup2(out.fileno(), 1)
os.dup2(err.fileno(), 2)
sys.argv = ["/input/candidate.py", "--source", "/source",
            "--workspace", "/workspace", "--result", "/result"]
runpy.run_path("/input/candidate.py", run_name="__main__")
"""


class PodmanRuntime:
    """Small Podman CLI adapter. Container output never uses runtime logs."""

    def __init__(self, executable: str = "podman", *, state_dir: Path):
        candidate = str(executable or "podman")
        resolved = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        self.executable = str(resolved or candidate)
        self.state_dir = Path(state_dir).resolve()

    def _environment(self) -> dict[str, str]:
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = "/"
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.state_dir),
        }
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        try:
            info = runtime_dir.stat()
        except OSError:
            pass
        else:
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        return env

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: float = 10.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(),
                cwd=self.state_dir,
                close_fds=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OCIExecutionError("runtime_command_unavailable") from exc
        if (
            len(completed.stdout) > MAX_CONTROL_OUTPUT_BYTES
            or len(completed.stderr) > MAX_CONTROL_OUTPUT_BYTES
        ):
            raise OCIExecutionError("runtime_control_output_too_large")
        if check and completed.returncode != 0:
            raise OCIExecutionError("runtime_command_failed")
        return completed

    def available(self) -> bool:
        if not Path(self.executable).is_file() or not os.access(
            self.executable, os.X_OK
        ):
            return False
        try:
            result = self._run(["info", "--format=json"], timeout=4.0)
        except OCIExecutionError:
            return False
        return bool(result.stdout.strip())

    def image_exists(self, image: str) -> bool:
        result = self._run(["image", "exists", image], timeout=10.0, check=False)
        return result.returncode == 0

    def create(
        self,
        *,
        name: str,
        image: str,
        input_dir: Path,
        source_dir: Path,
        labels: dict[str, str],
        limits: ResourceLimits,
    ) -> str:
        args = [
            "create",
            "--name",
            name,
            "--pull=never",
            "--log-driver=none",
            "--network=none",
            "--pid=private",
            "--read-only",
            "--read-only-tmpfs=false",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            f"--cpus={limits.cpu_count:g}",
            f"--memory={limits.memory_bytes}",
            f"--memory-swap={limits.memory_bytes}",
            f"--pids-limit={limits.pids}",
            f"--ulimit=fsize={limits.output_bytes_per_stream}:{limits.output_bytes_per_stream}",
            "--tmpfs",
            f"/workspace:rw,noexec,nosuid,nodev,size={limits.workspace_tmpfs_bytes},mode=0700,uid=65534,gid=65534",
            "--tmpfs",
            f"/result:rw,noexec,nosuid,nodev,size={limits.result_tmpfs_bytes},mode=0700,uid=65534,gid=65534",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={TEMP_TMPFS_BYTES},mode=0700,uid=65534,gid=65534",
            "--volume",
            f"{input_dir}:/input:ro",
            "--volume",
            f"{source_dir}:/source:ro",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin",
            "--env=HOME=/tmp",
            "--env=TMPDIR=/tmp",
            "--env=LANG=C.UTF-8",
            f"--env=ORCHESTRATOR_OUTPUT_LIMIT={limits.output_bytes_per_stream}",
            f"--env=ORCHESTRATOR_TIMEOUT={limits.timeout_seconds}",
        ]
        for key, value in labels.items():
            args.extend(("--label", f"{key}={value}"))
        args.extend(
            (
                "--entrypoint",
                "/usr/local/bin/python3",
                image,
                "-I",
                "-S",
                "-c",
                _RUNNER,
            )
        )
        self._run(args, timeout=30.0)
        details = self.inspect(name)
        if details is None:
            raise OCIExecutionError("created_container_not_inspectable")
        return str(details.get("Id") or name)

    def start(self, name: str) -> None:
        self._run(["start", name], timeout=30.0)

    def inspect(self, name: str) -> dict[str, Any] | None:
        result = self._run(["inspect", name], timeout=10.0, check=False)
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise OCIExecutionError("runtime_inspect_invalid") from exc
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise OCIExecutionError("runtime_inspect_invalid")
        return data

    def wait(self, name: str, timeout: float) -> None:
        self._run(["wait", name], timeout=max(1.0, timeout), check=False)

    def stop(self, name: str, *, grace_seconds: int = 2) -> None:
        self._run(
            ["stop", "--time", str(grace_seconds), name],
            timeout=grace_seconds + 5,
            check=False,
        )
        details = self.inspect(name)
        if details is not None and self._state(details) == "running":
            self._run(["kill", name], timeout=5.0, check=False)

    def remove(self, name: str) -> bool:
        result = self._run(["rm", "--force", name], timeout=15.0, check=False)
        return result.returncode == 0 or self.inspect(name) is None

    def copy_from(self, name: str, source: str, destination: Path) -> bool:
        result = self._run(
            ["cp", f"{name}:{source}", str(destination)], timeout=15.0, check=False
        )
        return result.returncode == 0

    def list_owned(self) -> list[dict[str, Any]]:
        result = self._run(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={_OWNER_LABEL}={_OWNER_VALUE}",
            ],
            timeout=15.0,
            check=False,
        )
        if result.returncode != 0:
            raise OCIExecutionError("runtime_container_list_failed")
        identifiers = [
            line.strip() for line in result.stdout.decode().splitlines() if line.strip()
        ]
        if len(identifiers) > 10000:
            raise OCIExecutionError("runtime_container_list_too_large")
        result_rows = []
        for identifier in identifiers:
            details = self.inspect(identifier)
            if details is not None:
                result_rows.append(details)
        return result_rows

    @staticmethod
    def _state(details: dict[str, Any]) -> str:
        state = details.get("State")
        return (
            str(state.get("Status", "unknown")).lower()
            if isinstance(state, dict)
            else "unknown"
        )


class OCIExecutor:
    """Own one deterministic OCI container per durably accepted request."""

    def __init__(
        self,
        *,
        ledger: AttemptLedger,
        state_dir: Path,
        content_store_dir: Path,
        image: str,
        runtime: str = "podman",
        timeout_seconds: int = 120,
        cpu_count: float = 1.0,
        memory_bytes: int = 536870912,
        pids_limit: int = 64,
        poll_interval_seconds: float = 0.2,
    ):
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= MAX_EXECUTION_TIMEOUT_SECONDS
        ):
            raise OCIExecutionError("execution_timeout_invalid")
        if cpu_count <= 0 or cpu_count > 4:
            raise OCIExecutionError("cpu_limit_invalid")
        if memory_bytes < 64 * 1024 * 1024 or memory_bytes > 8 * 1024 * 1024 * 1024:
            raise OCIExecutionError("memory_limit_invalid")
        if type(pids_limit) is not int or not 1 <= pids_limit <= 512:
            raise OCIExecutionError("pids_limit_invalid")
        self.ledger = ledger
        self.state_dir = Path(state_dir).resolve()
        self.content_store_dir = Path(content_store_dir).resolve()
        self.image = validate_image_reference(image)
        self.limits = ResourceLimits(
            cpu_count=float(cpu_count),
            memory_bytes=int(memory_bytes),
            pids=int(pids_limit),
            timeout_seconds=timeout_seconds,
        )
        self.runtime = PodmanRuntime(runtime, state_dir=self.state_dir)
        self._lifecycle_lock = threading.RLock()
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 2.0))
        self.attempts_root = self.state_dir / "oci-attempts"
        self.results_root = self.state_dir / "results"
        for directory in (self.attempts_root, self.results_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    @property
    def resource_limits(self) -> dict[str, int | float]:
        return self.limits.as_dict()

    def admission(self) -> tuple[bool, tuple[str, ...]]:
        if not self.content_store_dir.is_dir():
            return False, ("content_store_unavailable",)
        if not self.runtime.available():
            return False, ("oci_runtime_unavailable",)
        if not self.runtime.image_exists(self.image):
            return False, ("oci_executor_image_unavailable",)
        return True, ()

    def validate_request(self, request: dict) -> None:
        if (
            request["execution_profile_id"],
            request["execution_profile_version"],
        ) != SUPPORTED_PROFILE:
            raise OCIExecutionError("execution_profile_unsupported")
        if (
            request["operation_scope_id"],
            request["operation_scope_version"],
        ) != SUPPORTED_SCOPE:
            raise OCIExecutionError("operation_scope_unsupported")
        if request["operation_scope_digest"] != SUPPORTED_SCOPE_SHA256:
            raise OCIExecutionError("operation_scope_digest_unsupported")

    def _read_blob(self, digest: str, *, maximum: int) -> bytes:
        if not _SHA256.fullmatch(digest):
            raise OCIExecutionError("content_digest_invalid")
        path = self.content_store_dir / "sha256" / digest[:2] / digest
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIExecutionError("authorized_content_unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise OCIExecutionError("authorized_content_invalid")
            chunks = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(fd)
        if len(content) > maximum or hashlib.sha256(content).hexdigest() != digest:
            raise OCIExecutionError("authorized_content_digest_mismatch")
        return content

    def _extract_snapshot(self, snapshot: bytes, target: Path) -> None:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
        total = 0
        entries = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:*") as archive:
                for member in archive:
                    entries += 1
                    if entries > MAX_SNAPSHOT_ENTRIES:
                        raise OCIExecutionError("snapshot_entry_limit_exceeded")
                    raw_name = member.name
                    relative = PurePosixPath(raw_name)
                    if (
                        not raw_name
                        or "\x00" in raw_name
                        or relative.is_absolute()
                        or any(part in {"..", ""} for part in relative.parts)
                    ):
                        if raw_name in {".", "./"} and member.isdir():
                            continue
                        raise OCIExecutionError("snapshot_path_invalid")
                    if not (member.isdir() or member.isfile()):
                        raise OCIExecutionError("snapshot_entry_type_unsupported")
                    if member.size < 0 or member.size > MAX_SNAPSHOT_FILE_BYTES:
                        raise OCIExecutionError("snapshot_file_limit_exceeded")
                    total += member.size
                    if total > MAX_SNAPSHOT_EXPANDED_BYTES:
                        raise OCIExecutionError("snapshot_size_limit_exceeded")
                    output = target.joinpath(*relative.parts)
                    resolved_parent = output.parent.resolve()
                    if not _within(resolved_parent, target.resolve()):
                        raise OCIExecutionError("snapshot_path_invalid")
                    if member.isdir():
                        output.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise OCIExecutionError("snapshot_file_unreadable")
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    fd = os.open(output, flags, 0o600)
                    try:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(65536, remaining))
                            if not chunk:
                                raise OCIExecutionError("snapshot_file_truncated")
                            _write_all(fd, chunk)
                            remaining -= len(chunk)
                    finally:
                        os.close(fd)
            for root, directories, files in os.walk(target):
                for name in files:
                    (Path(root) / name).chmod(0o444)
                for name in directories:
                    (Path(root) / name).chmod(0o555)
            target.chmod(0o555)
        except OCIExecutionError:
            _clean_tree(target)
            raise
        except (tarfile.TarError, OSError) as exc:
            _clean_tree(target)
            raise OCIExecutionError("source_snapshot_invalid") from exc

    def _prepare_inputs(self, record: AttemptRecord) -> tuple[Path, Path]:
        request = record.request
        base = self.attempts_root / record.supervisor_attempt_id
        if base.exists() or base.is_symlink():
            _clean_tree(base)
        base.mkdir(mode=0o700)
        input_dir = base / "input"
        source_dir = base / "source"
        input_dir.mkdir(mode=0o700)
        try:
            candidate = self._read_blob(
                request["candidate_initial_digest"], maximum=MAX_CANDIDATE_BYTES
            )
            snapshot = self._read_blob(
                request["source_snapshot_digest"], maximum=MAX_SNAPSHOT_BYTES
            )
            try:
                compile(candidate.decode("utf-8"), "candidate.py", "exec")
            except (UnicodeDecodeError, SyntaxError) as exc:
                raise OCIExecutionError("approved_candidate_not_python") from exc
            candidate_path = input_dir / "candidate.py"
            fd = os.open(candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            try:
                _write_all(fd, candidate)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._extract_snapshot(snapshot, source_dir)
            os.chmod(input_dir, 0o555)
            return input_dir, source_dir
        except BaseException:
            _clean_tree(base)
            raise

    def _labels(self, record: AttemptRecord) -> dict[str, str]:
        return {
            _OWNER_LABEL: _OWNER_VALUE,
            _ATTEMPT_LABEL: record.attempt_idempotency_key,
            _REQUEST_LABEL: record.start_request_sha256,
            _AUTH_LABEL: record.authorization_identity_sha256,
            _ATTEMPT_ID_LABEL: record.supervisor_attempt_id,
        }

    def _verify_ownership(self, record: AttemptRecord, details: dict[str, Any]) -> bool:
        config = details.get("Config") or {}
        labels = config.get("Labels") or {}
        expected = self._labels(record)
        return all(labels.get(key) == value for key, value in expected.items())

    def _adopt_container(self, record: AttemptRecord) -> AttemptRecord | None:
        """Adopt an unrecorded deterministic container only after label checks."""

        if record.container_id is not None:
            return record
        details = self.runtime.inspect(record.container_name)
        if details is None:
            return None
        if not self._verify_ownership(record, details):
            self._finish(
                record,
                status="UNKNOWN",
                exit_code=None,
                reason="container_ownership_unverified",
            )
            return None
        if record.status != "CREATING":
            self._finish(
                record,
                status="UNKNOWN",
                exit_code=None,
                reason="container_state_unexpected",
            )
            return None
        container_id = str(details.get("Id") or record.container_name)
        if not self.ledger.transition(
            record.attempt_idempotency_key,
            "CREATING",
            "CREATING",
            container_id=container_id,
        ):
            return self.ledger.get(record.attempt_idempotency_key)
        return self.ledger.get(record.attempt_idempotency_key)

    @staticmethod
    def _runtime_state(details: dict[str, Any]) -> tuple[str, int | None, str | None]:
        state = details.get("State") or {}
        name = str(state.get("Status", "unknown")).lower()
        code = state.get("ExitCode")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        started = state.get("StartedAt") or state.get("Started")
        if isinstance(started, str) and started.startswith("0001-"):
            started = None
        return name, code, started

    def _write_capture(
        self, record: AttemptRecord, stream: str
    ) -> tuple[str, str, int] | None:
        result_dir = self.results_root / record.supervisor_attempt_id
        result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(result_dir, 0o700)
        destination = result_dir / f"{stream}.bin"
        destination.unlink(missing_ok=True)
        if not self.runtime.copy_from(
            record.container_name, f"/result/{stream}", destination
        ):
            return None
        try:
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE_BYTES:
                destination.unlink(missing_ok=True)
                return None
            data = destination.read_bytes()
        except OSError:
            destination.unlink(missing_ok=True)
            return None
        os.chmod(destination, 0o600)
        with destination.open("rb+") as handle:
            os.fsync(handle.fileno())
        relative = destination.relative_to(self.state_dir).as_posix()
        return relative, hashlib.sha256(data).hexdigest(), len(data)

    def _capture(self, record: AttemptRecord) -> dict[str, tuple[str, str, int] | None]:
        return {
            "stdout": self._write_capture(record, "stdout"),
            "stderr": self._write_capture(record, "stderr"),
        }

    def _finish(
        self,
        record: AttemptRecord,
        *,
        status: str,
        exit_code: int | None,
        reason: str | None = None,
        started_at: str | None = None,
    ) -> None:
        unverified_container = reason == "container_ownership_unverified"
        try:
            captures = (
                self._capture(record)
                if record.container_id and not unverified_container
                else {}
            )
        except (OCIExecutionError, OSError):
            captures = {}
            if reason is None:
                reason = "result_capture_failed"
            if status == "EXITED":
                status = "FAILED"
        stdout = captures.get("stdout")
        stderr = captures.get("stderr")
        if record.status in {"ACCEPTED", "CREATING", "RUNNING"}:
            changed = self.ledger.transition(
                record.attempt_idempotency_key,
                record.status,
                status,
                exit_code=exit_code,
                reason=reason,
                started_at=started_at,
                completed_at=_utc_text(),
                stdout_ref=stdout[0] if stdout else None,
                stdout_sha256=stdout[1] if stdout else None,
                stdout_bytes=stdout[2] if stdout else None,
                stderr_ref=stderr[0] if stderr else None,
                stderr_sha256=stderr[1] if stderr else None,
                stderr_bytes=stderr[2] if stderr else None,
            )
            if not changed:
                return
        if unverified_container:
            self.ledger.mark_cleanup(
                record.attempt_idempotency_key,
                "FAILED",
                "container_ownership_unverified",
            )
        else:
            self._cleanup(record)

    def _cleanup(self, record: AttemptRecord) -> None:
        if record.container_id is None:
            # A crash or lost Podman reply can leave a deterministic container
            # behind before its ID reached SQLite. Reconcile by name, but only
            # remove it after all persisted ownership labels match.
            try:
                details = self.runtime.inspect(record.container_name)
            except OCIExecutionError:
                details = None
                self.ledger.mark_cleanup(
                    record.attempt_idempotency_key,
                    "FAILED",
                    "runtime_container_inspect_failed",
                )
                return
            if details is None:
                self.ledger.mark_cleanup(record.attempt_idempotency_key, "NOT_REQUIRED")
                _clean_tree(self.attempts_root / record.supervisor_attempt_id)
                return
            if not self._verify_ownership(record, details):
                self.ledger.mark_cleanup(
                    record.attempt_idempotency_key,
                    "FAILED",
                    "container_ownership_unverified",
                )
                return
        try:
            removed = self.runtime.remove(record.container_name)
        except OCIExecutionError:
            removed = False
        if removed:
            self.ledger.mark_cleanup(record.attempt_idempotency_key, "SUCCEEDED")
            _clean_tree(self.attempts_root / record.supervisor_attempt_id)
        else:
            self.ledger.mark_cleanup(
                record.attempt_idempotency_key,
                "FAILED",
                "runtime_container_remove_failed",
            )

    def _mark_failed(self, record: AttemptRecord, reason: str) -> None:
        current = self.ledger.get(record.attempt_idempotency_key)
        if current is None:
            return
        if current.status in {"ACCEPTED", "CREATING", "RUNNING"}:
            self._finish(
                current,
                status="FAILED",
                exit_code=None,
                reason=reason,
            )

    def _create(self, record: AttemptRecord) -> AttemptRecord | None:
        with self._lifecycle_lock:
            return self._create_locked(record)

    def _create_locked(self, record: AttemptRecord) -> AttemptRecord | None:
        latest = self.ledger.get(record.attempt_idempotency_key)
        if latest is None or latest.status in TERMINAL_STATUSES:
            return latest
        if latest.cancel_requested_at:
            self._finish(latest, status="CANCELLED", exit_code=None, reason="cancelled")
            return self.ledger.get(latest.attempt_idempotency_key)
        request = record.request
        self.validate_request(request)
        if not self.ledger.transition(
            record.attempt_idempotency_key, "ACCEPTED", "CREATING"
        ):
            return self.ledger.get(record.attempt_idempotency_key)
        record = self.ledger.get(record.attempt_idempotency_key)
        assert record is not None
        try:
            input_dir, source_dir = self._prepare_inputs(record)
            latest = self.ledger.get(record.attempt_idempotency_key)
            if latest is not None and latest.cancel_requested_at:
                _clean_tree(self.attempts_root / record.supervisor_attempt_id)
                self._finish(
                    latest,
                    status="CANCELLED",
                    exit_code=None,
                    reason="cancelled",
                )
                return self.ledger.get(record.attempt_idempotency_key)
            saved_limits = ResourceLimits(
                cpu_count=float(record.resource_limits["cpu_count"]),
                memory_bytes=int(record.resource_limits["memory_bytes"]),
                pids=int(record.resource_limits["pids"]),
                timeout_seconds=int(record.execution_timeout_seconds),
                output_bytes_per_stream=int(
                    record.resource_limits["output_bytes_per_stream"]
                ),
                workspace_tmpfs_bytes=int(
                    record.resource_limits["workspace_tmpfs_bytes"]
                ),
                result_tmpfs_bytes=int(record.resource_limits["result_tmpfs_bytes"]),
            )
            container_id = self.runtime.create(
                name=record.container_name,
                image=self.image,
                input_dir=input_dir,
                source_dir=source_dir,
                labels=self._labels(record),
                limits=saved_limits,
            )
        except OCIExecutionError as exc:
            # The create reply may be lost after Podman created the container.
            try:
                details = self.runtime.inspect(record.container_name)
            except OCIExecutionError:
                details = None
            if details is None or not self._verify_ownership(record, details):
                self._mark_failed(record, exc.reason)
                return self.ledger.get(record.attempt_idempotency_key)
            container_id = str(details.get("Id") or record.container_name)
        if not self.ledger.transition(
            record.attempt_idempotency_key,
            "CREATING",
            "CREATING",
            container_id=container_id,
        ):
            return self.ledger.get(record.attempt_idempotency_key)
        record = self.ledger.get(record.attempt_idempotency_key)
        if record is not None and record.cancel_requested_at:
            self._stop_for_terminal(record, "cancelled")
            return self.ledger.get(record.attempt_idempotency_key)
        return record

    def _stop_for_terminal(self, record: AttemptRecord, reason: str) -> None:
        try:
            self.runtime.stop(record.container_name, grace_seconds=1)
        except OCIExecutionError:
            pass
        latest = self.ledger.get(record.attempt_idempotency_key) or record
        self._finish(
            latest,
            status="CANCELLED" if reason == "cancelled" else "TIMED_OUT",
            exit_code=None,
            reason=reason,
        )

    def execute_or_reconcile(self, key: str) -> None:
        record = self.ledger.get(key)
        if record is None or record.status in {
            "EXITED",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
            "INTERRUPTED",
            "UNKNOWN",
        }:
            if record is not None:
                self._cleanup(record)
            return
        if record.cancel_requested_at:
            self.cancel(key)
            return
        if record.status == "ACCEPTED":
            record = self._create(record)
            if record is None or record.status in TERMINAL_STATUSES:
                return
        if record.container_id is None:
            # Recover the create-before-ledger-write window by deterministic
            # name, but only if the runtime proves all ownership labels.
            try:
                details = self.runtime.inspect(record.container_name)
            except OCIExecutionError as exc:
                self._mark_failed(record, exc.reason)
                return
            if details is None:
                self._finish(
                    record,
                    status="INTERRUPTED",
                    exit_code=None,
                    reason="container_missing_after_create_started",
                )
                return
            if not self._verify_ownership(record, details):
                self._finish(
                    record,
                    status="UNKNOWN",
                    exit_code=None,
                    reason="container_ownership_unverified",
                )
                return
            container_id = str(details.get("Id") or record.container_name)
            if not self.ledger.transition(
                key,
                "CREATING",
                "CREATING",
                container_id=container_id,
            ):
                return
            record = self.ledger.get(key) or record

        try:
            details = self.runtime.inspect(record.container_name)
        except OCIExecutionError as exc:
            self._mark_failed(record, exc.reason)
            return
        if details is None:
            self._finish(
                record,
                status="INTERRUPTED",
                exit_code=None,
                reason="owned_container_missing_during_recovery",
            )
            return
        if not self._verify_ownership(record, details):
            self._finish(
                record,
                status="UNKNOWN",
                exit_code=None,
                reason="container_ownership_unverified",
            )
            return

        state, exit_code, started = self._runtime_state(details)
        if state == "created":
            # CREATING is durably written before create. A verified created
            # container has not run, so it can be started once after restart.
            with self._lifecycle_lock:
                current = self.ledger.get(key) or record
                if current.cancel_requested_at:
                    self._stop_for_terminal(current, "cancelled")
                    return
                try:
                    self.runtime.start(record.container_name)
                except OCIExecutionError:
                    latest = self.runtime.inspect(record.container_name)
                    if latest is None or self._runtime_state(latest)[0] == "created":
                        self._mark_failed(record, "container_start_failed")
                        return
            state = "running"
        if state == "running":
            now = _utc_now()
            observed_start = (
                _parse_time(started) or _parse_time(record.started_at) or now
            )
            if record.status == "CREATING":
                self.ledger.transition(
                    key,
                    "CREATING",
                    "RUNNING",
                    started_at=_utc_text(observed_start),
                )
                record = self.ledger.get(key) or record
            elapsed = max(0.0, (now - observed_start).total_seconds())
            if elapsed >= record.execution_timeout_seconds:
                self._stop_for_terminal(record, "timeout")
                return
            while True:
                current = self.ledger.get(key)
                if current is None:
                    return
                if current.cancel_requested_at:
                    self._stop_for_terminal(current, "cancelled")
                    return
                elapsed = max(0.0, (_utc_now() - observed_start).total_seconds())
                if elapsed >= current.execution_timeout_seconds:
                    self._stop_for_terminal(current, "timeout")
                    return
                time.sleep(self.poll_interval_seconds)
                try:
                    latest = self.runtime.inspect(current.container_name)
                except OCIExecutionError as exc:
                    self._mark_failed(current, exc.reason)
                    return
                if latest is None:
                    self._finish(
                        current,
                        status="INTERRUPTED",
                        exit_code=None,
                        reason="owned_container_disappeared",
                    )
                    return
                current = self.ledger.get(key) or current
                if not self._verify_ownership(current, latest):
                    self._finish(
                        current,
                        status="UNKNOWN",
                        exit_code=None,
                        reason="container_ownership_unverified",
                    )
                    return
                if current.cancel_requested_at:
                    self._stop_for_terminal(current, "cancelled")
                    return
                state, exit_code, _ = self._runtime_state(latest)
                if state == "exited" or state == "stopped":
                    terminal = "EXITED" if exit_code == 0 else "FAILED"
                    self._finish(
                        current,
                        status=terminal,
                        exit_code=exit_code,
                        reason=None if exit_code == 0 else "operation_nonzero_exit",
                        started_at=(
                            _utc_text(_parse_time(self._runtime_state(latest)[2]))
                            if self._runtime_state(latest)[2]
                            else current.started_at
                        ),
                    )
                    return
                if state != "running":
                    self._finish(
                        current,
                        status="INTERRUPTED",
                        exit_code=None,
                        reason="container_state_unexpected",
                    )
                    return
        elif state in {"exited", "stopped"}:
            terminal = "EXITED" if exit_code == 0 else "FAILED"
            self._finish(
                record,
                status=terminal,
                exit_code=exit_code,
                reason=None if exit_code == 0 else "operation_nonzero_exit",
                started_at=(
                    _utc_text(_parse_time(started)) if started else record.started_at
                ),
            )
        else:
            self._finish(
                record,
                status="INTERRUPTED",
                exit_code=None,
                reason="container_state_unexpected",
            )

    def cancel(self, key: str) -> AttemptRecord | None:
        record = self.ledger.mark_cancel_requested(key)
        if record is None:
            return None
        if record.status in TERMINAL_STATUSES:
            return record
        with self._lifecycle_lock:
            record = self.ledger.get(key) or record
            if record.status in TERMINAL_STATUSES:
                return record
            if record.status == "ACCEPTED":
                self._finish(
                    record, status="CANCELLED", exit_code=None, reason="cancelled"
                )
                return self.ledger.get(key)
            if record.status == "CREATING" and record.container_id is None:
                try:
                    record = self._adopt_container(record)
                except OCIExecutionError:
                    record = None
                if record is None:
                    latest = self.ledger.get(key)
                    if latest is not None and latest.status not in TERMINAL_STATUSES:
                        self._finish(
                            latest,
                            status="CANCELLED",
                            exit_code=None,
                            reason="cancelled",
                        )
                    return self.ledger.get(key)
            if record.container_id is None:
                self._finish(
                    record, status="CANCELLED", exit_code=None, reason="cancelled"
                )
                return self.ledger.get(key)
            self._stop_for_terminal(record, "cancelled")
            return self.ledger.get(key)

    def reconcile_startup(self) -> None:
        """Reattach to owned containers; never create from an uncertain row."""

        records = self.ledger.all_records()
        owned_names = {record.container_name for record in records}
        try:
            runtime_containers = self.runtime.list_owned()
        except OCIExecutionError:
            runtime_containers = []
        for details in runtime_containers:
            name = str(details.get("Name") or "").lstrip("/")
            if name and name not in owned_names:
                labels = (details.get("Config") or {}).get("Labels") or {}
                attempt_key = labels.get(_ATTEMPT_LABEL)
                if (
                    labels.get(_OWNER_LABEL) == _OWNER_VALUE
                    and isinstance(attempt_key, str)
                    and _SHA256.fullmatch(attempt_key)
                    and name == "orc-one-shot-" + attempt_key[:32]
                ):
                    try:
                        self.runtime.stop(name, grace_seconds=1)
                        self.runtime.remove(name)
                    except OCIExecutionError:
                        pass
        for record in records:
            if record.status in TERMINAL_STATUSES:
                self._cleanup(record)
        for record in records:
            if record.status in {"ACCEPTED", "CREATING", "RUNNING"}:
                # Supervisor schedules these rows after constructing threads.
                continue

    def recoverable_keys(self) -> list[str]:
        return [
            record.attempt_idempotency_key
            for record in self.ledger.all_records()
            if record.status in {"ACCEPTED", "CREATING", "RUNNING"}
        ]

    def fail_unexpected(self, key: str) -> None:
        """Persist an internal failure without attempting another launch."""

        record = self.ledger.get(key)
        if record is None or record.status in TERMINAL_STATUSES:
            return
        changed = self.ledger.transition(
            key,
            record.status,
            "UNKNOWN",
            reason="supervisor_internal_error",
            completed_at=_utc_text(),
        )
        if changed:
            latest = self.ledger.get(key) or record
            if latest.container_id is None:
                self.ledger.mark_cleanup(key, "NOT_REQUIRED")
            else:
                self._cleanup(latest)

    def result_document(self, record: AttemptRecord) -> dict[str, Any]:
        status = record.status
        terminal = status in TERMINAL_STATUSES
        return {
            "execution_id": record.supervisor_attempt_id,
            "authorization_uuid": record.authorization_uuid,
            "claim_uuid": record.claim_uuid,
            "attempt_idempotency_key": record.attempt_idempotency_key,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "status": status,
            "terminal": terminal,
            "terminal_status": status if terminal else None,
            "exit_code": record.exit_code,
            "timed_out": status == "TIMED_OUT",
            "cancelled": status == "CANCELLED",
            "interrupted": status == "INTERRUPTED",
            "failure_reason": record.status_reason,
            "stdout": self._stream_document(
                record.stdout_ref, record.stdout_sha256, record.stdout_bytes
            ),
            "stderr": self._stream_document(
                record.stderr_ref, record.stderr_sha256, record.stderr_bytes
            ),
            "resource_limits": record.resource_limits,
            "cleanup": {
                "status": record.cleanup_status,
                "reason": record.cleanup_reason,
            },
        }

    def _stream_document(
        self, reference: str | None, digest: str | None, byte_count: int | None
    ) -> dict[str, Any] | None:
        if reference is None or digest is None or byte_count is None:
            return None
        return {
            "reference": reference,
            "sha256": digest,
            "bytes": byte_count,
            "limit_bytes": MAX_CAPTURE_BYTES,
        }


def _container_labels(details: dict[str, Any]) -> dict[str, str]:
    config = details.get("Config") or {}
    labels = config.get("Labels") or {}
    return labels if isinstance(labels, dict) else {}
