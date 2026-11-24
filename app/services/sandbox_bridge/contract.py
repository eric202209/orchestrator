"""Versioned, bounded wire contract for the Host sandbox bridge prototype."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import time
import uuid
import secrets

PROFILE = "python312-no-network-v1"
MAX_CODE_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_REQUEST_AGE_SECONDS = 30
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class RequestRejected(ValueError):
    """Stable, fail-closed contract rejection."""


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
    # v2 only: C2-B operation binding (see authorization.py), HMAC-covered.
    authorization: dict[str, object] | None = None

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
        if self.version not in (1, 2):
            raise RequestRejected("unsupported_version")
        if (self.version == 2) != isinstance(self.authorization, dict):
            raise RequestRejected("invalid_authorization_presence")
        if self.execution_profile != PROFILE:
            raise RequestRejected("unauthorized_execution_profile")
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code.encode("utf-8")) > MAX_CODE_BYTES
        ):
            raise RequestRejected("invalid_code_size")
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
        if self.version == 1:
            # v1 wire shape is unchanged for the deployed Launcher.
            wire.pop("authorization")
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
        if not isinstance(value, dict) or set(value) != expected:
            raise RequestRejected("invalid_request_shape")
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
