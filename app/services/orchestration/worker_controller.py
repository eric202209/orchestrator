"""Read-only-first, Worker-only lifecycle controller.

The controller is a policy boundary, not a replacement supervisor.  It
accepts read-only observations and an explicitly injected process adapter;
without both, mutating operations refuse.  It never owns Beat/API, broker,
database, provider, or binding lifecycle authority.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from .worker_identity import (
    CandidateSourceManifest,
    IDENTITY_INCONCLUSIVE,
    IDENTITY_MISMATCH,
    IDENTITY_UNAVAILABLE,
    IDENTITY_VERIFIED,
    IdentityVerification,
    ProcessIdentity,
    WorkerBootIdentityManifest,
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


@dataclass(frozen=True)
class MaintenanceWindowProof:
    checked_at: datetime
    free_until: datetime
    required_margin_seconds: int
    schedule_identity: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class OperatorApproval:
    operation: str
    operator_identity: str
    approval_id: str
    nonce: str
    approved_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class DeploymentGate:
    operation: str
    candidate_head: str
    candidate_tree: str
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


class PidfdWorkerProcessAdapter:
    """Minimal Linux adapter for one graceful Worker-parent signal.

    Starting remains intentionally unimplemented here: a reviewed launcher
    must be injected by a future operator-controlled activation procedure.
    """

    def __init__(self, identity_reader: Callable[[int], ProcessIdentity | None]):
        self._identity_reader = identity_reader

    def stop_worker(self, identity: ProcessIdentity) -> None:
        current = self._identity_reader(identity.pid)
        if current is None or current.key() != identity.key():
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
            if current is None or current.key() != identity.key():
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
        boot_manifest: WorkerBootIdentityManifest | None,
        snapshot_provider: Callable[[], WorkerObservation] | None = None,
        process_adapter: WorkerProcessAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
        required_margin_seconds: int = 900,
        max_observation_age_seconds: int = 60,
    ) -> None:
        self.candidate_manifest = candidate_manifest
        self.boot_manifest = boot_manifest
        self.snapshot_provider = snapshot_provider
        self.process_adapter = process_adapter
        self.clock = clock or (lambda: datetime.now(UTC))
        self.required_margin_seconds = required_margin_seconds
        self.max_observation_age_seconds = max_observation_age_seconds
        self._consumed_approvals: set[tuple[str, str, str]] = set()

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
        candidate = verify_candidate_source_manifest(self.candidate_manifest)
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
        try:
            self._consume_approval(approval)
        except ControllerSafetyError as exc:
            return ControllerResult("stop", PREFLIGHT_BLOCKED, (str(exc),), snapshot)
        try:
            self.process_adapter.stop_worker(snapshot.worker)  # type: ignore[arg-type]
        except (ControllerSafetyError, OSError) as exc:
            return ControllerResult("stop", PREFLIGHT_BLOCKED, (str(exc),), snapshot)
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
        candidate = verify_candidate_source_manifest(self.candidate_manifest)
        if candidate.classification != IDENTITY_VERIFIED:
            return ControllerResult(
                "start", PREFLIGHT_BLOCKED, candidate.reasons, snapshot
            )
        reasons = self._common_safety_reasons(snapshot, worker_required=False)
        reasons.extend(self._fresh_observation_reasons(snapshot))
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
            self._consume_approval(approval)
        except ControllerSafetyError as exc:
            return ControllerResult("start", PREFLIGHT_BLOCKED, (str(exc),), snapshot)
        try:
            self.process_adapter.start_worker(self.candidate_manifest.launcher_identity)
        except (ControllerSafetyError, OSError) as exc:
            return ControllerResult("start", PREFLIGHT_BLOCKED, (str(exc),), snapshot)
        return ControllerResult("start", START_REQUESTED, observation=snapshot)

    def verify_started(
        self,
        *,
        expected_service_identities: Mapping[str, ProcessIdentity] | None = None,
        current_service_identities: Mapping[str, ProcessIdentity] | None = None,
        current_module_provenance: Sequence[Mapping[str, Any]] | None = None,
        current_configuration_fingerprint: str | None = None,
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
            expected_service_identities=expected_service_identities,
            current_service_identities=current_service_identities,
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
        if set(snapshot.registered_tasks) != set(
            self.boot_manifest.expected_task_registrations
        ):
            reasons.append("registration_drift")
        return reasons

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

    def _consume_approval(self, approval: OperatorApproval) -> None:
        key = (approval.operation, approval.approval_id, approval.nonce)
        if key in self._consumed_approvals:
            raise ControllerSafetyError("approval replay rejected")
        self._consumed_approvals.add(key)


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

    return DeploymentGate(
        operation=operation,
        candidate_head=candidate_manifest.candidate_head,
        candidate_tree=candidate_manifest.candidate_tree,
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
        stopped_verified_at=stopped_verified_at,
    )


__all__ = [
    "ControllerResult",
    "ControllerSafetyError",
    "DeploymentGate",
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
    "make_deployment_gate",
]
