"""Authenticated Unix-socket transport; never falls back to local execution."""

from __future__ import annotations

import base64
import errno
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import threading
import time
from typing import Callable

from .contract import (
    ExecutionRequest,
    ExecutionResult,
    MAX_RESULT_BYTES,
    RequestRejected,
)
from .authorization import verify_authorization
from .ledger import RequestLedger

MAX_FRAME = MAX_RESULT_BYTES


def _mac(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _send(sock: socket.socket, value: dict[str, object]) -> None:
    data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    if len(data) > MAX_FRAME:
        raise RequestRejected("frame_too_large")
    sock.sendall(struct.pack("!I", len(data)) + data)


def _send_response(sock: socket.socket, value: dict[str, object]) -> None:
    """Send a handler response, tolerating a peer that already disconnected."""
    try:
        _send(sock, value)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # The result cannot be delivered once the peer has gone away, and there
        # is no useful recovery for a per-connection handler thread to perform.
        return


def _receive(sock: socket.socket) -> dict[str, object]:
    def exact(n):
        data = bytearray()
        while len(data) < n:
            part = sock.recv(n - len(data))
            if not part:
                raise RequestRejected("incomplete_frame")
            data.extend(part)
        return bytes(data)

    size = struct.unpack("!I", exact(4))[0]
    if not 2 <= size <= MAX_FRAME:
        raise RequestRejected("frame_too_large")
    value = json.loads(exact(size))
    if not isinstance(value, dict):
        raise RequestRejected("invalid_frame")
    return value


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _result_wire(result: ExecutionResult) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "execution_status": result.execution_status,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "error_code": result.error_code,
        "duration": result.duration,
        "artifact_manifest": result.artifact_manifest or [],
        "artifacts": [
            base64.b64encode(x).decode("ascii") for x in (result.artifacts or [])
        ],
    }


class SandboxClient:
    def __init__(
        self, socket_path: str | Path, key: bytes, *, connect_timeout: float = 2.0
    ):
        if len(key) < 32:
            raise ValueError("bridge key must contain at least 32 bytes")
        self.socket_path, self.key, self.connect_timeout = (
            str(socket_path),
            key,
            connect_timeout,
        )

    def _connect_before_submission(self) -> socket.socket:
        deadline = time.monotonic() + self.connect_timeout
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sock.close()
                raise TimeoutError("launcher_connect_timeout")
            sock.settimeout(remaining)
            try:
                sock.connect(self.socket_path)
                return sock
            except OSError as exc:
                sock.close()
                # No request bytes have been sent, so this retry cannot replay
                # an accepted execution. Never retry after a successful connect.
                if (
                    exc.errno not in (errno.ENOENT, errno.ECONNREFUSED)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        request.validate()
        wire = request.to_wire()
        payload = json.dumps(wire, separators=(",", ":"), sort_keys=True).encode()
        try:
            sock = self._connect_before_submission()
            with sock:
                sock.settimeout(request.timeout_seconds + 10)
                _send(
                    sock,
                    {
                        "operation": "execute",
                        "request": wire,
                        "mac": _mac(self.key, payload),
                    },
                )
                response = _receive(sock)
        except (OSError, TimeoutError):
            return ExecutionResult.unavailable(request.request_id)
        try:
            expected = {
                "request_id",
                "execution_status",
                "exit_code",
                "stdout",
                "stderr",
                "truncated",
                "error_code",
                "duration",
                "artifact_manifest",
                "artifacts",
            }
            if (
                set(response) != expected
                or response["request_id"] != request.request_id
            ):
                raise RequestRejected("result_request_mismatch")
            encoded = response.pop("artifacts")
            result = ExecutionResult(
                **response,
                artifacts=[base64.b64decode(x, validate=True) for x in encoded],
            )
            result.validate()
            if len(result.artifact_manifest or []) != len(result.artifacts or []):
                raise RequestRejected("artifact_manifest_mismatch")
            for entry, data in zip(
                result.artifact_manifest or [], result.artifacts or []
            ):
                if (
                    entry.get("size") != len(data)
                    or entry.get("sha256") != hashlib.sha256(data).hexdigest()
                ):
                    raise RequestRejected("artifact_hash_mismatch")
            return result
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RequestRejected):
                raise
            raise RequestRejected("invalid_result") from None

    def launcher_epoch(self) -> int:
        """Return the current Launcher instance epoch (HMAC request and reply).

        The nonce binds the reply to this call, so a recorded reply from a
        previous Launcher instance cannot be replayed as the current epoch.
        Raises OSError/TimeoutError when unavailable, RequestRejected when the
        reply is not authentic.
        """

        nonce = secrets.token_hex(32)
        body = {"operation": "epoch", "nonce": nonce}
        sock = self._connect_before_submission()
        with sock:
            sock.settimeout(self.connect_timeout + 3)
            _send(sock, {**body, "mac": _mac(self.key, _canonical(body))})
            response = _receive(sock)
        if set(response) != {"launcher_epoch", "nonce", "mac"}:
            raise RequestRejected("epoch_reply_invalid")
        reply = {"launcher_epoch": response["launcher_epoch"], "nonce": nonce}
        if response["nonce"] != nonce or not hmac.compare_digest(
            str(response["mac"]), _mac(self.key, _canonical(reply))
        ):
            raise RequestRejected("epoch_reply_unauthenticated")
        epoch = response["launcher_epoch"]
        if type(epoch) is not int or epoch < 1:
            raise RequestRejected("epoch_reply_invalid")
        return epoch

    def cancel(self, request_id: str, cancellation_token: str) -> bool:
        body = {
            "operation": "cancel",
            "request_id": request_id,
            "cancellation_token": cancellation_token,
        }
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.connect_timeout)
                sock.connect(self.socket_path)
                _send(sock, {**body, "mac": _mac(self.key, payload)})
                return _receive(sock).get("cancelled") is True
        except OSError:
            return False


class HostSocketLauncher:
    """Host-owned socket server; executor and durable ledger are trusted dependencies."""

    def __init__(
        self,
        socket_path: str | Path,
        key: bytes,
        allowed_uid: int,
        execute_in_sandbox: Callable[
            [ExecutionRequest, threading.Event], ExecutionResult
        ],
        ledger_path: str | Path | None = None,
        *,
        require_authorization: bool = False,
        socket_dir_group_traverse: bool = False,
    ):
        if len(key) < 32:
            raise ValueError("bridge key must contain at least 32 bytes")
        self.socket_path, self.key, self.allowed_uid = (
            Path(socket_path),
            key,
            allowed_uid,
        )
        # C2-B: when set, only v2 requests with a verified binding execute.
        self.require_authorization = require_authorization
        # C2-C3A isolated instance only: the socket directory may additionally
        # grant group search (0710) so a non-root Worker in that group can
        # connect; SO_PEERCRED and HMAC still gate every request.
        self.socket_dir_group_traverse = socket_dir_group_traverse
        self.execute_in_sandbox = execute_in_sandbox
        self.ledger = RequestLedger(
            ledger_path or self.socket_path.with_suffix(".sqlite3")
        )
        self.started_at = int(time.time())
        # C2-C3A: fresh per Launcher instance; never persisted or reused.
        self.epoch = secrets.randbelow(2**62) + 1
        self._active: dict[str, tuple[threading.Event, bytes]] = {}
        self._lock = threading.Lock()

    def serve_forever(self, stop: threading.Event) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = self.socket_path.parent.lstat()
        allowed_other_bits = 0o010 if self.socket_dir_group_traverse else 0
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_mode & 0o077 & ~allowed_other_bits
            or parent_stat.st_uid != os.geteuid()
        ):
            raise RuntimeError("unsafe_socket_directory")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise RuntimeError("refusing_preexisting_socket_path")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            server.bind(str(self.socket_path))
            bound = self.socket_path.lstat()
            if not stat.S_ISSOCK(bound.st_mode):
                raise RuntimeError("socket_path_replaced")
            identity = (bound.st_dev, bound.st_ino)
            os.chmod(self.socket_path, 0o660)
            after_chmod = self.socket_path.lstat()
            if (after_chmod.st_dev, after_chmod.st_ino) != identity:
                raise RuntimeError("socket_path_replaced")
            server.listen(16)
            server.settimeout(0.25)
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            with self._lock:
                active = list(self._active.values())
            for cancellation, _token in active:
                cancellation.set()
            server.close()
            if identity:
                try:
                    current = self.socket_path.lstat()
                    if (
                        stat.S_ISSOCK(current.st_mode)
                        and (current.st_dev, current.st_ino) == identity
                    ):
                        self.socket_path.unlink()
                except FileNotFoundError:
                    pass

    def _authorized_peer(self, conn):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RequestRejected("peer_credentials_unavailable")
        _pid, uid, _gid = struct.unpack(
            "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != self.allowed_uid:
            raise RequestRejected("unauthorized_peer")

    def _handle(self, conn):
        with conn:
            conn.settimeout(10)
            request_id = "invalid"
            try:
                self._authorized_peer(conn)
                envelope = _receive(conn)
                if envelope.get("operation") == "epoch":
                    body = {"operation": "epoch", "nonce": envelope.get("nonce")}
                    if (
                        set(envelope) != {"operation", "nonce", "mac"}
                        or not isinstance(body["nonce"], str)
                        or len(body["nonce"]) != 64
                        or not hmac.compare_digest(
                            str(envelope["mac"]), _mac(self.key, _canonical(body))
                        )
                    ):
                        raise RequestRejected("invalid_authentication")
                    reply = {"launcher_epoch": self.epoch, "nonce": body["nonce"]}
                    _send(conn, {**reply, "mac": _mac(self.key, _canonical(reply))})
                    return
                if envelope.get("operation") == "cancel":
                    body = {
                        k: envelope[k]
                        for k in ("operation", "request_id", "cancellation_token")
                    }
                    payload = json.dumps(
                        body, separators=(",", ":"), sort_keys=True
                    ).encode()
                    if not hmac.compare_digest(
                        str(envelope.get("mac", "")), _mac(self.key, payload)
                    ):
                        raise RequestRejected("invalid_authentication")
                    token_hash = hashlib.sha256(
                        body["cancellation_token"].encode()
                    ).digest()
                    with self._lock:
                        active = self._active.get(body["request_id"])
                        allowed = active is not None and hmac.compare_digest(
                            active[1], token_hash
                        )
                        if allowed:
                            active[0].set()
                    _send(conn, {"cancelled": allowed})
                    return
                if (
                    set(envelope) != {"operation", "request", "mac"}
                    or envelope["operation"] != "execute"
                ):
                    raise RequestRejected("invalid_envelope")
                raw = json.dumps(
                    envelope["request"], separators=(",", ":"), sort_keys=True
                ).encode()
                if not hmac.compare_digest(str(envelope["mac"]), _mac(self.key, raw)):
                    raise RequestRejected("invalid_authentication")
                request = ExecutionRequest.from_wire(envelope["request"])
                request_id = request.request_id
                if request.issued_at < self.started_at:
                    raise RequestRejected("request_predates_launcher")
                if request.version in (2, 3):
                    verify_authorization(
                        request,
                        launcher_epoch=self.epoch,
                        require_epoch=self.require_authorization,
                    )
                elif self.require_authorization:
                    raise RequestRejected("authorization_required")
                if not self.ledger.admit(request_id):
                    raise RequestRejected("duplicate_request")
                cancel = threading.Event()
                with self._lock:
                    self._active[request_id] = (
                        cancel,
                        hashlib.sha256(request.cancellation_token.encode()).digest(),
                    )
                try:
                    result = self.execute_in_sandbox(request, cancel)
                    if result.request_id != request_id:
                        raise RequestRejected("executor_result_mismatch")
                    out, err = result.stdout.encode(), result.stderr.encode()
                    result = replace(
                        result,
                        stdout=out[:65536].decode("utf-8", "replace"),
                        stderr=err[:65536].decode("utf-8", "replace"),
                        truncated=result.truncated
                        or len(out) > 65536
                        or len(err) > 65536,
                    )
                    result.validate()
                    result_wire = _result_wire(result)
                    self.ledger.terminal(request_id, result.execution_status)
                except Exception:
                    result = ExecutionResult(
                        request_id,
                        "INTERNAL_ERROR",
                        None,
                        "",
                        "",
                        error_code="executor_failure",
                    )
                    result_wire = _result_wire(result)
                    self.ledger.terminal(request_id, "INTERNAL_ERROR")
                finally:
                    with self._lock:
                        self._active.pop(request_id, None)
                _send(conn, result_wire)
            except Exception as exc:
                rejected = ExecutionResult(
                    request_id, "REJECTED", None, "", "", error_code=str(exc)[:80]
                )
                _send_response(conn, _result_wire(rejected))
