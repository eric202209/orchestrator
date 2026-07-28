"""Canonical, fail-safe ownership evaluation for orphan recovery."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.execution.process_identity import current_process_start_identity


TERMINAL_EXECUTION_STATUSES = {"done", "failed", "cancelled"}
SUPPORTED_EXECUTION_STATUSES = {"pending", "running", *TERMINAL_EXECUTION_STATUSES}


@dataclass(frozen=True)
class ProcessObservation:
    state: str
    exists: bool | None
    process_start_identity: str | None


@dataclass(frozen=True)
class OwnershipEvaluation:
    execution_id: int | None
    task_id: int | None
    session_id: int | None
    observed_execution_status: str | None
    recorded_worker_hostname: str | None
    recorded_worker_pid: int | None
    recorded_process_start_identity: str | None
    current_process_start_identity: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    runtime_started_at: datetime | None
    stale_threshold_seconds: int
    process_probe_state: str
    ownership_classification: str
    decision_reason: str
    recovery_eligible: bool
    signals: tuple[str, ...]
    observed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "observed_execution_status": self.observed_execution_status,
            "recorded_worker_hostname": self.recorded_worker_hostname,
            "recorded_worker_pid": self.recorded_worker_pid,
            "recorded_process_start_identity": self.recorded_process_start_identity,
            "current_process_start_identity": self.current_process_start_identity,
            "lease_owner": self.lease_owner,
            "lease_expires_at": _iso(self.lease_expires_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "runtime_started_at": _iso(self.runtime_started_at),
            "stale_threshold_seconds": self.stale_threshold_seconds,
            "process_probe_state": self.process_probe_state,
            "ownership_classification": self.ownership_classification,
            "decision_reason": self.decision_reason,
            "recovery_eligible": self.recovery_eligible,
            "signals": list(self.signals),
            "observed_at": _iso(self.observed_at),
        }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _status_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).strip().lower() or None


def probe_process_identity(
    worker_hostname: str | None, worker_pid: int | None
) -> ProcessObservation:
    """Observe a local process without treating an unknown probe as dead."""

    if not worker_hostname or not worker_pid or int(worker_pid) <= 0:
        return ProcessObservation("not_found", False, None)
    if worker_hostname != socket.gethostname():
        return ProcessObservation("remote_unavailable", None, None)
    try:
        os.kill(int(worker_pid), 0)
    except ProcessLookupError:
        return ProcessObservation("not_found", False, None)
    except PermissionError:
        # Existence is known, but identity is not.  Never infer a match.
        return ProcessObservation("unavailable", True, None)
    except OSError:
        return ProcessObservation("unavailable", None, None)

    identity = current_process_start_identity(int(worker_pid))
    if identity is None:
        return ProcessObservation("unavailable", True, None)
    return ProcessObservation("observed", True, identity)


def _fresh(value: datetime | None, now: datetime, threshold: int) -> bool:
    observed = _utc(value)
    if observed is None:
        return False
    return (now - observed).total_seconds() <= max(0, int(threshold))


def evaluate_execution_ownership(
    execution: object,
    *,
    runtime_lease: object | None = None,
    now: datetime | None = None,
    stale_after_seconds: int = 2100,
    current_hostname: str | None = None,
) -> OwnershipEvaluation:
    """Evaluate ownership once; recovery mutation remains outside this service."""

    observed_at = _utc(now) or datetime.now(UTC)
    status = _status_value(getattr(execution, "status", None))
    execution_id = getattr(execution, "id", None)
    task_id = getattr(execution, "task_id", None)
    session_id = getattr(execution, "session_id", None)

    if status in TERMINAL_EXECUTION_STATUSES:
        return OwnershipEvaluation(
            execution_id,
            task_id,
            session_id,
            status,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            stale_after_seconds,
            "not_probed",
            "TERMINAL_EXECUTION",
            "terminal execution is protected from duplicate recovery",
            False,
            ("TERMINAL_EXECUTION",),
            observed_at,
        )
    if status not in SUPPORTED_EXECUTION_STATUSES:
        return OwnershipEvaluation(
            execution_id,
            task_id,
            session_id,
            status,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            stale_after_seconds,
            "not_probed",
            "AMBIGUOUS_FAIL_SAFE",
            "unsupported execution state cannot be safely classified",
            False,
            ("AMBIGUOUS_FAIL_SAFE",),
            observed_at,
        )

    source = runtime_lease or execution
    recorded_hostname = getattr(source, "worker_hostname", None)
    recorded_pid = getattr(source, "worker_pid", None)
    recorded_identity = getattr(source, "worker_process_start_identity", None)
    lease_expires_at = _utc(getattr(runtime_lease, "lease_expires_at", None))
    heartbeat_at = _utc(
        getattr(
            source,
            "last_heartbeat_at" if runtime_lease is not None else "heartbeat_at",
            None,
        )
    )
    runtime_started_at = _utc(
        getattr(
            source,
            "runtime_started_at" if runtime_lease is not None else "started_at",
            None,
        )
    )
    lease_owner = (
        getattr(runtime_lease, "worker_instance_id", None)
        or getattr(runtime_lease, "worker_id", None)
        if runtime_lease is not None
        else None
    )
    signals: list[str] = []

    lease_present = runtime_lease is not None
    lease_status = _status_value(getattr(runtime_lease, "lease_status", None))
    lease_active = bool(
        lease_present
        and lease_status == "active"
        and lease_expires_at is not None
        and lease_expires_at > observed_at
    )
    if lease_active:
        signals.append("LEASE_ACTIVE")
    elif lease_present:
        signals.append("LEASE_EXPIRED")

    heartbeat_fresh = _fresh(heartbeat_at, observed_at, stale_after_seconds)
    if heartbeat_fresh:
        signals.append("HEARTBEAT_FRESH")
    else:
        signals.append("HEARTBEAT_STALE")

    probe_hostname = current_hostname or socket.gethostname()
    if recorded_hostname != probe_hostname:
        probe = ProcessObservation("remote_unavailable", None, None)
    else:
        probe = probe_process_identity(recorded_hostname, recorded_pid)
    signals.append(
        "PROCESS_NOT_FOUND"
        if probe.exists is False
        else (
            "PROCESS_PROBE_UNAVAILABLE"
            if probe.exists is None or probe.process_start_identity is None
            else "PROCESS_OBSERVED"
        )
    )

    if recorded_identity and probe.process_start_identity:
        if recorded_identity == probe.process_start_identity:
            signals.append("PROCESS_IDENTITY_MATCH")
            identity_match = True
        else:
            signals.append("PROCESS_IDENTITY_MISMATCH")
            identity_match = False
    else:
        signals.append("OWNER_IDENTITY_INCOMPLETE")
        identity_match = None

    process_missing = probe.exists is False
    process_unknown = probe.exists is None or (
        probe.exists is True and probe.process_start_identity is None
    )

    if lease_active:
        if heartbeat_fresh and identity_match is True:
            classification = "LIVE_OWNER_CONFIRMED"
            reason = "active lease, fresh heartbeat, and matching process identity"
        elif heartbeat_fresh:
            classification = "AMBIGUOUS_FAIL_SAFE"
            reason = "active lease or fresh heartbeat conflicts with uncertain process evidence"
        else:
            classification = "LEASE_ACTIVE"
            reason = "active lease fences recovery despite stale or unavailable secondary evidence"
        return OwnershipEvaluation(
            execution_id,
            task_id,
            session_id,
            status,
            recorded_hostname,
            recorded_pid,
            recorded_identity,
            probe.process_start_identity,
            lease_owner,
            lease_expires_at,
            heartbeat_at,
            runtime_started_at,
            stale_after_seconds,
            probe.state,
            classification,
            reason,
            False,
            tuple(dict.fromkeys(signals)),
            observed_at,
        )

    if heartbeat_fresh:
        if identity_match is True:
            classification = "LIVE_OWNER_CONFIRMED"
            reason = "fresh legacy heartbeat and matching process identity"
        else:
            classification = "AMBIGUOUS_FAIL_SAFE"
            reason = "fresh heartbeat cannot be overridden by missing or conflicting process evidence"
        return OwnershipEvaluation(
            execution_id,
            task_id,
            session_id,
            status,
            recorded_hostname,
            recorded_pid,
            recorded_identity,
            probe.process_start_identity,
            lease_owner,
            lease_expires_at,
            heartbeat_at,
            runtime_started_at,
            stale_after_seconds,
            probe.state,
            classification,
            reason,
            False,
            tuple(dict.fromkeys(signals)),
            observed_at,
        )

    if process_missing or identity_match is False:
        return OwnershipEvaluation(
            execution_id,
            task_id,
            session_id,
            status,
            recorded_hostname,
            recorded_pid,
            recorded_identity,
            probe.process_start_identity,
            lease_owner,
            lease_expires_at,
            heartbeat_at,
            runtime_started_at,
            stale_after_seconds,
            probe.state,
            "RECOVERY_ELIGIBLE",
            "execution is stale and the recorded owner is absent or has a different process identity",
            True,
            tuple(dict.fromkeys(signals)),
            observed_at,
        )

    classification = "AMBIGUOUS_FAIL_SAFE"
    reason = (
        "stale execution has an unavailable or still-observed process; recovery is withheld"
        if process_unknown
        else "stale execution ownership is ambiguous"
    )
    return OwnershipEvaluation(
        execution_id,
        task_id,
        session_id,
        status,
        recorded_hostname,
        recorded_pid,
        recorded_identity,
        probe.process_start_identity,
        lease_owner,
        lease_expires_at,
        heartbeat_at,
        runtime_started_at,
        stale_after_seconds,
        probe.state,
        classification,
        reason,
        False,
        tuple(dict.fromkeys(signals)),
        observed_at,
    )
