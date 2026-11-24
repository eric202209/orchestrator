"""C2-B operation binding carried inside an HMAC-authenticated v2 request.

The Worker Gateway verifies live TaskExecution authority and then binds the
exact operation to that authority. The Host cannot read the Orchestrator
database; it verifies only that the authenticated binding matches the exact
request bytes, is unexpired, and is single-use (nonce == request_id, enforced
by the existing durable ledger). Stdlib only: this module ships with the Host
Launcher copy.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from .contract import MAX_REQUEST_AGE_SECONDS, RequestRejected

AUTHORIZATION_VERSION = 1
AUTHORIZATION_FIELDS = frozenset(
    {
        "version",
        "task_execution_id",
        "session_id",
        "session_instance_id",
        "task_id",
        "attempt_number",
        "worker_identity",
        "dispatch_fence",
        "dispatch_celery_task_id",
        "operation_sha256",
        "input_sha256",
        "nonce",
        "issued_at",
        "expires_at",
        "launcher_epoch",
    }
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=True
    ).encode("ascii")


def operation_sha256(*, execution_profile: str, code: str, timeout_seconds: int) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "execution_profile": execution_profile,
                "code": code,
                "timeout_seconds": timeout_seconds,
            }
        )
    ).hexdigest()


def input_sha256(code: str) -> str:
    """Digest of the only staged input (``input.py``); no workspace is staged."""

    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def execution_ref(task_execution_id: int, attempt_number: int) -> str:
    return f"te-{task_execution_id}-a{attempt_number}"


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def verify_authorization(
    request: Any, *, now: int | None = None, launcher_epoch: int | None = None
) -> None:
    """Fail closed unless the v2 binding matches this exact request."""

    auth = request.authorization
    if not isinstance(auth, dict) or set(auth) != AUTHORIZATION_FIELDS:
        raise RequestRejected("invalid_authorization_shape")
    if auth["version"] != AUTHORIZATION_VERSION:
        raise RequestRejected("unsupported_authorization_version")
    for name in ("task_execution_id", "session_id", "task_id", "attempt_number"):
        if not _positive_int(auth[name]):
            raise RequestRejected(f"invalid_authorization_{name}")
    if type(auth["dispatch_fence"]) is not int or auth["dispatch_fence"] < 1:
        raise RequestRejected("invalid_authorization_dispatch_fence")
    for name in ("session_instance_id", "worker_identity", "dispatch_celery_task_id"):
        if not isinstance(auth[name], str) or not 1 <= len(auth[name]) <= 255:
            raise RequestRejected(f"invalid_authorization_{name}")
    if auth["nonce"] != request.request_id:
        raise RequestRejected("authorization_nonce_mismatch")
    if request.task_id != str(auth["task_id"]) or request.execution_id != (
        execution_ref(auth["task_execution_id"], auth["attempt_number"])
    ):
        raise RequestRejected("authorization_identity_mismatch")
    if not (_SHA256.fullmatch(str(auth["operation_sha256"]))) or auth[
        "operation_sha256"
    ] != operation_sha256(
        execution_profile=request.execution_profile,
        code=request.code,
        timeout_seconds=request.timeout_seconds,
    ):
        raise RequestRejected("operation_digest_mismatch")
    if auth["input_sha256"] != input_sha256(request.code):
        raise RequestRejected("input_digest_mismatch")
    issued, expires = auth["issued_at"], auth["expires_at"]
    if type(issued) is not int or type(expires) is not int:
        raise RequestRejected("invalid_authorization_time")
    if issued != request.issued_at or not (
        0 < expires - issued <= MAX_REQUEST_AGE_SECONDS
    ):
        raise RequestRejected("invalid_authorization_window")
    if (int(time.time()) if now is None else now) > expires:
        raise RequestRejected("authorization_expired")
    epoch = auth["launcher_epoch"]
    if epoch is not None and (type(epoch) is not int or epoch != launcher_epoch):
        raise RequestRejected("launcher_epoch_mismatch")
