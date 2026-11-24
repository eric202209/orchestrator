"""P43-PRODUCT-22-C2-C2B: minimum authorized Sandbox execution Gateway.

Gated to disposable C2 mode (``WORKER_ISOLATION_MODE=disposable`` and
``WORKER_BRIDGE_MODE=c2b-sandbox``). Outside that mode nothing here runs and
existing behavior is unchanged. Inside it, exactly one operation is routed:
a self-contained ``python -c 'import sys; sys.exit(<int>)'`` verification
command from the existing simple-verification step. Every other local
execution route refuses (``refuse_local_execution``); no route falls back to
local execution when the Host refuses, times out, or is unavailable.

Authority comes only from the trusted Worker lifecycle context (TaskExecution
row claimed by this process) and is re-read from the database before dispatch
and again before a result is returned. Model content supplies only the script.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shlex
import socket
import time
import uuid
from typing import Any, Callable

from app.services.sandbox_bridge.authorization import (
    AUTHORIZATION_VERSION,
    execution_ref,
    input_sha256,
    operation_sha256,
)
from app.services.sandbox_bridge.contract import (
    MAX_TIMEOUT_SECONDS,
    PROFILE,
    ExecutionRequest,
    ExecutionResult,
    RequestRejected,
)

C2_BRIDGE_MODE = "c2b-sandbox"
AUTHORIZATION_TTL_SECONDS = 20
# C2-C3A: the execution loop's _reserve_mutation_authority holds the SQLite
# writer lock across this call. Worst-case hold is connect (2 s) + epoch reply
# (5 s) + timeout + 10 s response wait; with this cap it stays below the
# 30 s SQLite busy_timeout, so a concurrent cancellation waits instead of
# failing with "database is locked". Not a cancellation mechanism.
C2_SANDBOX_TIMEOUT_CAP_SECONDS = 10


def sandbox_c2_mode_enabled() -> bool:
    return (
        os.environ.get("WORKER_ISOLATION_MODE", "").strip() == "disposable"
        and os.environ.get("WORKER_BRIDGE_MODE", "").strip() == C2_BRIDGE_MODE
    )


class SandboxLocalExecutionRefused(RuntimeError):
    """A model-influenced local execution route is disabled in C2 mode."""


def refuse_local_execution(route: str) -> None:
    if sandbox_c2_mode_enabled():
        raise SandboxLocalExecutionRefused(
            f"c2_sandbox_mode_refuses_local_execution:{route}"
        )


@dataclass(frozen=True)
class SandboxExecutionContext:
    """Trusted identifiers held by the Worker lifecycle, never by the model."""

    db: Any
    task_execution_id: int
    session_id: int
    task_id: int
    session_instance_id: str | None


@dataclass(frozen=True)
class SandboxGatewayOutcome:
    accepted: bool
    reason: str | None = None
    result: ExecutionResult | None = None
    authorization: dict[str, Any] | None = None


class SandboxAuthorityError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sandboxable_python_script(command: str) -> str | None:
    """Return the script only for the admitted self-contained form."""

    from app.services.orchestration.phases.execution_local_steps import (
        _is_literal_sys_exit_script,
    )

    try:
        tokens = shlex.split(" ".join(str(command or "").split()), posix=True)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[0] not in {"python", "python3"} or tokens[1] != "-c":
        return None
    return tokens[2] if _is_literal_sys_exit_script(tokens[2]) else None


def _current_worker_identity() -> tuple[int, str, str | None]:
    from app.services.execution.process_identity import (
        current_process_start_identity,
    )

    pid = os.getpid()
    return pid, socket.gethostname(), current_process_start_identity(pid)


def resolve_attempt_authority(context: SandboxExecutionContext) -> dict[str, Any]:
    """Read the live attempt and verify this Worker owns its current claim."""

    from app.models import Session as SessionModel, Task, TaskExecution, TaskStatus
    from sqlalchemy import func

    db = context.db
    # Column queries read committed state without touching the identity map.
    row = (
        db.query(
            TaskExecution.session_id,
            TaskExecution.task_id,
            TaskExecution.attempt_number,
            TaskExecution.status,
            TaskExecution.dispatch_state,
            TaskExecution.dispatch_session_instance_id,
            TaskExecution.dispatch_celery_task_id,
            TaskExecution.dispatch_lease_fence,
            TaskExecution.worker_pid,
            TaskExecution.worker_hostname,
            TaskExecution.worker_process_start_identity,
        )
        .filter(TaskExecution.id == context.task_execution_id)
        .first()
    )
    if row is None:
        raise SandboxAuthorityError("task_execution_missing")
    if (row.session_id, row.task_id) != (context.session_id, context.task_id):
        raise SandboxAuthorityError("task_execution_binding_mismatch")
    if row.status == TaskStatus.CANCELLED:
        raise SandboxAuthorityError("task_execution_cancelled")
    if row.status != TaskStatus.RUNNING:
        raise SandboxAuthorityError("task_execution_not_running")
    if row.dispatch_state != "CLAIMED" or not row.dispatch_celery_task_id:
        raise SandboxAuthorityError("dispatch_not_claimed")
    if (
        not context.session_instance_id
        or row.dispatch_session_instance_id != context.session_instance_id
    ):
        raise SandboxAuthorityError("session_generation_mismatch")
    latest = (
        db.query(func.max(TaskExecution.attempt_number))
        .filter(
            TaskExecution.session_id == row.session_id,
            TaskExecution.task_id == row.task_id,
        )
        .scalar()
    )
    if latest != row.attempt_number:
        raise SandboxAuthorityError("stale_attempt")
    pid, hostname, start_identity = _current_worker_identity()
    if (
        row.worker_pid != pid
        or row.worker_hostname != hostname
        or not start_identity
        or row.worker_process_start_identity != start_identity
    ):
        raise SandboxAuthorityError("worker_ownership_mismatch")
    session = (
        db.query(
            SessionModel.status,
            SessionModel.is_active,
            SessionModel.instance_id,
            SessionModel.deleted_at,
        )
        .filter(SessionModel.id == row.session_id)
        .first()
    )
    if (
        session is None
        or session.status != "running"
        or not session.is_active
        or session.deleted_at is not None
    ):
        raise SandboxAuthorityError("session_not_running")
    if session.instance_id != context.session_instance_id:
        raise SandboxAuthorityError("session_generation_mismatch")
    task_status = db.query(Task.status).filter(Task.id == row.task_id).scalar()
    if task_status != TaskStatus.RUNNING:
        # The Task row is the existing cancellation authority.
        raise SandboxAuthorityError("task_not_running")
    return {
        "task_execution_id": context.task_execution_id,
        "session_id": row.session_id,
        "session_instance_id": context.session_instance_id,
        "task_id": row.task_id,
        "attempt_number": row.attempt_number,
        "worker_identity": f"{hostname}:{pid}:{start_identity}",
        "dispatch_fence": int(row.dispatch_lease_fence or 0),
        "dispatch_celery_task_id": row.dispatch_celery_task_id,
    }


class SandboxExecutionGateway:
    def __init__(self, client: Any, *, clock: Callable[[], float] = time.time):
        self.client, self.clock = client, clock

    @classmethod
    def from_environment(cls) -> "SandboxExecutionGateway":
        from app.services.sandbox_bridge.host_service import load_key
        from app.services.sandbox_bridge.transport import SandboxClient

        socket_path = os.environ.get("SANDBOX_BRIDGE_SOCKET", "").strip()
        key_file = os.environ.get("SANDBOX_BRIDGE_KEY_FILE", "").strip()
        if not socket_path or not key_file:
            raise SandboxAuthorityError("sandbox_bridge_unconfigured")
        try:
            key = load_key(Path(key_file))
        except Exception:
            raise SandboxAuthorityError("sandbox_bridge_key_unavailable") from None
        return cls(SandboxClient(socket_path, key))

    def execute_python(
        self, context: SandboxExecutionContext, script: str, timeout_seconds: int
    ) -> SandboxGatewayOutcome:
        try:
            authority = resolve_attempt_authority(context)
        except SandboxAuthorityError as exc:
            return SandboxGatewayOutcome(False, exc.reason)
        timeout = max(
            1,
            min(
                int(timeout_seconds),
                MAX_TIMEOUT_SECONDS,
                C2_SANDBOX_TIMEOUT_CAP_SECONDS,
            ),
        )
        try:
            launcher_epoch = self.client.launcher_epoch()
        except RequestRejected as exc:
            return SandboxGatewayOutcome(False, f"host_rejected:{exc}")
        except (OSError, TimeoutError):
            return SandboxGatewayOutcome(False, "host_unavailable")
        issued_at = int(self.clock())
        request_id = str(uuid.uuid4())
        authorization = {
            "version": AUTHORIZATION_VERSION,
            **authority,
            "operation_sha256": operation_sha256(
                execution_profile=PROFILE, code=script, timeout_seconds=timeout
            ),
            "input_sha256": input_sha256(script),
            "nonce": request_id,
            "issued_at": issued_at,
            "expires_at": issued_at + AUTHORIZATION_TTL_SECONDS,
            # Read over the authenticated socket just before binding; a
            # restarted Launcher has a new epoch and refuses this binding.
            "launcher_epoch": launcher_epoch,
        }
        request = ExecutionRequest(
            request_id,
            str(authority["task_id"]),
            execution_ref(authority["task_execution_id"], authority["attempt_number"]),
            str(uuid.uuid4()),
            PROFILE,
            script,
            timeout,
            issued_at,
            secrets.token_hex(32),
            version=2,
            authorization=authorization,
        )
        try:
            result = self.client.execute(request)
        except RequestRejected as exc:
            return SandboxGatewayOutcome(
                False, f"bridge_result_invalid:{exc}", authorization=authorization
            )
        if result.execution_status == "REJECTED":
            return SandboxGatewayOutcome(
                False,
                f"host_rejected:{result.error_code}",
                result,
                authorization,
            )
        if result.execution_status == "UNAVAILABLE":
            return SandboxGatewayOutcome(
                False, "host_unavailable", result, authorization
            )
        try:
            after = resolve_attempt_authority(context)
        except SandboxAuthorityError as exc:
            return SandboxGatewayOutcome(
                False, f"late_result:{exc.reason}", result, authorization
            )
        if after != authority:
            return SandboxGatewayOutcome(
                False, "late_result:dispatch_fence_changed", result, authorization
            )
        return SandboxGatewayOutcome(True, None, result, authorization)


__all__ = [
    "C2_BRIDGE_MODE",
    "C2_SANDBOX_TIMEOUT_CAP_SECONDS",
    "SandboxAuthorityError",
    "SandboxExecutionContext",
    "SandboxExecutionGateway",
    "SandboxGatewayOutcome",
    "SandboxLocalExecutionRefused",
    "refuse_local_execution",
    "resolve_attempt_authority",
    "sandbox_c2_mode_enabled",
    "sandboxable_python_script",
]
