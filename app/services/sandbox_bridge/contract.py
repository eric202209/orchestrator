"""Versioned, bounded wire contract for the Host sandbox bridge prototype."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import time
import uuid
import secrets

PROFILE = "python312-no-network-v1"
# P43-PRODUCT-23A: pytest (or the candidate compile check) over a bounded
# read-only workspace snapshot (v3 only). ``code`` is a canonical JSON tool
# invocation (see ``decode_tool_invocation``), never a script.
PYTEST_PROFILE = "python312-pytest-no-network-v1"
PROFILES = frozenset({PROFILE, PYTEST_PROFILE})
MAX_CODE_BYTES = 64 * 1024
# Provisional U1 evaluation bounds, not a general workspace limit.
MAX_SNAPSHOT_FILES = 256
MAX_SNAPSHOT_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SNAPSHOT_PATH_CHARS = 240
MAX_SNAPSHOT_DEPTH = 16
MAX_REQUEST_WIRE_BYTES = 2 * 1024 * 1024
MAX_PYTEST_ARGS = 32
MAX_PYTEST_ARG_CHARS = 512
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_REQUEST_AGE_SECONDS = 30
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class RequestRejected(ValueError):
    """Stable, fail-closed contract rejection."""


def validate_snapshot_path(name: object) -> PurePosixPath:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > MAX_SNAPSHOT_PATH_CHARS
        or "\\" in name
        or "\x00" in name
        or name != name.strip()
    ):
        raise RequestRejected("invalid_snapshot_path")
    parts = name.split("/")
    if (
        name.startswith("/")
        or len(parts) > MAX_SNAPSHOT_DEPTH
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 32 for char in name)
    ):
        raise RequestRejected("invalid_snapshot_path")
    return PurePosixPath(name)


def snapshot_manifest_sha256(manifest: list[list[object]]) -> str:
    """Canonical digest of ``[[path, size, sha256], ...]`` in path order."""

    return hashlib.sha256(
        json.dumps(
            manifest, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def decode_workspace_snapshot(
    entries: object,
) -> tuple[list[tuple[PurePosixPath, bytes]], str]:
    """Validate a wire snapshot; return decoded files and the canonical digest.

    Regular-file content only: entries are sorted unique safe relative paths,
    no path is also a directory of another entry, and every size/sha256
    matches the decoded bytes. Bounds are explicit and fail closed.
    """

    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SNAPSHOT_FILES:
        raise RequestRejected("invalid_snapshot_size")
    files: list[tuple[PurePosixPath, bytes]] = []
    manifest: list[list[object]] = []
    total = 0
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "size",
            "sha256",
            "content",
        }:
            raise RequestRejected("invalid_snapshot_entry")
        path = validate_snapshot_path(entry["path"])
        if not isinstance(entry["content"], str):
            raise RequestRejected("invalid_snapshot_entry")
        try:
            data = base64.b64decode(entry["content"], validate=True)
        except (binascii.Error, ValueError):
            raise RequestRejected("invalid_snapshot_content") from None
        size, digest = entry["size"], entry["sha256"]
        if type(size) is not int or size != len(data):
            raise RequestRejected("snapshot_size_mismatch")
        if len(data) > MAX_SNAPSHOT_FILE_BYTES:
            raise RequestRejected("snapshot_file_too_large")
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES:
            raise RequestRejected("snapshot_too_large")
        if not isinstance(digest, str) or digest != hashlib.sha256(data).hexdigest():
            raise RequestRejected("snapshot_digest_mismatch")
        names.append(str(path))
        files.append((path, data))
        manifest.append([str(path), size, digest])
    if names != sorted(names) or len(set(names)) != len(names):
        raise RequestRejected("snapshot_paths_not_canonical")
    present = set(names)
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in present:
                raise RequestRejected("snapshot_path_conflict")
    return files, snapshot_manifest_sha256(manifest)


SANDBOX_TOOLS = frozenset({"pytest", "compileall"})


def encode_tool_invocation(tool: str, args: list[str]) -> str:
    return json.dumps(
        {"tool": tool, "args": list(args)},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )


def decode_tool_invocation(code: str) -> tuple[str, list[str]]:
    """v3 ``code``: canonical ``{"tool": "pytest"|"compileall", "args": [...]}``.

    The tool selects one of two fixed bootstrap branches; nothing in it is
    executed as code and it never reaches Docker options.
    """

    try:
        value = json.loads(code)
    except ValueError:
        raise RequestRejected("invalid_tool_invocation") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"tool", "args"}
        or value["tool"] not in SANDBOX_TOOLS
    ):
        raise RequestRejected("invalid_tool_invocation")
    args = value["args"]
    if (
        not isinstance(args, list)
        or not 1 <= len(args) <= MAX_PYTEST_ARGS
        or any(
            not isinstance(arg, str)
            or not arg
            or len(arg) > MAX_PYTEST_ARG_CHARS
            or "\x00" in arg
            for arg in args
        )
        or encode_tool_invocation(value["tool"], args) != code
    ):
        raise RequestRejected("invalid_tool_invocation")
    return value["tool"], args


@dataclass(frozen=True)
class ExecutionRequest:
    request_id: str
    task_id: str
    execution_id: str
    workspace_ref: str
    execution_profile: str
    code: str
    timeout_seconds: int
    issued_at: int
    cancellation_token: str = ""
    version: int = 1
    # v2/v3: C2-B operation binding (see authorization.py), HMAC-covered.
    authorization: dict[str, object] | None = None
    # v3 only (pytest profile): bounded workspace snapshot, digest-bound.
    workspace_snapshot: list[dict[str, object]] | None = None

    def validate(self, *, now: int | None = None) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("task_id", self.task_id),
            ("execution_id", self.execution_id),
            ("workspace_ref", self.workspace_ref),
        ):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise RequestRejected(f"invalid_{name}")
        try:
            uuid.UUID(self.request_id)
            uuid.UUID(self.workspace_ref)
        except (ValueError, AttributeError):
            raise RequestRejected("invalid_uuid") from None
        if self.version not in (1, 2, 3):
            raise RequestRejected("unsupported_version")
        if (self.version >= 2) != isinstance(self.authorization, dict):
            raise RequestRejected("invalid_authorization_presence")
        if self.execution_profile not in PROFILES:
            raise RequestRejected("unauthorized_execution_profile")
        # The pytest profile exists only as v3 with a snapshot; v1/v2 keep
        # the original single-script profile and carry no snapshot.
        if (self.version == 3) != (self.execution_profile == PYTEST_PROFILE):
            raise RequestRejected("unauthorized_execution_profile")
        if (self.version == 3) != (self.workspace_snapshot is not None):
            raise RequestRejected("invalid_snapshot_presence")
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code.encode("utf-8")) > MAX_CODE_BYTES
        ):
            raise RequestRejected("invalid_code_size")
        if self.version == 3:
            decode_tool_invocation(self.code)
            decode_workspace_snapshot(self.workspace_snapshot)
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise RequestRejected("invalid_timeout")
        if type(self.issued_at) is not int:
            raise RequestRejected("invalid_issued_at")
        if (
            not isinstance(self.cancellation_token, str)
            or len(self.cancellation_token) != 64
        ):
            raise RequestRejected("invalid_cancellation_token")
        try:
            bytes.fromhex(self.cancellation_token)
        except ValueError:
            raise RequestRejected("invalid_cancellation_token") from None
        age = (int(time.time()) if now is None else now) - self.issued_at
        if age < -5 or age > MAX_REQUEST_AGE_SECONDS:
            raise RequestRejected("stale_request")

    def to_wire(self) -> dict[str, object]:
        self.validate()
        wire = asdict(self)
        if self.version < 3:
            # v1/v2 wire shapes are unchanged for the deployed Launchers.
            wire.pop("workspace_snapshot")
        if self.version == 1:
            wire.pop("authorization")
        if len(json.dumps(wire, separators=(",", ":"))) > MAX_REQUEST_WIRE_BYTES:
            raise RequestRejected("request_too_large")
        return wire

    @classmethod
    def from_wire(cls, value: object, *, now: int | None = None) -> "ExecutionRequest":
        expected = {
            "request_id",
            "task_id",
            "execution_id",
            "workspace_ref",
            "execution_profile",
            "code",
            "timeout_seconds",
            "issued_at",
            "cancellation_token",
            "version",
        }
        if isinstance(value, dict) and value.get("version") == 2:
            expected = expected | {"authorization"}
        if isinstance(value, dict) and value.get("version") == 3:
            expected = expected | {"authorization", "workspace_snapshot"}
        if not isinstance(value, dict) or set(value) != expected:
            raise RequestRejected("invalid_request_shape")
        if len(json.dumps(value, separators=(",", ":"))) > MAX_REQUEST_WIRE_BYTES:
            raise RequestRejected("request_too_large")
        try:
            request = cls(**value)
        except (TypeError, ValueError):
            raise RequestRejected("invalid_request_types") from None
        request.validate(now=now)
        return request


@dataclass(frozen=True)
class ExecutionResult:
    request_id: str
    execution_status: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False
    error_code: str | None = None
    duration: float = 0.0
    artifact_manifest: list[dict[str, object]] | None = None
    artifacts: list[bytes] | None = None

    def validate(self) -> None:
        if not _ID.fullmatch(self.request_id):
            raise RequestRejected("invalid_result_request_id")
        if self.execution_status not in {
            "SUCCESS",
            "EXECUTION_FAILED",
            "TIMEOUT",
            "CANCELLED",
            "REJECTED",
            "UNAVAILABLE",
            "INTERNAL_ERROR",
        }:
            raise RequestRejected("invalid_result_status")
        if (
            len(self.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
            or len(self.stderr.encode("utf-8")) > MAX_OUTPUT_BYTES
        ):
            raise RequestRejected("oversized_result")
        if not isinstance(self.duration, (int, float)) or self.duration < 0:
            raise RequestRejected("invalid_duration")
        manifest, artifacts = self.artifact_manifest or [], self.artifacts or []
        if (
            len(manifest) > 8
            or len(manifest) != len(artifacts)
            or sum(map(len, artifacts)) > 4 * 1024 * 1024
        ):
            raise RequestRejected("invalid_artifacts")
        for item, data in zip(manifest, artifacts):
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "sha256"}
                or not isinstance(item["path"], str)
                or item["path"].startswith("/")
                or "\\" in item["path"]
                or any(part in ("", ".", "..") for part in item["path"].split("/"))
                or item["size"] != len(data)
                or len(data) > 1024 * 1024
                or not re.fullmatch(r"[a-f0-9]{64}", str(item["sha256"]))
            ):
                raise RequestRejected("invalid_artifact_manifest")

    @classmethod
    def unavailable(cls, request_id: str) -> "ExecutionResult":
        return cls(
            request_id, "UNAVAILABLE", None, "", "", error_code="launcher_unavailable"
        )
