"""P43-PRODUCT-22-C2-C2B: minimum authorized Sandbox execution Gateway.

Gated to disposable C2 mode (``WORKER_ISOLATION_MODE=disposable`` and
``WORKER_BRIDGE_MODE=c2b-sandbox``). Outside that mode nothing here runs and
existing behavior is unchanged. Inside it, two operations are routed:
a self-contained ``python -c 'import sys; sys.exit(<int>)'`` verification
command (C2-C2B), and (P43-PRODUCT-23A) an admitted pytest command or the
candidate-validation compile check, run over a bounded, digest-bound snapshot
of the Worker-owned Task workspace. Structured file ops stay Worker-side under
Path Authority (``c2_structured_file_ops_authority_check``). Every other
local execution route refuses (``refuse_local_execution``); no route falls
back to local execution when the Host refuses, times out, or is unavailable.

Authority comes only from the trusted Worker lifecycle context (TaskExecution
row claimed by this process) and is re-read from the database before dispatch
and again before a result is returned. Model content supplies only the script.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import secrets
import stat
import shlex
import socket
import time
import uuid
from typing import Any, Callable, Iterator

from app.services.sandbox_bridge.authorization import (
    AUTHORIZATION_VERSION,
    execution_ref,
    input_sha256,
    operation_sha256,
)
from app.services.sandbox_bridge.contract import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_TIMEOUT_SECONDS,
    PROFILE,
    PYTEST_PROFILE,
    ExecutionRequest,
    ExecutionResult,
    RequestRejected,
    decode_workspace_snapshot,
    encode_tool_invocation,
    validate_snapshot_path,
)

logger = logging.getLogger(__name__)

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


_WORKER_WORKSPACE_ROOT_ENV = ("WORKSPACE_ROOT", "RUNTIME_ROOT")
_WORKER_PROTECTED_ROOT_ENV = (
    "WORKER_APP_ROOT",
    "WORKER_SOURCE_ROOT",
    "WORKER_DATABASE_DIR",
    "WORKER_IDENTITY_ROOT",
    "WORKER_CHECKPOINT_DIR",
    "CHECKPOINT_DIR",
)


def _env_paths(names: tuple[str, ...]) -> list[Path]:
    paths = []
    for name in names:
        value = os.environ.get(name, "").strip()
        if value and os.path.isabs(value):
            paths.append(Path(value).resolve())
    return paths


def c2_structured_file_ops_authority_check(
    project_dir: Path,
    *,
    accepted_path_authority: Any,
    authority_check: Callable[[], str | None] | None,
    attempt_context: "SandboxExecutionContext | None",
) -> Callable[[], str | None] | None:
    """P43-PRODUCT-23A: admit Worker-side structured file ops in C2 mode.

    Outside C2 mode returns ``authority_check`` unchanged. Inside it the
    existing deterministic executor may mutate only the Worker-owned Task
    workspace, and only with an accepted Path Authority plus the caller's
    mutation reservation and this attempt's live claim; the returned check
    re-verifies both immediately before every op. No shell or interpreter is
    involved. Refusal raises ``SandboxLocalExecutionRefused``.
    """

    if not sandbox_c2_mode_enabled():
        return authority_check

    def refuse(reason: str) -> None:
        raise SandboxLocalExecutionRefused(
            f"c2_sandbox_mode_refuses_local_execution:structured_file_ops:{reason}"
        )

    if accepted_path_authority is None:
        refuse("path_authority_missing")
    if authority_check is None or attempt_context is None:
        refuse("attempt_authority_missing")
    workspace = Path(project_dir).resolve()
    roots = _env_paths(_WORKER_WORKSPACE_ROOT_ENV)
    if not any(root in workspace.parents for root in roots):
        refuse("workspace_not_worker_owned")
    for protected in _env_paths(_WORKER_PROTECTED_ROOT_ENV):
        if (
            protected == workspace
            or protected in workspace.parents
            or workspace in protected.parents
        ):
            refuse("workspace_overlaps_worker_authority")
    try:
        resolve_attempt_authority(attempt_context)
    except SandboxAuthorityError as exc:
        refuse(exc.reason)

    def check() -> str | None:
        lost = authority_check()
        if lost is not None:
            return lost
        try:
            resolve_attempt_authority(attempt_context)
        except SandboxAuthorityError as exc:
            return exc.reason
        return None

    return check


@dataclass(frozen=True)
class SandboxExecutionContext:
    """Trusted identifiers held by the Worker lifecycle, never by the model."""

    db: Any
    task_execution_id: int
    session_id: int
    task_id: int
    session_instance_id: str | None


# Bound by the Worker lifecycle around completion so candidate validation can
# reach the Gateway without carrying identifiers through every Validator call.
_BOUND_CONTEXT: ContextVar[SandboxExecutionContext | None] = ContextVar(
    "c2_bound_sandbox_execution_context", default=None
)


@contextmanager
def bound_sandbox_execution_context(
    context: SandboxExecutionContext | None,
) -> Iterator[None]:
    token = _BOUND_CONTEXT.set(context)
    try:
        yield
    finally:
        _BOUND_CONTEXT.reset(token)


def current_sandbox_execution_context() -> SandboxExecutionContext | None:
    return _BOUND_CONTEXT.get()


def execute_candidate_command_in_sandbox(
    *, project_dir: Path, command: str, timeout_seconds: int
) -> tuple[int | None, str]:
    """C2 candidate validation: admitted pytest only, through the Gateway.

    Same contract as ``candidate_checks._execute_command``: ``None`` return
    code means timeout. Anything not runnable in the Sandbox raises OSError,
    which callers already record as a process that never started (a failed
    check, never a pass and never a local fallback).
    """

    tool, args = "pytest", sandbox_pytest_arguments(command, Path(project_dir))
    if args is None:
        tool, args = "compileall", sandbox_compileall_paths(command, Path(project_dir))
    if args is None:
        raise OSError("sandbox_gateway_refused:unsupported_operation")
    context = current_sandbox_execution_context()
    if context is None:
        raise OSError("sandbox_gateway_refused:trusted_execution_context_missing")
    try:
        gateway = SandboxExecutionGateway.from_environment()
    except SandboxAuthorityError as exc:
        raise OSError(f"sandbox_gateway_refused:{exc.reason}") from None
    outcome = gateway.execute_pytest(
        context, args, Path(project_dir), max(1, int(timeout_seconds)), tool=tool
    )
    if not outcome.accepted or outcome.result is None:
        raise OSError(f"sandbox_gateway_refused:{outcome.reason}")
    result = outcome.result
    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    if result.execution_status == "TIMEOUT":
        return None, output
    if result.execution_status not in {"SUCCESS", "EXECUTION_FAILED"}:
        raise OSError(f"sandbox_gateway_refused:{result.execution_status.lower()}")
    return result.exit_code, output


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


_PYTHON_EXECUTABLES = frozenset({"python", "python3"})
_VENV_PYTHONS = frozenset(
    {".venv/bin/python", ".venv/bin/python3", "venv/bin/python", "venv/bin/python3"}
)


def sandbox_pytest_arguments(command: str, project_dir: Path) -> list[str] | None:
    """Pytest arguments for an admitted pytest command, else None.

    Reuses the existing acceptance-command grammar (``_admit_pytest_command``:
    no shell metacharacters, allowlisted flags, workspace-relative paths). The
    interpreter token is not executed: the Sandbox image provides Python, so
    an absolute or project-venv interpreter is normalized to ``python``.
    """

    from app.services.orchestration.validation.candidate_checks import (
        _admit_pytest_command,
    )

    tokens = " ".join(str(command or "").split()).split(" ")
    if tokens and (Path(tokens[0]).is_absolute() or tokens[0] in _VENV_PYTHONS):
        if Path(tokens[0]).name.startswith("python"):
            tokens[0] = "python"
    if not tokens or not (
        tokens[0] == "pytest"
        or (tokens[0] in _PYTHON_EXECUTABLES and tokens[1:3] == ["-m", "pytest"])
    ):
        return None
    admitted, _reason = _admit_pytest_command(" ".join(tokens), project_dir)
    if admitted is None:
        return None
    return list(admitted.pytest_args) or None


def sandbox_compileall_paths(command: str, project_dir: Path) -> list[str] | None:
    """Paths for the Orchestrator-built ``<python> -m compileall -q <paths>``.

    Only that exact candidate-validation shape: existing workspace-relative
    ``.py`` files, no options other than ``-q``.
    """

    tokens = " ".join(str(command or "").split()).split(" ")
    if not tokens or not Path(tokens[0]).name.startswith("python"):
        return None
    if tokens[1:4] != ["-m", "compileall", "-q"] or len(tokens) < 5:
        return None
    root = Path(project_dir).resolve()
    paths = []
    for raw in tokens[4:]:
        try:
            validate_snapshot_path(raw)
        except RequestRejected:
            return None
        candidate = (root / raw).resolve()
        if not raw.endswith(".py") or root not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        paths.append(raw)
    return paths


class SandboxSnapshotRejected(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Regenerable caches only; excluded content can never be read by a test.
_SNAPSHOT_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})
_SNAPSHOT_CREDENTIAL_NAMES = frozenset(
    {".env", ".netrc", ".pypirc", ".npmrc", "credentials.json"}
)
_SNAPSHOT_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _credential_like(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _SNAPSHOT_CREDENTIAL_NAMES
        or lowered.startswith((".env.", "id_rsa", "id_ed25519", "id_ecdsa"))
        or lowered.endswith(_SNAPSHOT_CREDENTIAL_SUFFIXES)
    )


def build_workspace_snapshot(
    project_dir: Path,
) -> tuple[list[dict[str, object]], str]:
    """Read the Task workspace into the bounded v3 wire snapshot.

    Fails closed (never skips) on symlinks, special or multiply-linked files,
    credential-like names, unsafe paths and any bound. The returned digest is
    computed from exactly the bytes sent.
    """

    root = Path(project_dir)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise SandboxSnapshotRejected("snapshot_root_invalid")
    if root.resolve() != root:
        raise SandboxSnapshotRejected("snapshot_root_not_canonical")
    entries: list[dict[str, object]] = []
    total = 0

    def walk(directory: Path, prefix: str) -> None:
        nonlocal total
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            rel = f"{prefix}{child.name}"
            st = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                if child.name in _SNAPSHOT_SKIPPED_DIRS:
                    continue
                walk(directory / child.name, rel + "/")
                continue
            if child.name.endswith((".pyc", ".pyo")):
                continue
            if stat.S_ISLNK(st.st_mode):
                raise SandboxSnapshotRejected("snapshot_symlink_rejected")
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise SandboxSnapshotRejected("snapshot_special_file_rejected")
            if _credential_like(child.name):
                raise SandboxSnapshotRejected("snapshot_credential_material")
            try:
                validate_snapshot_path(rel)
            except RequestRejected:
                raise SandboxSnapshotRejected("snapshot_path_invalid") from None
            if len(entries) >= MAX_SNAPSHOT_FILES:
                raise SandboxSnapshotRejected("snapshot_file_count_exceeded")
            if st.st_size > MAX_SNAPSHOT_FILE_BYTES:
                raise SandboxSnapshotRejected("snapshot_file_too_large")
            fd = os.open(directory / child.name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                    raise SandboxSnapshotRejected("snapshot_changed_during_read")
                chunks = bytearray()
                while len(chunks) <= MAX_SNAPSHOT_FILE_BYTES:
                    part = os.read(fd, MAX_SNAPSHOT_FILE_BYTES + 1 - len(chunks))
                    if not part:
                        break
                    chunks.extend(part)
            finally:
                os.close(fd)
            data = bytes(chunks)
            total += len(data)
            if len(data) > MAX_SNAPSHOT_FILE_BYTES:
                raise SandboxSnapshotRejected("snapshot_file_too_large")
            if total > MAX_SNAPSHOT_BYTES:
                raise SandboxSnapshotRejected("snapshot_too_large")
            entries.append(
                {
                    "path": rel,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content": base64.b64encode(data).decode("ascii"),
                }
            )

    try:
        walk(root, "")
    except OSError:
        raise SandboxSnapshotRejected("snapshot_read_failed") from None
    entries.sort(key=lambda entry: str(entry["path"]))
    if not entries:
        raise SandboxSnapshotRejected("snapshot_empty")
    try:
        _files, digest = decode_workspace_snapshot(entries)
    except RequestRejected as exc:
        raise SandboxSnapshotRejected(str(exc)) from None
    return entries, digest


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
        return self._execute(context, PROFILE, script, timeout_seconds)

    def execute_pytest(
        self,
        context: SandboxExecutionContext,
        pytest_args: list[str],
        project_dir: Path,
        timeout_seconds: int,
        *,
        tool: str = "pytest",
    ) -> SandboxGatewayOutcome:
        """Run admitted pytest arguments over the current Task workspace.

        Attempt authority is verified first; the snapshot is then read from
        the trusted lifecycle's workspace path and bound by digest. The
        Sandbox result never writes back to the workspace.
        """

        try:
            resolve_attempt_authority(context)
        except SandboxAuthorityError as exc:
            return SandboxGatewayOutcome(False, exc.reason)
        try:
            snapshot, digest = build_workspace_snapshot(Path(project_dir))
        except SandboxSnapshotRejected as exc:
            return SandboxGatewayOutcome(False, exc.reason)
        code = encode_tool_invocation(tool, list(pytest_args))
        return self._execute(
            context,
            PYTEST_PROFILE,
            code,
            timeout_seconds,
            snapshot=snapshot,
            snapshot_sha256=digest,
        )

    def _execute(
        self,
        context: SandboxExecutionContext,
        profile: str,
        code: str,
        timeout_seconds: int,
        *,
        snapshot: list[dict[str, object]] | None = None,
        snapshot_sha256: str | None = None,
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
                execution_profile=profile,
                code=code,
                timeout_seconds=timeout,
                snapshot_sha256=snapshot_sha256,
            ),
            "input_sha256": input_sha256(code, snapshot_sha256),
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
            profile,
            code,
            timeout,
            issued_at,
            secrets.token_hex(32),
            version=3 if snapshot is not None else 2,
            authorization=authorization,
            workspace_snapshot=snapshot,
        )
        try:
            result = self.client.execute(request)
        except RequestRejected as exc:
            return SandboxGatewayOutcome(
                False, f"bridge_result_invalid:{exc}", authorization=authorization
            )
        # Identifiers only: correlates this attempt with the Launcher journal
        # and ledger, which record the request ID but not the binding.
        logger.info(
            "[C2_SANDBOX] request=%s task_execution_id=%s attempt=%s session_id=%s"
            " dispatch_fence=%s celery_task_id=%s launcher_epoch=%s status=%s exit=%s",
            request_id,
            authority["task_execution_id"],
            authority["attempt_number"],
            authority["session_id"],
            authority["dispatch_fence"],
            authority["dispatch_celery_task_id"],
            launcher_epoch,
            result.execution_status,
            result.exit_code,
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
    "bound_sandbox_execution_context",
    "current_sandbox_execution_context",
    "execute_candidate_command_in_sandbox",
    "c2_structured_file_ops_authority_check",
    "SandboxSnapshotRejected",
    "build_workspace_snapshot",
    "sandbox_compileall_paths",
    "sandbox_pytest_arguments",
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
