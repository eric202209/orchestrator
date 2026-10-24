"""Local trusted Supervisor process for Phase 39 one-shot attempts.

Run: ``python -m app.supervisor.server --socket PATH --state-dir DIR
--allowed-uid UID [--allowed-uid UID ...]``.

- Transport: one JSON request line and one JSON response line per connection
  on a Unix domain socket (mode 0660 in a directory that is not world
  writable). There is no network listener.
- Authentication: the kernel-reported peer UID (``SO_PEERCRED``) must be in
  the explicit ``--allowed-uid`` set; anything else is refused unread.
- Operations: ``admission``, ``get_attempt_status``, ``start_attempt``. A
  start request must match the fixed F1c-1 ``executor-start-attempt/v1``
  shape exactly; it cannot carry a command, image, mount, or environment.
- Execution: the only operation is the built-in fixed no-op. No real
  executor exists in F2.
- Durability: ``AttemptLedger`` in the 0700 state directory; one Supervisor
  per state directory (exclusive lock). On start, RUNNING attempts become
  UNKNOWN (never re-run) and ACCEPTED attempts, which never began, run once.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
import uuid

from app.supervisor.ledger import AttemptLedger, LedgerError

PROTOCOL = "one-shot-supervisor/v1"
START_REQUEST_SCHEMA_VERSION = "executor-start-attempt/v1"
SUPPORTED_EXECUTION_PROFILES = frozenset({("executor-contained-planning", "v1")})
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0

_DIGEST_FIELDS = (
    "attempt_idempotency_key",
    "request_payload_sha256",
    "authorization_identity_sha256",
    "source_snapshot_digest",
    "candidate_initial_digest",
    "operation_scope_digest",
)
_UUID_FIELDS = ("authorization_uuid", "claim_uuid", "candidate_uuid")
_ID_FIELDS = ("project_id", "session_id", "task_id", "task_execution_id")
_TEXT_FIELDS = (
    "schema_version",
    "session_instance_id",
    "source_revision_identity",
    "execution_profile_id",
    "execution_profile_version",
    "operation_scope_id",
    "operation_scope_version",
)
START_REQUEST_FIELDS = frozenset(
    _DIGEST_FIELDS + _UUID_FIELDS + _ID_FIELDS + _TEXT_FIELDS
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InvalidRequest(ValueError):
    pass


def _digest(value) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_start_request(request) -> dict:
    """Return the request if it is exactly a valid fixed start request."""

    if not isinstance(request, dict) or set(request) != START_REQUEST_FIELDS:
        raise InvalidRequest("start_request_fields_invalid")
    if request["schema_version"] != START_REQUEST_SCHEMA_VERSION:
        raise InvalidRequest("start_request_schema_unsupported")
    for field in _DIGEST_FIELDS:
        if not _digest(request[field]):
            raise InvalidRequest(f"invalid_{field}")
    for field in _UUID_FIELDS:
        if not _canonical_uuid(request[field]):
            raise InvalidRequest(f"invalid_{field}")
    for field in _ID_FIELDS:
        value = request[field]
        if type(value) is not int or value <= 0:
            raise InvalidRequest(f"invalid_{field}")
    for field in _TEXT_FIELDS:
        value = request[field]
        if not isinstance(value, str) or not value or len(value) > 256:
            raise InvalidRequest(f"invalid_{field}")
    profile = (request["execution_profile_id"], request["execution_profile_version"])
    if profile not in SUPPORTED_EXECUTION_PROFILES:
        raise InvalidRequest("execution_profile_unsupported")
    return request


def _receipt(record, *, deduplicated=False) -> dict:
    return {
        "attempt_idempotency_key": record.attempt_idempotency_key,
        "status": record.status,
        "request_payload_sha256": record.request_payload_sha256,
        "authorization_identity_sha256": record.authorization_identity_sha256,
        "supervisor_attempt_id": record.supervisor_attempt_id,
        "deduplicated": deduplicated,
        "exit_code": record.exit_code,
        "status_reason": record.status_reason,
    }


class Supervisor:
    def __init__(self, ledger: AttemptLedger, allowed_uids, noop_seconds=0.0):
        self.ledger = ledger
        self.allowed_uids = frozenset(allowed_uids)
        self.noop_seconds = float(noop_seconds)

    # Fixed operation ------------------------------------------------------

    def run_attempt(self, key: str) -> None:
        # RUNNING is durable before the operation begins: a crash after this
        # point is reported UNKNOWN on restart and never re-run.
        if not self.ledger.transition(key, "ACCEPTED", "RUNNING"):
            return
        time.sleep(self.noop_seconds)
        self.ledger.transition(key, "RUNNING", "EXITED", exit_code=0)

    def launch(self, key: str) -> None:
        threading.Thread(target=self.run_attempt, args=(key,), daemon=True).start()

    def recover(self) -> None:
        for key in self.ledger.keys_with_status("RUNNING"):
            self.ledger.transition(
                key, "RUNNING", "UNKNOWN", reason="supervisor_restarted_while_running"
            )
        for key in self.ledger.keys_with_status("ACCEPTED"):
            self.launch(key)

    # Requests -------------------------------------------------------------

    def handle(self, message) -> dict:
        if not isinstance(message, dict) or message.get("protocol") != PROTOCOL:
            raise InvalidRequest("protocol_unsupported")
        op = message.get("op")
        if op == "admission" and set(message) == {"protocol", "op"}:
            try:
                self.ledger.check()
            except LedgerError:
                return {"status": "UNAVAILABLE", "durable_deduplication": False}
            return {"status": "TRUSTED", "durable_deduplication": True}
        if op == "get_attempt_status" and set(message) == {"protocol", "op", "key"}:
            if not _digest(message["key"]):
                raise InvalidRequest("invalid_attempt_idempotency_key")
            record = self.ledger.get(message["key"])
            if record is None:
                return {"receipt": None}
            return {"receipt": _receipt(record)}
        if op == "start_attempt" and set(message) == {"protocol", "op", "request"}:
            request = validate_start_request(message["request"])
            registration = self.ledger.register(request)
            if registration.conflict:
                return {
                    "error": "attempt_conflict",
                    "receipt": _receipt(registration.record),
                }
            if registration.created:
                self.launch(request["attempt_idempotency_key"])
            return {
                "receipt": _receipt(
                    registration.record, deduplicated=not registration.created
                )
            }
        raise InvalidRequest("operation_unsupported")


def _peer_uid(connection: socket.socket) -> int:
    creds = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _, uid, _ = struct.unpack("3i", creds)
    return uid


def _read_line(connection: socket.socket) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST_BYTES:
            raise InvalidRequest("request_too_large")
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise InvalidRequest("request_framing_invalid")
    return data[:-1]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        supervisor: Supervisor = self.server.supervisor
        connection: socket.socket = self.request
        connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        try:
            if _peer_uid(connection) not in supervisor.allowed_uids:
                response = {"error": "caller_not_authenticated"}
            else:
                message = json.loads(_read_line(connection))
                response = supervisor.handle(message)
        except InvalidRequest as exc:
            response = {"error": "invalid_request", "reason": str(exc)}
        except (ValueError, UnicodeDecodeError):
            response = {"error": "invalid_request", "reason": "request_not_json"}
        except (LedgerError, OSError) as exc:
            response = {"error": "supervisor_unavailable", "reason": type(exc).__name__}
        except Exception as exc:  # fail closed on anything unexpected
            response = {
                "error": "supervisor_internal_error",
                "reason": type(exc).__name__,
            }
        response["protocol"] = PROTOCOL
        try:
            connection.sendall(json.dumps(response, sort_keys=True).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    # Clients connect with a timeout (non-blocking), so a full backlog is an
    # immediate EAGAIN rather than a wait; the default of 5 refuses bursts.
    request_queue_size = 128


def _private_directory(path: Path, *, owner_only: bool) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SystemExit(f"{path} must be a directory owned by the Supervisor user")
    forbidden = 0o077 if owner_only else 0o002
    if info.st_mode & forbidden:
        raise SystemExit(f"{path} permissions are too open")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--allowed-uid", required=True, type=int, action="append")
    parser.add_argument("--noop-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    _private_directory(args.state_dir, owner_only=True)
    _private_directory(args.socket.parent, owner_only=False)
    lock = open(args.state_dir / "supervisor.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another Supervisor holds this state directory")

    ledger = AttemptLedger(args.state_dir / "attempts.sqlite3")
    ledger.initialize()
    supervisor = Supervisor(ledger, args.allowed_uid, args.noop_seconds)

    if args.socket.exists() or args.socket.is_symlink():
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):
            raise SystemExit(f"{args.socket} exists and is not a socket")
        args.socket.unlink()
    old_umask = os.umask(0o117)
    try:
        server = _Server(str(args.socket), _Handler)
    finally:
        os.umask(old_umask)
    os.chmod(args.socket, 0o660)
    server.supervisor = supervisor
    supervisor.recover()

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"SUPERVISOR_READY pid={os.getpid()}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        try:
            args.socket.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
