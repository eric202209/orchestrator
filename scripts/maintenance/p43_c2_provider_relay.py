"""P43-PRODUCT-23C disposable provider relay (evaluation adapter only).

One controlled real-provider evaluation window gives the disposable C2 Worker
exactly one route to a model: this relay. It is not a production component
and not a general HTTP proxy.

* One upstream (``P43_RELAY_UPSTREAM``, ``http://host:port``) and one model
  (``P43_RELAY_MODEL``), both fixed at start.
* Exactly ``GET /v1/models`` and ``POST /v1/chat/completions``. Any other
  method, path, query, absolute-form target, streaming or multi-choice
  request, or other model is refused without contacting the upstream.
* No redirects, model fallback, upstream retries, routing or balancing.
* At most ``MAX_COMPLETIONS`` forwarded completion requests for the window,
  counted atomically, and durably recorded, *before* forwarding. Failed,
  timed-out and disconnected forwards consume budget. Model-list requests
  are counted separately and never consume it.
* No completion is admitted before the Host arms the window by writing the
  deadline file, nor after the deadline. Each upstream exchange is bounded
  by the remaining evaluation time.

The listener binds only the address the container has when it starts
(``P43_RELAY_BIND=self``): the Host starts the relay on the internal network
and attaches the provider network afterwards, so clients on the provider
network cannot reach it.

State (ledger and recorded request/response bodies) is written under
``P43_RELAY_STATE_DIR``, which must be empty at start: a restarted relay can
never reset the budget of a window. Headers are never recorded. Standard
library only; run with ``python3 -I``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

MAX_COMPLETIONS = 6
MAX_MODEL_LISTS = 32
MAX_CONCURRENCY = 2
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_UPSTREAM_SECONDS = 600.0
MODEL_LIST_SECONDS = 10.0
CLIENT_SOCKET_SECONDS = 30.0
MODELS_PATH = "/v1/models"
COMPLETIONS_PATH = "/v1/chat/completions"
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")


class RelayConfigurationError(ValueError):
    """The relay was started outside its single fixed contract."""


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_upstream(value: str) -> tuple[str, int]:
    """``http://host:port`` only: no credentials, path, query or fragment."""

    parts = urlsplit(str(value or "").strip())
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise RelayConfigurationError("upstream_must_be_http_host_port")
    try:
        port = parts.port
    except ValueError as exc:
        raise RelayConfigurationError("upstream_port_invalid") from exc
    if port is None:
        raise RelayConfigurationError("upstream_port_required")
    return parts.hostname, port


def validate_model(value: str) -> str:
    model = str(value or "").strip()
    if not _MODEL_RE.fullmatch(model):
        raise RelayConfigurationError("model_identity_invalid")
    return model


class Deadline:
    """The Host-written evaluation deadline, read once and then fixed."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._value: float | None = None

    def value(self) -> float | None:
        with self._lock:
            if self._value is None:
                path = self._path
                if path.is_symlink() or not path.is_file():
                    return None
                text = path.read_text(encoding="ascii").strip()
                if not re.fullmatch(r"[0-9]{9,11}", text):
                    return None
                self._value = float(text)
            return self._value

    def remaining(self) -> float | None:
        value = self.value()
        return None if value is None else value - self._clock()


class Ledger:
    """Append-only JSONL ledger plus bounded bodies; created exclusively."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        if not state_dir.is_dir() or state_dir.is_symlink():
            raise RelayConfigurationError("state_dir_missing")
        if any(state_dir.iterdir()):
            raise RelayConfigurationError("state_dir_not_empty")
        self.bodies = state_dir / "bodies"
        self.bodies.mkdir(mode=0o700)
        fd = os.open(
            state_dir / "ledger.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND,
            0o600,
        )
        self._file = os.fdopen(fd, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._file.write(json.dumps(record, sort_keys=True) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())

    def body(self, name: str, data: bytes) -> None:
        fd = os.open(self.bodies / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


class Relay:
    def __init__(
        self,
        *,
        upstream: str,
        model: str,
        state_dir: Path,
        deadline_file: Path,
        clock: Callable[[], float] = time.time,
    ):
        self.host, self.port = parse_upstream(upstream)
        self.model = validate_model(model)
        self.ledger = Ledger(state_dir)
        self.deadline = Deadline(deadline_file, clock)
        self.clock = clock
        self._lock = threading.Lock()
        self._sequence = 0
        self._completions = 0
        self._model_lists = 0
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self.ledger.append(
            {
                "event": "relay_started",
                "at": clock(),
                "upstream": f"http://{self.host}:{self.port}",
                "model": self.model,
                "max_completions": MAX_COMPLETIONS,
                "max_concurrency": MAX_CONCURRENCY,
            }
        )

    # -- admission ---------------------------------------------------------

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def admit_completion(self, sequence: int) -> tuple[int, float]:
        """Atomically consume one completion slot and record it durably.

        Raises ``Refused`` (nothing consumed) when unarmed or past the
        deadline, and when the budget is exhausted.
        """

        with self._lock:
            remaining = self.deadline.remaining()
            if remaining is None:
                raise Refused(503, "evaluation_not_armed")
            if remaining <= 0:
                raise Refused(503, "evaluation_deadline_exhausted")
            if self._completions >= MAX_COMPLETIONS:
                raise Refused(429, "completion_budget_exhausted")
            self._completions += 1
            index = self._completions
            self.ledger.append(
                {
                    "event": "completion_admitted",
                    "sequence": sequence,
                    "completion_index": index,
                    "at": self.clock(),
                    "remaining_seconds": round(remaining, 3),
                }
            )
            return index, remaining

    def admit_model_list(self) -> float:
        with self._lock:
            remaining = self.deadline.remaining()
            if remaining is not None and remaining <= 0:
                raise Refused(503, "evaluation_deadline_exhausted")
            if self._model_lists >= MAX_MODEL_LISTS:
                raise Refused(429, "model_list_limit_exhausted")
            self._model_lists += 1
            return MODEL_LIST_SECONDS if remaining is None else remaining

    def validate_completion(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise Refused(400, "request_not_json") from exc
        if not isinstance(payload, dict):
            raise Refused(400, "request_not_object")
        if payload.get("model") != self.model:
            raise Refused(403, "model_not_approved")
        if payload.get("stream") not in (None, False):
            raise Refused(400, "streaming_not_supported")
        if payload.get("n") not in (None, 1):
            raise Refused(400, "multiple_choices_not_supported")
        if not isinstance(payload.get("messages"), list):
            raise Refused(400, "messages_required")

    # -- upstream ----------------------------------------------------------

    def exchange(
        self, method: str, path: str, body: bytes | None, budget_seconds: float
    ) -> tuple[int, bytes]:
        """One upstream exchange within ``budget_seconds`` total; no retry."""

        stop_at = time.monotonic() + max(0.001, budget_seconds)

        def left() -> float:
            value = stop_at - time.monotonic()
            if value <= 0:
                raise socket.timeout("relay_total_deadline")
            return value

        connection = http.client.HTTPConnection(self.host, self.port, timeout=left())
        try:
            headers = {"Accept": "application/json", "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            connection.sock.settimeout(left())
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise Refused(502, "upstream_redirect_refused")
            chunks: list[bytes] = []
            size = 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(left())
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise Refused(502, "upstream_response_too_large")
                chunks.append(chunk)
            return response.status, b"".join(chunks)
        finally:
            connection.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_error(reason: str) -> bytes:
    return json.dumps({"error": {"type": "relay_refused", "code": reason}}).encode()


class Handler(BaseHTTPRequestHandler):
    relay: Relay
    server_version = "p43-c2-relay"
    sys_version = ""
    timeout = CLIENT_SOCKET_SECONDS
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), format % args))
        sys.stdout.flush()

    def _send(self, status: int, data: bytes) -> bool:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _record(self, record: dict[str, Any]) -> None:
        self.relay.ledger.append(record)

    def _refuse(self, sequence: int, kind: str, error: Refused, **extra: Any) -> None:
        self._record(
            {
                "event": "refused",
                "sequence": sequence,
                "kind": kind,
                "method": self.command,
                "path": self.path[:256],
                "reason": error.reason,
                "http_status": error.status,
                "forwarded": False,
                "at": self.relay.clock(),
                **extra,
            }
        )
        self._send(error.status, _json_error(error.reason))

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise Refused(411, "chunked_request_refused")
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdigit():
            raise Refused(411, "content_length_required")
        length = int(raw)
        if length > MAX_REQUEST_BYTES:
            raise Refused(413, "request_too_large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise Refused(400, "request_truncated")
        return data

    def do_GET(self) -> None:  # noqa: N802
        sequence = self.relay._next_sequence()
        if self.path != MODELS_PATH:
            self._refuse(sequence, "other", Refused(404, "path_not_allowed"))
            return
        try:
            budget = self.relay.admit_model_list()
        except Refused as error:
            self._refuse(sequence, "models", error)
            return
        started = self.relay.clock()
        record: dict[str, Any] = {
            "event": "model_list",
            "sequence": sequence,
            "forwarded": True,
            "at": started,
        }
        try:
            status, data = self.relay.exchange(
                "GET", MODELS_PATH, None, min(MODEL_LIST_SECONDS, budget)
            )
            listed = json.loads(data).get("data") if status == 200 else None
            if not isinstance(listed, list):
                raise Refused(502, f"upstream_status_{status}")
            served = [
                entry
                for entry in listed
                if isinstance(entry, dict) and entry.get("id") == self.relay.model
            ]
            if len(served) != 1:
                raise Refused(502, "approved_model_not_served")
            entry = served[0]
            record.update(
                outcome="completed",
                upstream_status=status,
                served_model=entry.get("id"),
                served_root=entry.get("root"),
                served_max_model_len=entry.get("max_model_len"),
                upstream_model_ids=sorted(
                    str(e.get("id")) for e in listed if isinstance(e, dict)
                )[:16],
            )
            reply = json.dumps({"object": "list", "data": served}).encode()
            status_out = 200
        except Refused as error:
            record.update(outcome=error.reason, http_status=error.status)
            reply, status_out = _json_error(error.reason), error.status
        except (OSError, ValueError, http.client.HTTPException) as error:
            reason = "upstream_timeout" if _is_timeout(error) else "upstream_error"
            record.update(outcome=reason, error_type=type(error).__name__)
            reply = _json_error(reason)
            status_out = 504 if reason == "upstream_timeout" else 502
        record["http_status"] = status_out
        record["duration_seconds"] = round(self.relay.clock() - started, 3)
        record["client_delivered"] = self._send(status_out, reply)
        self._record(record)

    def do_POST(self) -> None:  # noqa: N802
        relay = self.relay
        sequence = relay._next_sequence()
        if self.path != COMPLETIONS_PATH:
            self._refuse(sequence, "other", Refused(404, "path_not_allowed"))
            return
        try:
            body = self._read_body()
        except Refused as error:
            self._refuse(sequence, "completion", error)
            return
        digest = _sha256(body)
        try:
            relay.validate_completion(body)
        except Refused as error:
            self._refuse(sequence, "completion", error, request_sha256=digest)
            return
        if not relay._slots.acquire(blocking=False):
            self._refuse(
                sequence,
                "completion",
                Refused(429, "relay_concurrency_exhausted"),
                request_sha256=digest,
            )
            return
        try:
            try:
                index, remaining = relay.admit_completion(sequence)
            except Refused as error:
                self._refuse(sequence, "completion", error, request_sha256=digest)
                return
            relay.ledger.body(f"{sequence:04d}.request.json", body)
            timeout = min(MAX_UPSTREAM_SECONDS, remaining)
            started = relay.clock()
            record: dict[str, Any] = {
                "event": "completion",
                "sequence": sequence,
                "completion_index": index,
                "forwarded": True,
                "at": started,
                "timeout_seconds": round(timeout, 3),
                "request_bytes": len(body),
                "request_sha256": digest,
            }
            try:
                status, data = relay.exchange("POST", COMPLETIONS_PATH, body, timeout)
                relay.ledger.body(f"{sequence:04d}.response.json", data)
                record.update(
                    upstream_status=status,
                    response_bytes=len(data),
                    response_sha256=_sha256(data),
                )
                reply, status_out = data, status
                if status == 200:
                    parsed = json.loads(data)
                    record["response_model"] = parsed.get("model")
                    record["usage"] = parsed.get("usage")
                    if parsed.get("model") != relay.model:
                        raise Refused(502, "upstream_model_mismatch")
                    record["outcome"] = "completed"
                else:
                    record["outcome"] = f"upstream_status_{status}"
            except Refused as error:
                record.update(outcome=error.reason)
                reply, status_out = _json_error(error.reason), error.status
            except (OSError, ValueError, http.client.HTTPException) as error:
                reason = "upstream_timeout" if _is_timeout(error) else "upstream_error"
                record.update(outcome=reason, error_type=type(error).__name__)
                reply = _json_error(reason)
                status_out = 504 if reason == "upstream_timeout" else 502
            record["duration_seconds"] = round(relay.clock() - started, 3)
            record["http_status"] = status_out
            record["client_delivered"] = self._send(status_out, reply)
            self._record(record)
        finally:
            relay._slots.release()

    def _method_not_allowed(self) -> None:
        sequence = self.relay._next_sequence()
        self._refuse(sequence, "other", Refused(405, "method_not_allowed"))

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed
    do_CONNECT = do_TRACE = _method_not_allowed


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, (socket.timeout, TimeoutError))


def build_server(relay: Relay, address: tuple[str, int]) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"relay": relay})
    server = ThreadingHTTPServer(address, handler)
    server.daemon_threads = True
    return server


def main(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        relay = Relay(
            upstream=environ.get("P43_RELAY_UPSTREAM", ""),
            model=environ.get("P43_RELAY_MODEL", ""),
            state_dir=Path(environ.get("P43_RELAY_STATE_DIR", "")),
            deadline_file=Path(environ.get("P43_RELAY_DEADLINE_FILE", "")),
        )
        port = int(environ.get("P43_RELAY_PORT", "8000"))
        bind = str(environ.get("P43_RELAY_BIND", "self"))
        if bind == "self":
            bind = socket.gethostbyname(socket.gethostname())
        server = build_server(relay, (bind, port))
    except (RelayConfigurationError, ValueError, OSError) as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 78
    print(
        f"p43-c2-relay listening bind={bind} port={port} "
        f"upstream=http://{relay.host}:{relay.port} "
        f"model={relay.model} max_completions={MAX_COMPLETIONS}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
