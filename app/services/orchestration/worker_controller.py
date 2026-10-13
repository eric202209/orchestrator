"""Read-only-first, Worker-only lifecycle controller.

The controller is a policy boundary, not a replacement supervisor.  It
accepts read-only observations and an explicitly injected process adapter;
without both, mutating operations refuse.  It never owns Beat/API, broker,
database, provider, or binding lifecycle authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import fcntl

from .worker_identity import (
    CandidateManifestError,
    CandidateSourceManifest,
    CandidateIdentityPin,
    IDENTITY_INCONCLUSIVE,
    IDENTITY_MISMATCH,
    IDENTITY_UNAVAILABLE,
    IDENTITY_VERIFIED,
    IdentityVerification,
    ProcessIdentity,
    WorkerBootIdentityManifest,
    canonical_launcher_identity,
    configuration_pin_reasons,
    verify_candidate_source_manifest,
    verify_worker_identity,
)


PREFLIGHT_READY = "PREFLIGHT_READY"
PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
PREFLIGHT_INCONCLUSIVE = "PREFLIGHT_INCONCLUSIVE"
HOLD_MAINTENANCE_WINDOW_NOT_PROVEN = "HOLD_MAINTENANCE_WINDOW_NOT_PROVEN"
STOPPED_VERIFIED = "STOPPED_VERIFIED"
START_REQUESTED = "START_REQUESTED"
STOP_REQUESTED = "STOP_REQUESTED"
STARTED_VERIFIED = "STARTED_VERIFIED"


class ControllerSafetyError(RuntimeError):
    """A required safety or approval condition was not satisfied."""


CLAIM_AVAILABLE = "AVAILABLE"
CLAIM_CLAIMED = "CLAIMED"
CLAIM_OPERATION_ATTEMPTED = "OPERATION_ATTEMPTED"
CLAIM_COMPLETED = "COMPLETED"
CLAIM_FAILED_UNCERTAIN = "FAILED_UNCERTAIN"


@dataclass(frozen=True)
class ApprovalVerification:
    verified: bool
    principal: str | None = None
    reasons: tuple[str, ...] = ()


class ApprovalVerifier(Protocol):
    """Trusted issuer/verifier boundary; caller strings are not authentication."""

    def verify(
        self,
        approval: "OperatorApproval",
        *,
        operation: str,
        candidate_pin: CandidateIdentityPin,
        gate: "DeploymentGate",
        now: datetime,
    ) -> ApprovalVerification: ...


class TrustedObserverVerifier(Protocol):
    """Read-only observer authentication and freshness boundary."""

    def verify(
        self,
        observation: "WorkerObservation",
        *,
        candidate_pin: CandidateIdentityPin,
        expected_worker: ProcessIdentity | None,
        now: datetime,
    ) -> tuple[bool, Sequence[str]]: ...


class ApprovalClaimStore(Protocol):
    """Atomic, cross-controller single-use approval claim contract."""

    def claim(
        self,
        *,
        claim_id: str,
        binding: Mapping[str, Any],
        expires_at: datetime,
        now: datetime,
    ) -> None: ...

    def transition(self, claim_id: str, *, expected: str, state: str) -> None: ...


class FileApprovalClaimStore:
    """Durable provider-free claim store with a legal, serialized state graph.

    Deployment must inject an explicitly configured location.  The controller
    never chooses a production path and therefore cannot silently write to
    application or Redis state.  A per-claim advisory lock serializes every
    read/validate/replace transition across threads and processes.  Claim
    creation is exclusive; replacement is temporary-file plus file and
    parent-directory fsync.  A failed persistence operation is left as an
    uncertainty and is never silently retried by this store.
    """

    _LEGAL_TRANSITIONS = {
        CLAIM_AVAILABLE: frozenset({CLAIM_CLAIMED}),
        CLAIM_CLAIMED: frozenset({CLAIM_OPERATION_ATTEMPTED}),
        CLAIM_OPERATION_ATTEMPTED: frozenset({CLAIM_COMPLETED, CLAIM_FAILED_UNCERTAIN}),
        CLAIM_COMPLETED: frozenset(),
        CLAIM_FAILED_UNCERTAIN: frozenset(),
    }

    def __init__(self, root: str | Path) -> None:
        path = Path(root).expanduser()
        self._reject_symlink_ancestors(path)
        if path.exists():
            self._check_secure_directory(path)
        else:
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise ControllerSafetyError(
                    "approval claim directory create failed"
                ) from exc
        self._check_secure_directory(path)
        self.root = path.absolute()

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        absolute = path.absolute()
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor /= part
            try:
                item = os.lstat(cursor)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ControllerSafetyError(
                    "approval claim directory ancestry unavailable"
                ) from exc
            if stat.S_ISLNK(item.st_mode):
                raise ControllerSafetyError(
                    "approval claim directory has a symlinked ancestor"
                )

    @staticmethod
    def _check_secure_directory(path: Path) -> None:
        try:
            item = os.lstat(path)
        except OSError as exc:
            raise ControllerSafetyError("approval claim directory unavailable") from exc
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            raise ControllerSafetyError("approval claim root is not a directory")
        if item.st_mode & 0o077:
            raise ControllerSafetyError("approval claim directory permissions unsafe")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and item.st_uid != getuid():
            raise ControllerSafetyError("approval claim directory ownership unsafe")

    @staticmethod
    def _check_secure_file(path: Path, *, label: str) -> None:
        try:
            item = os.lstat(path)
        except OSError as exc:
            raise ControllerSafetyError(f"{label} unavailable") from exc
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise ControllerSafetyError(f"{label} is not a regular file")
        if item.st_mode & 0o077:
            raise ControllerSafetyError(f"{label} permissions unsafe")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and item.st_uid != getuid():
            raise ControllerSafetyError(f"{label} ownership unsafe")

    def _check_root(self) -> None:
        self._reject_symlink_ancestors(self.root)
        self._check_secure_directory(self.root)

    def _path(self, claim_id: str) -> Path:
        if len(claim_id) != 64 or any(ch not in "0123456789abcdef" for ch in claim_id):
            raise ControllerSafetyError("invalid approval claim id")
        self._check_root()
        path = self.root / f"{claim_id}.json"
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ControllerSafetyError("approval claim path is a symlink")
        except FileNotFoundError:
            pass
        else:
            self._check_secure_file(path, label="approval claim file")
        lock_path = self.root / f".{claim_id}.lock"
        try:
            if stat.S_ISLNK(os.lstat(lock_path).st_mode):
                raise ControllerSafetyError("approval claim lock is a symlink")
        except FileNotFoundError:
            pass
        return path

    @contextmanager
    def _claim_lock(self, claim_id: str):
        path = self._path(claim_id)
        lock_path = self.root / f".{claim_id}.lock"
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise ControllerSafetyError("approval claim lock unavailable") from exc
        try:
            item = os.fstat(fd)
            if item.st_mode & 0o077:
                raise ControllerSafetyError("approval claim lock permissions unsafe")
            getuid = getattr(os, "getuid", None)
            if getuid is not None and item.st_uid != getuid():
                raise ControllerSafetyError("approval claim lock ownership unsafe")
        except (ControllerSafetyError, OSError):
            os.close(fd)
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        except OSError as exc:
            raise ControllerSafetyError("approval claim lock failed") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _fsync_directory(self) -> None:
        self._check_root()
        try:
            directory_fd = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ControllerSafetyError(
                "approval claim directory durability failed"
            ) from exc

    @staticmethod
    def _read_payload(path: Path, claim_id: str) -> dict[str, Any]:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ControllerSafetyError("approval claim unavailable") from exc
        if not isinstance(payload, dict) or payload.get("claim_id") != claim_id:
            raise ControllerSafetyError("approval claim record invalid")
        if payload.get("state") not in FileApprovalClaimStore._LEGAL_TRANSITIONS:
            raise ControllerSafetyError("approval claim state invalid")
        if not isinstance(payload.get("binding"), dict):
            raise ControllerSafetyError("approval claim binding invalid")
        return payload

    def _write_replacement(self, path: Path, payload: Mapping[str, Any]) -> None:
        temporary: Path | None = None
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_directory()
        except OSError as exc:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ControllerSafetyError("approval claim transition failed") from exc

    def claim(
        self,
        *,
        claim_id: str,
        binding: Mapping[str, Any],
        expires_at: datetime,
        now: datetime,
    ) -> None:
        if expires_at <= now:
            raise ControllerSafetyError("approval claim expired")
        with self._claim_lock(claim_id) as path:
            payload = {
                "claim_id": claim_id,
                "state": CLAIM_CLAIMED,
                "claimed_at": now.astimezone(UTC).isoformat(),
                "expires_at": expires_at.astimezone(UTC).isoformat(),
                "binding": dict(binding),
                "state_history": [
                    {"state": CLAIM_CLAIMED, "at": now.astimezone(UTC).isoformat()}
                ],
            }
            encoded = (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            try:
                fd = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError as exc:
                raise ControllerSafetyError("approval replay rejected") from exc
            except OSError as exc:
                raise ControllerSafetyError("approval claim create failed") from exc
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._fsync_directory()
            except OSError as exc:
                # Do not remove a possibly durable/partial claim.  An
                # ambiguous claim must block replay until an operator reviews
                # the record; the controller never auto-releases it.
                raise ControllerSafetyError("approval claim write uncertain") from exc

    def transition(self, claim_id: str, *, expected: str, state: str) -> None:
        if state not in self._LEGAL_TRANSITIONS:
            raise ControllerSafetyError("invalid approval claim state")
        allowed = self._LEGAL_TRANSITIONS.get(expected, frozenset())
        if state not in allowed:
            raise ControllerSafetyError("illegal approval claim transition")
        with self._claim_lock(claim_id) as path:
            payload = self._read_payload(path, claim_id)
            if payload.get("state") != expected:
                raise ControllerSafetyError("approval claim lifecycle mismatch")
            history = payload.get("state_history", [])
            if not isinstance(history, list):
                raise ControllerSafetyError("approval claim history invalid")
            payload["state"] = state
            payload["state_history"] = [
                *history,
                {"state": state, "at": datetime.now(UTC).isoformat()},
            ]
            self._write_replacement(path, payload)


@dataclass(frozen=True)
class MaintenanceWindowProof:
    checked_at: datetime
    free_until: datetime
    required_margin_seconds: int
    schedule_identity: tuple[str, ...] = ()
    maintenance_identity: str | None = None

    def is_proven(self, now: datetime) -> bool:
        return (
            self.checked_at <= now
            and self.free_until >= now + timedelta(seconds=self.required_margin_seconds)
            and self.required_margin_seconds >= 0
        )


@dataclass(frozen=True)
class WorkerObservation:
    """One fresh, read-only snapshot from trusted project observers.

    ``None`` means evidence was unavailable, never zero/false.  This keeps
    the controller fail-closed without recreating lifecycle or dispatch logic.
    """

    observed_at: datetime
    worker: ProcessIdentity | None
    worker_children: tuple[ProcessIdentity, ...] | None
    worker_identities: tuple[ProcessIdentity, ...] | None
    beat_identity: ProcessIdentity | None
    api_identity: ProcessIdentity | None
    registered_tasks: tuple[str, ...] | None
    celery_active: int | None
    celery_reserved: int | None
    celery_scheduled: int | None
    redis_queued: int | None
    redis_unacked: int | None
    active_sessions: int | None
    active_task_executions: int | None
    active_runtime_leases: int | None
    retry_or_redelivery: int | None
    active_cli_processes: int | None
    managed_bindings: int | None
    binding_locks: int | None
    binding_construction: int | None
    capacity_state: str | None
    project_169_integrity: bool | None
    unknown_ownership: bool | None
    beat_maintenance_free: bool | None
    maintenance_window: MaintenanceWindowProof | None
    notes: tuple[str, ...] = ()
    worker_node_identity: str | None = None
    observer_provenance: str | None = None
    observer_candidate_source_manifest_digest: str | None = None
    observer_worker_key: tuple[str, int, str] | None = None
    beat_maintenance_identity: str | None = None


@dataclass(frozen=True)
class OperatorApproval:
    operation: str
    operator_identity: str
    approval_id: str
    nonce: str
    approved_at: datetime
    expires_at: datetime
    candidate_head: str | None = None
    candidate_tree: str | None = None
    source_manifest_digest: str | None = None
    worker_pid: int | None = None
    worker_start_ticks: str | None = None
    linux_boot_id: str | None = None
    gate_id: str | None = None


@dataclass(frozen=True)
class DeploymentGate:
    operation: str
    candidate_head: str
    candidate_tree: str
    candidate_source_manifest_digest: str
    worker_pid: int
    worker_start_ticks: str
    linux_boot_id: str
    beat_identity: ProcessIdentity
    api_identity: ProcessIdentity
    quiescence_observed_at: datetime
    maintenance_free_until: datetime
    expires_at: datetime
    operator_approval_identity: str
    approval_id: str
    nonce: str
    gate_id: str
    stopped_verified_at: datetime | None = None


@dataclass(frozen=True)
class ControllerResult:
    operation: str
    status: str
    reasons: tuple[str, ...] = ()
    observation: WorkerObservation | None = None
    identity: IdentityVerification | None = None


class WorkerProcessAdapter(Protocol):
    """Explicit operator-owned process boundary.

    Implementations must use a PID/start-tick checked process handle and must
    not broaden the target to a process group or command pattern.
    """

    def stop_worker(self, identity: ProcessIdentity) -> None: ...

    def start_worker(self, launcher_identity: Mapping[str, Any]) -> None: ...


def _process_provenance_reasons(
    expected: ProcessIdentity, actual: ProcessIdentity
) -> list[str]:
    return [
        field_name
        for field_name in (
            "linux_boot_id",
            "pid",
            "start_ticks",
            "executable",
            "cwd",
            "argv",
        )
        if getattr(expected, field_name) != getattr(actual, field_name)
    ]


class PidfdWorkerProcessAdapter:
    """Minimal Linux adapter for one graceful Worker-parent signal.

    Starting remains intentionally unimplemented here: a reviewed launcher
    must be injected by a future operator-controlled activation procedure.
    """

    def __init__(self, identity_reader: Callable[[int], ProcessIdentity | None]):
        self._identity_reader = identity_reader

    def stop_worker(self, identity: ProcessIdentity) -> None:
        current = self._identity_reader(identity.pid)
        if current is None or _process_provenance_reasons(identity, current):
            raise ControllerSafetyError("Worker process identity changed before stop")
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(os, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            raise ControllerSafetyError(
                "race-resistant Linux process handle unavailable"
            )
        process_fd = pidfd_open(identity.pid, 0)
        try:
            current = self._identity_reader(identity.pid)
            if current is None or _process_provenance_reasons(identity, current):
                raise ControllerSafetyError(
                    "Worker process identity changed after handle open"
                )
            pidfd_send_signal(process_fd, signal.SIGTERM, None, 0)
        finally:
            os.close(process_fd)

    def start_worker(self, launcher_identity: Mapping[str, Any]) -> None:
        del launcher_identity
        raise ControllerSafetyError("no default Worker launcher is installed")


class WorkerOnlyController:
    """Narrow controller with separate, approval-gated operations."""

    def __init__(
        self,
        *,
        candidate_manifest: CandidateSourceManifest,
        candidate_pin: CandidateIdentityPin | None = None,
        boot_manifest: WorkerBootIdentityManifest | None,
        snapshot_provider: Callable[[], WorkerObservation] | None = None,
        process_adapter: WorkerProcessAdapter | None = None,
        approval_verifier: ApprovalVerifier | None = None,
        observer_verifier: TrustedObserverVerifier | None = None,
        claim_store: ApprovalClaimStore | None = None,
        clock: Callable[[], datetime] | None = None,
        required_margin_seconds: int = 900,
        max_observation_age_seconds: int = 60,
    ) -> None:
        self.candidate_manifest = candidate_manifest
        self.candidate_pin = candidate_pin
        self.boot_manifest = boot_manifest
        self.snapshot_provider = snapshot_provider
        self.process_adapter = process_adapter
        self.approval_verifier = approval_verifier
        self.observer_verifier = observer_verifier
        self.claim_store = claim_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.required_margin_seconds = required_margin_seconds
        self.max_observation_age_seconds = max_observation_age_seconds

    def inspect(self) -> ControllerResult:
        snapshot, error = self._snapshot()
        if snapshot is None:
            return ControllerResult(
                "inspect", PREFLIGHT_INCONCLUSIVE, (error or "observation unavailable",)
            )
        return ControllerResult("inspect", "INSPECTED", observation=snapshot)

    def preflight(self) -> ControllerResult:
        snapshot, error = self._snapshot()
        if snapshot is None:
            return ControllerResult(
                "preflight",
                PREFLIGHT_INCONCLUSIVE,
                (error or "observation unavailable",),
            )
        if self.candidate_pin is None:
            return ControllerResult(
                "preflight",
                PREFLIGHT_INCONCLUSIVE,
                ("candidate_pin_unavailable",),
                snapshot,
            )
        configuration_reasons = configuration_pin_reasons(self.candidate_pin)
        if configuration_reasons:
            return ControllerResult(
                "preflight",
                PREFLIGHT_INCONCLUSIVE,
                configuration_reasons,
                snapshot,
            )
        candidate = verify_candidate_source_manifest(
            self.candidate_manifest,
            expected_head=self.candidate_pin.candidate_head,
            expected_tree=self.candidate_pin.candidate_tree,
            expected_source_root=self.candidate_pin.source_root,
            expected_source_manifest_digest=self.candidate_pin.source_manifest_digest,
        )
        if candidate.classification != IDENTITY_VERIFIED:
            return ControllerResult(
                "preflight",
                (
                    PREFLIGHT_INCONCLUSIVE
                    if candidate.classification == IDENTITY_UNAVAILABLE
                    else PREFLIGHT_BLOCKED
                ),
                candidate.reasons,
                snapshot,
            )
        reasons = self._common_safety_reasons(snapshot, worker_required=True)
        reasons.extend(self._fresh_observation_reasons(snapshot))
        reasons.extend(self._worker_identity_reasons(snapshot))
        reasons.extend(self._observer_reasons(snapshot))
        maintenance_status = self._maintenance_status(snapshot)
        if maintenance_status is not None:
            return ControllerResult(
                "preflight",
                HOLD_MAINTENANCE_WINDOW_NOT_PROVEN,
                tuple(reasons) + (maintenance_status,),
                snapshot,
            )
        return ControllerResult(
            "preflight",
            PREFLIGHT_READY if not reasons else PREFLIGHT_BLOCKED,
            tuple(dict.fromkeys(reasons)),
            snapshot,
        )

    def stop(
        self, approval: OperatorApproval, gate: DeploymentGate
    ) -> ControllerResult:
        """Request one graceful stop after a fresh, operation-specific gate."""

        preflight = self.preflight()
        if preflight.status != PREFLIGHT_READY or preflight.observation is None:
            return ControllerResult(
                "stop", preflight.status, preflight.reasons, preflight.observation
            )
        snapshot = preflight.observation
        gate_reasons = self._gate_reasons("stop", approval, gate, snapshot)
        if gate_reasons:
            return ControllerResult(
                "stop", PREFLIGHT_BLOCKED, tuple(gate_reasons), snapshot
            )
        if self.process_adapter is None:
            return ControllerResult(
                "stop",
                PREFLIGHT_INCONCLUSIVE,
                ("process_adapter_unavailable",),
                snapshot,
            )
        claim_id, authorization_reasons = self._authorize_and_claim(
            "stop", approval, gate, snapshot
        )
        if authorization_reasons:
            return ControllerResult(
                "stop",
                self._authorization_failure_status(authorization_reasons),
                tuple(authorization_reasons),
                snapshot,
            )
        try:
            self.process_adapter.stop_worker(snapshot.worker)  # type: ignore[arg-type]
        except (ControllerSafetyError, OSError) as exc:
            transition_ok = self._transition_claim(
                claim_id, CLAIM_OPERATION_ATTEMPTED, CLAIM_FAILED_UNCERTAIN
            )
            reasons = [str(exc)]
            if not transition_ok:
                reasons.append("claim_failure_persistence_uncertain")
            return ControllerResult("stop", PREFLIGHT_BLOCKED, tuple(reasons), snapshot)
        if not self._transition_claim(
            claim_id, CLAIM_OPERATION_ATTEMPTED, CLAIM_COMPLETED
        ):
            return ControllerResult(
                "stop",
                PREFLIGHT_INCONCLUSIVE,
                ("claim_completion_persistence_uncertain",),
                snapshot,
            )
        return ControllerResult("stop", STOP_REQUESTED, observation=snapshot)

    def verify_stopped(self) -> ControllerResult:
        """Verify the old Worker and every known child are gone, read-only."""

        snapshot, error = self._snapshot()
        if snapshot is None:
            return ControllerResult(
                "verify-stopped",
                PREFLIGHT_INCONCLUSIVE,
                (error or "observation unavailable",),
            )
        reasons = self._fresh_observation_reasons(snapshot)
        if snapshot.worker is not None:
            reasons.append("Worker_parent_still_alive")
        if snapshot.worker_children is None:
            reasons.append("Worker_child_evidence_unavailable")
        elif snapshot.worker_children:
            reasons.append("Worker_child_survivor")
        if snapshot.worker_identities is None:
            reasons.append("Worker_duplication_evidence_unavailable")
        elif snapshot.worker_identities:
            reasons.append("unexpected_Worker_survivor")
        return ControllerResult(
            "verify-stopped",
            STOPPED_VERIFIED if not reasons else PREFLIGHT_BLOCKED,
            tuple(dict.fromkeys(reasons)),
            snapshot,
        )

    def start(
        self, approval: OperatorApproval, gate: DeploymentGate
    ) -> ControllerResult:
        """Request one candidate Worker start after independent stop proof."""

        snapshot, error = self._snapshot()
        if snapshot is None:
            return ControllerResult(
                "start", PREFLIGHT_INCONCLUSIVE, (error or "observation unavailable",)
            )
        if self.candidate_pin is None:
            return ControllerResult(
                "start",
                PREFLIGHT_INCONCLUSIVE,
                ("candidate_pin_unavailable",),
                snapshot,
            )
        configuration_reasons = configuration_pin_reasons(self.candidate_pin)
        if configuration_reasons:
            return ControllerResult(
                "start",
                PREFLIGHT_INCONCLUSIVE,
                configuration_reasons,
                snapshot,
            )
        candidate = verify_candidate_source_manifest(
            self.candidate_manifest,
            expected_head=self.candidate_pin.candidate_head,
            expected_tree=self.candidate_pin.candidate_tree,
            expected_source_root=self.candidate_pin.source_root,
            expected_source_manifest_digest=self.candidate_pin.source_manifest_digest,
        )
        if candidate.classification != IDENTITY_VERIFIED:
            return ControllerResult(
                "start", PREFLIGHT_BLOCKED, candidate.reasons, snapshot
            )
        reasons = self._common_safety_reasons(snapshot, worker_required=False)
        reasons.extend(self._fresh_observation_reasons(snapshot))
        reasons.extend(self._observer_reasons(snapshot))
        reasons.extend(self._approved_launcher_reasons(snapshot))
        if snapshot.worker is not None:
            reasons.append("old_or_duplicate_Worker_alive")
        if snapshot.worker_children is None:
            reasons.append("Worker_child_evidence_unavailable")
        elif snapshot.worker_children:
            reasons.append("old_Worker_child_survivor")
        if snapshot.worker_identities is None:
            reasons.append("Worker_duplication_evidence_unavailable")
        elif snapshot.worker_identities:
            reasons.append("unexpected_Worker_survivor")
        maintenance_status = self._maintenance_status(snapshot)
        if maintenance_status is not None:
            reasons.append(maintenance_status)
        gate_reasons = self._gate_reasons("start", approval, gate, snapshot)
        reasons.extend(gate_reasons)
        if reasons:
            status = (
                HOLD_MAINTENANCE_WINDOW_NOT_PROVEN
                if HOLD_MAINTENANCE_WINDOW_NOT_PROVEN in reasons
                else PREFLIGHT_BLOCKED
            )
            return ControllerResult(
                "start", status, tuple(dict.fromkeys(reasons)), snapshot
            )
        if self.process_adapter is None:
            return ControllerResult(
                "start",
                PREFLIGHT_INCONCLUSIVE,
                ("process_adapter_unavailable",),
                snapshot,
            )
        try:
            launcher_request = self._approved_launcher_request()
        except ControllerSafetyError as exc:
            return ControllerResult("start", PREFLIGHT_BLOCKED, (str(exc),), snapshot)
        claim_id, authorization_reasons = self._authorize_and_claim(
            "start", approval, gate, snapshot
        )
        if authorization_reasons:
            return ControllerResult(
                "start",
                self._authorization_failure_status(authorization_reasons),
                tuple(authorization_reasons),
                snapshot,
            )
        try:
            self.process_adapter.start_worker(launcher_request)
        except (ControllerSafetyError, OSError) as exc:
            transition_ok = self._transition_claim(
                claim_id, CLAIM_OPERATION_ATTEMPTED, CLAIM_FAILED_UNCERTAIN
            )
            reasons = [str(exc)]
            if not transition_ok:
                reasons.append("claim_failure_persistence_uncertain")
            return ControllerResult(
                "start", PREFLIGHT_BLOCKED, tuple(reasons), snapshot
            )
        if not self._transition_claim(
            claim_id, CLAIM_OPERATION_ATTEMPTED, CLAIM_COMPLETED
        ):
            return ControllerResult(
                "start",
                PREFLIGHT_INCONCLUSIVE,
                ("claim_completion_persistence_uncertain",),
                snapshot,
            )
        return ControllerResult("start", START_REQUESTED, observation=snapshot)

    def verify_started(
        self,
        *,
        expected_service_identities: Mapping[str, ProcessIdentity] | None = None,
        current_service_identities: Mapping[str, ProcessIdentity] | None = None,
        current_module_provenance: Sequence[Mapping[str, Any]] | None = None,
        current_configuration_fingerprint: str | None = None,
        current_configuration_schema_version: int | None = None,
        current_configuration_policy_id: str | None = None,
    ) -> ControllerResult:
        """Verify the newly observed Worker without starting or stopping anything."""

        snapshot, error = self._snapshot()
        if snapshot is None:
            return ControllerResult(
                "verify-started",
                IDENTITY_UNAVAILABLE,
                (error or "observation unavailable",),
            )
        if self.boot_manifest is None or snapshot.worker is None:
            return ControllerResult(
                "verify-started",
                IDENTITY_UNAVAILABLE,
                ("boot_or_process_identity_unavailable",),
                snapshot,
            )
        identity = verify_worker_identity(
            self.boot_manifest,
            self.candidate_manifest,
            current_process=snapshot.worker,
            current_registered_tasks=snapshot.registered_tasks,
            current_child_identities=snapshot.worker_children,
            current_worker_identities=snapshot.worker_identities,
            current_module_provenance=current_module_provenance,
            current_configuration_fingerprint=current_configuration_fingerprint,
            current_configuration_schema_version=current_configuration_schema_version,
            current_configuration_policy_id=current_configuration_policy_id,
            expected_service_identities=expected_service_identities,
            current_service_identities=current_service_identities,
            candidate_pin=self.candidate_pin,
            current_worker_node_identity=snapshot.worker_node_identity,
        )
        safety_reasons = self._common_safety_reasons(snapshot, worker_required=True)
        safety_reasons.extend(self._fresh_observation_reasons(snapshot))
        if safety_reasons:
            return ControllerResult(
                "verify-started",
                PREFLIGHT_BLOCKED,
                tuple(dict.fromkeys(safety_reasons)) + identity.reasons,
                snapshot,
                identity,
            )
        return ControllerResult(
            "verify-started",
            (
                STARTED_VERIFIED
                if identity.classification == IDENTITY_VERIFIED
                else identity.classification
            ),
            identity.reasons,
            snapshot,
            identity,
        )

    def _snapshot(self) -> tuple[WorkerObservation | None, str | None]:
        if self.snapshot_provider is None:
            return None, "read_only_observer_unavailable"
        try:
            snapshot = self.snapshot_provider()
        except Exception as exc:  # noqa: BLE001 - controller must fail closed
            return None, f"read_only_observer_failed:{type(exc).__name__}"
        if not isinstance(snapshot, WorkerObservation):
            return None, "read_only_observer_returned_invalid_evidence"
        return snapshot, None

    def _fresh_observation_reasons(self, snapshot: WorkerObservation) -> list[str]:
        now = self.clock()
        age = (now - snapshot.observed_at).total_seconds()
        if age < 0 or age > self.max_observation_age_seconds:
            return ["quiescence_observation_stale"]
        return []

    def _observer_reasons(self, snapshot: WorkerObservation) -> list[str]:
        if self.candidate_pin is None:
            return ["candidate_pin_unavailable"]
        contract_reasons: list[str] = []
        if not snapshot.observer_provenance:
            contract_reasons.append("observer_provenance_unavailable")
        if (
            snapshot.observer_candidate_source_manifest_digest
            != self.candidate_pin.source_manifest_digest
        ):
            contract_reasons.append("observer_candidate_binding_mismatch")
        if (
            snapshot.worker is not None
            and snapshot.observer_worker_key != snapshot.worker.key()
        ):
            contract_reasons.append("observer_worker_binding_mismatch")
        if not snapshot.beat_maintenance_identity:
            contract_reasons.append("beat_maintenance_identity_unavailable")
        if self.observer_verifier is None:
            return [*contract_reasons, "trusted_observer_unavailable"]
        try:
            verified, observer_reasons = self.observer_verifier.verify(
                snapshot,
                candidate_pin=self.candidate_pin,
                expected_worker=snapshot.worker,
                now=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001 - observer is a fail-closed boundary
            return [
                *contract_reasons,
                f"trusted_observer_failed:{type(exc).__name__}",
            ]
        if not verified:
            return [
                "untrusted_observer",
                *contract_reasons,
                *map(str, observer_reasons),
            ]
        return contract_reasons

    def _common_safety_reasons(
        self,
        snapshot: WorkerObservation,
        *,
        worker_required: bool,
    ) -> list[str]:
        reasons: list[str] = []
        if worker_required and snapshot.worker is None:
            reasons.append("Worker_identity_unavailable")
        if snapshot.worker_children is None:
            reasons.append("Worker_child_evidence_unavailable")
        if snapshot.worker_identities is None:
            reasons.append("Worker_duplication_evidence_unavailable")
        elif worker_required and len(snapshot.worker_identities) != 1:
            reasons.append("unexpected_Worker_duplication")
        for field_name in (
            "celery_active",
            "celery_reserved",
            "celery_scheduled",
            "redis_queued",
            "redis_unacked",
            "active_sessions",
            "active_task_executions",
            "active_runtime_leases",
            "retry_or_redelivery",
            "active_cli_processes",
            "managed_bindings",
            "binding_locks",
            "binding_construction",
        ):
            value = getattr(snapshot, field_name)
            if value is None:
                reasons.append(f"{field_name}_unavailable")
            elif value != 0:
                reasons.append(f"{field_name}_nonzero")
        if snapshot.capacity_state is None:
            reasons.append("capacity_state_unavailable")
        elif snapshot.capacity_state != "STABLE":
            reasons.append("capacity_state_not_stable")
        if snapshot.project_169_integrity is not True:
            reasons.append("project_169_integrity_unproven")
        if snapshot.unknown_ownership is not False:
            reasons.append("ownership_unknown")
        if snapshot.beat_maintenance_free is not True:
            reasons.append("beat_maintenance_state_unproven")
        if snapshot.beat_identity is None:
            reasons.append("Beat_identity_unavailable")
        if snapshot.api_identity is None:
            reasons.append("API_identity_unavailable")
        return reasons

    def _worker_identity_reasons(self, snapshot: WorkerObservation) -> list[str]:
        if self.candidate_pin is None:
            return ["candidate_pin_unavailable"]
        if self.boot_manifest is None or snapshot.worker is None:
            return ["boot_identity_unavailable"]
        if snapshot.registered_tasks is None:
            return ["registrations_unavailable"]
        reasons = []
        if snapshot.worker.pid != self.boot_manifest.worker_pid:
            reasons.append("Worker_pid_mismatch")
        if snapshot.worker.start_ticks != self.boot_manifest.worker_start_ticks:
            reasons.append("Worker_start_ticks_mismatch")
        if snapshot.worker.linux_boot_id != self.boot_manifest.linux_boot_id:
            reasons.append("linux_boot_id_mismatch")
        for prefix, process in (
            (
                "boot_launcher_",
                ProcessIdentity(
                    pid=self.boot_manifest.worker_pid,
                    linux_boot_id=self.boot_manifest.linux_boot_id,
                    start_ticks=self.boot_manifest.worker_start_ticks,
                    executable=self.boot_manifest.worker_executable,
                    cwd=self.boot_manifest.worker_cwd,
                    argv=self.boot_manifest.worker_argv,
                ),
            ),
            ("current_launcher_", snapshot.worker),
        ):
            try:
                normalized = self.candidate_pin.normalized_launcher()
                if normalized["executable"] != process.executable:
                    reasons.append(f"{prefix}executable_mismatch")
                if normalized["cwd"] != process.cwd:
                    reasons.append(f"{prefix}cwd_mismatch")
                if tuple(normalized["argv"]) != process.argv:
                    reasons.append(f"{prefix}argv_mismatch")
            except (OSError, ValueError, TypeError):
                reasons.append(f"{prefix}evidence_invalid")
        if not snapshot.worker_node_identity:
            reasons.append("current_worker_node_identity_unavailable")
        elif snapshot.worker_node_identity != self.candidate_pin.worker_node_identity:
            reasons.append("current_worker_node_identity_mismatch")
        if (
            self.boot_manifest.worker_node_identity
            != self.candidate_pin.worker_node_identity
        ):
            reasons.append("boot_worker_node_identity_mismatch")
        if set(snapshot.registered_tasks) != set(
            self.boot_manifest.expected_task_registrations
        ):
            reasons.append("registration_drift")
        return reasons

    def _approved_launcher_reasons(self, snapshot: WorkerObservation) -> list[str]:
        """Bind start's adapter request to the external candidate pin."""

        if self.candidate_pin is None:
            return ["candidate_pin_unavailable"]
        reasons: list[str] = []
        try:
            approved = self.candidate_pin.normalized_launcher()
            candidate = canonical_launcher_identity(
                self.candidate_manifest.launcher_identity
            )
            if candidate != approved:
                reasons.append("approved_launcher_candidate_manifest_mismatch")
            request = dict(approved)
            request["worker_node_identity"] = self.candidate_pin.worker_node_identity
            if (
                request["worker_node_identity"]
                != self.candidate_pin.worker_node_identity
            ):
                reasons.append("approved_launcher_request_node_mismatch")
        except (CandidateManifestError, OSError, TypeError, ValueError) as exc:
            reasons.append(f"approved_launcher_identity_invalid:{type(exc).__name__}")
        if not self.candidate_pin.worker_node_identity:
            reasons.append("approved_worker_node_identity_unavailable")
        if not snapshot.worker_node_identity:
            reasons.append("start_worker_node_identity_unavailable")
        elif snapshot.worker_node_identity != self.candidate_pin.worker_node_identity:
            reasons.append("start_worker_node_identity_mismatch")
        return reasons

    def _approved_launcher_request(self) -> dict[str, Any]:
        if self.candidate_pin is None:
            raise ControllerSafetyError("candidate pin unavailable for start")
        try:
            approved = self.candidate_pin.normalized_launcher()
            candidate = canonical_launcher_identity(
                self.candidate_manifest.launcher_identity
            )
        except (CandidateManifestError, OSError, TypeError, ValueError) as exc:
            raise ControllerSafetyError("approved launcher identity invalid") from exc
        if candidate != approved:
            raise ControllerSafetyError("approved launcher identity changed")
        request = dict(approved)
        request["worker_node_identity"] = self.candidate_pin.worker_node_identity
        return request

    def _maintenance_status(self, snapshot: WorkerObservation) -> str | None:
        if snapshot.maintenance_window is None:
            return HOLD_MAINTENANCE_WINDOW_NOT_PROVEN
        if not snapshot.maintenance_window.is_proven(self.clock()):
            return HOLD_MAINTENANCE_WINDOW_NOT_PROVEN
        return None

    def _gate_reasons(
        self,
        operation: str,
        approval: OperatorApproval,
        gate: DeploymentGate,
        snapshot: WorkerObservation,
    ) -> list[str]:
        now = self.clock()
        reasons: list[str] = []
        if approval.operation != operation:
            reasons.append("approval_operation_mismatch")
        if gate.operation != operation:
            reasons.append("gate_operation_mismatch")
        if (
            not approval.operator_identity
            or approval.operator_identity != gate.operator_approval_identity
        ):
            reasons.append("operator_approval_identity_mismatch")
        if approval.approval_id != gate.approval_id or approval.nonce != gate.nonce:
            reasons.append("approval_gate_binding_mismatch")
        if approval.expires_at <= now or gate.expires_at <= now:
            reasons.append("approval_or_gate_expired")
        if approval.approved_at > now:
            reasons.append("approval_from_future")
        if gate.candidate_head != self.candidate_manifest.candidate_head:
            reasons.append("gate_candidate_head_mismatch")
        if gate.candidate_tree != self.candidate_manifest.candidate_tree:
            reasons.append("gate_candidate_tree_mismatch")
        if (
            gate.candidate_source_manifest_digest
            != self.candidate_manifest.source_manifest_digest
        ):
            reasons.append("gate_source_manifest_digest_mismatch")
        if gate.gate_id != _deployment_gate_id_from_gate(gate):
            reasons.append("gate_id_integrity_mismatch")
        if (
            snapshot.beat_identity is None
            or snapshot.beat_identity.key() != gate.beat_identity.key()
        ):
            reasons.append("Beat_identity_drift")
        if (
            snapshot.api_identity is None
            or snapshot.api_identity.key() != gate.api_identity.key()
        ):
            reasons.append("API_identity_drift")
        if operation == "stop":
            if snapshot.worker is None:
                reasons.append("Worker_identity_unavailable")
            else:
                if snapshot.worker.pid != gate.worker_pid:
                    reasons.append("gate_Worker_pid_mismatch")
                if snapshot.worker.start_ticks != gate.worker_start_ticks:
                    reasons.append("gate_Worker_start_ticks_mismatch")
                if snapshot.worker.linux_boot_id != gate.linux_boot_id:
                    reasons.append("gate_linux_boot_id_mismatch")
        else:
            if gate.stopped_verified_at is None:
                reasons.append("clean_stop_not_verified")
            elif gate.stopped_verified_at > now:
                reasons.append("stopped_verification_from_future")
        if gate.maintenance_free_until < now + timedelta(
            seconds=self.required_margin_seconds
        ):
            reasons.append("maintenance_margin_insufficient")
        if gate.quiescence_observed_at > now:
            reasons.append("quiescence_observation_from_future")
        if now - gate.quiescence_observed_at > timedelta(
            seconds=self.max_observation_age_seconds
        ):
            reasons.append("gate_quiescence_observation_stale")
        return reasons

    def _approval_contract_reasons(
        self,
        operation: str,
        approval: OperatorApproval,
        gate: DeploymentGate,
    ) -> tuple[list[str], ApprovalVerification | None]:
        if self.candidate_pin is None:
            return ["candidate_pin_unavailable"], None
        if self.approval_verifier is None:
            return ["approval_verifier_unavailable"], None
        reasons: list[str] = []
        if approval.candidate_head != self.candidate_pin.candidate_head:
            reasons.append("approval_candidate_head_mismatch")
        if approval.candidate_tree != self.candidate_pin.candidate_tree:
            reasons.append("approval_candidate_tree_mismatch")
        if approval.source_manifest_digest != self.candidate_pin.source_manifest_digest:
            reasons.append("approval_source_manifest_digest_mismatch")
        if approval.worker_pid != gate.worker_pid:
            reasons.append("approval_worker_pid_mismatch")
        if approval.worker_start_ticks != gate.worker_start_ticks:
            reasons.append("approval_worker_start_ticks_mismatch")
        if approval.linux_boot_id != gate.linux_boot_id:
            reasons.append("approval_linux_boot_id_mismatch")
        if approval.gate_id != gate.gate_id:
            reasons.append("approval_gate_id_mismatch")
        try:
            verification = self.approval_verifier.verify(
                approval,
                operation=operation,
                candidate_pin=self.candidate_pin,
                gate=gate,
                now=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001 - authorization fails closed
            return [f"approval_verifier_failed:{type(exc).__name__}"], None
        if not verification.verified:
            reasons.append("approval_unverified")
            reasons.extend(str(item) for item in verification.reasons)
        return reasons, verification

    def _claim_binding(
        self,
        operation: str,
        approval: OperatorApproval,
        gate: DeploymentGate,
        principal: str,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "approval_id": approval.approval_id,
            "nonce": approval.nonce,
            "principal": principal,
            "candidate_head": self.candidate_manifest.candidate_head,
            "candidate_tree": self.candidate_manifest.candidate_tree,
            "source_manifest_digest": self.candidate_manifest.source_manifest_digest,
            "gate_id": gate.gate_id,
            "worker_pid": gate.worker_pid,
            "worker_start_ticks": gate.worker_start_ticks,
            "linux_boot_id": gate.linux_boot_id,
            "approved_launcher_identity": (
                self.candidate_pin.normalized_launcher()
                if self.candidate_pin is not None
                else None
            ),
            "approved_worker_node_identity": (
                self.candidate_pin.worker_node_identity
                if self.candidate_pin is not None
                else None
            ),
            "approved_configuration_schema_version": (
                self.candidate_pin.configuration_schema_version
                if self.candidate_pin is not None
                else None
            ),
            "approved_configuration_fingerprint": (
                self.candidate_pin.configuration_fingerprint
                if self.candidate_pin is not None
                else None
            ),
            "approved_configuration_policy_id": (
                self.candidate_pin.configuration_policy_id
                if self.candidate_pin is not None
                else None
            ),
        }

    def _authorize_and_claim(
        self,
        operation: str,
        approval: OperatorApproval,
        gate: DeploymentGate,
        snapshot: WorkerObservation,
    ) -> tuple[str | None, list[str]]:
        reasons, verification = self._approval_contract_reasons(
            operation, approval, gate
        )
        if reasons:
            return None, reasons
        if self.claim_store is None:
            return None, ["approval_claim_store_unavailable"]
        # The verifier's authenticated principal is bound into the durable
        # claim; caller-supplied ``operator_identity`` is never sufficient.
        if verification is None:
            return None, ["approval_verification_unavailable"]
        principal = verification.principal or ""
        if not principal:
            return None, ["authenticated_operator_principal_unavailable"]
        binding = self._claim_binding(operation, approval, gate, principal)
        claim_id = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            self.claim_store.claim(
                claim_id=claim_id,
                binding=binding,
                expires_at=min(approval.expires_at, gate.expires_at),
                now=self.clock(),
            )
            self.claim_store.transition(
                claim_id, expected=CLAIM_CLAIMED, state=CLAIM_OPERATION_ATTEMPTED
            )
        except (ControllerSafetyError, OSError) as exc:
            return None, [str(exc)]
        del snapshot
        return claim_id, []

    def _transition_claim(
        self, claim_id: str | None, expected: str, state: str
    ) -> bool:
        if claim_id is None or self.claim_store is None:
            return False
        try:
            self.claim_store.transition(claim_id, expected=expected, state=state)
        except (ControllerSafetyError, OSError):
            # The operation is already single-use.  A lifecycle persistence
            # failure is intentionally not converted into a retryable control
            # action.
            return False
        return True

    @staticmethod
    def _authorization_failure_status(reasons: Sequence[str]) -> str:
        if any(
            "replay rejected" in reason
            or "claim expired" in reason
            or "claim lifecycle" in reason
            for reason in reasons
        ):
            return PREFLIGHT_BLOCKED
        return PREFLIGHT_INCONCLUSIVE


def _deployment_gate_id(
    *,
    operation: str,
    candidate_head: str,
    candidate_tree: str,
    source_manifest_digest: str,
    worker_pid: int,
    worker_start_ticks: str,
    linux_boot_id: str,
    beat: ProcessIdentity,
    api: ProcessIdentity,
    quiescence_observed_at: datetime,
    maintenance_free_until: datetime,
    expires_at: datetime,
    approval_id: str,
    nonce: str,
) -> str:
    payload = {
        "operation": operation,
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "source_manifest_digest": source_manifest_digest,
        "worker_pid": worker_pid,
        "worker_start_ticks": worker_start_ticks,
        "linux_boot_id": linux_boot_id,
        "beat": beat.to_dict(),
        "api": api.to_dict(),
        "quiescence_observed_at": quiescence_observed_at.astimezone(UTC).isoformat(),
        "maintenance_free_until": maintenance_free_until.astimezone(UTC).isoformat(),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "approval_id": approval_id,
        "nonce": nonce,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _deployment_gate_id_from_gate(gate: DeploymentGate) -> str:
    return _deployment_gate_id(
        operation=gate.operation,
        candidate_head=gate.candidate_head,
        candidate_tree=gate.candidate_tree,
        source_manifest_digest=gate.candidate_source_manifest_digest,
        worker_pid=gate.worker_pid,
        worker_start_ticks=gate.worker_start_ticks,
        linux_boot_id=gate.linux_boot_id,
        beat=gate.beat_identity,
        api=gate.api_identity,
        quiescence_observed_at=gate.quiescence_observed_at,
        maintenance_free_until=gate.maintenance_free_until,
        expires_at=gate.expires_at,
        approval_id=gate.approval_id,
        nonce=gate.nonce,
    )


def make_deployment_gate(
    *,
    operation: str,
    candidate_manifest: CandidateSourceManifest,
    worker: ProcessIdentity,
    beat: ProcessIdentity,
    api: ProcessIdentity,
    quiescence_observed_at: datetime,
    maintenance_free_until: datetime,
    expires_at: datetime,
    approval: OperatorApproval,
    stopped_verified_at: datetime | None = None,
) -> DeploymentGate:
    """Create a value-only gate; validation remains controller-owned."""
    gate_id = _deployment_gate_id(
        operation=operation,
        candidate_head=candidate_manifest.candidate_head,
        candidate_tree=candidate_manifest.candidate_tree,
        source_manifest_digest=candidate_manifest.source_manifest_digest,
        worker_pid=worker.pid,
        worker_start_ticks=worker.start_ticks,
        linux_boot_id=worker.linux_boot_id,
        beat=beat,
        api=api,
        quiescence_observed_at=quiescence_observed_at,
        maintenance_free_until=maintenance_free_until,
        expires_at=expires_at,
        approval_id=approval.approval_id,
        nonce=approval.nonce,
    )
    return DeploymentGate(
        operation=operation,
        candidate_head=candidate_manifest.candidate_head,
        candidate_tree=candidate_manifest.candidate_tree,
        candidate_source_manifest_digest=candidate_manifest.source_manifest_digest,
        worker_pid=worker.pid,
        worker_start_ticks=worker.start_ticks,
        linux_boot_id=worker.linux_boot_id,
        beat_identity=beat,
        api_identity=api,
        quiescence_observed_at=quiescence_observed_at,
        maintenance_free_until=maintenance_free_until,
        expires_at=expires_at,
        operator_approval_identity=approval.operator_identity,
        approval_id=approval.approval_id,
        nonce=approval.nonce,
        gate_id=gate_id,
        stopped_verified_at=stopped_verified_at,
    )


__all__ = [
    "ApprovalClaimStore",
    "ApprovalVerification",
    "ApprovalVerifier",
    "CLAIM_AVAILABLE",
    "CLAIM_CLAIMED",
    "CLAIM_COMPLETED",
    "CLAIM_FAILED_UNCERTAIN",
    "CLAIM_OPERATION_ATTEMPTED",
    "ControllerResult",
    "ControllerSafetyError",
    "DeploymentGate",
    "FileApprovalClaimStore",
    "HOLD_MAINTENANCE_WINDOW_NOT_PROVEN",
    "MaintenanceWindowProof",
    "OperatorApproval",
    "PREFLIGHT_BLOCKED",
    "PREFLIGHT_INCONCLUSIVE",
    "PREFLIGHT_READY",
    "PidfdWorkerProcessAdapter",
    "STARTED_VERIFIED",
    "START_REQUESTED",
    "STOPPED_VERIFIED",
    "STOP_REQUESTED",
    "WorkerObservation",
    "WorkerOnlyController",
    "WorkerProcessAdapter",
    "TrustedObserverVerifier",
    "make_deployment_gate",
]
