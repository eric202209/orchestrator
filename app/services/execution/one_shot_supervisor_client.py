"""F2 adapter: the F1c-1 ``OneShotSupervisor`` protocol over the local socket.

The adapter authenticates the Supervisor before trusting it: the socket must
be a real socket owned by the expected Supervisor UID and not world
writable, and the kernel-reported peer UID must match. Any transport,
framing, or protocol failure raises ``SupervisorTransportError`` so the F1c-1
boundary holds instead of guessing. A conflicting start under an existing
key is returned as REJECTED with the existing attempt's bindings; the F1c-1
boundary then reports a conflict or rejection and never starts again.

The Worker selects its adapter with ``configured_supervisor``: no endpoint
configured gives ``UnavailableSupervisor``; a partial or invalid endpoint
gives ``MisconfiguredSupervisor``, which refuses admission.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import stat
import struct

from app.services.execution.one_shot_dispatch import (
    AttemptStatus,
    ExecutorStartRequest,
    SupervisorAdmission,
    SupervisorAdmissionStatus,
    SupervisorReceipt,
    SupervisorTransportError,
    UnavailableSupervisor,
)

PROTOCOL = "one-shot-supervisor/v1"
MAX_RESPONSE_BYTES = 64 * 1024
_LEDGER_STATUSES = {
    "ACCEPTED": AttemptStatus.ACCEPTED,
    "RUNNING": AttemptStatus.RUNNING,
    "EXITED": AttemptStatus.EXITED,
    "UNKNOWN": AttemptStatus.UNKNOWN,
}


class SupervisorUntrusted(SupervisorTransportError):
    """The endpoint is not the expected Supervisor or refused this caller."""


class MisconfiguredSupervisor:
    """A Supervisor endpoint is configured but unusable: refuse, never guess."""

    def __init__(self, reason: str):
        self.reason = reason

    def admission(self) -> SupervisorAdmission:
        return SupervisorAdmission(
            SupervisorAdmissionStatus.UNAVAILABLE, False, (self.reason,)
        )

    def get_attempt_status(self, attempt_idempotency_key: str) -> SupervisorReceipt:
        raise SupervisorTransportError(self.reason)

    def start_attempt(self, request: ExecutorStartRequest) -> SupervisorReceipt:
        raise SupervisorTransportError(self.reason)


def configured_supervisor(socket_path: str | None, supervisor_uid: str | None):
    """Select the Worker's Supervisor adapter from its two settings."""

    socket_path = (socket_path or "").strip()
    supervisor_uid = (supervisor_uid or "").strip()
    if not socket_path and not supervisor_uid:
        return UnavailableSupervisor()
    if not socket_path or not supervisor_uid:
        return MisconfiguredSupervisor("supervisor_configuration_incomplete")
    uid_valid = supervisor_uid.isascii() and supervisor_uid.isdigit()
    if not Path(socket_path).is_absolute() or not uid_valid:
        return MisconfiguredSupervisor("supervisor_configuration_invalid")
    return UnixSocketSupervisor(socket_path, supervisor_uid=int(supervisor_uid))


class UnixSocketSupervisor:
    def __init__(
        self,
        socket_path: str | os.PathLike,
        *,
        supervisor_uid: int,
        timeout_seconds: float = 5.0,
    ):
        self.socket_path = Path(socket_path)
        self.supervisor_uid = int(supervisor_uid)
        self.timeout_seconds = float(timeout_seconds)

    def _call(self, message: dict) -> dict:
        try:
            info = self.socket_path.lstat()
        except OSError as exc:
            raise SupervisorTransportError("supervisor_socket_unavailable") from exc
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != self.supervisor_uid
            or info.st_mode & 0o002
        ):
            raise SupervisorUntrusted("supervisor_socket_untrusted")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(self.socket_path))
            creds = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            if struct.unpack("3i", creds)[1] != self.supervisor_uid:
                raise SupervisorUntrusted("supervisor_peer_uid_mismatch")
            payload = dict(message, protocol=PROTOCOL)
            connection.sendall(json.dumps(payload).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES:
                    raise SupervisorTransportError("supervisor_response_too_large")
        except OSError as exc:
            raise SupervisorTransportError("supervisor_transport_failed") from exc
        finally:
            connection.close()
        try:
            response = json.loads(data)
        except ValueError as exc:
            raise SupervisorTransportError("supervisor_reply_lost_or_invalid") from exc
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
            raise SupervisorTransportError("supervisor_protocol_mismatch")
        if response.get("error") == "caller_not_authenticated":
            raise SupervisorUntrusted("caller_not_authenticated")
        return response

    @staticmethod
    def _receipt(key: str, data, *, rejected=False) -> SupervisorReceipt:
        if data is None:
            return SupervisorReceipt(key, AttemptStatus.NOT_FOUND)
        if not isinstance(data, dict) or data.get("status") not in _LEDGER_STATUSES:
            raise SupervisorTransportError("supervisor_receipt_invalid")
        return SupervisorReceipt(
            attempt_idempotency_key=data.get("attempt_idempotency_key"),
            status=(
                AttemptStatus.REJECTED if rejected else _LEDGER_STATUSES[data["status"]]
            ),
            request_payload_sha256=data.get("request_payload_sha256"),
            authorization_identity_sha256=data.get("authorization_identity_sha256"),
            supervisor_attempt_id=data.get("supervisor_attempt_id"),
            deduplicated=data.get("deduplicated") is True,
        )

    def admission(self) -> SupervisorAdmission:
        try:
            response = self._call({"op": "admission"})
        except SupervisorUntrusted as exc:
            return SupervisorAdmission(
                SupervisorAdmissionStatus.UNTRUSTED, False, (str(exc),)
            )
        if (
            response.get("status") == "TRUSTED"
            and response.get("durable_deduplication") is True
        ):
            return SupervisorAdmission(SupervisorAdmissionStatus.TRUSTED, True)
        return SupervisorAdmission(
            SupervisorAdmissionStatus.UNAVAILABLE,
            False,
            (str(response.get("error") or "supervisor_ledger_unavailable"),),
        )

    def get_attempt_status(self, attempt_idempotency_key: str) -> SupervisorReceipt:
        response = self._call(
            {"op": "get_attempt_status", "key": attempt_idempotency_key}
        )
        if "error" in response:
            raise SupervisorTransportError(str(response["error"]))
        return self._receipt(attempt_idempotency_key, response.get("receipt"))

    def start_attempt(self, request: ExecutorStartRequest) -> SupervisorReceipt:
        response = self._call({"op": "start_attempt", "request": asdict(request)})
        error = response.get("error")
        key = request.attempt_idempotency_key
        if error is None:
            return self._receipt(key, response.get("receipt"))
        if error == "attempt_conflict":
            return self._receipt(key, response.get("receipt"), rejected=True)
        if error == "invalid_request":
            # Refused before registration: bound to this request, never ledgered.
            return SupervisorReceipt(
                key,
                AttemptStatus.REJECTED,
                request.request_payload_sha256,
                request.authorization_identity_sha256,
            )
        raise SupervisorTransportError(str(error))
